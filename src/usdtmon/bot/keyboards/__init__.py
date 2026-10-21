"""Inline keyboards and callback-data formats."""

from __future__ import annotations

from collections.abc import Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from usdtmon.models import Cryptocurrency
from usdtmon.services.assets import AssetInfo
from usdtmon.services.subscriptions import SubscriptionView
from usdtmon.services.transfers import shorten_address

# Callback data: "add:c:<cryptocurrency id>", "add:n:<network asset id>",
# "add:cancel", "rm:<subscription id>".
ADD_CRYPTO = "add:c:"
ADD_NETWORK = "add:n:"
ADD_CANCEL = "add:cancel"
REMOVE = "rm:"


def parse_id(data: str | None, prefix: str) -> int | None:
    if not data or not data.startswith(prefix):
        return None
    try:
        return int(data.removeprefix(prefix))
    except ValueError:
        return None


def _cancel_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton("Cancel", callback_data=ADD_CANCEL)]


def cryptocurrencies_keyboard(cryptos: Sequence[Cryptocurrency]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"{c.symbol} — {c.name}", callback_data=f"{ADD_CRYPTO}{c.id}")]
        for c in cryptos
    ]
    return InlineKeyboardMarkup([*rows, _cancel_row()])


def networks_keyboard(assets: Sequence[AssetInfo]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(a.label, callback_data=f"{ADD_NETWORK}{a.id}")] for a in assets]
    return InlineKeyboardMarkup([*rows, _cancel_row()])


def subscription_title(sub: SubscriptionView) -> str:
    return f"{sub.asset.symbol} · {sub.asset.network_name} · {shorten_address(sub.address)}"


def remove_keyboard(subs: Sequence[SubscriptionView]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"Remove {subscription_title(s)}", callback_data=f"{REMOVE}{s.id}"
                )
            ]
            for s in subs
        ]
    )
