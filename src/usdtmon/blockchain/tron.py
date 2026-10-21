"""Tron adapter: TRC-20 Transfer logs via java-tron's Ethereum-compatible /jsonrpc.

Tron addresses are base58check-encoded ``0x41 || 20-byte address``. The JSON-RPC
interface (TronGrid ``/jsonrpc``) speaks the same 20 bytes in hex, so the EVM log
engine is reused with a base58 <-> hex codec.
"""

from __future__ import annotations

import hashlib

from usdtmon.blockchain.evm import EvmLogAdapter

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(_ALPHABET)}
TRON_PREFIX = 0x41


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = _ALPHABET[rem] + out
    pad = len(data) - len(data.lstrip(b"\0"))
    return "1" * pad + out


def b58decode(text: str) -> bytes:
    n = 0
    for c in text:
        if c not in _INDEX:
            raise ValueError(f"invalid base58 character {c!r}")
        n = n * 58 + _INDEX[c]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(text) - len(text.lstrip("1"))
    return b"\0" * pad + body


def _checksum(payload: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]


def tron_address_to_bytes(address: str) -> bytes:
    """Decode a base58check Tron address to its 21-byte payload (0x41 prefix)."""
    raw = b58decode(address)
    if len(raw) != 25:
        raise ValueError("Tron address must decode to 25 bytes")
    payload, checksum = raw[:21], raw[21:]
    if payload[0] != TRON_PREFIX:
        raise ValueError("Tron address must start with 0x41")
    if _checksum(payload) != checksum:
        raise ValueError("Tron address checksum mismatch")
    return payload


def bytes_to_tron_address(payload: bytes) -> str:
    return b58encode(payload + _checksum(payload))


def is_valid_tron_address(address: str) -> bool:
    if not isinstance(address, str) or len(address) != 34 or not address.startswith("T"):
        return False
    try:
        tron_address_to_bytes(address)
    except ValueError:
        return False
    return True


class TronAdapter(EvmLogAdapter):
    code = "tron"

    def validate_address(self, address: str) -> bool:
        return is_valid_tron_address(address.strip())

    def normalize_address(self, address: str) -> str:
        address = address.strip()
        if not is_valid_tron_address(address):
            raise ValueError(f"invalid Tron address: {address!r}")
        # base58check is case-sensitive and canonical.
        return address

    def to_hex20(self, normalized: str) -> str:
        return "0x" + tron_address_to_bytes(normalized)[1:].hex()

    def from_hex20(self, hex20: str) -> str:
        body = hex20.removeprefix("0x")
        # Some nodes return the 21-byte form with the 41 prefix.
        if len(body) == 42 and body[:2] == "41":
            body = body[2:]
        return bytes_to_tron_address(bytes([TRON_PREFIX]) + bytes.fromhex(body))

    def normalize_tx_hash(self, tx_hash: str) -> str:
        # Tron transaction IDs are shown without the 0x prefix (e.g. on Tronscan).
        return tx_hash.lower().removeprefix("0x")
