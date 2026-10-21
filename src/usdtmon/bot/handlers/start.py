from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from usdtmon.services.subscriptions import upsert_user

WELCOME = (
    "Welcome! I watch cryptocurrency wallets and notify you about incoming transfers.\n\n"
    "Use /add to start monitoring a wallet, /list to see your subscriptions and "
    "/help for details.\n\n"
    "I am read-only: I never ask for private keys or seed phrases."
)

HELP = (
    "<b>Commands</b>\n"
    "/add — monitor a wallet\n"
    "/list — your subscriptions\n"
    "/remove — stop monitoring a wallet\n"
    "/cancel — cancel the current action\n"
    "/help — this message\n\n"
    "<b>How monitoring works</b>\n"
    "• Only official token contracts are watched (for USDT: the Tether contracts on "
    "Ethereum and TRON). Look-alike tokens are ignored.\n"
    "• You get a message for each incoming transfer of at least the minimum amount "
    "(0.5 USDT by default). Outgoing transfers are ignored.\n"
    "• A transfer is reported once its block is final, so notifications can take a "
    "few minutes (about 15 minutes on Ethereum, about 1 minute on TRON).\n"
    "• Only transfers made after you subscribe are reported.\n\n"
    "Never share your private keys or seed phrase with anyone, including this bot."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.effective_message is None:
        return
    await upsert_user(user.id, user.username)
    await update.effective_message.reply_text(WELCOME)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is not None:
        await update.effective_message.reply_html(HELP)
