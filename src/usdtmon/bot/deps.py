"""Access to services stored in ``Application.bot_data``."""

from __future__ import annotations

from telegram.ext import ContextTypes

from usdtmon.config import Settings
from usdtmon.services.subscriptions import SubscriptionService

SETTINGS_KEY = "settings"
SUBSCRIPTIONS_KEY = "subscriptions"


def settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.bot_data[SETTINGS_KEY]


def subscriptions(context: ContextTypes.DEFAULT_TYPE) -> SubscriptionService:
    return context.bot_data[SUBSCRIPTIONS_KEY]
