"""Telegram handlers with stub Update/Context objects and the real services."""

from types import SimpleNamespace

import pytest
from telegram.ext import ConversationHandler

from tests.fixtures.chain import ALICE_ETH
from tests.integration.helpers import asset_for
from usdtmon.bot import deps
from usdtmon.bot.handlers import add, remove
from usdtmon.bot.handlers.list import list_command
from usdtmon.models import Subscription
from usdtmon.services.subscriptions import SubscriptionService


class Msg:
    def __init__(self, text=""):
        self.text = text
        self.replies: list[str] = []
        self.markups: list = []

    async def reply_text(self, text, reply_markup=None, **kw):
        self.replies.append(text)
        self.markups.append(reply_markup)

    reply_html = reply_text


class Query:
    def __init__(self, data):
        self.data = data
        self.answers: list[tuple[str | None, bool]] = []
        self.edits: list[str] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kw):
        self.edits.append(text)


def update(user_id, message=None, query=None):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username=f"u{user_id}"),
        effective_message=message,
        callback_query=query,
    )


@pytest.fixture
def context(registry, settings):
    bot_data = {
        deps.SETTINGS_KEY: settings,
        deps.SUBSCRIPTIONS_KEY: SubscriptionService(registry, settings),
    }
    return SimpleNamespace(bot_data=bot_data, user_data={})


async def test_add_conversation(context, settings):
    msg = Msg()
    assert await add.add_start(update(1, msg), context) == add.CHOOSE_CRYPTO
    assert msg.replies == ["Select a cryptocurrency:"]

    usdt_button = msg.markups[0].inline_keyboard[0][0]
    q = Query(usdt_button.callback_data)
    assert await add.crypto_chosen(update(1, query=q), context) == add.CHOOSE_NETWORK

    asset = await asset_for("ethereum", settings)
    q = Query(f"add:n:{asset.id}")
    assert await add.network_chosen(update(1, query=q), context) == add.ENTER_ADDRESS
    assert "Ethereum wallet address" in q.edits[0]

    bad = Msg("hello")
    assert await add.address_entered(update(1, bad), context) == add.ENTER_ADDRESS
    assert "not a valid Ethereum address" in bad.replies[0]

    good = Msg(ALICE_ETH)
    assert await add.address_entered(update(1, good), context) == ConversationHandler.END
    assert "Monitoring is active" in good.replies[0]
    assert await Subscription.filter(active=True).count() == 1


async def test_list_and_remove_only_own(context, settings):
    asset = await asset_for("ethereum", settings)
    service = context.bot_data[deps.SUBSCRIPTIONS_KEY]
    view = await service.subscribe(1, None, asset.id, ALICE_ETH)

    msg = Msg()
    await list_command(update(1, msg), context)
    assert "Your subscriptions" in msg.replies[0] and "Status: Active" in msg.replies[0]

    # Another user presses a forged remove button for user 1's subscription.
    q = Query(f"rm:{view.id}")
    await remove.remove_callback(update(2, query=q), context)
    assert q.answers[0][1] is True  # alert: not found
    assert (await Subscription.get(id=view.id)).active

    q = Query(f"rm:{view.id}")
    await remove.remove_callback(update(1, query=q), context)
    assert not (await Subscription.get(id=view.id)).active
    assert "no subscriptions" in q.edits[0]
