# usdtmon

A read-only Telegram bot that watches cryptocurrency wallets and notifies users about
**incoming** transfers. Out of the box it supports USDT on Ethereum (ERC-20) and TRON
(TRC-20). The bot never asks for or stores private keys.

- Only allowlisted token contracts are monitored. Fake "USDT" tokens are ignored.
- Transfers are reported only after their block is final, and only if they were made after the subscription.
- Every (subscription, transfer) pair gets one message. This is enforced by database
  constraints and a transactional outbox.
- The bot, the monitor and the notifier are separate processes that share one PostgreSQL database.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design and the reasons behind it.

## Requirements

- Python 3.12+
- PostgreSQL 14+
- An Ethereum JSON-RPC endpoint that supports `eth_getLogs` and the `finalized` block tag.
  Any major provider (Infura, Alchemy, QuickNode, publicnode) or your own node works.
- A TRON endpoint that exposes java-tron's `/jsonrpc`. TronGrid works. An API key is strongly
  recommended: without one TronGrid rate limits aggressively.
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## Installation

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e .            # add ".[dev]" for the test tools
cp .env.example .env        # then edit it
```

Create the database and role:

```bash
sudo -u postgres createuser --pwprompt usdtmon
sudo -u postgres createdb --owner usdtmon usdtmon
```

Apply the migrations. They are Tortoise ORM native migrations and are committed in
`src/usdtmon/migrations/`:

```bash
tortoise migrate        # reads [tool.tortoise] in pyproject.toml and DATABASE_URL
# or equivalently
usdtmon migrate
```

The schema is never auto-generated. Always run migrations before you start a new release.

## Running

Each command is a long-running process. Run them under systemd, supervisord or similar:

| Command            | What it does                                                   |
|--------------------|----------------------------------------------------------------|
| `usdtmon bot`      | Telegram bot (long polling): `/start /add /list /remove /help` |
| `usdtmon monitor`  | Polls the blockchains and writes transfers and notifications   |
| `usdtmon notifier` | Delivers pending notifications to Telegram                     |
| `usdtmon worker`   | `monitor` and `notifier` in one process (for small setups)     |

You can run several `monitor` or `notifier` instances at the same time. Per-asset leases
and `FOR UPDATE SKIP LOCKED` stop them from doing the same work twice, and compare-and-set
cursor updates keep the results correct even if a lease is lost.

The bot also needs the blockchain endpoints. It uses them to validate addresses and to
record the subscription checkpoint (the latest finalized block).

Example systemd unit:

```ini
[Unit]
Description=usdtmon monitor
After=network-online.target postgresql.service

[Service]
WorkingDirectory=/opt/usdtmon
EnvironmentFile=/opt/usdtmon/.env
ExecStart=/opt/usdtmon/.venv/bin/usdtmon monitor
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Use `usdtmon check-config` to validate the environment without starting anything.

## Configuration

All settings come from environment variables. A `.env` file in the working directory is
also read, but variables already set in the environment take precedence over it.
`.env.example` lists every setting.

| Variable | Default | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Required for `bot`, `notifier` and `worker` |
| `DATABASE_URL` | — | `postgres://user:pass@host:5432/db` |
| `ETHEREUM_RPC_URL` | — | Required while the `ethereum` network is enabled |
| `TRON_API_URL` | `https://api.trongrid.io` | `/jsonrpc` is appended automatically |
| `TRON_API_KEY` | — | Sent as `TRON-PRO-API-KEY` |
| `MONITOR_INTERVAL` | `30` | Seconds. `network.polling_interval` or `<NET>_POLL_INTERVAL` override it per network |
| `<SYMBOL>_MIN_AMOUNT` | DB value | Per-cryptocurrency threshold override, e.g. `USDT_MIN_AMOUNT=0.5` |
| `<NET>_FINALITY` | `finalized` | `finalized` uses the chain's finality. `confirmations` uses latest minus `<NET>_CONFIRMATIONS` |
| `<NET>_CONFIRMATIONS` | 12 (ETH), 20 (TRON) | Only used in `confirmations` mode |
| `<NET>_MAX_BLOCK_RANGE` | 500 (ETH), 1000 (TRON) | Blocks per `eth_getLogs` request. Halved automatically if the provider refuses |
| `<NET>_MAX_ADDRESSES_PER_QUERY` | 100 | Wallets per `eth_getLogs` request (topic OR-list) |
| `<NET>_SCAN_BATCH_BLOCKS` | 2000 | Blocks committed per database transaction |
| `<NET>_VERIFY_RECEIPTS` | `true` (TRON), `false` (ETH) | Check each matching transaction's receipt status. Costs one request per transaction |
| `<NET>_REQUEST_TIMEOUT` | 15 | Seconds |
| `<NET>_MAX_CONCURRENCY` | 4 | Parallel requests per provider |
| `<NET>_RATE_LIMIT` | 10 | Requests per second per provider |
| `<NET>_MAX_RETRIES`, `<NET>_BACKOFF_BASE`, `<NET>_BACKOFF_MAX` | 5, 0.5, 30 | Exponential backoff with jitter |
| `NOTIFY_POLL_INTERVAL` | 2 | Seconds between outbox polls |
| `NOTIFY_MAX_ATTEMPTS` | 10 | Attempts before a notification is marked failed |
| `NOTIFY_RATE_LIMIT` | 25 | Messages per second (Telegram allows about 30) |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` | `LOG_FORMAT=text` for human-readable logs |

`<NET>` is the upper-case network code (`ETHEREUM`, `TRON`).

The minimum notification amount is stored per cryptocurrency in
`cryptocurrency.minimum_notification_amount` (0.5 for USDT). An incoming transfer of
exactly the minimum is reported.

## Adding a token

To add an ERC-20 or TRC-20 token on a network that is already supported, register its
**verified** contract. This needs no code changes:

```bash
usdtmon register-asset --symbol USDC --name "USD Coin" --network ethereum \
    --contract 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48 --decimals 6 --min-amount 1
```

For a reproducible deployment, put the same `INSERT` statements in a data migration (see
`0002_seed_usdt.py`).

## Adding a blockchain

1. Implement `BlockchainAdapter` (`src/usdtmon/blockchain/base.py`). For EVM-compatible
   chains, subclass `EvmLogAdapter` and provide the address codec.
2. Add a factory to `ADAPTER_FACTORIES` in `blockchain/registry.py` and default settings
   in `config.py`.
3. Add a migration that inserts the `network` row and its `network_asset` rows.

The Telegram handlers, subscription, monitoring and notification services do not change.

## Changing models

```bash
tortoise makemigrations      # writes src/usdtmon/migrations/000N_*.py; review and commit it
tortoise migrate
```

Caveat for tortoise-orm 1.1: `CreateModel` drops `Meta.constraints`. When a migration creates a
model that has CHECK or partial UNIQUE constraints, move them into explicit
`ops.AddConstraint(...)` operations, as `0001_initial.py` does. The test suite checks that
the committed migrations match the models.

## Tests

```bash
pip install -e ".[dev]"
pytest tests/unit                                              # no database needed
TEST_DATABASE_URL=postgres://postgres@localhost/usdtmon_test pytest   # everything
```

Integration tests **drop and recreate** the database named in `TEST_DATABASE_URL`, apply
the migrations, and run against real PostgreSQL. Blockchain APIs are mocked: unit tests
use `respx` for JSON-RPC, and integration tests use an in-memory adapter.

## Operations

- **Restarts:** cursors are stored in PostgreSQL, so after a restart the monitor resumes
  at the first unprocessed block.
- **Provider outages:** a failed range is never committed. The worker backs off, up to 10
  minutes between cycles, and later scans the whole gap in batches.
- **Notifier crash:** a message whose delivery status was not recorded is sent again once
  its claim lease expires (`NOTIFY_LEASE_SECONDS`, default 120). The re-sent message is
  marked as a possible duplicate.
- **Blocked bot:** a user who blocks the bot is flagged and gets no new notifications.
  Sending `/start` again clears the flag.
- **Useful queries:**
  - `SELECT status, count(*) FROM notification GROUP BY 1;`
  - `SELECT * FROM monitoring_cursor ORDER BY updated_at;` (stale cursors mean a stuck network)
  - `SELECT * FROM worker_lease;`
