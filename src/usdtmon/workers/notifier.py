"""Notifier worker: delivers the notification outbox to Telegram."""

from __future__ import annotations

import asyncio
import logging

from usdtmon.services.notifications import NotificationService

log = logging.getLogger(__name__)


async def run_notifier(
    service: NotificationService, stop: asyncio.Event, *, poll_interval: float, batch_size: int
) -> None:
    log.info("notifier_started")
    while not stop.is_set():
        try:
            stats = await service.deliver_due(batch_size)
        except Exception:
            log.exception("notifier_cycle_failed")
            stats = None
        if stats and stats.claimed:
            log.info(
                "notifier_cycle",
                extra={
                    "claimed": stats.claimed,
                    "sent": stats.sent,
                    "retried": stats.retried,
                    "failed": stats.failed,
                },
            )
            # A full batch probably means more is waiting.
            if stats.claimed >= batch_size:
                continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_interval)
        except TimeoutError:
            pass
    log.info("notifier_stopped")
