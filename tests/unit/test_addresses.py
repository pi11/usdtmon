import pytest

from usdtmon.blockchain.ethereum import EthereumAdapter, is_valid_eth_address, to_checksum_address
from usdtmon.blockchain.rpc import JsonRpcClient
from usdtmon.blockchain.tron import (
    TronAdapter,
    b58decode,
    b58encode,
    bytes_to_tron_address,
    is_valid_tron_address,
)

USDT_ETH_CHECKSUM = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
USDT_TRON = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


@pytest.mark.parametrize(
    "address",
    [
        USDT_ETH_CHECKSUM,
        USDT_ETH_CHECKSUM.lower(),
        "0x" + USDT_ETH_CHECKSUM[2:].upper(),
        "0x52908400098527886E0F7030069857D2E4169EE7",  # EIP-55 test vector (all caps)
        "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",  # EIP-55 test vector
    ],
)
def test_valid_ethereum_addresses(address):
    assert is_valid_eth_address(address)


@pytest.mark.parametrize(
    "address",
    [
        "",
        "0x",
        "dac17f958d2ee523a2206206994597c13d831ec7",  # missing 0x
        "0xdac17f958d2ee523a2206206994597c13d831ec",  # 39 hex chars
        "0xdac17f958d2ee523a2206206994597c13d831ec7a",  # 41 hex chars
        "0xgac17f958d2ee523a2206206994597c13d831ec7",  # non-hex
        "0xdAC17F958D2ee523a2206206994597C13D831eC7",  # bad checksum (one case flipped)
        USDT_TRON,
    ],
)
def test_invalid_ethereum_addresses(address):
    assert not is_valid_eth_address(address)


def test_checksum_encoding():
    assert to_checksum_address(USDT_ETH_CHECKSUM.lower()) == USDT_ETH_CHECKSUM


@pytest.mark.parametrize(
    "address",
    [USDT_TRON, "TLa2f6VPqDgRE67v1736s7bJ8Ray5wYjU7", "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"],
)
def test_valid_tron_addresses(address):
    assert is_valid_tron_address(address)


@pytest.mark.parametrize(
    "address",
    [
        "",
        "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6u",  # checksum mismatch
        "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6",  # too short
        "tr7nhqjekqxgtci8q8zy4pl8otszgjlj6t",  # case matters in base58
        "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj0t",  # '0' is not base58
        "1BoatSLRHtKNngkdXEeobR76b53LETtpyT",  # bitcoin address (wrong prefix)
        USDT_ETH_CHECKSUM,
    ],
)
def test_invalid_tron_addresses(address):
    assert not is_valid_tron_address(address)


def test_tron_wrong_version_byte_rejected():
    payload = bytes([0x42]) + bytes(20)
    assert not is_valid_tron_address(bytes_to_tron_address(payload))


def test_base58_roundtrip():
    for data in (b"", b"\0\0abc", bytes(range(30))):
        assert b58decode(b58encode(data)) == data


def _rpc():
    return JsonRpcClient("http://rpc.invalid")


def test_ethereum_normalization_and_display():
    adapter = EthereumAdapter(_rpc())
    assert adapter.normalize_address(f"  {USDT_ETH_CHECKSUM} ") == USDT_ETH_CHECKSUM.lower()
    assert adapter.display_address(USDT_ETH_CHECKSUM.lower()) == USDT_ETH_CHECKSUM
    with pytest.raises(ValueError):
        adapter.normalize_address("0x123")


def test_tron_hex_codec_roundtrip():
    adapter = TronAdapter(_rpc())
    hex20 = adapter.to_hex20(USDT_TRON)
    assert hex20 == "0xa614f803b6fd780986a42c78ec9c7f77e6ded13c"
    assert adapter.from_hex20(hex20) == USDT_TRON
    # 21-byte form with the 41 prefix is accepted too.
    assert adapter.from_hex20("0x41a614f803b6fd780986a42c78ec9c7f77e6ded13c") == USDT_TRON
    assert adapter.normalize_tx_hash("0xABCD") == "abcd"
