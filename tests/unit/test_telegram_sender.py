from datetime import timedelta
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

from usdtmon.bot.sender import TelegramSender
from usdtmon.services.notifications import (
    PermanentDeliveryError,
    RecipientBlocked,
    TemporaryDeliveryError,
)


class FakeBot:
    def __init__(self, error=None):
        self.error = error
        self.kwargs = None

    async def send_message(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return SimpleNamespace(message_id=42)


async def test_success_returns_message_id():
    bot = FakeBot()
    assert await TelegramSender(bot).send(7, "<b>hi</b>") == 42
    assert bot.kwargs["chat_id"] == 7 and bot.kwargs["parse_mode"] == "HTML"


@pytest.mark.parametrize(
    ("error", "expected", "uncertain"),
    [
        (Forbidden("bot was blocked by the user"), RecipientBlocked, None),
        (BadRequest("chat not found"), PermanentDeliveryError, None),
        (TimedOut(), TemporaryDeliveryError, True),
        (NetworkError("connection reset"), TemporaryDeliveryError, False),
    ],
)
async def test_error_mapping(error, expected, uncertain):
    with pytest.raises(expected) as info:
        await TelegramSender(FakeBot(error)).send(1, "x")
    if uncertain is not None:
        assert info.value.uncertain is uncertain


async def test_retry_after():
    with pytest.raises(TemporaryDeliveryError) as info:
        await TelegramSender(FakeBot(RetryAfter(timedelta(seconds=17)))).send(1, "x")
    assert info.value.retry_after == 17
