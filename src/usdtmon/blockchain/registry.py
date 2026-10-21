"""Maps network codes to adapter factories.

Adding a blockchain: implement a :class:`BlockchainAdapter`, add a factory to
``ADAPTER_FACTORIES`` and insert a ``network`` row (migration). Telegram
handlers, subscriptions and notifications are untouched.
"""

from __future__ import annotations

from collections.abc import Callable

from usdtmon.blockchain.base import BlockchainAdapter
from usdtmon.blockchain.ethereum import EthereumAdapter
from usdtmon.blockchain.rpc import JsonRpcClient
from usdtmon.blockchain.tron import TronAdapter
from usdtmon.config import ConfigError, NetworkSettings, Settings


def _rpc(ns: NetworkSettings, url: str, headers: dict[str, str] | None = None) -> JsonRpcClient:
    return JsonRpcClient(
        url,
        headers=headers,
        timeout=ns.request_timeout,
        max_concurrency=ns.max_concurrency,
        rate_limit=ns.rate_limit,
        max_retries=ns.max_retries,
        backoff_base=ns.backoff_base,
        backoff_max=ns.backoff_max,
    )


def _require_url(ns: NetworkSettings, var: str) -> str:
    if not ns.rpc_url:
        raise ConfigError(f"{var} is required to monitor {ns.code}")
    return ns.rpc_url


def build_ethereum(ns: NetworkSettings) -> BlockchainAdapter:
    return EthereumAdapter(
        _rpc(ns, _require_url(ns, "ETHEREUM_RPC_URL")),
        finality=ns.finality,
        confirmations=ns.confirmations,
        max_block_range=ns.max_block_range,
        max_addresses_per_query=ns.max_addresses_per_query,
        verify_receipts=ns.verify_receipts,
    )


def build_tron(ns: NetworkSettings) -> BlockchainAdapter:
    base = _require_url(ns, "TRON_API_URL")
    headers = {"TRON-PRO-API-KEY": ns.api_key} if ns.api_key else None
    return TronAdapter(
        _rpc(ns, base if base.endswith("/jsonrpc") else base + "/jsonrpc", headers),
        finality=ns.finality,
        confirmations=ns.confirmations,
        max_block_range=ns.max_block_range,
        max_addresses_per_query=ns.max_addresses_per_query,
        verify_receipts=ns.verify_receipts,
    )


ADAPTER_FACTORIES: dict[str, Callable[[NetworkSettings], BlockchainAdapter]] = {
    "ethereum": build_ethereum,
    "tron": build_tron,
}


class AdapterRegistry:
    """Creates adapters lazily and keeps one instance per network."""

    def __init__(
        self,
        settings: Settings,
        factories: dict[str, Callable[[NetworkSettings], BlockchainAdapter]] | None = None,
    ) -> None:
        self._settings = settings
        self._factories = factories if factories is not None else ADAPTER_FACTORIES
        self._adapters: dict[str, BlockchainAdapter] = {}

    @classmethod
    def from_adapters(
        cls, settings: Settings, adapters: dict[str, BlockchainAdapter]
    ) -> AdapterRegistry:
        registry = cls(settings, factories={})
        registry._adapters.update(adapters)
        return registry

    def supports(self, code: str) -> bool:
        return code in self._adapters or code in self._factories

    def get(self, code: str) -> BlockchainAdapter:
        adapter = self._adapters.get(code)
        if adapter is None:
            factory = self._factories.get(code)
            if factory is None:
                raise ConfigError(f"No blockchain adapter registered for network {code!r}")
            adapter = factory(self._settings.network(code))
            self._adapters[code] = adapter
        return adapter

    async def aclose(self) -> None:
        for adapter in self._adapters.values():
            await adapter.aclose()
        self._adapters.clear()
