"""Notification outbox delivery.

Rows are created by ``ingest_range`` in the same transaction as the transfer.
A notifier claims due rows (``FOR UPDATE SKIP LOCKED``) by setting them to
``sending`` with a lease, sends them, and records the outcome.

Crash safety: if a notifier dies after Telegram accepted a message but before
the row was marked ``sent``, the lease expires and the row is claimed again.
Telegram has no idempotency keys, so delivery is at-least-once: the re-sent
message carries a "possible duplicate" note (``delivery_uncertain``). The same
applies to timeouts, where Telegram may or may not have delivered the message.
Missing a payment notification is considered worse than a marked duplicate.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from tortoise import connections

from usdtmon.models import Notification, NotificationStatus, Subscription, TelegramUser
from usdtmon.retry import RateLimiter, backoff_delay
from usdtmon.services.assets import asset_info
from usdtmon.services.transfers import format_amount, shorten_address

log = logging.getLogger(__name__)


class DeliveryError(Exception):
    """Base class for sender errors."""


class RecipientBlocked(DeliveryError):
    """The user blocked the bot or deleted their account."""


class PermanentDeliveryError(DeliveryError):
    """The message can never be delivered (e.g. chat not found)."""


class TemporaryDeliveryError(DeliveryError):
    def __init__(
        self, message: str, *, retry_after: float | None = None, uncertain: bool = False
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        # The message may have been delivered despite the error (e.g. timeout).
        self.uncertain = uncertain


class MessageSender(Protocol):
    async def send(self, chat_id: int, text: str) -> int:
        """Send an HTML message and return Telegram's message id."""
        ...


DUPLICATE_NOTE = (
    "\n\n<i>Note: a previous delivery attempt was interrupted, "
    "so this message may be a duplicate.</i>"
)


def render_notification(notification: Notification) -> str:
    """Requires transfer and subscription (wallet, asset, network, crypto) fetched."""
    sub = notification.subscription
    transfer = notification.transfer
    asset = asset_info(sub.network_asset, None)
    amount = format_amount(transfer.amount_raw.scaleb(-asset.decimals))
    url = asset.explorer_tx_url.format(tx=transfer.transaction_hash)
    e = html.escape
    lines = [
        f"<b>Incoming {e(asset.symbol)} Transfer</b>",
        f"Network: {e(asset.label)}",
        f"Amount: +{e(amount)} {e(asset.symbol)}",
        f"Wallet: <code>{e(shorten_address(sub.wallet.address))}</code>",
        f"From: <code>{e(shorten_address(transfer.from_address))}</code>",
        f'Transaction: <a href="{e(url, quote=True)}">{e(url)}</a>',
        "Status: Confirmed",
    ]
    text = "\n".join(lines)
    if notification.delivery_uncertain:
        text += DUPLICATE_NOTE
    return text


_CLAIM_SQL = """
WITH due AS (
    SELECT id FROM notification
    WHERE (status = 'pending' AND next_retry_at <= now())
       OR (status = 'sending' AND locked_until < now())
    ORDER BY next_retry_at, id
    LIMIT $1
    FOR UPDATE SKIP LOCKED
)
UPDATE notification AS n
SET delivery_uncertain = n.delivery_uncertain OR n.status = 'sending',
    status = 'sending',
    locked_until = now() + make_interval(secs => $2),
    attempts = n.attempts + 1
FROM due
WHERE n.id = due.id
RETURNING n.id
"""


@dataclass
class DeliveryStats:
    claimed: int = 0
    sent: int = 0
    retried: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


class NotificationService:
    def __init__(
        self,
        sender: MessageSender,
        *,
        max_attempts: int = 10,
        lease_seconds: int = 120,
        rate_limit: float = 25.0,
        retry_base: float = 5.0,
        retry_max: float = 3600.0,
        concurrency: int = 8,
    ) -> None:
        self.sender = sender
        self.max_attempts = max_attempts
        self.lease_seconds = lease_seconds
        self.retry_base = retry_base
        self.retry_max = retry_max
        self._limiter = RateLimiter(rate_limit)
        self._concurrency = asyncio.Semaphore(concurrency)
        # Telegram flood control (RetryAfter): no sends before this monotonic time.
        self._paused_until = 0.0

    def paused_for(self) -> float:
        return max(0.0, self._paused_until - time.monotonic())

    async def claim(self, limit: int) -> list[int]:
        _, rows = await connections.get("default").execute_query(
            _CLAIM_SQL, [limit, float(self.lease_seconds)]
        )
        return [row["id"] for row in rows]

    async def deliver_due(self, limit: int = 50) -> DeliveryStats:
        stats = DeliveryStats()
        if self.paused_for():
            return stats
        ids = await self.claim(limit)
        stats.claimed = len(ids)
        if not ids:
            return stats
        notifications = (
            await Notification.filter(id__in=ids)
            .select_related("transfer")
            # Nested select_related() drops columns in tortoise-orm 1.1: prefetch.
            .prefetch_related(
                "subscription__user",
                "subscription__wallet",
                "subscription__network_asset__cryptocurrency",
                "subscription__network_asset__network",
            )
            .order_by("id")
        )
        await asyncio.gather(*(self._deliver(n, stats) for n in notifications))
        return stats

    async def _deliver(self, n: Notification, stats: DeliveryStats) -> None:
        sub: Subscription = n.subscription
        user: TelegramUser = sub.user
        if not sub.active:
            await self._finish(n, NotificationStatus.FAILED, error="subscription removed")
            stats.failed += 1
            return
        if user.is_blocked:
            await self._finish(n, NotificationStatus.FAILED, error="user blocked the bot")
            stats.failed += 1
            return

        text = render_notification(n)
        async with self._concurrency:
            await self._limiter.acquire()
            if pause := self.paused_for():
                # Flood control hit by another message of this batch: give the
                # row back without spending an attempt.
                await self._postpone(n, pause)
                return
            try:
                message_id = await self.sender.send(user.telegram_id, text)
            except RecipientBlocked as exc:
                await TelegramUser.filter(id=user.id).update(is_blocked=True)
                await self._finish(n, NotificationStatus.FAILED, error=f"blocked: {exc}")
                stats.failed += 1
                log.info("recipient_blocked", extra={"notification": n.id, "user": user.id})
                return
            except PermanentDeliveryError as exc:
                await self._finish(n, NotificationStatus.FAILED, error=str(exc))
                stats.failed += 1
                log.warning("notification_failed", extra={"notification": n.id, "error": str(exc)})
                return
            except TemporaryDeliveryError as exc:
                if exc.retry_after:
                    self._paused_until = max(self._paused_until, time.monotonic() + exc.retry_after)
                await self._retry(
                    n, str(exc), retry_after=exc.retry_after, uncertain=exc.uncertain, stats=stats
                )
                return
            except Exception as exc:  # noqa: BLE001 - unknown errors are retried
                log.exception("notification_send_error", extra={"notification": n.id})
                await self._retry(n, repr(exc), retry_after=None, uncertain=True, stats=stats)
                return

        await self._finish(n, NotificationStatus.SENT, message_id=message_id)
        stats.sent += 1
        log.info(
            "notification_sent",
            extra={
                "notification": n.id,
                "attempts": n.attempts,
                "duplicate_note": n.delivery_uncertain,
            },
        )

    async def _retry(
        self,
        n: Notification,
        error: str,
        *,
        retry_after: float | None,
        uncertain: bool,
        stats: DeliveryStats,
    ) -> None:
        stats.errors.append(error)
        if n.attempts >= self.max_attempts:
            await self._finish(n, NotificationStatus.FAILED, error=f"gave up: {error}")
            stats.failed += 1
            log.error("notification_gave_up", extra={"notification": n.id, "error": error})
            return
        delay = backoff_delay(n.attempts, self.retry_base, self.retry_max)
        if retry_after is not None:
            delay = max(delay, retry_after)
        updated = await Notification.filter(
            id=n.id, status=NotificationStatus.SENDING, attempts=n.attempts
        ).update(
            status=NotificationStatus.PENDING,
            next_retry_at=datetime.now(UTC) + timedelta(seconds=delay),
            locked_until=None,
            last_error=error[:1000],
            delivery_uncertain=n.delivery_uncertain or uncertain,
        )
        if updated:
            stats.retried += 1
        log.warning(
            "notification_retry_scheduled",
            extra={
                "notification": n.id,
                "attempt": n.attempts,
                "delay": round(delay, 1),
                "error": error,
            },
        )

    async def _postpone(self, n: Notification, delay: float) -> None:
        await Notification.filter(
            id=n.id, status=NotificationStatus.SENDING, attempts=n.attempts
        ).update(
            status=NotificationStatus.PENDING,
            attempts=n.attempts - 1,
            next_retry_at=datetime.now(UTC) + timedelta(seconds=delay),
            locked_until=None,
        )

    async def _finish(
        self,
        n: Notification,
        status: NotificationStatus,
        *,
        error: str | None = None,
        message_id: int | None = None,
    ) -> None:
        # Guarded by (status, attempts): if our lease expired and another worker
        # re-claimed the row, its outcome wins.
        await Notification.filter(
            id=n.id, status=NotificationStatus.SENDING, attempts=n.attempts
        ).update(
            status=status,
            locked_until=None,
            last_error=error[:1000] if error else None,
            sent_at=datetime.now(UTC) if status is NotificationStatus.SENT else None,
            telegram_message_id=message_id,
        )
