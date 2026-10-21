from __future__ import annotations

from usdtmon.models import NetworkAsset
from usdtmon.services.assets import AssetInfo, get_asset


async def asset_for(network_code: str, settings, symbol: str = "USDT") -> AssetInfo:
    asset = await NetworkAsset.get(network__code=network_code, cryptocurrency__symbol=symbol)
    info = await get_asset(asset.id, settings)
    assert info is not None
    return info
