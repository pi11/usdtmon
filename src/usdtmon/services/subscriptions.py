"""Users, wallets and subscriptions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from tortoise.exceptions import IntegrityError
from tortoise.transactions import in_transaction

from usdtmon.blockchain.base import BlockchainError
from usdtmon.blockchain.registry import AdapterRegistry
from usdtmon.config import Settings
from usdtmon.models import MonitoringCursor, Subscription, TelegramUser, Wallet
from usdtmon.services.assets import AssetInfo, asset_info, get_asset

log = logging.getLogger(__name__)


# Nested select_related() drops columns in tortoise-orm 1.1 (e.g. the Decimal
# threshold of the cryptocurrency), so nested relations are prefetched instead.
_ASSET_RELATIONS = ("network_asset__cryptocurrency", "network_asset__network")


class SubscriptionError(Exception):
    """Base class; ``str(exc)`` is safe to show to the user."""


class AssetUnavailable(SubscriptionError):
    pass


class InvalidAddress(SubscriptionError):
    pass


class DuplicateSubscription(SubscriptionError):
    pass


class CheckpointUnavailable(SubscriptionError):
    pass


@dataclass(frozen=True)
class SubscriptionView:
    id: int
    asset: AssetInfo
    address: str
    active: bool
    created_at: datetime

    @property
    def status(self) -> str:
        return "Active" if self.active else "Removed"


async def upsert_user(telegram_id: int, username: str | None) -> TelegramUser:
    """Create or refresh a user; a user who talks to the bot is not blocking it."""
    user, created = await TelegramUser.get_or_create(
        telegram_id=telegram_id, defaults={"username": username}
    )
    if not created and (user.username != username or user.is_blocked):
        user.username = username
        user.is_blocked = False
        await user.save(update_fields=["username", "is_blocked"])
    return user


class SubscriptionService:
    def __init__(self, registry: AdapterRegistry, settings: Settings | None = None) -> None:
        self.registry = registry
        self.settings = settings

    async def subscribe(
        self, telegram_id: int, username: str | None, asset_id: int, address: str
    ) -> SubscriptionView:
        asset = await get_asset(asset_id, self.settings)
        if asset is None or not self.registry.supports(asset.network_code):
            raise AssetUnavailable("This network is not available.")
        adapter = self.registry.get(asset.network_code)
        address = address.strip()
        if not adapter.validate_address(address):
            raise InvalidAddress(f"This is not a valid {asset.network_name} address.")
        normalized = adapter.normalize_address(address)

        user = await upsert_user(telegram_id, username)
        if await Subscription.exists(
            user_id=user.id,
            network_asset_id=asset.id,
            active=True,
            wallet__network_id=asset.network_id,
            wallet__normalized_address=normalized,
        ):
            raise DuplicateSubscription("You are already monitoring this wallet.")

        # The checkpoint: transfers at or below this finalized block are history.
        try:
            checkpoint = await adapter.get_latest_finalized_block()
        except BlockchainError as exc:
            log.warning(
                "checkpoint_unavailable", extra={"network": asset.network_code, "error": str(exc)}
            )
            raise CheckpointUnavailable(
                "The blockchain provider is temporarily unavailable. Please try again later."
            ) from exc

        try:
            async with in_transaction():
                await Wallet.bulk_create(
                    [
                        Wallet(
                            network_id=asset.network_id,
                            address=adapter.display_address(normalized),
                            normalized_address=normalized,
                        )
                    ],
                    ignore_conflicts=True,
                )
                wallet = await Wallet.get(
                    network_id=asset.network_id, normalized_address=normalized
                )
                await MonitoringCursor.bulk_create(
                    [
                        MonitoringCursor(
                            network_asset_id=asset.id,
                            wallet_id=wallet.id,
                            last_processed_block=checkpoint,
                        )
                    ],
                    ignore_conflicts=True,
                )
                cursor = await MonitoringCursor.select_for_update().get(
                    network_asset_id=asset.id, wallet_id=wallet.id
                )
                watched = await Subscription.exists(
                    network_asset_id=asset.id, wallet_id=wallet.id, active=True
                )
                if not watched and cursor.last_processed_block < checkpoint:
                    # Nobody watched this wallet since the cursor stopped: skip the
                    # unwatched blocks instead of rescanning them.
                    cursor.last_processed_block = checkpoint
                    await cursor.save(update_fields=["last_processed_block", "updated_at"])
                sub = await Subscription.create(
                    user_id=user.id,
                    wallet_id=wallet.id,
                    network_asset_id=asset.id,
                    active=True,
                    start_block=checkpoint,
                )
        except IntegrityError as exc:
            # Lost a race against a concurrent identical request.
            raise DuplicateSubscription("You are already monitoring this wallet.") from exc

        log.info(
            "subscription_created",
            extra={
                "subscription": sub.id,
                "asset": asset.id,
                "wallet": wallet.id,
                "start_block": checkpoint,
            },
        )
        return SubscriptionView(sub.id, asset, wallet.address, True, sub.created_at)

    async def list_for_user(self, telegram_id: int) -> list[SubscriptionView]:
        subs = (
            await Subscription.filter(user__telegram_id=telegram_id, active=True)
            .select_related("wallet")
            .prefetch_related(*_ASSET_RELATIONS)
            .order_by("created_at")
        )
        return [self._view(s) for s in subs]

    async def remove(self, telegram_id: int, subscription_id: int) -> SubscriptionView | None:
        """Deactivate a subscription owned by ``telegram_id``. None if not found/owned."""
        sub = (
            await Subscription.filter(
                id=subscription_id, user__telegram_id=telegram_id, active=True
            )
            .select_related("wallet")
            .prefetch_related(*_ASSET_RELATIONS)
            .first()
        )
        if sub is None:
            return None
        updated = await Subscription.filter(id=sub.id, active=True).update(
            active=False, removed_at=datetime.now(UTC)
        )
        if not updated:
            return None
        log.info("subscription_removed", extra={"subscription": sub.id})
        sub.active = False
        return self._view(sub)

    def _view(self, sub: Subscription) -> SubscriptionView:
        return SubscriptionView(
            id=sub.id,
            asset=asset_info(sub.network_asset, self.settings),
            address=sub.wallet.address,
            active=sub.active,
            created_at=sub.created_at,
        )
