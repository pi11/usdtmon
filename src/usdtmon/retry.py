"""Small async helpers for rate limiting and exponential backoff."""

from __future__ import annotations

import asyncio
import random
import time


def backoff_delay(attempt: int, base: float, maximum: float, *, jitter: bool = True) -> float:
    """Delay before retry number ``attempt`` (1-based): base * 2^(attempt-1), capped."""
    delay = min(maximum, base * (2 ** max(0, attempt - 1)))
    if jitter:
        # "Equal jitter": keep at least half of the delay, randomize the rest.
        delay = delay / 2 + random.uniform(0, delay / 2)
    return delay


class RateLimiter:
    """Spaces calls evenly at ``rate`` calls per second (shared across tasks)."""

    def __init__(self, rate: float) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self._interval = 1.0 / rate
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            # Sleep while holding the lock (waiters queue in FIFO order) and re-check
            # afterwards, so a penalty also delays calls that are already waiting.
            while (wait := self._next - time.monotonic()) > 0:
                await asyncio.sleep(wait)
            self._next = time.monotonic() + self._interval

    def penalize(self, seconds: float) -> None:
        """Push back all future calls, e.g. after an HTTP 429."""
        self._next = max(self._next, time.monotonic() + seconds)
