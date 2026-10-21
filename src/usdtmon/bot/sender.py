"""Telegram implementation of the notification ``MessageSender``."""

from __future__ import annotations

import warnings
from datetime import timedelta

from telegram import Bot, LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.error import (
    BadRequest,
    ChatMigrated,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)

from usdtmon.services.notifications import (
    PermanentDeliveryError,
    RecipientBlocked,
    TemporaryDeliveryError,
)


class TelegramSender:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def send(self, chat_id: int, text: str) -> int:
        try:
            message = await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except Forbidden as exc:
            raise RecipientBlocked(str(exc)) from exc
        except RetryAfter as exc:
            with warnings.catch_warnings():
                # PTB warns that the int form becomes a timedelta; both are handled.
                warnings.simplefilter("ignore")
                value = exc.retry_after
            seconds = value.total_seconds() if isinstance(value, timedelta) else float(value)
            raise TemporaryDeliveryError(f"rate limited: {exc}", retry_after=seconds) from exc
        except (BadRequest, ChatMigrated, InvalidToken) as exc:
            # BadRequest must be checked before NetworkError (it is a subclass).
            raise PermanentDeliveryError(f"{type(exc).__name__}: {exc}") from exc
        except TimedOut as exc:
            # The request may have reached Telegram: delivery is uncertain.
            raise TemporaryDeliveryError(f"timed out: {exc}", uncertain=True) from exc
        except NetworkError as exc:
            raise TemporaryDeliveryError(f"network error: {exc}") from exc
        except TelegramError as exc:
            raise TemporaryDeliveryError(f"{type(exc).__name__}: {exc}", uncertain=True) from exc
        return message.message_id
