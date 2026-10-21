"""Ethereum adapter: ERC-20 Transfer logs over standard JSON-RPC."""

from __future__ import annotations

import re

from Crypto.Hash import keccak

from usdtmon.blockchain.evm import EvmLogAdapter

_HEX_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _keccak_hex(data: bytes) -> str:
    return keccak.new(digest_bits=256, data=data).hexdigest()


def to_checksum_address(address: str) -> str:
    """EIP-55 mixed-case checksum encoding."""
    body = address.removeprefix("0x").lower()
    digest = _keccak_hex(body.encode("ascii"))
    return "0x" + "".join(c.upper() if int(digest[i], 16) >= 8 else c for i, c in enumerate(body))


def is_valid_eth_address(address: str) -> bool:
    if not isinstance(address, str) or not _HEX_ADDRESS.match(address):
        return False
    body = address[2:]
    # All-lowercase or all-uppercase addresses carry no checksum.
    if body == body.lower() or body == body.upper():
        return True
    return address == to_checksum_address(address)


class EthereumAdapter(EvmLogAdapter):
    code = "ethereum"

    def validate_address(self, address: str) -> bool:
        return is_valid_eth_address(address.strip())

    def normalize_address(self, address: str) -> str:
        address = address.strip()
        if not is_valid_eth_address(address):
            raise ValueError(f"invalid Ethereum address: {address!r}")
        return address.lower()

    def display_address(self, normalized: str) -> str:
        return to_checksum_address(normalized)

    def to_hex20(self, normalized: str) -> str:
        return normalized.lower()

    def from_hex20(self, hex20: str) -> str:
        return hex20.lower()
