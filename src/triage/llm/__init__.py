"""Model providers behind one small interface, plus pricing.

Providers raise LLMError with a `kind` the rest of the service acts on:
retryable kinds are retried with backoff, breaker kinds count toward opening
the circuit breaker, and every attempt is recorded with its outcome.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol


@dataclass
class Request:
    system: str
    user: str
    schema: dict
    max_tokens: int


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class Response:
    text: str
    usage: Usage
    model: str
    stop_reason: str


# kind -> (retryable, counts toward the circuit breaker)
KINDS = {
    "rate_limited": (True, True),
    "overloaded": (True, True),
    "server_error": (True, True),
    "timeout": (True, True),
    "connection": (True, True),
    "auth": (False, True),  # every request will fail the same way
    "bad_request": (False, False),  # our request is wrong; the service is fine
    "refusal": (False, False),
    "truncated": (False, False),
    "invalid_output": (True, False),
    "circuit_open": (False, False),
}


class LLMError(Exception):
    def __init__(self, kind: str, message: str, retry_after: float | None = None) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.retry_after = retry_after
        # Tokens billed for a request that still failed (a refusal or a
        # truncated note is paid for).
        self.usage: Usage | None = None

    def with_usage(self, usage: Usage) -> LLMError:
        self.usage = usage
        return self

    @property
    def retryable(self) -> bool:
        return KINDS.get(self.kind, (False, False))[0]

    @property
    def trips_breaker(self) -> bool:
        return KINDS.get(self.kind, (False, False))[1]


class LLM(Protocol):
    provider: str
    model: str

    async def complete(self, req: Request) -> Response: ...


# US dollars per million tokens: input, output, cache write (5 minute), cache
# read, for prompts up to 100k tokens; then the same for longer prompts. From
# the Claude pricing docs, October 2026; check them before trusting a bill.
PRICES: dict[str, tuple[tuple[float, float, float, float], tuple[float, float, float, float]]] = {
    "claude-haiku-5-5": ((0.10, 0.50, 0.125, 0.01), (0.50, 2.50, 0.625, 0.05)),
    "claude-sonnet-5-5": ((2.0, 10.0, 2.5, 0.2), (2.0, 10.0, 2.5, 0.2)),
    "claude-opus-5-5": ((4.0, 20.0, 5.0, 0.4), (4.0, 20.0, 5.0, 0.4)),
}
LONG_PROMPT = 100_000


def cost(provider: str, model: str, u: Usage) -> float:
    """Estimated cost of one request. Local models are free; the fake model is
    priced like Claude Haiku so budgets and spend alerts behave as they would."""
    if provider == "ollama":
        return 0.0
    key = "claude-haiku-5-5" if provider == "fake" else model
    tiers = PRICES.get(key, PRICES["claude-haiku-5-5"])
    prompt = u.input_tokens + u.cache_read_tokens + u.cache_write_tokens
    p_in, p_out, p_cw, p_cr = tiers[1] if prompt > LONG_PROMPT else tiers[0]
    if os.environ.get("LLM_PRICE_INPUT"):
        p_in = float(os.environ["LLM_PRICE_INPUT"])
    if os.environ.get("LLM_PRICE_OUTPUT"):
        p_out = float(os.environ["LLM_PRICE_OUTPUT"])
    return (
        u.input_tokens * p_in
        + u.output_tokens * p_out
        + u.cache_write_tokens * p_cw
        + u.cache_read_tokens * p_cr
    ) / 1_000_000
