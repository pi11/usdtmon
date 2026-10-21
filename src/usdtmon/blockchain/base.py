"""Network-agnostic blockchain adapter interface and transfer representation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


class BlockchainError(Exception):
    """A provider request failed (after retries) or returned unusable data."""


@dataclass(frozen=True)
class Token:
    """A verified token contract on one network (an allowlist entry)."""

    symbol: str
    # Normalized contract address (see BlockchainAdapter.normalize_address).
    contract_address: str
    decimals: int


@dataclass(frozen=True)
class TransferEvent:
    """A token transfer normalized from a chain-specific log/event."""

    network: str
    symbol: str
    contract_address: str
    transaction_hash: str
    # Position of the event in its block/transaction; with the hash it forms
    # the event identity.
    event_index: int
    from_address: str
    to_address: str
    # Smallest denomination; never a float.
    amount_raw: int
    decimals: int
    block_number: int
    block_timestamp: datetime
    # The block is at or below the network's finality point.
    confirmed: bool
    # The transaction executed successfully.
    success: bool = True

    @property
    def amount(self) -> Decimal:
        return Decimal(self.amount_raw).scaleb(-self.decimals)


class BlockchainAdapter(ABC):
    """One implementation per network. Adding a network = one new adapter.

    Differences from the conceptual interface in the spec:
    - ``validate_address``/``normalize_address`` are synchronous (no I/O).
    - ``get_incoming_transfers`` takes many addresses so one provider query
      covers every watched wallet instead of one query per wallet.
    """

    code: str

    @abstractmethod
    def validate_address(self, address: str) -> bool: ...

    @abstractmethod
    def normalize_address(self, address: str) -> str:
        """Canonical form used for storage and comparison. Address must be valid."""

    def display_address(self, normalized: str) -> str:
        return normalized

    @abstractmethod
    async def get_latest_finalized_block(self) -> int:
        """Highest block that is safe from reorganizations under the configured finality."""

    @abstractmethod
    async def get_incoming_transfers(
        self,
        addresses: Sequence[str],
        token: Token,
        from_block: int,
        to_block: int,
    ) -> list[TransferEvent]:
        """All transfers of ``token`` to any of ``addresses`` in [from_block, to_block].

        Must return complete results for the whole range or raise
        :class:`BlockchainError`; partial results would create gaps.
        """

    def explorer_tx_url(self, template: str, tx_hash: str) -> str:
        return template.format(tx=tx_hash)

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        """Release network resources."""
