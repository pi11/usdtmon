"""JSON-RPC over HTTP with timeouts, bounded concurrency, rate limiting and retries."""

from __future__ import annotations

import asyncio
import itertools
import logging
from typing import Any

import httpx

from usdtmon.blockchain.base import BlockchainError
from usdtmon.retry import RateLimiter, backoff_delay

log = logging.getLogger(__name__)


class TransientError(BlockchainError):
    """Temporary failure; the request may succeed if retried."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


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
        return TransientError(f"rate limited: {message}")
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
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = url
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

    @property
    def http(self) -> httpx.AsyncClient:
        return self._client

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def call(self, method: str, params: list[Any]) -> Any:
        return await self.with_retries(lambda: self._call_once(method, params), what=method)

    async def request_json(self, method: str, url: str, **kwargs: Any) -> Any:
        """Plain HTTP request (non JSON-RPC) with the same limits and retries."""
        return await self.with_retries(
            lambda: self._http_once(method, url, **kwargs), what=f"{method} {url}"
        )

    async def with_retries(self, fn: Any, *, what: str) -> Any:
        attempt = 0
        while True:
            attempt += 1
            try:
                async with self._semaphore:
                    await self._limiter.acquire()
                    return await fn()
            except TransientError as exc:
                if attempt > self._max_retries:
                    raise BlockchainError(f"{what} failed after {attempt} attempts: {exc}") from exc
                delay = backoff_delay(attempt, self._backoff_base, self._backoff_max)
                if exc.retry_after is not None:
                    delay = max(delay, exc.retry_after)
                    self._limiter.penalize(exc.retry_after)
                log.warning(
                    "provider_request_retry",
                    extra={
                        "what": what,
                        "attempt": attempt,
                        "delay": round(delay, 2),
                        "error": str(exc),
                    },
                )
                await asyncio.sleep(delay)

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._client.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientError(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code == 429:
            raise TransientError("HTTP 429", retry_after=_retry_after(response))
        if response.status_code >= 500 or response.status_code == 408:
            raise TransientError(f"HTTP {response.status_code}", retry_after=_retry_after(response))
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
