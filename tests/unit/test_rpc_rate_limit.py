"""Rate-limit handling and diagnostics of the JSON-RPC client."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from usdtmon import retry
from usdtmon.blockchain import rpc
from usdtmon.blockchain.base import BlockchainError
from usdtmon.blockchain.rpc import JsonRpcClient, TransientError, classify_rpc_error
from usdtmon.logs import JsonFormatter
from usdtmon.retry import RateLimiter

SUSPENDED = (
    "request credit rate exceeded the allowed_credit(100 credits/s), "
    "and the query server is suspended for 5 s."
)
LOG_QUERY = {
    "fromBlock": "0x64",
    "toBlock": "0xc7",
    "address": "0xabc",
    "topics": ["0x1", None, ["0xwallet1", "0xwallet2"]],
}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    # Both modules share the same time/asyncio modules: replace only their references.
    for module in (retry, rpc):
        monkeypatch.setattr(module, "time", fake)
        monkeypatch.setattr(
            module,
            "asyncio",
            type(
                "A", (), {"sleep": fake.sleep, "Lock": asyncio.Lock, "Semaphore": asyncio.Semaphore}
            ),
        )
    return fake


def ok() -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": []})


def make_client(clock, responses, **kwargs):
    queue = list(responses)
    sent: list[float] = []

    def handler(request):
        sent.append(clock.now)
        return queue.pop(0)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    kwargs.setdefault("rate_limit", 1000.0)
    kwargs.setdefault("backoff_base", 0.0)
    return JsonRpcClient("https://rpc.test/secret-path", client=http, **kwargs), sent


def record(caplog, event):
    (found,) = [r for r in caplog.records if r.getMessage() == event]
    return found


async def test_suspension_in_body_is_honored(clock):
    client, sent = make_client(clock, [httpx.Response(429, json={"Error": SUSPENDED}), ok()])
    assert await client.call("eth_getLogs", [LOG_QUERY]) == []
    assert sent[1] - sent[0] == pytest.approx(5.5)


@pytest.mark.parametrize(("header", "expected"), [("2", 5.5), ("9", 9.0)])
async def test_longest_advertised_wait_wins(clock, header, expected):
    limited = httpx.Response(429, headers={"Retry-After": header}, text=SUSPENDED)
    client, sent = make_client(clock, [limited, ok()])
    await client.call("eth_getLogs", [LOG_QUERY])
    assert sent[1] - sent[0] == pytest.approx(expected)


async def test_429_without_hint_holds_back_the_whole_provider(clock):
    client, _ = make_client(clock, [httpx.Response(429, text="slow down"), ok()], backoff_base=4)
    penalties = []
    original = client._limiter.penalize
    client._limiter.penalize = lambda s: (penalties.append(s), original(s))
    await client.call("eth_getLogs", [LOG_QUERY])
    assert len(penalties) == 1 and 2 <= penalties[0] <= 4


async def test_plain_server_error_does_not_hold_back_others(clock):
    client, _ = make_client(clock, [httpx.Response(503), ok()], backoff_base=4)
    client._limiter.penalize = lambda s: pytest.fail("must not penalize")
    await client.call("eth_getLogs", [LOG_QUERY])


async def test_very_long_suspension_fails_fast(clock, caplog):
    body = SUSPENDED.replace("5 s", "86400 s")
    client, sent = make_client(clock, [httpx.Response(429, text=body)])
    with caplog.at_level(logging.INFO), pytest.raises(BlockchainError, match="HTTP 429"):
        await client.call("eth_getLogs", [LOG_QUERY])
    assert len(sent) == 1 and clock.sleeps == []
    assert record(caplog, "provider_request_failed").reason == "retry_after_too_long"


def test_rpc_level_rate_limit_error_keeps_suspension():
    exc = classify_rpc_error(-32005, "rate limit: suspended for 3 seconds")
    assert isinstance(exc, TransientError)
    assert exc.rate_limited and exc.retry_after == 3.5


async def test_retry_log_has_debug_info_without_secrets(clock, caplog):
    limited = httpx.Response(
        429,
        headers={"X-RateLimit-Remaining": "0", "X-Other": "x"},
        text=f"{SUSPENDED} key=my-secret-key " + "z" * 2000,
    )
    with caplog.at_level(logging.INFO):
        client, _ = make_client(
            clock, [limited, ok()], network="tron", headers={"TRON-PRO-API-KEY": "my-secret-key"}
        )
        await client.call("eth_getLogs", [LOG_QUERY])
    entry = record(caplog, "provider_request_retry")
    assert entry.network == "tron"
    assert (getattr(entry, "from"), entry.to, entry.blocks, entry.wallets) == (100, 199, 100, 2)
    assert entry.status == 429 and entry.rate_limited and entry.retry_after == 5.5
    assert entry.auth_header is True and entry.requests_10s == 1
    assert entry.limit_headers == {"x-ratelimit-remaining": "0"}
    assert "allowed_credit(100 credits/s)" in entry.body and len(entry.body) == 500
    recovered = record(caplog, "provider_request_recovered")
    assert recovered.attempt == 2 and recovered.seconds == 5.5
    configured = record(caplog, "provider_configured")
    assert configured.host == "rpc.test" and configured.auth_header is True
    ours = [
        r for r in caplog.records if r.name.startswith("usdtmon")
    ]  # httpx is muted in production
    output = "\n".join(JsonFormatter().format(r) for r in ours)
    assert json.loads(output.splitlines()[0])
    for secret in ("my-secret-key", "secret-path", "0xwallet1"):
        assert secret not in output


async def test_exhausted_retries_are_logged(clock, caplog):
    responses = [httpx.Response(429, text=SUSPENDED) for _ in range(2)]
    client, _ = make_client(clock, responses, max_retries=1)
    with caplog.at_level(logging.INFO), pytest.raises(BlockchainError):
        await client.call("eth_getLogs", [LOG_QUERY])
    entry = record(caplog, "provider_request_failed")
    assert entry.reason == "retries_exhausted" and entry.attempt == 2
    assert entry.requests_10s == 2 and entry.auth_header is False


async def test_penalty_delays_calls_already_waiting():
    limiter = RateLimiter(50.0)
    await limiter.acquire()
    waiting = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0)  # the task is now sleeping for its 20 ms slot
    limiter.penalize(0.2)
    await asyncio.sleep(0.1)
    assert not waiting.done()
    await asyncio.wait_for(waiting, 2)
