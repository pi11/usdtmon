from __future__ import annotations

import html
from collections.abc import Sequence

from telegram import Update
from telegram.ext import ContextTypes

from usdtmon.bot import deps
from usdtmon.bot.keyboards import remove_keyboard
from usdtmon.services.subscriptions import SubscriptionView

EMPTY = "You have no subscriptions. Use /add to monitor a wallet."


def render_subscriptions(subs: Sequence[SubscriptionView]) -> str:
    blocks = []
    for i, sub in enumerate(subs, 1):
        blocks.append(
            f"<b>{i}. {html.escape(sub.asset.symbol)}</b>\n"
            f"Network: {html.escape(sub.asset.label)}\n"
            f"Wallet: <code>{html.escape(sub.address)}</code>\n"
            f"Status: {sub.status}"
        )
    return "<b>Your subscriptions</b>\n\n" + "\n\n".join(blocks)


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if message is None or user is None:
        return
    subs = await deps.subscriptions(context).list_for_user(user.id)
    if not subs:
        await message.reply_text(EMPTY)
        return
    await message.reply_html(render_subscriptions(subs), reply_markup=remove_keyboard(subs))
