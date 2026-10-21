"""Builders for fake JSON-RPC payloads and a scriptable in-memory adapter."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from usdtmon.blockchain.base import BlockchainAdapter, BlockchainError, Token, TransferEvent
from usdtmon.blockchain.evm import TRANSFER_TOPIC, address_topic

USDT_ETH = "0xdac17f958d2ee523a2206206994597c13d831ec7"
FAKE_USDT_ETH = "0x1111111111111111111111111111111111111111"
USDT_TRON = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

ALICE_ETH = "0x" + "a1" * 20
BOB_ETH = "0x" + "b2" * 20
CAROL_ETH = "0x" + "c3" * 20


def log_entry(
    *,
    contract: str = USDT_ETH,
    sender: str = BOB_ETH,
    recipient: str = ALICE_ETH,
    amount: int = 1_000_000,
    block: int = 100,
    tx: str = "0x" + "ab" * 32,
    index: int = 0,
    block_hash: str | None = None,
    timestamp: int | None = 1_700_000_000,
    removed: bool = False,
) -> dict:
    entry = {
        "address": contract,
        "topics": [TRANSFER_TOPIC, address_topic(sender), address_topic(recipient)],
        "data": "0x" + amount.to_bytes(32, "big").hex(),
        "blockNumber": hex(block),
        "blockHash": block_hash or "0x" + f"{block:064x}",
        "transactionHash": tx,
        "transactionIndex": "0x0",
        "logIndex": hex(index),
        "removed": removed,
    }
    if timestamp is not None:
        entry["blockTimestamp"] = hex(timestamp)
    return entry


def event(
    *,
    network: str = "ethereum",
    contract: str = USDT_ETH,
    sender: str = BOB_ETH,
    recipient: str = ALICE_ETH,
    amount: int = 1_000_000,
    block: int = 100,
    tx: str = "0x" + "ab" * 32,
    index: int = 0,
    decimals: int = 6,
    confirmed: bool = True,
    success: bool = True,
) -> TransferEvent:
    return TransferEvent(
        network=network,
        symbol="USDT",
        contract_address=contract,
        transaction_hash=tx,
        event_index=index,
        from_address=sender,
        to_address=recipient,
        amount_raw=amount,
        decimals=decimals,
        block_number=block,
        block_timestamp=datetime.fromtimestamp(1_700_000_000 + block, UTC),
        confirmed=confirmed,
        success=success,
    )


class FakeAdapter(BlockchainAdapter):
    """In-memory chain: set ``head`` and add events; can be told to fail."""

    def __init__(self, code: str = "ethereum") -> None:
        self.code = code
        self.head = 100
        self.events: list[TransferEvent] = []
        self.fail_next = 0
        self.calls: list[tuple[tuple[str, ...], int, int]] = []

    def validate_address(self, address: str) -> bool:
        return address.startswith("0x") and len(address) == 42

    def normalize_address(self, address: str) -> str:
        return address.lower()

    async def get_latest_finalized_block(self) -> int:
        return self.head

    async def get_incoming_transfers(
        self, addresses: Sequence[str], token: Token, from_block: int, to_block: int
    ) -> list[TransferEvent]:
        self.calls.append((tuple(sorted(addresses)), from_block, to_block))
        if self.fail_next:
            self.fail_next -= 1
            raise BlockchainError("provider down")
        wanted = set(addresses)
        return [
            e
            for e in self.events
            if e.to_address in wanted
            and from_block <= e.block_number <= to_block
            and e.contract_address == token.contract_address
        ]
