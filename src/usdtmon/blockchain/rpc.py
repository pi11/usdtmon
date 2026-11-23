"""JSON-RPC over HTTP with timeouts, bounded concurrency, rate limiting and retries."""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
import time
from collections import deque
from typing import Any
from urllib.parse import urlsplit

import httpx

from usdtmon.blockchain.base import BlockchainError
from usdtmon.retry import RateLimiter, backoff_delay

log = logging.getLogger(__name__)


class TransientError(BlockchainError):
    """Temporary failure; the request may succeed if retried."""

    def __init__(
        self,
        message: str,
        retry_after: float | None = None,
        *,
        rate_limited: bool = False,
        debug: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.rate_limited = rate_limited
        self.debug = debug or {}


class RangeTooLargeError(BlockchainError):
    """The provider refused the request because the block range/result set is too big."""


class RpcError(BlockchainError):
    def __init__(self, code: int | None, message: str) -> None:
        super().__init__(f"JSON-RPC error {code}: {message}")
        self.code = code
        self.rpc_message = message


_RATE_LIMIT_MARKERS = (
    "rate limit",
    "too many requests",
    "exceeded the quota",
    "capacity",
    "throttl",
)
_RANGE_MARKERS = (
    "block range",
    "range is too",
    "range too",
    "too many results",
    "more than",
    "max block",
    "maximum block",
    "response size",
    "result window",
    "limit exceeded",
    "exceed",
)
# Server-side errors that are commonly temporary (node lagging, overloaded, ...).
_TRANSIENT_RPC_CODES = {-32000, -32002, -32003, -32005, -32603}


def classify_rpc_error(code: int | None, message: str) -> BlockchainError:
    text = message.lower()
    if any(m in text for m in _RATE_LIMIT_MARKERS):
        return TransientError(
            f"rate limited: {message}",
            retry_after=_suspension(message),
            rate_limited=True,
        )
    if any(m in text for m in _RANGE_MARKERS):
        return RangeTooLargeError(message)
    if code in _TRANSIENT_RPC_CODES:
        return TransientError(f"JSON-RPC error {code}: {message}")
    return RpcError(code, message)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


# TronGrid: "... the query server is suspended for 5 s. ..."
_SUSPENDED = re.compile(r"suspended for\s+(\d+(?:\.\d+)?)\s*s(?:ec(?:ond)?s?)?\b", re.IGNORECASE)
# The advertised time is rounded; wait slightly longer than that.
_SUSPENSION_MARGIN = 0.5
_BODY_LIMIT = 500
_STATS_WINDOW = 10.0


def _suspension(text: str) -> float | None:
    match = _SUSPENDED.search(text)
    return float(match.group(1)) + _SUSPENSION_MARGIN if match else None


def _rate_limit_wait(response: httpx.Response) -> float | None:
    """Longest wait the provider asks for, in the Retry-After header or the body."""
    waits = [w for w in (_retry_after(response), _suspension(response.text)) if w is not None]
    return max(waits, default=None)


def _describe_params(method: str, params: list[Any]) -> dict[str, Any]:
    """Compact request description for logs (no wallet addresses)."""
    if method != "eth_getLogs" or not params:
        return {}
    query = params[0]
    first, last = int(query["fromBlock"], 16), int(query["toBlock"], 16)
    topics = query.get("topics") or []
    wallets = topics[2] if len(topics) > 2 else None
    return {
        "from": first,
        "to": last,
        "blocks": last - first + 1,
        "wallets": len(wallets) if isinstance(wallets, list) else None,
    }


class JsonRpcClient:
    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 15.0,
        max_concurrency: int = 4,
        rate_limit: float = 10.0,
        max_retries: int = 5,
        backoff_base: float = 0.5,
        backoff_max: float = 30.0,
        max_retry_after: float = 120.0,
        network: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = url
        self.network = network
        self._secrets = [v for v in (headers or {}).values() if v]
        self._max_retry_after = max_retry_after
        self._sent: deque[float] = deque()
        self._client = client or httpx.AsyncClient(timeout=timeout, headers=headers)
        self._owns_client = client is None
        if client is not None and headers:
            self._client.headers.update(headers)
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._limiter = RateLimiter(rate_limit)
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._ids = itertools.count(1)
        log.info(
            "provider_configured",
            extra={
                "network": network,
                "host": urlsplit(url).hostname,
                "auth_header": bool(self._secrets),
                "rate_limit": rate_limit,
                "max_concurrency": max_concurrency,
                "max_retries": max_retries,
                "backoff_base": backoff_base,
                "backoff_max": backoff_max,
            },
        )

    @property
    def http(self) -> httpx.AsyncClient:
        return self._client

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def call(self, method: str, params: list[Any]) -> Any:
        return await self.with_retries(
            lambda: self._call_once(method, params),
            what=method,
            detail=_describe_params(method, params),
        )

    async def request_json(self, method: str, url: str, **kwargs: Any) -> Any:
        """Plain HTTP request (non JSON-RPC) with the same limits and retries."""
        return await self.with_retries(
            lambda: self._http_once(method, url, **kwargs), what=f"{method} {url}"
        )

    async def with_retries(
        self, fn: Any, *, what: str, detail: dict[str, Any] | None = None
    ) -> Any:
        attempt = 0
        started = time.monotonic()
        while True:
            attempt += 1
            try:
                async with self._semaphore:
                    await self._limiter.acquire()
                    result = await fn()
                if attempt > 1:
                    log.info(
                        "provider_request_recovered",
                        extra={
                            "network": self.network,
                            "what": what,
                            "attempt": attempt,
                            "seconds": round(time.monotonic() - started, 2),
                            **(detail or {}),
                        },
                    )
                return result
            except TransientError as exc:
                context = {
                    "network": self.network,
                    "what": what,
                    "attempt": attempt,
                    "error": str(exc),
                    "rate_limited": exc.rate_limited,
                    "retry_after": exc.retry_after,
                    "auth_header": bool(self._secrets),
                    "requests_10s": self._recent_requests(),
                    **(detail or {}),
                    **exc.debug,
                }
                too_long = exc.retry_after is not None and exc.retry_after > self._max_retry_after
                if attempt > self._max_retries or too_long:
                    # Waiting out a very long suspension here would stall the cycle;
                    # fail instead and let the worker's own backoff handle it.
                    log.warning(
                        "provider_request_failed",
                        extra={
                            **context,
                            "reason": "retry_after_too_long" if too_long else "retries_exhausted",
                            "seconds": round(time.monotonic() - started, 2),
                        },
                    )
                    raise BlockchainError(f"{what} failed after {attempt} attempts: {exc}") from exc
                delay = backoff_delay(attempt, self._backoff_base, self._backoff_max)
                if exc.retry_after is not None:
                    delay = max(delay, exc.retry_after)
                if exc.rate_limited or exc.retry_after is not None:
                    # The limit applies to the provider, so hold back every request.
                    self._limiter.penalize(delay)
                log.warning(
                    "provider_request_retry",
                    extra={**context, "delay": round(delay, 2)},
                )
                await asyncio.sleep(delay)

    def _recent_requests(self) -> int:
        cutoff = time.monotonic() - _STATS_WINDOW
        while self._sent and self._sent[0] < cutoff:
            self._sent.popleft()
        return len(self._sent)

    def _response_debug(self, response: httpx.Response, started: float) -> dict[str, Any]:
        body = response.text
        for secret in self._secrets:
            body = body.replace(secret, "***")
        return {
            "status": response.status_code,
            "body": body[:_BODY_LIMIT],
            "limit_headers": {
                k: v
                for k, v in response.headers.items()
                if k == "retry-after" or "ratelimit" in k or "rate-limit" in k
            },
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        started = time.monotonic()
        self._recent_requests()  # drop expired entries
        self._sent.append(started)
        try:
            response = await self._client.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientError(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code == 429:
            raise TransientError(
                "HTTP 429",
                retry_after=_rate_limit_wait(response),
                rate_limited=True,
                debug=self._response_debug(response, started),
            )
        if response.status_code >= 500 or response.status_code == 408:
            raise TransientError(
                f"HTTP {response.status_code}",
                retry_after=_retry_after(response),
                debug=self._response_debug(response, started),
            )
        if response.status_code >= 400:
            raise BlockchainError(f"HTTP {response.status_code}: {response.text[:200]}")
        return response

    async def _http_once(self, method: str, url: str, **kwargs: Any) -> Any:
        response = await self._send(method, url, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise TransientError("invalid JSON in response") from exc

    async def _call_once(self, method: str, params: list[Any]) -> Any:
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        response = await self._send("POST", self.url, json=payload)
        try:
            body = response.json()
        except ValueError as exc:
            raise TransientError("invalid JSON in response") from exc
        if not isinstance(body, dict):
            raise TransientError("unexpected JSON-RPC response shape")
        error = body.get("error")
        if error:
            if isinstance(error, dict):
                raise classify_rpc_error(error.get("code"), str(error.get("message", "")))
            raise classify_rpc_error(None, str(error))
        if "result" not in body:
            raise TransientError("JSON-RPC response without result")
        return body["result"]
