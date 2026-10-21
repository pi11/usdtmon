"""Environment-based configuration.

All secrets and deployment-specific values come from environment variables. A
``.env`` file in the working directory is loaded for convenience; variables that
are already set in the environment take precedence over it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse


class ConfigError(Exception):
    """Raised when the configuration is missing or invalid."""


class FinalityMode(StrEnum):
    # Use the chain's own finality signal ("finalized" block tag).
    FINALIZED = "finalized"
    # Use latest block minus a fixed confirmation depth.
    CONFIRMATIONS = "confirmations"


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal ``.env`` loader: KEY=VALUE lines, ``#`` comments, optional quotes."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class NetworkSettings:
    """Provider settings for one blockchain network (env prefix ``<CODE>_``)."""

    code: str
    rpc_url: str | None
    api_key: str | None
    finality: FinalityMode
    confirmations: int
    max_block_range: int
    max_addresses_per_query: int
    request_timeout: float
    max_concurrency: int
    rate_limit: float
    max_retries: int
    backoff_base: float
    backoff_max: float
    # Max blocks committed in one monitoring step (one DB transaction).
    scan_batch_blocks: int
    poll_interval: float | None
    # Check each matching transaction's receipt status (one request per tx).
    verify_receipts: bool


@dataclass(frozen=True)
class Settings:
    database_url: str
    telegram_bot_token: str | None
    monitor_interval: float
    log_level: str
    log_format: str
    notify_poll_interval: float
    notify_max_attempts: int
    notify_rate_limit: float
    notify_batch_size: int
    notify_lease_seconds: int
    networks: Mapping[str, NetworkSettings] = field(default_factory=dict)
    min_amount_overrides: Mapping[str, Decimal] = field(default_factory=dict)

    def network(self, code: str) -> NetworkSettings:
        try:
            return self.networks[code]
        except KeyError:
            raise ConfigError(f"No settings for network {code!r}") from None

    def min_amount_override(self, symbol: str) -> Decimal | None:
        return self.min_amount_overrides.get(symbol.upper())

    def require_telegram_token(self) -> str:
        if not self.telegram_bot_token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is required")
        return self.telegram_bot_token


# Built-in defaults per network; every value can be overridden with <CODE>_<NAME>.
_NETWORK_DEFAULTS: dict[str, dict[str, str]] = {
    "ethereum": {
        "FINALITY": "finalized",
        "CONFIRMATIONS": "12",
        "MAX_BLOCK_RANGE": "500",
    },
    "tron": {
        "API_URL": "https://api.trongrid.io",
        "FINALITY": "finalized",
        "CONFIRMATIONS": "20",
        "MAX_BLOCK_RANGE": "1000",
        "VERIFY_RECEIPTS": "true",
    },
}

KNOWN_NETWORKS = tuple(_NETWORK_DEFAULTS)


class _Env:
    def __init__(self, env: Mapping[str, str]) -> None:
        self.env = env

    def text(self, name: str, default: str | None = None) -> str | None:
        value = self.env.get(name)
        if value is None or value.strip() == "":
            return default
        return value.strip()

    def number(self, name: str, default: float, *, minimum: float = 0.0) -> float:
        raw = self.text(name)
        if raw is None:
            return default
        try:
            value = float(raw)
        except ValueError:
            raise ConfigError(f"{name} must be a number, got {raw!r}") from None
        if value < minimum:
            raise ConfigError(f"{name} must be >= {minimum}")
        return value

    def integer(self, name: str, default: int, *, minimum: int = 0) -> int:
        raw = self.text(name)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError:
            raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
        if value < minimum:
            raise ConfigError(f"{name} must be >= {minimum}")
        return value


def _validate_url(name: str, url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(f"{name} must be an http(s) URL")
    return url.rstrip("/")


def _bool(name: str, raw: str) -> bool:
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false")


def _network_settings(e: _Env, code: str) -> NetworkSettings:
    prefix = code.upper() + "_"
    defaults = _NETWORK_DEFAULTS.get(code, {})

    def s(name: str) -> str | None:
        return e.text(prefix + name, defaults.get(name))

    url = s("RPC_URL") or s("API_URL")
    if url:
        url = _validate_url(prefix + ("RPC_URL" if s("RPC_URL") else "API_URL"), url)
    finality_raw = (s("FINALITY") or "finalized").lower()
    try:
        finality = FinalityMode(finality_raw)
    except ValueError:
        raise ConfigError(f"{prefix}FINALITY must be 'finalized' or 'confirmations'") from None
    poll = e.text(prefix + "POLL_INTERVAL")
    return NetworkSettings(
        code=code,
        rpc_url=url,
        api_key=s("API_KEY"),
        finality=finality,
        confirmations=e.integer(prefix + "CONFIRMATIONS", int(defaults.get("CONFIRMATIONS", "12"))),
        max_block_range=e.integer(
            prefix + "MAX_BLOCK_RANGE", int(defaults.get("MAX_BLOCK_RANGE", "500")), minimum=1
        ),
        max_addresses_per_query=e.integer(prefix + "MAX_ADDRESSES_PER_QUERY", 100, minimum=1),
        request_timeout=e.number(prefix + "REQUEST_TIMEOUT", 15.0, minimum=0.1),
        max_concurrency=e.integer(prefix + "MAX_CONCURRENCY", 4, minimum=1),
        rate_limit=e.number(prefix + "RATE_LIMIT", 10.0, minimum=0.01),
        max_retries=e.integer(prefix + "MAX_RETRIES", 5),
        backoff_base=e.number(prefix + "BACKOFF_BASE", 0.5),
        backoff_max=e.number(prefix + "BACKOFF_MAX", 30.0),
        scan_batch_blocks=e.integer(prefix + "SCAN_BATCH_BLOCKS", 2000, minimum=1),
        poll_interval=e.number(prefix + "POLL_INTERVAL", 0.0, minimum=1.0) if poll else None,
        verify_receipts=_bool(prefix + "VERIFY_RECEIPTS", s("VERIFY_RECEIPTS") or "false"),
    )


def _min_amount_overrides(env: Mapping[str, str]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for key, raw in env.items():
        if not key.endswith("_MIN_AMOUNT") or not raw.strip():
            continue
        symbol = key.removesuffix("_MIN_AMOUNT")
        try:
            value = Decimal(raw.strip())
        except InvalidOperation:
            raise ConfigError(f"{key} must be a decimal number, got {raw!r}") from None
        if not value.is_finite() or value < 0:
            raise ConfigError(f"{key} must be a non-negative number")
        out[symbol.upper()] = value
    return out


def load_settings(env: Mapping[str, str] | None = None, *, dotenv: bool = True) -> Settings:
    """Build and validate settings. Raises :class:`ConfigError` on invalid input."""
    if env is None:
        if dotenv:
            load_dotenv()
        env = os.environ
    e = _Env(env)

    database_url = e.text("DATABASE_URL")
    if not database_url:
        raise ConfigError("DATABASE_URL is required")
    if urlparse(database_url).scheme not in ("postgres", "postgresql", "asyncpg"):
        raise ConfigError("DATABASE_URL must be a PostgreSQL URL (postgres://...)")

    log_level = (e.text("LOG_LEVEL", "INFO") or "INFO").upper()
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ConfigError(f"LOG_LEVEL is invalid: {log_level}")
    log_format = (e.text("LOG_FORMAT", "json") or "json").lower()
    if log_format not in ("json", "text"):
        raise ConfigError("LOG_FORMAT must be 'json' or 'text'")

    return Settings(
        database_url=database_url,
        telegram_bot_token=e.text("TELEGRAM_BOT_TOKEN"),
        monitor_interval=e.number("MONITOR_INTERVAL", 30.0, minimum=1.0),
        log_level=log_level,
        log_format=log_format,
        notify_poll_interval=e.number("NOTIFY_POLL_INTERVAL", 2.0, minimum=0.1),
        notify_max_attempts=e.integer("NOTIFY_MAX_ATTEMPTS", 10, minimum=1),
        notify_rate_limit=e.number("NOTIFY_RATE_LIMIT", 25.0, minimum=0.1),
        notify_batch_size=e.integer("NOTIFY_BATCH_SIZE", 50, minimum=1),
        notify_lease_seconds=e.integer("NOTIFY_LEASE_SECONDS", 120, minimum=10),
        networks={code: _network_settings(e, code) for code in KNOWN_NETWORKS},
        min_amount_overrides=_min_amount_overrides(env),
    )
