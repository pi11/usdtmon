import asyncio
from decimal import Decimal

import pytest

from tests.fixtures.chain import ALICE_ETH, BOB_ETH, CAROL_ETH, FAKE_USDT_ETH, event
from tests.integration.helpers import asset_for
from usdtmon.blockchain.base import BlockchainError
from usdtmon.models import (
    Cryptocurrency,
    MonitoringCursor,
    Notification,
    Subscription,
    TelegramUser,
    Transfer,
)
from usdtmon.services.leases import acquire_lease, release_lease
from usdtmon.services.monitoring import MonitoringService
from usdtmon.services.subscriptions import SubscriptionService
from usdtmon.services.transfers import CursorConflict, ingest_range
from usdtmon.workers.monitor import MonitorWorker


@pytest.fixture
async def asset(settings):
    return await asset_for("ethereum", settings)


@pytest.fixture
def subs(registry, settings):
    return SubscriptionService(registry, settings)


@pytest.fixture
def monitor(registry):
    return MonitoringService(registry, scan_batch_blocks=1000)


async def notified_users() -> list[int]:
    rows = await Notification.all().prefetch_related("subscription__user").order_by("id")
    return [n.subscription.user.telegram_id for n in rows]


async def test_incoming_transfer_creates_notification(subs, monitor, asset, eth):
    await subs.subscribe(1, None, asset.id, ALICE_ETH)  # checkpoint at 100
    eth.events = [event(block=105, amount=125_500_000)]
    eth.head = 110

    result = await monitor.process_asset(asset)

    assert result.transfers == 1 and result.notifications == 1
    t = await Transfer.get()
    assert t.amount_raw == Decimal(125_500_000) and t.to_address == ALICE_ETH
    assert await notified_users() == [1]
    assert (await MonitoringCursor.get()).last_processed_block == 110
    assert eth.calls == [((ALICE_ETH,), 101, 110)]


async def test_historical_transfers_are_not_notified(subs, monitor, asset, eth):
    eth.events = [event(block=90), event(block=100, index=1)]
    eth.head = 100
    await subs.subscribe(1, None, asset.id, ALICE_ETH)
    eth.head = 120
    await monitor.process_asset(asset)
    assert await Transfer.all().count() == 0
    assert await Notification.all().count() == 0
    assert eth.calls[0][1] == 101  # scan starts after the checkpoint


async def test_filters_applied_at_ingestion(subs, monitor, asset, eth):
    await subs.subscribe(1, None, asset.id, ALICE_ETH)
    eth.events = [
        event(block=101, amount=499_999, tx="0x01"),  # below 0.5 USDT
        event(block=102, amount=500_000, tx="0x02"),  # exactly 0.5 USDT
        event(block=103, contract=FAKE_USDT_ETH, tx="0x03"),  # fake token
        event(block=104, sender=ALICE_ETH, recipient=BOB_ETH, tx="0x04"),  # outgoing
        event(block=105, success=False, tx="0x05"),  # failed tx
    ]
    eth.head = 110
    await monitor.process_asset(asset)
    assert [t.transaction_hash for t in await Transfer.all()] == ["0x02"]


async def test_threshold_is_configurable_per_cryptocurrency(
    subs, monitor, asset, eth, registry, settings
):
    await Cryptocurrency.filter(symbol="USDT").update(minimum_notification_amount=Decimal("10"))
    asset = await asset_for("ethereum", settings)
    await subs.subscribe(1, None, asset.id, ALICE_ETH)
    eth.events = [
        event(block=101, amount=9_999_999, tx="0x01"),
        event(block=102, amount=10_000_000, tx="0x02"),
    ]
    eth.head = 110
    await monitor.process_asset(asset)
    assert [t.transaction_hash for t in await Transfer.all()] == ["0x02"]


async def test_env_override_of_threshold(settings, asset):
    from usdtmon.config import load_settings
    from usdtmon.services.assets import get_asset

    custom = load_settings({"DATABASE_URL": "postgres://x/y", "USDT_MIN_AMOUNT": "2"})
    assert (await get_asset(asset.id, custom)).min_amount == Decimal("2")


async def test_multiple_users_on_one_wallet_each_notified_once(subs, monitor, asset, eth):
    await subs.subscribe(1, None, asset.id, ALICE_ETH)
    await subs.subscribe(2, None, asset.id, ALICE_ETH)
    await subs.subscribe(3, None, asset.id, BOB_ETH)
    eth.events = [event(block=101)]
    eth.head = 110
    await monitor.process_asset(asset)
    assert sorted(await notified_users()) == [1, 2]
    # The wallet was scanned once for both users, together with Bob's wallet.
    assert eth.calls == [((ALICE_ETH, BOB_ETH), 101, 110)]


async def test_repeated_polling_does_not_duplicate(subs, monitor, asset, eth):
    await subs.subscribe(1, None, asset.id, ALICE_ETH)
    eth.events = [event(block=101)]
    eth.head = 110
    await monitor.process_asset(asset)
    await monitor.process_asset(asset)  # nothing new
    # Force a replay of the same range (e.g. manual rewind): still no duplicates.
    await MonitoringCursor.all().update(last_processed_block=100)
    await monitor.process_asset(asset)
    assert await Transfer.all().count() == 1
    assert await Notification.all().count() == 1


async def test_duplicate_events_from_provider(subs, monitor, asset, eth):
    await subs.subscribe(1, None, asset.id, ALICE_ETH)
    eth.events = [event(block=101), event(block=101)]
    eth.head = 110
    await monitor.process_asset(asset)
    assert await Transfer.all().count() == 1
    assert await Notification.all().count() == 1


async def test_multiple_transfers_in_one_transaction(subs, monitor, asset, eth):
    await subs.subscribe(1, None, asset.id, ALICE_ETH)
    eth.events = [event(block=101, index=i, amount=1_000_000 * (i + 1)) for i in range(3)]
    eth.head = 110
    await monitor.process_asset(asset)
    assert await Transfer.all().count() == 3
    assert await Notification.all().count() == 3


async def test_provider_failure_does_not_advance_cursor(subs, monitor, asset, eth):
    await subs.subscribe(1, None, asset.id, ALICE_ETH)
    eth.events = [event(block=105)]
    eth.head = 110
    eth.fail_next = 1
    with pytest.raises(BlockchainError):
        await monitor.process_asset(asset)
    assert (await MonitoringCursor.get()).last_processed_block == 100
    assert await Transfer.all().count() == 0

    # Provider recovers after a long outage: the gap is scanned, nothing is lost.
    eth.head = 5000
    eth.events.append(event(block=4000, tx="0x02"))
    await monitor.process_asset(asset)
    assert await Notification.all().count() == 2
    assert (await MonitoringCursor.get()).last_processed_block == 5000
    # Catch-up happened in bounded batches.
    assert [c[1:] for c in eth.calls[1:]] == [
        (101, 1100),
        (1101, 2100),
        (2101, 3100),
        (3101, 4100),
        (4101, 5000),
    ]


async def test_restart_resumes_from_persisted_cursor(subs, registry, asset, eth):
    await subs.subscribe(1, None, asset.id, ALICE_ETH)
    eth.head = 150
    await MonitoringService(registry).process_asset(asset)

    # "Restart": a brand-new service instance (no in-memory state).
    eth.events = [event(block=151), event(block=140, tx="0x0old")]
    eth.head = 160
    await MonitoringService(registry).process_asset(asset)
    assert eth.calls[-1][1:] == (151, 160)
    assert [t.block_number for t in await Transfer.all()] == [151]


async def test_lagging_wallet_group_catches_up_and_merges(subs, monitor, asset, eth):
    await subs.subscribe(1, None, asset.id, ALICE_ETH)  # cursor 100
    eth.head = 150
    await subs.subscribe(2, None, asset.id, BOB_ETH)  # cursor 150
    eth.head = 170
    await monitor.process_asset(asset)
    assert eth.calls == [((ALICE_ETH,), 101, 150), ((ALICE_ETH, BOB_ETH), 151, 170)]


async def test_later_subscriber_does_not_get_earlier_transfers(subs, monitor, asset, eth):
    await subs.subscribe(1, None, asset.id, ALICE_ETH)  # start 100
    eth.head = 150
    await subs.subscribe(2, None, asset.id, ALICE_ETH)  # start 150, cursor still 100
    eth.events = [event(block=120, tx="0x01"), event(block=160, tx="0x02")]
    eth.head = 170
    await monitor.process_asset(asset)
    rows = (
        await Notification.all()
        .select_related("transfer")
        .prefetch_related("subscription__user")
        .order_by("id")
    )
    assert sorted((n.subscription.user.telegram_id, n.transfer.transaction_hash) for n in rows) == [
        (1, "0x01"),
        (1, "0x02"),
        (2, "0x02"),
    ]


async def test_removed_and_blocked_users_get_no_notifications(subs, monitor, asset, eth):
    removed = await subs.subscribe(1, None, asset.id, ALICE_ETH)
    await subs.subscribe(2, None, asset.id, ALICE_ETH)
    await subs.subscribe(3, None, asset.id, ALICE_ETH)
    await subs.remove(1, removed.id)
    await TelegramUser.filter(telegram_id=2).update(is_blocked=True)
    eth.events = [event(block=101)]
    eth.head = 110
    await monitor.process_asset(asset)
    assert await notified_users() == [3]


async def test_unwatched_wallets_are_not_scanned(subs, monitor, asset, eth):
    view = await subs.subscribe(1, None, asset.id, ALICE_ETH)
    await subs.remove(1, view.id)
    eth.head = 200
    await monitor.process_asset(asset)
    assert eth.calls == []


async def test_cursor_compare_and_set_rolls_back(subs, asset):
    await subs.subscribe(1, None, asset.id, ALICE_ETH)
    cursor = await MonitoringCursor.get()
    sub = await Subscription.get()
    with pytest.raises(CursorConflict):
        await ingest_range(
            network_id=asset.network_id,
            asset_id=asset.id,
            events=[event(block=150)],
            wallet_ids={ALICE_ETH: sub.wallet_id},
            cursor_ids=[cursor.id],
            expected_block=99,  # stale: the cursor is at 100
            new_block=200,
        )
    # Transfers and notifications were rolled back with the failed cursor move.
    assert await Transfer.all().count() == 0
    assert await Notification.all().count() == 0
    assert (await MonitoringCursor.get()).last_processed_block == 100


async def test_overlapping_workers_do_not_duplicate(subs, registry, asset, eth):
    await subs.subscribe(1, None, asset.id, ALICE_ETH)
    await subs.subscribe(2, None, asset.id, CAROL_ETH)
    eth.events = [event(block=101), event(block=102, recipient=CAROL_ETH, tx="0x02")]
    eth.head = 110
    workers = [MonitoringService(registry) for _ in range(3)]
    await asyncio.gather(*(w.process_asset(asset) for w in workers), return_exceptions=True)
    assert await Transfer.all().count() == 2
    assert await Notification.all().count() == 2
    assert {c.last_processed_block for c in await MonitoringCursor.all()} == {110}


async def test_leases(database):
    assert await acquire_lease("x", "a", 60)
    assert await acquire_lease("x", "a", 60)  # renewal
    assert not await acquire_lease("x", "b", 60)
    await release_lease("x", "a")
    assert await acquire_lease("x", "b", 60)
    # An expired lease can be taken over.
    assert await acquire_lease("y", "a", 0.001)
    await asyncio.sleep(0.01)
    assert await acquire_lease("y", "b", 60)


async def test_monitor_worker_skips_asset_leased_by_another_worker(
    subs, registry, settings, asset, eth
):
    await subs.subscribe(1, None, asset.id, ALICE_ETH)
    eth.head = 110
    worker = MonitorWorker(settings, registry, owner="me")
    assert await acquire_lease(f"monitor:asset:{asset.id}", "someone-else", 60)
    assert await worker.process_asset(asset)
    assert eth.calls == []
    await release_lease(f"monitor:asset:{asset.id}", "someone-else")
    assert await worker.process_asset(asset)
    assert eth.calls == [((ALICE_ETH,), 101, 110)]


async def test_monitor_worker_reports_provider_failure(subs, registry, settings, asset, eth):
    await subs.subscribe(1, None, asset.id, ALICE_ETH)
    eth.head = 110
    eth.fail_next = 1
    worker = MonitorWorker(settings, registry, owner="me")
    assert not await worker.process_asset(asset)
    # The lease is released so the next cycle (or another worker) can retry.
    assert await acquire_lease(f"monitor:asset:{asset.id}", "other", 60)
