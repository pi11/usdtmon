"""Monitoring worker: polls each enabled network and ingests new transfers.

Run several instances safely: per-asset leases keep them from scanning the same
asset at once, and compare-and-set cursor updates keep results correct even if a
lease is lost mid-step.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid

from usdtmon.blockchain.base import BlockchainError
from usdtmon.blockchain.registry import AdapterRegistry
from usdtmon.config import ConfigError, Settings
from usdtmon.models import Network
from usdtmon.retry import backoff_delay
from usdtmon.services.assets import AssetInfo, enabled_assets
from usdtmon.services.leases import acquire_lease, release_lease
from usdtmon.services.monitoring import MonitoringService

log = logging.getLogger(__name__)


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class MonitorWorker:
    def __init__(
        self,
        settings: Settings,
        registry: AdapterRegistry,
        *,
        owner: str | None = None,
        lease_seconds: float = 300.0,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.owner = owner or worker_id()
        self.lease_seconds = lease_seconds
        self.service = MonitoringService(
            registry,
            scan_batch_blocks=lambda code: (
                settings.network(code).scan_batch_blocks if code in settings.networks else 2000
            ),
        )
        self._failures: dict[str, int] = {}

    def interval_for(self, network: Network) -> float:
        ns = self.settings.networks.get(network.code)
        if ns and ns.poll_interval:
            return ns.poll_interval
        if network.polling_interval:
            return float(network.polling_interval)
        return self.settings.monitor_interval

    async def process_network(self, network: Network) -> bool:
        """One cycle over the network's assets. Returns False if any asset failed."""
        ok = True
        for asset in await enabled_assets(self.settings, network_id=network.id):
            ok = await self.process_asset(asset) and ok
        return ok

    async def process_asset(self, asset: AssetInfo) -> bool:
        lease = f"monitor:asset:{asset.id}"
        if not await acquire_lease(lease, self.owner, self.lease_seconds):
            log.debug("lease_busy", extra={"lease": lease})
            return True
        started = time.monotonic()
        try:
            result = await self.service.process_asset(
                asset, keepalive=lambda: acquire_lease(lease, self.owner, self.lease_seconds)
            )
        except (BlockchainError, ConfigError) as exc:
            log.error(
                "asset_cycle_failed",
                extra={"network": asset.network_code, "asset": asset.symbol, "error": str(exc)},
            )
            return False
        except Exception:
            log.exception(
                "asset_cycle_crashed", extra={"network": asset.network_code, "asset": asset.symbol}
            )
            return False
        finally:
            await release_lease(lease, self.owner)
        log.info(
            "asset_cycle_done",
            extra={
                "network": asset.network_code,
                "asset": asset.symbol,
                "head": result.head,
                "steps": result.steps,
                "blocks": result.blocks,
                "transfers": result.transfers,
                "notifications": result.notifications,
                "caught_up": result.caught_up,
                "seconds": round(time.monotonic() - started, 2),
            },
        )
        return True

    async def run(self, stop: asyncio.Event) -> None:
        log.info("monitor_started", extra={"owner": self.owner})
        due: dict[str, float] = {}
        tasks: dict[str, asyncio.Task[None]] = {}

        async def cycle(network: Network) -> None:
            try:
                ok = await self.process_network(network)
            except Exception:
                log.exception("network_cycle_crashed", extra={"network": network.code})
                ok = False
            failures = 0 if ok else self._failures.get(network.code, 0) + 1
            self._failures[network.code] = failures
            interval = self.interval_for(network)
            if failures:
                # Back off on repeated provider failures, up to 10 minutes.
                interval = max(interval, backoff_delay(failures, interval, 600.0, jitter=False))
            due[network.code] = time.monotonic() + interval

        try:
            while not stop.is_set():
                try:
                    networks = await Network.filter(enabled=True)
                except Exception:
                    log.exception("network_list_failed")
                    networks = []
                now = time.monotonic()
                for network in networks:
                    if not self.registry.supports(network.code):
                        continue
                    task = tasks.get(network.code)
                    if (task is None or task.done()) and due.get(network.code, 0) <= now:
                        tasks[network.code] = asyncio.create_task(cycle(network))
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1.0)
                except TimeoutError:
                    pass
        finally:
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
            log.info("monitor_stopped", extra={"owner": self.owner})


async def check_network_config(settings: Settings, registry: AdapterRegistry) -> None:
    """Fail fast if an enabled network has no adapter or provider configuration."""
    for network in await Network.filter(enabled=True):
        if not registry.supports(network.code):
            raise ConfigError(f"Network {network.code!r} is enabled but has no adapter")
        registry.get(network.code)
