"""Telegram bot application wiring."""

from __future__ import annotations

import logging

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from usdtmon.blockchain.registry import AdapterRegistry
from usdtmon.bot import deps
from usdtmon.bot.handlers.add import build_add_handler
from usdtmon.bot.handlers.list import list_command
from usdtmon.bot.handlers.remove import remove_callback, remove_command
from usdtmon.bot.handlers.start import help_command, start
from usdtmon.bot.keyboards import REMOVE
from usdtmon.config import Settings
from usdtmon.db import close_db, init_db
from usdtmon.services.subscriptions import SubscriptionService

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand("add", "Monitor a wallet"),
    BotCommand("list", "Your subscriptions"),
    BotCommand("remove", "Stop monitoring a wallet"),
    BotCommand("help", "How monitoring works"),
]


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("handler_error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text(
                "Sorry, something went wrong. Please try again later."
            )
        except Exception:  # noqa: BLE001 - best effort
            pass


def build_application(settings: Settings, registry: AdapterRegistry) -> Application:
    async def post_init(app: Application) -> None:
        await init_db(settings.database_url)
        await app.bot.set_my_commands(COMMANDS)
        log.info("bot_started", extra={"username": app.bot.username})

    async def post_shutdown(app: Application) -> None:
        await registry.aclose()
        await close_db()

    app = (
        ApplicationBuilder()
        .token(settings.require_telegram_token())
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.bot_data[deps.SETTINGS_KEY] = settings
    app.bot_data[deps.SUBSCRIPTIONS_KEY] = SubscriptionService(registry, settings)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(build_add_handler())
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("remove", remove_command))
    app.add_handler(CallbackQueryHandler(remove_callback, pattern=f"^{REMOVE}\\d+$"))
    app.add_error_handler(on_error)
    return app
