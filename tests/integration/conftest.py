"""Integration tests against a real PostgreSQL.

Set TEST_DATABASE_URL (e.g. postgres://postgres@127.0.0.1:5432/usdtmon_test).
The database is dropped and recreated, then migrated with the project's native
Tortoise migrations, so the schema under test is exactly the migrated one.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest
from tortoise import connections

from tests.fixtures.chain import FakeAdapter
from usdtmon.blockchain.registry import AdapterRegistry
from usdtmon.config import load_settings
from usdtmon.db import close_db, init_db
from usdtmon.migrations_runner import migrate

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.integration

DYNAMIC_TABLES = (
    "notification",
    "transfer",
    "monitoring_cursor",
    "subscription",
    "wallet",
    "telegram_user",
    "worker_lease",
)


def pytest_collection_modifyitems(config, items):
    if TEST_DATABASE_URL:
        return
    skip = pytest.mark.skip(reason="TEST_DATABASE_URL is not set")
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(skip)


async def _recreate_database(url: str) -> None:
    parsed = urlparse(url)
    name = parsed.path.lstrip("/")
    admin = urlunparse(parsed._replace(path="/postgres", scheme="postgresql"))
    conn = await asyncpg.connect(admin)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
async def database():
    assert TEST_DATABASE_URL
    await _recreate_database(TEST_DATABASE_URL)
    await migrate(TEST_DATABASE_URL)
    await init_db(TEST_DATABASE_URL)
    yield TEST_DATABASE_URL
    await close_db()


@pytest.fixture(autouse=True)
async def clean_db(database):
    conn = connections.get("default")
    await conn.execute_script(
        f"TRUNCATE {', '.join(DYNAMIC_TABLES)} RESTART IDENTITY CASCADE;"
        "DELETE FROM network_asset WHERE cryptocurrency_id IN "
        "(SELECT id FROM cryptocurrency WHERE symbol <> 'USDT');"
        "DELETE FROM cryptocurrency WHERE symbol <> 'USDT';"
        "UPDATE cryptocurrency SET minimum_notification_amount = 0.5, enabled = TRUE;"
        "UPDATE network SET enabled = TRUE; UPDATE network_asset SET enabled = TRUE;"
    )
    yield


@pytest.fixture
def settings():
    return load_settings({"DATABASE_URL": TEST_DATABASE_URL or "postgres://x/y"})


@pytest.fixture
def eth():
    return FakeAdapter("ethereum")


@pytest.fixture
def registry(settings, eth):
    return AdapterRegistry.from_adapters(settings, {"ethereum": eth})
