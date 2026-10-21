"""Transfer qualification and transactional ingestion.

A transfer produces notifications only if it is: from the allowlisted contract,
to a watched wallet (incoming), not a self-transfer, successful, finalized, and
at least the cryptocurrency's minimum amount.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from tortoise.transactions import in_transaction

from usdtmon.blockchain.base import Token, TransferEvent
from usdtmon.models import (
    MonitoringCursor,
    Notification,
    NotificationStatus,
    Subscription,
    Transfer,
)

log = logging.getLogger(__name__)


class CursorConflict(Exception):
    """Another worker advanced the cursor first; this range must be discarded."""


def rejection_reason(
    event: TransferEvent, token: Token, watched: set[str], min_amount: Decimal
) -> str | None:
    """Why ``event`` must not notify, or None if it qualifies."""
    if event.contract_address != token.contract_address:
        return "unrecognized_contract"
    if event.to_address not in watched:
        return "not_incoming"
    if event.from_address == event.to_address:
        return "self_transfer"
    if not event.success:
        return "failed_transaction"
    if not event.confirmed:
        return "not_finalized"
    if event.amount_raw <= 0 or event.amount < min_amount:
        return "below_minimum"
    return None


def select_qualifying(
    events: Iterable[TransferEvent], token: Token, watched: set[str], min_amount: Decimal
) -> list[TransferEvent]:
    out = []
    for event in events:
        reason = rejection_reason(event, token, watched, min_amount)
        if reason is None:
            out.append(event)
        else:
            log.debug(
                "transfer_ignored",
                extra={"tx": event.transaction_hash, "index": event.event_index, "reason": reason},
            )
    return out


@dataclass(frozen=True)
class IngestResult:
    transfers: int
    notifications: int


async def ingest_range(
    *,
    network_id: int,
    asset_id: int,
    events: Sequence[TransferEvent],
    wallet_ids: dict[str, int],
    cursor_ids: Sequence[int],
    expected_block: int,
    new_block: int,
) -> IngestResult:
    """Atomically store transfers, create outbox rows and advance cursors.

    ``events`` must already be qualifying transfers to wallets in ``wallet_ids``
    (normalized address -> wallet id). Cursors are advanced with a
    compare-and-set from ``expected_block`` to ``new_block``; if any cursor moved
    in the meantime the whole transaction is rolled back (:class:`CursorConflict`).
    Duplicate events and notifications are ignored by unique constraints, so
    replaying a range is harmless.
    """
    now = datetime.now(UTC)
    async with in_transaction():
        notifications = 0
        if events:
            await Transfer.bulk_create(
                [
                    Transfer(
                        network_id=network_id,
                        network_asset_id=asset_id,
                        wallet_id=wallet_ids[e.to_address],
                        transaction_hash=e.transaction_hash,
                        event_index=e.event_index,
                        from_address=e.from_address,
                        to_address=e.to_address,
                        amount_raw=Decimal(e.amount_raw),
                        block_number=e.block_number,
                        block_timestamp=e.block_timestamp,
                    )
                    for e in events
                ],
                ignore_conflicts=True,
            )
            stored = await Transfer.filter(
                network_id=network_id,
                transaction_hash__in=list({e.transaction_hash for e in events}),
            )
            keys = {(e.transaction_hash, e.event_index) for e in events}
            transfers = [t for t in stored if (t.transaction_hash, t.event_index) in keys]

            subs = await Subscription.filter(
                network_asset_id=asset_id,
                wallet_id__in=list({t.wallet_id for t in transfers}),
                active=True,
                user__is_blocked=False,
            )
            rows = [
                Notification(
                    subscription_id=s.id,
                    transfer_id=t.id,
                    status=NotificationStatus.PENDING,
                    next_retry_at=now,
                )
                for t in transfers
                for s in subs
                # Never notify about transfers from before the subscription.
                if s.wallet_id == t.wallet_id and t.block_number > s.start_block
            ]
            if rows:
                await Notification.bulk_create(rows, ignore_conflicts=True)
            notifications = len(rows)

        if cursor_ids:
            updated = await MonitoringCursor.filter(
                id__in=list(cursor_ids), last_processed_block=expected_block
            ).update(last_processed_block=new_block, updated_at=now)
            if updated != len(cursor_ids):
                raise CursorConflict(
                    f"{len(cursor_ids) - updated} cursor(s) moved past {expected_block}"
                )
        return IngestResult(transfers=len(events), notifications=notifications)


def format_amount(amount: Decimal) -> str:
    """Exact amount with thousands separators and at least two decimals."""
    normalized = amount.normalize()
    exponent = normalized.as_tuple().exponent
    places = max(2, -exponent if isinstance(exponent, int) else 0)
    return f"{amount:,.{places}f}"


def shorten_address(address: str, head: int = 5, tail: int = 5) -> str:
    if len(address) <= head + tail + 3:
        return address
    return f"{address[:head]}...{address[-tail:]}"
