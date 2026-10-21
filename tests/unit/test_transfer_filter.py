from decimal import Decimal

import pytest

from tests.fixtures.chain import ALICE_ETH, BOB_ETH, FAKE_USDT_ETH, USDT_ETH, event
from usdtmon.blockchain.base import Token
from usdtmon.services.transfers import (
    format_amount,
    rejection_reason,
    select_qualifying,
    shorten_address,
)

TOKEN = Token("USDT", USDT_ETH, 6)
WATCHED = {ALICE_ETH}
MIN = Decimal("0.5")


def reason(**kwargs):
    return rejection_reason(event(**kwargs), TOKEN, WATCHED, MIN)


def test_legitimate_incoming_transfer_qualifies():
    assert reason() is None


def test_fake_usdt_contract_rejected():
    assert reason(contract=FAKE_USDT_ETH) == "unrecognized_contract"


def test_outgoing_transfer_rejected():
    assert reason(sender=ALICE_ETH, recipient=BOB_ETH) == "not_incoming"


def test_self_transfer_rejected():
    assert reason(sender=ALICE_ETH, recipient=ALICE_ETH) == "self_transfer"


def test_failed_transaction_rejected():
    assert reason(success=False) == "failed_transaction"


def test_unconfirmed_rejected():
    assert reason(confirmed=False) == "not_finalized"


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (499_999, "below_minimum"),
        (1, "below_minimum"),
        (0, "below_minimum"),
        (500_000, None),  # exactly 0.5 USDT
        (500_001, None),
    ],
)
def test_threshold(amount, expected):
    assert reason(amount=amount) == expected


def test_threshold_uses_token_decimals():
    # 0.5 of an 18-decimals token
    token = Token("DAI", USDT_ETH, 18)
    ok = event(amount=5 * 10**17, decimals=18)
    low = event(amount=5 * 10**17 - 1, decimals=18)
    assert rejection_reason(ok, token, WATCHED, MIN) is None
    assert rejection_reason(low, token, WATCHED, MIN) == "below_minimum"


def test_multiple_transfers_in_one_transaction_are_kept_separately():
    events = [
        event(index=0, amount=1_000_000),
        event(index=1, amount=200_000),  # below threshold
        event(index=2, amount=3_000_000),
        event(index=3, contract=FAKE_USDT_ETH, amount=9_000_000),
    ]
    kept = select_qualifying(events, TOKEN, WATCHED, MIN)
    assert [e.event_index for e in kept] == [0, 2]


def test_amount_is_exact_decimal():
    e = event(amount=123_456_789_012_345_678_901)
    assert e.amount == Decimal("123456789012345.678901")


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("125.5", "125.50"),
        ("0.5", "0.50"),
        ("1", "1.00"),
        ("0.123456", "0.123456"),
        ("1234567.890001", "1,234,567.890001"),
        ("1E+3", "1,000.00"),
    ],
)
def test_format_amount(value, text):
    assert format_amount(Decimal(value)) == text


def test_shorten_address():
    assert shorten_address("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t") == "TR7NH...jLj6t"
    assert shorten_address("short") == "short"
