"""Retries with backoff, and a circuit breaker.

Retries use exponential backoff with full jitter, and wait at least as long
as a Retry-After header asks. The breaker opens after a run of failures that
say the provider is unhealthy (429s, 5xx, timeouts, auth), fails fast while
open so the worker stops paying for doomed requests, and lets one probe
through after the cool-down.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable

from . import LLMError

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


class CircuitBreaker:
    def __init__(self, failures: int, cooldown_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.threshold = failures
        self.cooldown = cooldown_s
        self.clock = clock
        self.state = CLOSED
        self.failures = 0
        self.opened_at = 0.0
        self._probe = False

    def allow(self) -> bool:
        if self.state == OPEN and self.clock() - self.opened_at >= self.cooldown:
            self.state, self._probe = HALF_OPEN, False
        if self.state == HALF_OPEN:
            if self._probe:
                return False  # one probe at a time
            self._probe = True
            return True
        return self.state == CLOSED

    def success(self) -> None:
        self.state, self.failures, self._probe = CLOSED, 0, False

    def failure(self) -> None:
        self.failures += 1
        if self.state == HALF_OPEN or self.failures >= self.threshold:
            self.state, self.opened_at, self._probe = OPEN, self.clock(), False

    @property
    def is_open(self) -> bool:
        return self.state == OPEN


async def with_retries[T](
    call: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    breaker: CircuitBreaker,
    on_error: Callable[[LLMError, int], Awaitable[None]],
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Runs call until it succeeds, fails with a non-retryable error, or runs out
    of attempts. on_error sees every failed attempt."""
    for attempt in range(1, attempts + 1):
        if not breaker.allow():
            raise LLMError("circuit_open", "too many recent LLM failures; not calling")
        try:
            result = await call()
        except LLMError as e:
            if e.trips_breaker:
                breaker.failure()
            else:
                # A 400, a refusal or a malformed note still means the provider
                # answered: it's healthy, even if this request wasn't.
                breaker.success()
            await on_error(e, attempt)
            if not e.retryable or attempt == attempts:
                raise
            delay = random.uniform(0, min(max_delay, base_delay * 2 ** (attempt - 1)))
            if e.retry_after:
                delay = max(delay, min(e.retry_after, max_delay))
            await sleep(delay)
            continue
        breaker.success()
        return result
    raise AssertionError("unreachable")
