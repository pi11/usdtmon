from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from usdtmon.bot import deps
from usdtmon.bot.handlers.list import EMPTY, render_subscriptions
from usdtmon.bot.keyboards import REMOVE, parse_id, remove_keyboard, subscription_title


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if message is None or user is None:
        return
    subs = await deps.subscriptions(context).list_for_user(user.id)
    if not subs:
        await message.reply_text(EMPTY)
        return
    await message.reply_text(
        "Select the subscription to remove:", reply_markup=remove_keyboard(subs)
    )


async def remove_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query, user = update.callback_query, update.effective_user
    if query is None or user is None:
        return
    sub_id = parse_id(query.data, REMOVE)
    service = deps.subscriptions(context)
    # Ownership is enforced by the service: other users' ids are "not found".
    removed = await service.remove(user.id, sub_id) if sub_id is not None else None
    if removed is None:
        await query.answer("Subscription not found or already removed.", show_alert=True)
    else:
        await query.answer(f"Removed: {subscription_title(removed)}")
    subs = await service.list_for_user(user.id)
    if subs:
        await query.edit_message_text(
            render_subscriptions(subs), parse_mode="HTML", reply_markup=remove_keyboard(subs)
        )
    else:
        await query.edit_message_text(EMPTY)
