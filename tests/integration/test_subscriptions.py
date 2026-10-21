import pytest
from tortoise.exceptions import IntegrityError

from tests.fixtures.chain import ALICE_ETH, BOB_ETH
from tests.integration.helpers import asset_for
from usdtmon.blockchain.base import BlockchainError
from usdtmon.models import MonitoringCursor, Subscription, TelegramUser, Wallet
from usdtmon.services.subscriptions import (
    AssetUnavailable,
    CheckpointUnavailable,
    DuplicateSubscription,
    InvalidAddress,
    SubscriptionService,
)


@pytest.fixture
async def service(registry, settings):
    return SubscriptionService(registry, settings)


@pytest.fixture
async def asset(settings):
    return await asset_for("ethereum", settings)


async def test_subscribe_records_checkpoint(service, asset, eth):
    eth.head = 500
    view = await service.subscribe(1, "alice", asset.id, ALICE_ETH.upper().replace("0X", "0x"))
    assert view.active and view.asset.symbol == "USDT"

    sub = await Subscription.get(id=view.id).prefetch_related("wallet", "user")
    assert sub.start_block == 500
    assert sub.wallet.normalized_address == ALICE_ETH
    assert sub.user.telegram_id == 1 and sub.user.username == "alice"
    cursor = await MonitoringCursor.get(wallet_id=sub.wallet_id, network_asset_id=asset.id)
    assert cursor.last_processed_block == 500


async def test_duplicate_subscription_rejected(service, asset):
    await service.subscribe(1, None, asset.id, ALICE_ETH)
    with pytest.raises(DuplicateSubscription):
        # Same wallet in a different letter case is the same wallet.
        await service.subscribe(1, None, asset.id, ALICE_ETH.upper().replace("0X", "0x"))
    assert await Subscription.filter(active=True).count() == 1


async def test_database_prevents_duplicate_active_subscription(service, asset):
    view = await service.subscribe(1, None, asset.id, ALICE_ETH)
    sub = await Subscription.get(id=view.id)
    with pytest.raises(IntegrityError):
        await Subscription.create(
            user_id=sub.user_id,
            wallet_id=sub.wallet_id,
            network_asset_id=asset.id,
            active=True,
            start_block=1,
        )


async def test_multiple_users_share_one_wallet_and_cursor(service, asset, eth):
    await service.subscribe(1, None, asset.id, ALICE_ETH)
    eth.head = 120
    await service.subscribe(2, None, asset.id, ALICE_ETH)
    assert await Wallet.all().count() == 1
    assert await MonitoringCursor.all().count() == 1
    subs = await Subscription.all().order_by("id")
    assert [s.start_block for s in subs] == [100, 120]
    # The shared cursor is not moved forward while it is still being used.
    assert (await MonitoringCursor.get()).last_processed_block == 100


async def test_invalid_address(service, asset):
    with pytest.raises(InvalidAddress):
        await service.subscribe(1, None, asset.id, "not-an-address")
    assert await Subscription.all().count() == 0


async def test_unknown_or_unsupported_asset(service, settings):
    with pytest.raises(AssetUnavailable):
        await service.subscribe(1, None, 999_999, ALICE_ETH)
    tron = await asset_for("tron", settings)  # no Tron adapter in this registry
    with pytest.raises(AssetUnavailable):
        await service.subscribe(1, None, tron.id, ALICE_ETH)


async def test_checkpoint_unavailable(service, asset, eth, monkeypatch):
    async def down():
        raise BlockchainError("down")

    monkeypatch.setattr(eth, "get_latest_finalized_block", down)
    with pytest.raises(CheckpointUnavailable):
        await service.subscribe(1, None, asset.id, ALICE_ETH)
    assert await Subscription.all().count() == 0


async def test_users_manage_only_their_own_subscriptions(service, asset):
    mine = await service.subscribe(1, None, asset.id, ALICE_ETH)
    theirs = await service.subscribe(2, None, asset.id, BOB_ETH)

    assert [s.id for s in await service.list_for_user(1)] == [mine.id]
    assert await service.remove(1, theirs.id) is None
    assert (await Subscription.get(id=theirs.id)).active

    removed = await service.remove(1, mine.id)
    assert removed is not None and not removed.active
    assert await service.list_for_user(1) == []
    assert await service.remove(1, mine.id) is None  # already removed


async def test_resubscribe_after_removal(service, asset, eth):
    first = await service.subscribe(1, None, asset.id, ALICE_ETH)
    await service.remove(1, first.id)
    eth.head = 300
    second = await service.subscribe(1, None, asset.id, ALICE_ETH)
    assert second.id != first.id
    assert (await Subscription.get(id=second.id)).start_block == 300
    # Nobody watched the wallet in between: skip ahead instead of rescanning.
    assert (await MonitoringCursor.get()).last_processed_block == 300


async def test_start_clears_blocked_flag(service, asset):
    from usdtmon.services.subscriptions import upsert_user

    user = await upsert_user(5, "old")
    await TelegramUser.filter(id=user.id).update(is_blocked=True)
    user = await upsert_user(5, "new")
    assert not user.is_blocked and user.username == "new"
