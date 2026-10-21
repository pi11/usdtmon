from decimal import Decimal

import pytest

from usdtmon.config import ConfigError, FinalityMode, load_settings

BASE = {"DATABASE_URL": "postgres://u:p@localhost/db"}


def test_defaults():
    s = load_settings(BASE)
    assert s.monitor_interval == 30
    assert s.telegram_bot_token is None
    assert s.network("tron").rpc_url == "https://api.trongrid.io"
    assert s.network("ethereum").rpc_url is None
    assert s.network("ethereum").finality is FinalityMode.FINALIZED
    assert s.min_amount_override("USDT") is None
    assert s.network("tron").verify_receipts and not s.network("ethereum").verify_receipts


def test_overrides():
    s = load_settings(
        {
            **BASE,
            "USDT_MIN_AMOUNT": "0.5",
            "MONITOR_INTERVAL": "10",
            "ETHEREUM_RPC_URL": "https://eth.example/rpc/",
            "ETHEREUM_FINALITY": "confirmations",
            "ETHEREUM_CONFIRMATIONS": "20",
            "TRON_POLL_INTERVAL": "5",
        }
    )
    assert s.min_amount_override("usdt") == Decimal("0.5")
    assert s.monitor_interval == 10
    eth = s.network("ethereum")
    assert eth.rpc_url == "https://eth.example/rpc"
    assert eth.finality is FinalityMode.CONFIRMATIONS
    assert eth.confirmations == 20
    assert s.network("tron").poll_interval == 5


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"DATABASE_URL": "mysql://x/y"},
        {**BASE, "MONITOR_INTERVAL": "abc"},
        {**BASE, "MONITOR_INTERVAL": "0"},
        {**BASE, "USDT_MIN_AMOUNT": "-1"},
        {**BASE, "USDT_MIN_AMOUNT": "lots"},
        {**BASE, "ETHEREUM_RPC_URL": "ftp://nope"},
        {**BASE, "ETHEREUM_FINALITY": "vibes"},
        {**BASE, "LOG_LEVEL": "LOUD"},
        {**BASE, "TRON_VERIFY_RECEIPTS": "maybe"},
    ],
)
def test_invalid(env):
    with pytest.raises(ConfigError):
        load_settings(env)


def test_telegram_token_required_on_demand():
    with pytest.raises(ConfigError):
        load_settings(BASE).require_telegram_token()
    assert load_settings({**BASE, "TELEGRAM_BOT_TOKEN": "t"}).require_telegram_token() == "t"
