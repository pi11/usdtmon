"""Supported assets (the verified-contract allowlist) and their thresholds."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tortoise.transactions import in_transaction

from usdtmon.blockchain.base import Token
from usdtmon.blockchain.registry import AdapterRegistry
from usdtmon.config import Settings
from usdtmon.models import Cryptocurrency, Network, NetworkAsset


@dataclass(frozen=True)
class AssetInfo:
    id: int
    symbol: str
    name: str
    network_id: int
    network_code: str
    network_name: str
    token_standard: str
    explorer_tx_url: str
    contract_address: str
    decimals: int
    min_amount: Decimal

    @property
    def token(self) -> Token:
        return Token(self.symbol, self.contract_address, self.decimals)

    @property
    def label(self) -> str:
        """E.g. "TRON (TRC-20)"."""
        return f"{self.network_name} ({self.token_standard})"


def min_amount_for(crypto: Cryptocurrency, settings: Settings | None) -> Decimal:
    """Threshold: ``<SYMBOL>_MIN_AMOUNT`` env override, else the database value."""
    override = settings.min_amount_override(crypto.symbol) if settings else None
    return override if override is not None else Decimal(crypto.minimum_notification_amount)


def asset_info(asset: NetworkAsset, settings: Settings | None) -> AssetInfo:
    """``asset`` must have ``cryptocurrency`` and ``network`` fetched."""
    crypto, network = asset.cryptocurrency, asset.network
    return AssetInfo(
        id=asset.id,
        symbol=crypto.symbol,
        name=crypto.name,
        network_id=network.id,
        network_code=network.code,
        network_name=network.name,
        token_standard=network.token_standard,
        explorer_tx_url=network.explorer_tx_url,
        contract_address=asset.contract_address,
        decimals=asset.effective_decimals(),
        min_amount=min_amount_for(crypto, settings),
    )


async def get_asset(asset_id: int, settings: Settings | None) -> AssetInfo | None:
    asset = await NetworkAsset.get_or_none(id=asset_id).select_related("cryptocurrency", "network")
    return asset_info(asset, settings) if asset else None


async def enabled_assets(
    settings: Settings | None,
    *,
    network_id: int | None = None,
    cryptocurrency_id: int | None = None,
) -> list[AssetInfo]:
    qs = NetworkAsset.filter(
        enabled=True, cryptocurrency__enabled=True, network__enabled=True
    ).select_related("cryptocurrency", "network")
    if network_id is not None:
        qs = qs.filter(network_id=network_id)
    if cryptocurrency_id is not None:
        qs = qs.filter(cryptocurrency_id=cryptocurrency_id)
    assets = await qs.order_by("network__id")
    return [asset_info(a, settings) for a in assets]


async def enabled_cryptocurrencies() -> list[Cryptocurrency]:
    """Cryptocurrencies that have at least one enabled network asset."""
    return (
        await Cryptocurrency.filter(
            enabled=True, assets__enabled=True, assets__network__enabled=True
        )
        .distinct()
        .order_by("symbol")
    )


async def register_asset(
    registry: AdapterRegistry,
    *,
    symbol: str,
    name: str,
    network_code: str,
    contract_address: str,
    decimals: int,
    min_amount: Decimal,
) -> NetworkAsset:
    """Register a token contract on an existing network (e.g. a new ERC-20).

    The cryptocurrency is created if it does not exist yet. The contract must be
    a verified address: it becomes part of the allowlist.
    """
    symbol = symbol.strip().upper()
    if not symbol or decimals < 0 or min_amount < 0:
        raise ValueError("symbol, decimals and min_amount must be valid")
    network = await Network.get_or_none(code=network_code)
    if network is None:
        raise ValueError(f"unknown network {network_code!r}")
    adapter = registry.get(network_code)
    if not adapter.validate_address(contract_address):
        raise ValueError(f"invalid {network_code} contract address: {contract_address!r}")
    normalized = adapter.normalize_address(contract_address)
    async with in_transaction():
        crypto, _ = await Cryptocurrency.get_or_create(
            symbol=symbol,
            defaults={
                "name": name,
                "decimals": decimals,
                "minimum_notification_amount": min_amount,
            },
        )
        return await NetworkAsset.create(
            cryptocurrency=crypto,
            network=network,
            contract_address=normalized,
            decimals=decimals,
            enabled=True,
        )
