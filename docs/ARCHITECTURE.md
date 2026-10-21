# Architecture and decisions

## Processes

```
            ┌──────────────┐   subscribe / list / remove   ┌──────────────┐
 Telegram ◀─┤  usdtmon bot ├──────────────────────────────▶│              │
            └──────┬───────┘   checkpoint = finalized head │              │
                   │ validate address                      │  PostgreSQL  │
                   ▼                                       │              │
            ┌──────────────┐  transfers + notifications +  │              │
 RPC nodes ◀┤   monitor    ├───── cursor move (1 txn) ────▶│              │
            └──────────────┘                               │              │
            ┌──────────────┐  claim (SKIP LOCKED) / mark   │              │
 Telegram ◀─┤   notifier   ├──────────────────────────────▶│              │
            └──────────────┘                               └──────────────┘
```

The only thing the processes share is the database. There is no broker. The
`notification` table works as a transactional outbox.

## Code layout

```
src/usdtmon/
  config.py            env parsing and validation (no secrets in code)
  db.py                Tortoise config (also used by the `tortoise` CLI)
  models/              Tortoise models; integrity rules are PostgreSQL constraints
  migrations/          native Tortoise migrations (0001 schema, 0002 USDT seed)
  blockchain/
    base.py            BlockchainAdapter, Token, TransferEvent
    rpc.py             JSON-RPC client: timeouts, concurrency, rate limit, retries
    evm.py             shared eth_getLogs engine for EVM-style logs
    ethereum.py        EIP-55 address codec
    tron.py            base58check <-> hex codec, receipt verification
    registry.py        network code -> adapter factory
  services/            all business logic (handlers stay thin)
  bot/                 python-telegram-bot handlers, keyboards, Telegram sender
  workers/             monitor and notifier loops
```

## Decisions

### One log engine for Ethereum and TRON
java-tron provides an Ethereum-compatible JSON-RPC interface (`/jsonrpc` on TronGrid) with
`eth_getLogs`, the `finalized` block tag, `blockTimestamp` and receipts. TRC-20 emits the same
`Transfer(address,address,uint256)` event as ERC-20. Both adapters therefore subclass
`EvmLogAdapter` and differ only in their address codec. This approach was checked against
mainnet for both chains during development.

The alternative was TronGrid's REST API (`/v1/accounts/{addr}/transactions/trc20`). It was
rejected because it returns no block numbers and no log index. It would force
timestamp-based cursors, and it cannot tell apart two identical transfers in one
transaction.

### The allowlist is the query, and it is checked again
`eth_getLogs` is called with `address = <allowlisted contract>`. Fake tokens therefore never
match, whatever their symbol or name. Every returned log is still re-checked: contract,
event signature, topic layout, recipient in the watched set, block within the requested
range, and `removed=false`. A buggy or malicious provider cannot inject transfers. Token
identity is always the contract address, never the symbol.

### One query covers many wallets
Recipients go into topic 2 as an OR-list, chunked with `MAX_ADDRESSES_PER_QUERY`.
Cursors are stored per (asset, wallet), as the specification asks. The monitor groups
wallets that have the same cursor value and scans each group with one request. A wallet
watched by many users is scanned once. A group that falls behind (for example, a new
subscription made during an outage) is scanned up to the next group's cursor and then
merges with that group.

### Finality instead of reorg rollback
Only blocks at or below the finalized head are scanned. On Ethereum that is the
`finalized` tag (about 13 minutes behind the tip). On TRON it is the solidified block
(about 1 minute behind). A transfer is therefore never reported and then reverted.
`<NET>_FINALITY=confirmations` switches to latest minus N blocks, for providers that do not
support `finalized`. In that mode, the block hash of every matching log is compared with the
canonical block at that height. A mismatch means a reorg deeper than N. The range is then
rejected and retried later, so its cursor does not move. If the provider reports a lower
finalized head than it did before, the adapter keeps the earlier head.

### Never advance a cursor without the data
One database transaction does three things: it inserts the transfers
(`ON CONFLICT DO NOTHING` on `(network, tx_hash, event_index)`), inserts the notification
rows (`ON CONFLICT DO NOTHING` on `(subscription, transfer)`), and moves the group's
cursors with a compare-and-set (`WHERE last_processed_block = <expected>`). A provider
error raises before the transaction starts. A cursor that another worker has already moved
rolls everything back. In both cases the range is retried in full later. The adapter
contract is "the complete range or an exception", so a partial page cannot create a gap.

### Leases are for efficiency, not for correctness
`worker_lease` gives a monitor exclusive, time-limited ownership of an asset
(`monitor:asset:<id>`) and is renewed after every step. If a lease is lost, for example
because of a long GC pause, the compare-and-set cursor update and the unique constraints
still prevent duplicates. Leases only prevent wasted work.

### No historical notifications
When a subscription is created, the bot records the current finalized block in
`subscription.start_block`. Only transfers in later blocks create notifications for that
subscription. This holds even when the wallet's shared cursor is behind because another
user's subscription is still catching up. If nobody watched the wallet in the meantime, a
new subscription moves the cursor forward instead of rescanning blocks nobody watched.

### Outbox delivery semantics
The notifier claims rows atomically:
`UPDATE ... FROM (SELECT ... FOR UPDATE SKIP LOCKED)` sets `status='sending'`,
`locked_until` and `attempts+1`. After sending, the row is updated only if it is still the
same claim (same `status` and `attempts`).

Telegram has no idempotency key. If the process dies after Telegram accepted a message but
before the row was marked `sent`, there is no way to know whether the message arrived. The
row is claimed again once its lease expires, and the message is sent again with a
**"possible duplicate"** note. A timeout gets the same treatment. Delivery is therefore
at-least-once, with honest labeling. For payment notifications, a missed message is worse
than a marked duplicate. Under normal operation each (subscription, transfer) pair produces
exactly one message.

Errors are classified as follows:
- `Forbidden` (user blocked the bot): the user is flagged, the notification fails, and other users are not affected.
- `BadRequest`: permanent failure.
- `RetryAfter`: the notifier pauses claiming, and messages still in the current batch are
  rescheduled without using up an attempt.
- Network errors and timeouts: exponential backoff, up to `NOTIFY_MAX_ATTEMPTS`.

### Amounts
Raw integer amounts are stored in `NUMERIC(78,0)`, which is large enough for uint256. For
display and threshold comparisons they are converted to `Decimal` with
`scaleb(-decimals)`. Floats are never used. The threshold check is
`amount >= minimum`, so a transfer of exactly 0.5 USDT is reported.

### What counts as a qualifying transfer
A transfer qualifies only if all of the following are true:
- it comes from the allowlisted contract;
- its recipient is a watched wallet;
- its sender differs from its recipient (self-transfers are ignored);
- its receipt status is success (TRON checks receipts explicitly; EVM nodes drop logs of reverted transactions);
- its block is at or below the finalized head;
- its amount is at least the cryptocurrency's minimum.

### Tortoise ORM 1.1 workarounds
- `CreateModel` drops `Meta.constraints`. The initial migration therefore adds the CHECK
  and partial UNIQUE constraints with `AddConstraint`. A test verifies that the migrations
  and the models do not drift.
- Nested `select_related("a__b")` loses columns when hydrating objects. Nested relations
  are loaded with `prefetch_related` instead.
- `execute_query()` drops `RETURNING` rows for `INSERT` statements. The lease query uses
  `execute_query_dict()`.

### Deviations from the suggested structure
- The package lives in `src/usdtmon/` (a src layout with a named package) rather than
  directly in `src/`.
- `validate_address` is synchronous because it does no I/O.
- `get_incoming_transfers` takes a list of addresses, which enables batching.
- Added: `worker_lease` (worker coordination), `subscription.start_block` and `removed_at`,
  `telegram_user.is_blocked`, the notification columns `locked_until`,
  `delivery_uncertain`, `telegram_message_id` and `last_error`, the notification status
  `sending`, `network.token_standard` and `explorer_tx_url`, and `transfer.network_id`
  (part of the event identity).
