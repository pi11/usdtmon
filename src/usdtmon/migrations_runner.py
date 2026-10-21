"""Programmatic equivalent of ``tortoise migrate`` (used by ``usdtmon migrate`` and tests)."""

from __future__ import annotations

from tortoise.context import TortoiseContext
from tortoise.migrations.api import migrate as migrate_api

from usdtmon.db import tortoise_config


async def migrate(database_url: str) -> None:
    # An isolated context: does not disturb an already initialized application.
    async with TortoiseContext():
        await migrate_api(config=tortoise_config(database_url))
