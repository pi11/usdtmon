from tortoise import Tortoise, connections
from tortoise.migrations.autodetector import MigrationAutodetector

from usdtmon.db import tortoise_config
from usdtmon.migrations_runner import migrate
from usdtmon.models import Cryptocurrency, NetworkAsset


async def test_migrations_create_schema_and_seed(database):
    conn = connections.get("default")
    _, rows = await conn.execute_query(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    )
    tables = {r["tablename"] for r in rows}
    assert {
        "telegram_user",
        "cryptocurrency",
        "network",
        "network_asset",
        "wallet",
        "subscription",
        "transfer",
        "monitoring_cursor",
        "notification",
        "worker_lease",
    } <= tables

    usdt = await Cryptocurrency.get(symbol="USDT")
    assert usdt.decimals == 6 and str(usdt.minimum_notification_amount.normalize()) == "0.5"
    contracts = {
        a.network.code: a.contract_address
        for a in await NetworkAsset.filter(cryptocurrency=usdt).select_related("network")
    }
    assert contracts == {
        "ethereum": "0xdac17f958d2ee523a2206206994597c13d831ec7",
        "tron": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
    }


async def test_constraints_exist(database):
    conn = connections.get("default")
    _, rows = await conn.execute_query("SELECT conname FROM pg_constraint")
    names = {r["conname"] for r in rows}
    assert {"ck_transfer_amount_positive", "ck_crypto_min_amount_non_negative"} <= names
    _, rows = await conn.execute_query(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_subscription_active'"
    )
    assert "WHERE active" in rows[0]["indexdef"]


async def test_migrations_are_idempotent(database):
    # Second run applies nothing and does not fail.
    await migrate(database)


async def test_models_match_migrations(database):
    """makemigrations would produce nothing: committed migrations cover the models."""
    config = tortoise_config(database)
    detector = MigrationAutodetector(Tortoise.apps, config["apps"])
    assert await detector.changes() == []
