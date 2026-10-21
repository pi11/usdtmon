"""Tortoise ORM configuration and lifecycle helpers."""

from __future__ import annotations

import os
from typing import Any

from tortoise import Tortoise

from usdtmon.config import load_dotenv

APP_LABEL = "models"


def tortoise_config(database_url: str) -> dict[str, Any]:
    return {
        "connections": {"default": database_url},
        "apps": {
            APP_LABEL: {
                "models": ["usdtmon.models"],
                "migrations": "usdtmon.migrations",
                "default_connection": "default",
            }
        },
        "use_tz": True,
        "timezone": "UTC",
    }


def _cli_database_url() -> str:
    load_dotenv()
    # The CLI only needs a syntactically valid URL to build migrations offline.
    return os.environ.get("DATABASE_URL") or "postgres://localhost:5432/usdtmon"


# Used by the ``tortoise`` CLI (see [tool.tortoise] in pyproject.toml).
TORTOISE_ORM = tortoise_config(_cli_database_url())


async def init_db(database_url: str) -> None:
    # Global fallback makes the context visible to tasks spawned by
    # python-telegram-bot and by the workers (contextvars do not propagate up).
    await Tortoise.init(config=tortoise_config(database_url), _enable_global_fallback=True)


async def close_db() -> None:
    await Tortoise.close_connections()
