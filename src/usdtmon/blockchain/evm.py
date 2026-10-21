"""Shared engine for chains exposing EVM-style logs via JSON-RPC ``eth_getLogs``.

Ethereum and Tron (java-tron's /jsonrpc) both emit the standard
``Transfer(address,address,uint256)`` event, so the scanning logic lives here and
each network adapter only supplies its address codec.

Fetching strategy: one ``eth_getLogs`` request per (address chunk, block chunk)
with the token contract as ``address`` and the watched wallets as an OR-list in
the third topic (recipient). Only the allowlisted contract is queried, and every
returned log is re-checked, so a misbehaving provider cannot inject logs from
other contracts or for other recipients.
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from collections.abc import Sequence
from datetime import UTC, datetime
from itertools import batched
from typing import Any

from usdtmon.blockchain.base import BlockchainAdapter, BlockchainError, Token, TransferEvent
from usdtmon.blockchain.rpc import JsonRpcClient, RangeTooLargeError, TransientError
from usdtmon.config import FinalityMode

log = logging.getLogger(__name__)

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def address_topic(hex20: str) -> str:
    """Left-pad a 20-byte hex address to a 32-byte topic."""
    return "0x" + "0" * 24 + hex20.removeprefix("0x").lower()


def topic_address(topic: str) -> str | None:
    """Extract a 20-byte address from an indexed topic, or None if malformed."""
    body = topic.removeprefix("0x").lower()
    if len(body) != 64 or body[:24] != "0" * 24:
        return None
    try:
        int(body, 16)
    except ValueError:
        return None
    return "0x" + body[24:]


def _hex_int(value: Any) -> int:
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        raise ValueError(f"expected hex quantity, got {value!r}")
    return int(value, 16)


class EvmLogAdapter(BlockchainAdapter):
    def __init__(
        self,
        rpc: JsonRpcClient,
        *,
        finality: FinalityMode = FinalityMode.FINALIZED,
        confirmations: int = 12,
        max_block_range: int = 500,
        max_addresses_per_query: int = 100,
        verify_receipts: bool = False,
    ) -> None:
        self.rpc = rpc
        self.finality = finality
        self.confirmations = confirmations
        self.max_block_range = max_block_range
        self.max_addresses_per_query = max_addresses_per_query
        # Logs of reverted transactions are discarded by EVM nodes, but checking
        # the receipt status is cheap (only for matching transfers) and guards
        # against nodes that behave differently.
        self.verify_receipts = verify_receipts
        self._finalized_head: int | None = None

    # --- address codec (network specific) ---------------------------------

    @abstractmethod
    def to_hex20(self, normalized: str) -> str:
        """Normalized address -> "0x" + 40 lowercase hex chars."""

    @abstractmethod
    def from_hex20(self, hex20: str) -> str:
        """ "0x" + 40 hex chars -> normalized address."""

    def normalize_tx_hash(self, tx_hash: str) -> str:
        return tx_hash.lower()

    # --- BlockchainAdapter --------------------------------------------------

    async def get_latest_finalized_block(self) -> int:
        if self.finality is FinalityMode.FINALIZED:
            block = await self.rpc.call("eth_getBlockByNumber", ["finalized", False])
            if not block or "number" not in block:
                raise BlockchainError(f"{self.code}: provider returned no finalized block")
            head = _hex_int(block["number"])
        else:
            latest = _hex_int(await self.rpc.call("eth_blockNumber", []))
            head = max(0, latest - self.confirmations)
        if self._finalized_head is not None and head < self._finalized_head:
            # A lagging/load-balanced provider node; never move backwards.
            log.warning(
                "finalized_head_regressed",
                extra={"network": self.code, "head": head, "previous": self._finalized_head},
            )
            return self._finalized_head
        self._finalized_head = head
        return head

    async def get_incoming_transfers(
        self,
        addresses: Sequence[str],
        token: Token,
        from_block: int,
        to_block: int,
    ) -> list[TransferEvent]:
        if from_block > to_block or not addresses:
            return []
        contract_hex = self.to_hex20(token.contract_address)
        wanted = {self.to_hex20(a) for a in addresses}

        raw_logs: list[dict[str, Any]] = []
        for chunk in batched(sorted(wanted), self.max_addresses_per_query):
            topics = [TRANSFER_TOPIC, None, [address_topic(a) for a in chunk]]
            raw_logs.extend(await self._get_logs(contract_hex, topics, from_block, to_block))

        parsed = [
            p
            for entry in raw_logs
            if (p := self._parse_log(entry, contract_hex, wanted, from_block, to_block)) is not None
        ]
        # The same log can only come back once per address chunk, but be safe.
        unique = {(p["tx"], p["index"]): p for p in parsed}
        if not unique:
            return []

        blocks = await self._block_info(unique.values())
        success = await self._receipt_status({p["tx"] for p in unique.values()})

        events: list[TransferEvent] = []
        for p in unique.values():
            block_hash, timestamp = blocks[p["block"]]
            if p["block_hash"] and block_hash and p["block_hash"] != block_hash:
                # The log belongs to a block that is no longer canonical: a reorg
                # deeper than the confirmation depth. Do not commit this range.
                raise TransientError(
                    f"{self.code}: block {p['block']} hash changed (reorg); retry later"
                )
            head = self._finalized_head
            events.append(
                TransferEvent(
                    network=self.code,
                    symbol=token.symbol,
                    contract_address=token.contract_address,
                    transaction_hash=self.normalize_tx_hash(p["tx"]),
                    event_index=p["index"],
                    from_address=self.from_hex20(p["from"]),
                    to_address=self.from_hex20(p["to"]),
                    amount_raw=p["amount"],
                    decimals=token.decimals,
                    block_number=p["block"],
                    block_timestamp=timestamp,
                    confirmed=head is not None and p["block"] <= head,
                    success=success.get(p["tx"], True),
                )
            )
        events.sort(key=lambda e: (e.block_number, e.event_index))
        return events

    async def aclose(self) -> None:
        await self.rpc.aclose()

    # --- internals ------------------------------------------------------------

    async def _get_logs(
        self, contract_hex: str, topics: list[Any], from_block: int, to_block: int
    ) -> list[dict[str, Any]]:
        """Fetch logs for the whole range, splitting it into provider-sized chunks.

        The chunk size shrinks when the provider rejects a range as too large and
        is kept for the rest of the range.
        """
        out: list[dict[str, Any]] = []
        span = self.max_block_range
        start = from_block
        while start <= to_block:
            end = min(to_block, start + span - 1)
            params = {
                "fromBlock": hex(start),
                "toBlock": hex(end),
                "address": contract_hex,
                "topics": topics,
            }
            try:
                logs = await self.rpc.call("eth_getLogs", [params])
            except RangeTooLargeError:
                if end == start:
                    raise BlockchainError(
                        f"{self.code}: provider rejects eth_getLogs even for a single block"
                    ) from None
                span = max(1, (end - start + 1) // 2)
                log.info(
                    "logs_range_reduced", extra={"network": self.code, "span": span, "from": start}
                )
                continue
            if not isinstance(logs, list):
                raise TransientError(f"{self.code}: eth_getLogs returned {type(logs).__name__}")
            out.extend(logs)
            start = end + 1
        return out

    def _parse_log(
        self,
        entry: dict[str, Any],
        contract_hex: str,
        wanted: set[str],
        from_block: int,
        to_block: int,
    ) -> dict[str, Any] | None:
        try:
            if entry.get("removed"):
                return None
            if str(entry.get("address", "")).lower() != contract_hex:
                log.warning(
                    "log_from_unexpected_contract",
                    extra={"network": self.code, "address": entry.get("address")},
                )
                return None
            topics = entry.get("topics") or []
            if len(topics) != 3 or str(topics[0]).lower() != TRANSFER_TOPIC:
                return None
            sender = topic_address(topics[1])
            recipient = topic_address(topics[2])
            if sender is None or recipient is None or recipient not in wanted:
                return None
            data = str(entry.get("data", "")).removeprefix("0x")
            if len(data) != 64:
                return None
            amount = int(data, 16)
            block = _hex_int(entry["blockNumber"])
            if not from_block <= block <= to_block:
                return None
            ts = entry.get("blockTimestamp")
            return {
                "tx": str(entry["transactionHash"]).lower(),
                "index": _hex_int(entry["logIndex"]),
                "block": block,
                "block_hash": str(entry.get("blockHash") or "").lower() or None,
                "timestamp": _hex_int(ts) if ts is not None else None,
                "from": sender,
                "to": recipient,
                "amount": amount,
            }
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("malformed_log_skipped", extra={"network": self.code, "error": str(exc)})
            return None

    async def _block_info(self, parsed: Any) -> dict[int, tuple[str | None, datetime]]:
        """Block hash and timestamp per block number of the parsed logs.

        In confirmations mode every block is fetched so its canonical hash can be
        compared with the log's block hash (reorg protection). In finalized mode a
        block is only fetched if the provider did not include ``blockTimestamp``.
        """
        result: dict[int, tuple[str | None, datetime]] = {}
        to_fetch: set[int] = set()
        for p in parsed:
            if self.finality is FinalityMode.CONFIRMATIONS or p["timestamp"] is None:
                to_fetch.add(p["block"])
            else:
                result[p["block"]] = (None, datetime.fromtimestamp(p["timestamp"], UTC))

        async def fetch(number: int) -> None:
            block = await self.rpc.call("eth_getBlockByNumber", [hex(number), False])
            if not block:
                raise TransientError(f"{self.code}: block {number} not available")
            result[number] = (
                str(block.get("hash") or "").lower() or None,
                datetime.fromtimestamp(_hex_int(block["timestamp"]), UTC),
            )

        await asyncio.gather(*(fetch(n) for n in sorted(to_fetch)))
        return result

    async def _receipt_status(self, tx_hashes: set[str]) -> dict[str, bool]:
        if not self.verify_receipts:
            return {}
        result: dict[str, bool] = {}

        async def fetch(tx: str) -> None:
            receipt = await self.rpc.call("eth_getTransactionReceipt", [tx])
            if not receipt:
                raise TransientError(f"{self.code}: receipt for {tx} not available")
            result[tx] = _hex_int(receipt.get("status", "0x0")) == 1

        await asyncio.gather(*(fetch(tx) for tx in sorted(tx_hashes)))
        return result
