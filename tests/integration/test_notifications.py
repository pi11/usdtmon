import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from tests.fixtures.chain import ALICE_ETH, event
from tests.integration.helpers import asset_for
from usdtmon.models import Notification, NotificationStatus, TelegramUser
from usdtmon.services.monitoring import MonitoringService
from usdtmon.services.notifications import (
    DUPLICATE_NOTE,
    NotificationService,
    PermanentDeliveryError,
    RecipientBlocked,
    TemporaryDeliveryError,
)
from usdtmon.services.subscriptions import SubscriptionService


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.errors: dict[int, list[Exception]] = {}

    def fail(self, chat_id: int, *errors: Exception) -> None:
        self.errors.setdefault(chat_id, []).extend(errors)

    async def send(self, chat_id: int, text: str) -> int:
        queue = self.errors.get(chat_id)
        if queue:
            raise queue.pop(0)
        self.sent.append((chat_id, text))
        return len(self.sent)


@pytest.fixture
def sender():
    return FakeSender()


@pytest.fixture
def notifier(sender):
    return NotificationService(
        sender, max_attempts=3, rate_limit=1000, retry_base=1, lease_seconds=60
    )


@pytest.fixture
async def pending(registry, settings, eth):
    """Two users watch Alice's wallet; one 125.5 USDT transfer arrives."""
    asset = await asset_for("ethereum", settings)
    subs = SubscriptionService(registry, settings)
    await subs.subscribe(1, None, asset.id, ALICE_ETH)
    await subs.subscribe(2, None, asset.id, ALICE_ETH)
    eth.events = [event(block=101, amount=125_500_000, tx="0x" + "ab" * 32)]
    eth.head = 110
    await MonitoringService(registry).process_asset(asset)
    assert await Notification.all().count() == 2


async def make_due() -> None:
    await Notification.filter(status=NotificationStatus.PENDING).update(
        next_retry_at=datetime.now(UTC) - timedelta(seconds=1)
    )


async def statuses() -> list[tuple[int, str]]:
    rows = await Notification.all().prefetch_related("subscription__user").order_by("id")
    return [(n.subscription.user.telegram_id, n.status.value) for n in rows]


async def test_message_format(pending, notifier, sender):
    await notifier.deliver_due()
    text = sender.sent[0][1]
    assert text == (
        "<b>Incoming USDT Transfer</b>\n"
        "Network: Ethereum (ERC-20)\n"
        "Amount: +125.50 USDT\n"
        "Wallet: <code>0xa1a...1a1a1</code>\n"
        "From: <code>0xb2b...2b2b2</code>\n"
        f'Transaction: <a href="https://etherscan.io/tx/0x{"ab" * 32}">'
        f"https://etherscan.io/tx/0x{'ab' * 32}</a>\n"
        "Status: Confirmed"
    )


async def test_each_subscriber_gets_exactly_one_message(pending, notifier, sender):
    stats = await notifier.deliver_due()
    assert stats.sent == 2
    assert sorted(chat for chat, _ in sender.sent) == [1, 2]
    assert await notifier.deliver_due() == type(stats)()  # nothing left
    rows = await Notification.all()
    assert all(
        n.status is NotificationStatus.SENT and n.sent_at and n.telegram_message_id for n in rows
    )


async def test_temporary_failure_is_retried_with_backoff(pending, notifier, sender):
    sender.fail(1, TemporaryDeliveryError("network down"))
    stats = await notifier.deliver_due()
    assert stats.sent == 1 and stats.retried == 1
    n = await Notification.get(subscription__user__telegram_id=1)
    assert n.status is NotificationStatus.PENDING and n.attempts == 1
    assert n.next_retry_at > datetime.now(UTC)
    assert n.last_error == "network down"

    assert (await notifier.deliver_due()).claimed == 0  # not due yet
    await make_due()
    assert (await notifier.deliver_due()).sent == 1
    assert await statuses() == [(1, "sent"), (2, "sent")]
    assert [chat for chat, _ in sender.sent].count(1) == 1


async def test_rate_limit_retry_after_is_respected(pending, notifier, sender):
    sender.fail(1, TemporaryDeliveryError("rate limited", retry_after=300))
    await notifier.deliver_due()
    n = await Notification.get(subscription__user__telegram_id=1)
    assert n.next_retry_at > datetime.now(UTC) + timedelta(seconds=290)


async def test_gives_up_after_max_attempts(pending, notifier, sender):
    sender.fail(1, *[TemporaryDeliveryError("down")] * 5)
    for _ in range(3):
        await notifier.deliver_due()
        await make_due()
    n = await Notification.get(subscription__user__telegram_id=1)
    assert n.status is NotificationStatus.FAILED and n.attempts == 3
    assert "gave up" in n.last_error


async def test_blocked_user_does_not_disrupt_others(
    pending, notifier, sender, registry, eth, settings
):
    sender.fail(1, RecipientBlocked("Forbidden: bot was blocked by the user"))
    stats = await notifier.deliver_due()
    assert stats.sent == 1 and stats.failed == 1
    assert await statuses() == [(1, "failed"), (2, "sent")]
    assert (await TelegramUser.get(telegram_id=1)).is_blocked

    # Monitoring continues; the blocked user gets no new notification rows.
    eth.events.append(event(block=111, tx="0x02"))
    eth.head = 120
    await MonitoringService(registry).process_asset(await asset_for("ethereum", settings))
    await notifier.deliver_due()
    assert [chat for chat, _ in sender.sent] == [2, 2]


async def test_permanent_error_fails_immediately(pending, notifier, sender):
    sender.fail(1, PermanentDeliveryError("chat not found"))
    await notifier.deliver_due()
    assert await statuses() == [(1, "failed"), (2, "sent")]


async def test_crash_after_send_is_redelivered_with_duplicate_note(pending, notifier, sender):
    """A notifier claims rows, Telegram accepts, the process dies before recording it."""
    claimed = await notifier.claim(10)
    assert len(claimed) == 2
    # ... crash. Nothing is delivered again while the claim lease is valid:
    assert (await notifier.deliver_due()).claimed == 0
    # Lease expires:
    await Notification.all().update(locked_until=datetime.now(UTC) - timedelta(seconds=1))
    stats = await notifier.deliver_due()
    assert stats.sent == 2
    assert all(text.endswith(DUPLICATE_NOTE) for _, text in sender.sent)
    rows = await Notification.all()
    assert all(n.attempts == 2 and n.status is NotificationStatus.SENT for n in rows)


async def test_timeout_marks_retry_as_possible_duplicate(pending, notifier, sender):
    sender.fail(1, TemporaryDeliveryError("timed out", uncertain=True))
    await notifier.deliver_due()
    await make_due()
    await notifier.deliver_due()
    user1 = [text for chat, text in sender.sent if chat == 1]
    assert len(user1) == 1 and user1[0].endswith(DUPLICATE_NOTE)
    user2 = [text for chat, text in sender.sent if chat == 2]
    assert not user2[0].endswith(DUPLICATE_NOTE)


async def test_removed_subscription_is_not_notified(pending, notifier, sender, registry, settings):
    subs = SubscriptionService(registry, settings)
    (view,) = await subs.list_for_user(1)
    await subs.remove(1, view.id)
    await notifier.deliver_due()
    assert [chat for chat, _ in sender.sent] == [2]
    assert await statuses() == [(1, "failed"), (2, "sent")]


async def test_concurrent_notifiers_send_once(pending, sender):
    services = [NotificationService(sender, rate_limit=1000) for _ in range(4)]
    await asyncio.gather(*(s.deliver_due() for s in services))
    assert sorted(chat for chat, _ in sender.sent) == [1, 2]


async def test_flood_control_pauses_without_burning_attempts(pending, notifier, sender):
    sender.fail(1, TemporaryDeliveryError("rate limited", retry_after=300))
    sender.fail(2, TemporaryDeliveryError("rate limited", retry_after=300))
    await notifier.deliver_due()
    assert notifier.paused_for() > 290
    assert (await notifier.deliver_due()).claimed == 0  # paused: nothing claimed
    rows = await Notification.all()
    assert all(n.status is NotificationStatus.PENDING for n in rows)
    assert sorted(n.attempts for n in rows) in ([0, 1], [1, 1])
