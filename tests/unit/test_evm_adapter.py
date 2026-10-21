"""Ethereum/Tron adapters against a mocked JSON-RPC endpoint."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
import respx

from tests.fixtures.chain import (
    ALICE_ETH,
    BOB_ETH,
    CAROL_ETH,
    FAKE_USDT_ETH,
    USDT_ETH,
    USDT_TRON,
    log_entry,
)
from usdtmon.blockchain.base import BlockchainError, Token
from usdtmon.blockchain.ethereum import EthereumAdapter
from usdtmon.blockchain.evm import TRANSFER_TOPIC, address_topic
from usdtmon.blockchain.rpc import JsonRpcClient
from usdtmon.blockchain.tron import TronAdapter
from usdtmon.config import FinalityMode

URL = "https://rpc.test/"
TOKEN = Token("USDT", USDT_ETH, 6)


class FakeNode:
    """Dispatches JSON-RPC methods to handlers; records every request."""

    def __init__(self) -> None:
        self.handlers: dict[str, Callable[[list], object]] = {}
        self.requests: list[dict] = []

    def on(self, method: str, handler: Callable[[list], object]) -> None:
        self.handlers[method] = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        result = self.handlers[body["method"]](body["params"])
        if isinstance(result, httpx.Response):
            return result
        if isinstance(result, dict) and "__error__" in result:
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": body["id"], "error": result["__error__"]}
            )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    def calls(self, method: str) -> list[dict]:
        return [r for r in self.requests if r["method"] == method]


def rpc_client(**kwargs) -> JsonRpcClient:
    kwargs.setdefault("backoff_base", 0.0)
    kwargs.setdefault("rate_limit", 1000.0)
    return JsonRpcClient(URL, **kwargs)


@pytest.fixture
def node():
    fake = FakeNode()
    with respx.mock(assert_all_called=False) as mock:
        mock.post(URL).mock(side_effect=fake)
        yield fake


def filter_logs(logs: list[dict]) -> Callable[[list], list[dict]]:
    """Emulate a node: apply the contract/topic/block filters to ``logs``."""

    def handler(params: list) -> list[dict]:
        f = params[0]
        lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
        recipients = set(f["topics"][2])
        return [
            entry
            for entry in logs
            if entry["address"] == f["address"]
            and lo <= int(entry["blockNumber"], 16) <= hi
            and entry["topics"][2] in recipients
        ]

    return handler


async def test_finalized_block_uses_finalized_tag(node):
    node.on(
        "eth_getBlockByNumber", lambda p: {"number": hex(1234)} if p[0] == "finalized" else None
    )
    adapter = EthereumAdapter(rpc_client())
    assert await adapter.get_latest_finalized_block() == 1234


async def test_finalized_block_never_regresses(node):
    heads = iter([200, 150])
    node.on("eth_getBlockByNumber", lambda p: {"number": hex(next(heads))})
    adapter = EthereumAdapter(rpc_client())
    assert await adapter.get_latest_finalized_block() == 200
    assert await adapter.get_latest_finalized_block() == 200


async def test_confirmations_mode_subtracts_depth(node):
    node.on("eth_blockNumber", lambda p: hex(1000))
    adapter = EthereumAdapter(rpc_client(), finality=FinalityMode.CONFIRMATIONS, confirmations=12)
    assert await adapter.get_latest_finalized_block() == 988


async def test_incoming_transfers_parsed_and_filtered(node):
    logs = [
        log_entry(recipient=ALICE_ETH, amount=1_500_000, block=101, index=3),
        log_entry(recipient=CAROL_ETH, amount=2_000_000, block=102, tx="0x" + "cd" * 32),
        # A fake USDT contract with the same event signature.
        log_entry(contract=FAKE_USDT_ETH, recipient=ALICE_ETH, amount=9_000_000, block=101),
        # Outgoing from Alice.
        log_entry(sender=ALICE_ETH, recipient=BOB_ETH, block=101, index=5),
    ]
    node.on("eth_getBlockByNumber", lambda p: {"number": hex(200)})
    node.on("eth_getLogs", filter_logs(logs))
    adapter = EthereumAdapter(rpc_client())
    await adapter.get_latest_finalized_block()
    events = await adapter.get_incoming_transfers([ALICE_ETH, CAROL_ETH], TOKEN, 100, 200)

    assert [(e.to_address, e.amount_raw, e.event_index) for e in events] == [
        (ALICE_ETH, 1_500_000, 3),
        (CAROL_ETH, 2_000_000, 0),
    ]
    first = events[0]
    assert first.from_address == BOB_ETH
    assert first.contract_address == USDT_ETH
    assert first.confirmed and first.success
    assert first.block_timestamp.timestamp() == 1_700_000_000

    # One query for both wallets: recipients as an OR-list in topic 2.
    (call,) = node.calls("eth_getLogs")
    f = call["params"][0]
    assert f["address"] == USDT_ETH
    assert f["topics"][0] == TRANSFER_TOPIC and f["topics"][1] is None
    assert set(f["topics"][2]) == {address_topic(ALICE_ETH), address_topic(CAROL_ETH)}


async def test_provider_returning_foreign_logs_is_not_trusted(node):
    """Even if a provider ignores the filter, fake contracts/recipients are dropped."""
    node.on(
        "eth_getLogs",
        lambda p: [
            log_entry(contract=FAKE_USDT_ETH, recipient=ALICE_ETH),
            log_entry(recipient=BOB_ETH),
            log_entry(recipient=ALICE_ETH, block=999),  # outside the requested range
            log_entry(recipient=ALICE_ETH, removed=True),
            {**log_entry(recipient=ALICE_ETH), "data": "0xzz"},
            {**log_entry(recipient=ALICE_ETH), "topics": [TRANSFER_TOPIC]},
        ],
    )
    adapter = EthereumAdapter(rpc_client())
    assert await adapter.get_incoming_transfers([ALICE_ETH], TOKEN, 100, 200) == []


async def test_multiple_transfers_in_one_transaction(node):
    tx = "0x" + "ee" * 32
    logs = [log_entry(tx=tx, index=i, amount=(i + 1) * 1_000_000) for i in range(3)]
    node.on("eth_getLogs", filter_logs(logs))
    adapter = EthereumAdapter(rpc_client())
    events = await adapter.get_incoming_transfers([ALICE_ETH], TOKEN, 100, 100)
    assert [(e.transaction_hash, e.event_index, e.amount_raw) for e in events] == [
        (tx, 0, 1_000_000),
        (tx, 1, 2_000_000),
        (tx, 2, 3_000_000),
    ]


async def test_block_range_is_chunked(node):
    logs = [log_entry(block=b, index=0, tx="0x" + f"{b:064x}") for b in (100, 349, 350, 600)]
    node.on("eth_getLogs", filter_logs(logs))
    adapter = EthereumAdapter(rpc_client(), max_block_range=250)
    events = await adapter.get_incoming_transfers([ALICE_ETH], TOKEN, 100, 600)
    assert [e.block_number for e in events] == [100, 349, 350, 600]
    ranges = [
        (int(c["params"][0]["fromBlock"], 16), int(c["params"][0]["toBlock"], 16))
        for c in node.calls("eth_getLogs")
    ]
    assert ranges == [(100, 349), (350, 599), (600, 600)]


async def test_range_too_large_is_split(node):
    logs = [log_entry(block=b, tx="0x" + f"{b:064x}") for b in (100, 160, 199)]
    inner = filter_logs(logs)

    def handler(params):
        f = params[0]
        if int(f["toBlock"], 16) - int(f["fromBlock"], 16) + 1 > 25:
            return {
                "__error__": {"code": -32005, "message": "query returned more than 10000 results"}
            }
        return inner(params)

    node.on("eth_getLogs", handler)
    adapter = EthereumAdapter(rpc_client(), max_block_range=100)
    events = await adapter.get_incoming_transfers([ALICE_ETH], TOKEN, 100, 199)
    assert [e.block_number for e in events] == [100, 160, 199]
    covered = sorted(
        (int(c["params"][0]["fromBlock"], 16), int(c["params"][0]["toBlock"], 16))
        for c in node.calls("eth_getLogs")
        if "error" not in c
    )
    assert covered[0][0] == 100 and max(hi for _, hi in covered) == 199


async def test_addresses_are_chunked(node):
    wallets = ["0x" + f"{i:040x}" for i in range(1, 6)]
    node.on("eth_getLogs", lambda p: [])
    adapter = EthereumAdapter(rpc_client(), max_addresses_per_query=2)
    await adapter.get_incoming_transfers(wallets, TOKEN, 1, 1)
    sizes = [len(c["params"][0]["topics"][2]) for c in node.calls("eth_getLogs")]
    assert sizes == [2, 2, 1]


async def test_transient_failures_are_retried(node):
    attempts = {"n": 0}

    def flaky(params):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(503)
        if attempts["n"] == 2:
            return httpx.Response(429, headers={"Retry-After": "0"})
        if attempts["n"] == 3:
            return {"__error__": {"code": -32603, "message": "internal error"}}
        return [log_entry()]

    node.on("eth_getLogs", flaky)
    adapter = EthereumAdapter(rpc_client(max_retries=5))
    events = await adapter.get_incoming_transfers([ALICE_ETH], TOKEN, 100, 100)
    assert len(events) == 1 and attempts["n"] == 4


async def test_persistent_failure_raises(node):
    node.on("eth_getLogs", lambda p: httpx.Response(502))
    adapter = EthereumAdapter(rpc_client(max_retries=2))
    with pytest.raises(BlockchainError):
        await adapter.get_incoming_transfers([ALICE_ETH], TOKEN, 100, 100)
    assert len(node.calls("eth_getLogs")) == 3


async def test_network_errors_are_retried():
    with respx.mock() as mock:
        route = mock.post(URL)
        route.side_effect = [
            httpx.ConnectError("boom"),
            httpx.ReadTimeout("slow"),
            httpx.Response(200, json={"jsonrpc": "2.0", "id": 3, "result": hex(5)}),
        ]
        client = rpc_client(max_retries=3)
        assert await client.call("eth_blockNumber", []) == "0x5"


async def test_auth_errors_are_not_retried(node):
    node.on("eth_blockNumber", lambda p: httpx.Response(401, text="bad key"))
    client = rpc_client(max_retries=5)
    with pytest.raises(BlockchainError):
        await client.call("eth_blockNumber", [])
    assert len(node.requests) == 1


async def test_missing_block_timestamp_fetches_block(node):
    node.on("eth_getLogs", lambda p: [log_entry(timestamp=None, block=150)])
    node.on(
        "eth_getBlockByNumber",
        lambda p: {"number": p[0], "hash": "0x" + f"{150:064x}", "timestamp": hex(1_700_000_150)},
    )
    adapter = EthereumAdapter(rpc_client())
    (e,) = await adapter.get_incoming_transfers([ALICE_ETH], TOKEN, 100, 200)
    assert e.block_timestamp.timestamp() == 1_700_000_150


async def test_confirmations_mode_detects_reorged_log(node):
    """A log whose block hash is no longer canonical aborts the range (no commit)."""
    node.on("eth_getLogs", lambda p: [log_entry(block=150, block_hash="0x" + "99" * 32)])
    node.on(
        "eth_getBlockByNumber",
        lambda p: {"number": p[0], "hash": "0x" + "11" * 32, "timestamp": hex(1)},
    )
    adapter = EthereumAdapter(rpc_client(max_retries=0), finality=FinalityMode.CONFIRMATIONS)
    with pytest.raises(BlockchainError, match="reorg"):
        await adapter.get_incoming_transfers([ALICE_ETH], TOKEN, 100, 200)


async def test_confirmed_flag_reflects_finalized_head(node):
    node.on("eth_getBlockByNumber", lambda p: {"number": hex(120)})
    node.on("eth_getLogs", lambda p: [log_entry(block=110), log_entry(block=130, index=1)])
    adapter = EthereumAdapter(rpc_client())
    await adapter.get_latest_finalized_block()
    events = await adapter.get_incoming_transfers([ALICE_ETH], TOKEN, 100, 200)
    assert [(e.block_number, e.confirmed) for e in events] == [(110, True), (130, False)]


async def test_new_erc20_token_without_adapter_changes(node):
    """Any allowlisted ERC-20 is scanned by the same adapter, with its own decimals."""
    usdc = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    node.on("eth_getLogs", filter_logs([log_entry(contract=usdc, amount=2_500_000)]))
    adapter = EthereumAdapter(rpc_client())
    (e,) = await adapter.get_incoming_transfers([ALICE_ETH], Token("USDC", usdc, 6), 100, 100)
    assert e.symbol == "USDC" and e.contract_address == usdc and str(e.amount) == "2.500000"


# --- Tron -----------------------------------------------------------------

TRON_TOKEN = Token("USDT", USDT_TRON, 6)
TRON_WALLET = "TLa2f6VPqDgRE67v1736s7bJ8Ray5wYjU7"


async def test_tron_transfers_use_hex_addresses_and_check_receipts(node):
    adapter = TronAdapter(rpc_client(), verify_receipts=True)
    contract_hex = adapter.to_hex20(USDT_TRON)
    wallet_hex = adapter.to_hex20(TRON_WALLET)
    sender_hex = "0x" + "77" * 20
    ok_tx, failed_tx = "0x" + "01" * 32, "0x" + "02" * 32
    logs = [
        log_entry(
            contract=contract_hex,
            sender=sender_hex,
            recipient=wallet_hex,
            tx=ok_tx,
            amount=7_000_000,
        ),
        log_entry(
            contract=contract_hex, sender=sender_hex, recipient=wallet_hex, tx=failed_tx, index=1
        ),
    ]
    node.on("eth_getLogs", filter_logs(logs))
    node.on("eth_getTransactionReceipt", lambda p: {"status": "0x1" if p[0] == ok_tx else "0x0"})
    events = await adapter.get_incoming_transfers([TRON_WALLET], TRON_TOKEN, 100, 100)

    assert [(e.transaction_hash, e.success) for e in events] == [
        ("01" * 32, True),
        ("02" * 32, False),
    ]
    assert events[0].to_address == TRON_WALLET
    assert events[0].from_address.startswith("T")
    assert events[0].contract_address == USDT_TRON
    f = node.calls("eth_getLogs")[0]["params"][0]
    assert f["address"] == contract_hex
