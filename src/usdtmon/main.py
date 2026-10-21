"""Command-line entry point: ``usdtmon <command>`` (or ``python -m usdtmon``)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Awaitable, Callable
from decimal import Decimal

from usdtmon.blockchain.registry import AdapterRegistry
from usdtmon.config import ConfigError, Settings, load_settings
from usdtmon.db import close_db, init_db, tortoise_config
from usdtmon.logs import setup_logging

log = logging.getLogger("usdtmon")


def _stop_event() -> asyncio.Event:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    return stop


async def _with_db(settings: Settings, fn: Callable[[], Awaitable[None]]) -> None:
    await init_db(settings.database_url)
    try:
        await fn()
    finally:
        await close_db()


def run_bot(settings: Settings) -> None:
    from usdtmon.bot.app import build_application

    registry = AdapterRegistry(settings)
    app = build_application(settings, registry)
    app.run_polling(allowed_updates=["message", "callback_query"])


async def _monitor(settings: Settings, stop: asyncio.Event) -> None:
    from usdtmon.workers.monitor import MonitorWorker, check_network_config

    registry = AdapterRegistry(settings)
    try:
        await check_network_config(settings, registry)
        await MonitorWorker(settings, registry).run(stop)
    finally:
        await registry.aclose()


async def _notifier(settings: Settings, stop: asyncio.Event) -> None:
    from telegram import Bot

    from usdtmon.bot.sender import TelegramSender
    from usdtmon.services.notifications import NotificationService
    from usdtmon.workers.notifier import run_notifier

    async with Bot(settings.require_telegram_token()) as bot:
        service = NotificationService(
            TelegramSender(bot),
            max_attempts=settings.notify_max_attempts,
            lease_seconds=settings.notify_lease_seconds,
            rate_limit=settings.notify_rate_limit,
        )
        await run_notifier(
            service,
            stop,
            poll_interval=settings.notify_poll_interval,
            batch_size=settings.notify_batch_size,
        )


async def run_workers(settings: Settings, *, monitor: bool, notifier: bool) -> None:
    stop = _stop_event()

    async def body() -> None:
        tasks = []
        if monitor:
            tasks.append(asyncio.create_task(_monitor(settings, stop)))
        if notifier:
            tasks.append(asyncio.create_task(_notifier(settings, stop)))
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        stop.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result

    await _with_db(settings, body)


async def run_migrate(settings: Settings) -> None:
    from usdtmon.migrations_runner import migrate

    await migrate(settings.database_url)


async def run_register_asset(settings: Settings, args: argparse.Namespace) -> None:
    from usdtmon.services.assets import register_asset

    registry = AdapterRegistry(settings)

    async def body() -> None:
        asset = await register_asset(
            registry,
            symbol=args.symbol,
            name=args.name,
            network_code=args.network,
            contract_address=args.contract,
            decimals=args.decimals,
            min_amount=Decimal(args.min_amount),
        )
        print(f"Registered {args.symbol.upper()} on {args.network}: asset id {asset.id}")

    try:
        await _with_db(settings, body)
    finally:
        await registry.aclose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="usdtmon", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("bot", help="Run the Telegram bot")
    sub.add_parser("monitor", help="Run the blockchain monitoring worker")
    sub.add_parser("notifier", help="Run the notification delivery worker")
    sub.add_parser("worker", help="Run monitor and notifier in one process")
    sub.add_parser("migrate", help="Apply database migrations")
    sub.add_parser("check-config", help="Validate configuration and exit")
    reg = sub.add_parser("register-asset", help="Allowlist a token contract on a network")
    reg.add_argument("--symbol", required=True)
    reg.add_argument("--name", required=True)
    reg.add_argument("--network", required=True, help="Network code, e.g. ethereum")
    reg.add_argument("--contract", required=True, help="Verified token contract address")
    reg.add_argument("--decimals", type=int, required=True)
    reg.add_argument("--min-amount", required=True, help="Minimum notification amount")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
        if args.command in ("bot", "notifier", "worker"):
            settings.require_telegram_token()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(2)
    setup_logging(settings.log_level, settings.log_format)

    try:
        match args.command:
            case "bot":
                run_bot(settings)
            case "monitor":
                asyncio.run(run_workers(settings, monitor=True, notifier=False))
            case "notifier":
                asyncio.run(run_workers(settings, monitor=False, notifier=True))
            case "worker":
                asyncio.run(run_workers(settings, monitor=True, notifier=True))
            case "migrate":
                asyncio.run(run_migrate(settings))
            case "register-asset":
                asyncio.run(run_register_asset(settings, args))
            case "check-config":
                tortoise_config(settings.database_url)
                for code, ns in settings.networks.items():
                    print(f"{code}: {'configured' if ns.rpc_url else 'NOT configured'}")
                print("Configuration OK")
    except ConfigError as exc:
        log.error("configuration_error", extra={"error": str(exc)})
        sys.exit(2)


if __name__ == "__main__":
    main()
