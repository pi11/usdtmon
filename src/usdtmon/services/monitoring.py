"""Monitoring engine: scan finalized blocks for transfers to subscribed wallets.

Per asset, each wallet has a cursor (last fully processed block). Wallets that
share the same cursor are scanned together with one provider query, so a wallet
watched by many users is scanned once, and many wallets cost few requests.
A lagging group (e.g. after downtime) is scanned up to the next group's cursor,
where the groups merge.

Each step covers ``(cursor, end]`` and commits transfers, notifications and the
cursor move in one transaction. If anything fails the cursor stays put and the
same range is retried on the next cycle, so outages never create gaps.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from usdtmon.blockchain.registry import AdapterRegistry
from usdtmon.models import MonitoringCursor, Subscription
from usdtmon.services.assets import AssetInfo
from usdtmon.services.transfers import CursorConflict, ingest_range, select_qualifying

log = logging.getLogger(__name__)


@dataclass
class CycleResult:
    head: int = 0
    steps: int = 0
    blocks: int = 0
    transfers: int = 0
    notifications: int = 0
    caught_up: bool = True


class MonitoringService:
    def __init__(
        self,
        registry: AdapterRegistry,
        *,
        scan_batch_blocks: Callable[[str], int] | int = 2000,
        max_steps_per_cycle: int = 100,
    ) -> None:
        self.registry = registry
        self._batch = scan_batch_blocks
        self.max_steps_per_cycle = max_steps_per_cycle

    def _batch_for(self, network_code: str) -> int:
        return self._batch(network_code) if callable(self._batch) else self._batch

    async def process_asset(
        self,
        asset: AssetInfo,
        *,
        keepalive: Callable[[], Awaitable[bool]] | None = None,
    ) -> CycleResult:
        """Process every finalized block not yet seen for this asset's wallets.

        ``keepalive`` is called between steps (lease renewal); returning False
        stops the cycle. Provider errors propagate and leave cursors untouched.
        """
        adapter = self.registry.get(asset.network_code)
        token = asset.token
        head = await adapter.get_latest_finalized_block()
        result = CycleResult(head=head)
        batch = self._batch_for(asset.network_code)

        while True:
            cursors = await self._lagging_cursors(asset.id, head)
            if not cursors:
                break
            if result.steps >= self.max_steps_per_cycle:
                result.caught_up = False
                break
            if keepalive is not None and result.steps and not await keepalive():
                result.caught_up = False
                break

            low = min(c.last_processed_block for c in cursors)
            group = [c for c in cursors if c.last_processed_block == low]
            higher = [c.last_processed_block for c in cursors if c.last_processed_block > low]
            end = min(head, low + batch, min(higher) if higher else head)
            wallet_ids = {c.wallet.normalized_address: c.wallet_id for c in group}

            events = await adapter.get_incoming_transfers(list(wallet_ids), token, low + 1, end)
            qualifying = select_qualifying(events, token, set(wallet_ids), asset.min_amount)
            try:
                ingested = await ingest_range(
                    network_id=asset.network_id,
                    asset_id=asset.id,
                    events=qualifying,
                    wallet_ids=wallet_ids,
                    cursor_ids=[c.id for c in group],
                    expected_block=low,
                    new_block=end,
                )
            except CursorConflict as exc:
                log.warning(
                    "cursor_conflict", extra={"asset": asset.id, "from": low + 1, "error": str(exc)}
                )
                result.caught_up = False
                break

            result.steps += 1
            result.blocks += end - low
            result.transfers += ingested.transfers
            result.notifications += ingested.notifications
            log.info(
                "range_processed",
                extra={
                    "network": asset.network_code,
                    "asset": asset.symbol,
                    "from": low + 1,
                    "to": end,
                    "wallets": len(group),
                    "events": len(events),
                    "transfers": ingested.transfers,
                    "notifications": ingested.notifications,
                },
            )
        return result

    async def _lagging_cursors(self, asset_id: int, head: int) -> list[MonitoringCursor]:
        wallet_ids = await (
            Subscription.filter(network_asset_id=asset_id, active=True)
            .distinct()
            .values_list("wallet_id", flat=True)
        )
        if not wallet_ids:
            return []
        return await MonitoringCursor.filter(
            network_asset_id=asset_id,
            wallet_id__in=list(wallet_ids),
            last_processed_block__lt=head,
        ).select_related("wallet")
