"""/add conversation: cryptocurrency -> network -> address."""

from __future__ import annotations

import html
import warnings

from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.warnings import PTBUserWarning

from usdtmon.bot import deps
from usdtmon.bot.keyboards import (
    ADD_CANCEL,
    ADD_CRYPTO,
    ADD_NETWORK,
    cryptocurrencies_keyboard,
    networks_keyboard,
    parse_id,
)
from usdtmon.services.assets import enabled_assets, enabled_cryptocurrencies, get_asset
from usdtmon.services.subscriptions import InvalidAddress, SubscriptionError

CHOOSE_CRYPTO, CHOOSE_NETWORK, ENTER_ADDRESS = range(3)
ASSET_KEY = "add_asset_id"


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    if message is None:
        return ConversationHandler.END
    cryptos = await enabled_cryptocurrencies()
    if not cryptos:
        await message.reply_text("No cryptocurrencies are available right now.")
        return ConversationHandler.END
    await message.reply_text(
        "Select a cryptocurrency:", reply_markup=cryptocurrencies_keyboard(cryptos)
    )
    return CHOOSE_CRYPTO


async def crypto_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None
    await query.answer()
    crypto_id = parse_id(query.data, ADD_CRYPTO)
    assets = (
        await enabled_assets(deps.settings(context), cryptocurrency_id=crypto_id)
        if crypto_id is not None
        else []
    )
    if not assets:
        await query.edit_message_text(
            "This cryptocurrency is not available. Use /add to start again."
        )
        return ConversationHandler.END
    await query.edit_message_text(
        f"Select the {html.escape(assets[0].symbol)} network:",
        reply_markup=networks_keyboard(assets),
    )
    return CHOOSE_NETWORK


async def network_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None
    await query.answer()
    asset_id = parse_id(query.data, ADD_NETWORK)
    asset = await get_asset(asset_id, deps.settings(context)) if asset_id is not None else None
    if asset is None:
        await query.edit_message_text("This network is not available. Use /add to start again.")
        return ConversationHandler.END
    assert context.user_data is not None
    context.user_data[ASSET_KEY] = asset.id
    await query.edit_message_text(
        f"Send the {asset.network_name} wallet address you want to monitor for incoming "
        f"{asset.symbol} ({asset.token_standard}) transfers.\n\nSend /cancel to abort."
    )
    return ENTER_ADDRESS


async def address_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    user = update.effective_user
    assert message is not None and user is not None and context.user_data is not None
    asset_id = context.user_data.get(ASSET_KEY)
    if asset_id is None:
        await message.reply_text("Something went wrong. Use /add to start again.")
        return ConversationHandler.END
    try:
        sub = await deps.subscriptions(context).subscribe(
            user.id, user.username, asset_id, message.text or ""
        )
    except InvalidAddress as exc:
        await message.reply_text(f"{exc} Please check it and send it again, or /cancel.")
        return ENTER_ADDRESS
    except SubscriptionError as exc:
        await message.reply_text(str(exc))
        context.user_data.pop(ASSET_KEY, None)
        return ConversationHandler.END
    context.user_data.pop(ASSET_KEY, None)
    await message.reply_html(
        "✅ Monitoring is active.\n\n"
        f"Cryptocurrency: {html.escape(sub.asset.symbol)}\n"
        f"Network: {html.escape(sub.asset.label)}\n"
        f"Wallet: <code>{html.escape(sub.address)}</code>\n\n"
        f"You will be notified about incoming transfers of at least "
        f"{sub.asset.min_amount.normalize():f} {html.escape(sub.asset.symbol)} made from now on."
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if context.user_data is not None:
        context.user_data.pop(ASSET_KEY, None)
    query = update.callback_query
    if query is not None:
        await query.answer()
        await query.edit_message_text("Cancelled.")
    elif update.effective_message is not None:
        await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END


def build_add_handler() -> ConversationHandler:
    # The flow mixes buttons and a typed address, so it is tracked per chat/user
    # (per_message=False); PTB warns about that combination by design.
    warnings.filterwarnings("ignore", message="If 'per_message=False'", category=PTBUserWarning)
    return ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            CHOOSE_CRYPTO: [CallbackQueryHandler(crypto_chosen, pattern=f"^{ADD_CRYPTO}\\d+$")],
            CHOOSE_NETWORK: [CallbackQueryHandler(network_chosen, pattern=f"^{ADD_NETWORK}\\d+$")],
            ENTER_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, address_entered)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern=f"^{ADD_CANCEL}$"),
            CommandHandler("add", add_start),
        ],
        allow_reentry=True,
        conversation_timeout=600,
    )
