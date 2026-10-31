import pytest

from triage.llm import LLMError
from triage.llm.resilience import CircuitBreaker, with_retries


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def failing(*kinds, then="ok"):
    seq = list(kinds)

    async def call():
        if seq:
            k = seq.pop(0)
            raise LLMError(k, "boom", retry_after=7.0 if k == "rate_limited" else None)
        return then

    return call


async def run(call, attempts=4, breaker=None):
    sleeps, errors = [], []

    async def sleep(d):
        sleeps.append(d)

    async def on_error(e, n):
        errors.append((e.kind, n))

    b = breaker or CircuitBreaker(100, 60)
    result = await with_retries(call, attempts=attempts, breaker=b, on_error=on_error, sleep=sleep)
    return result, sleeps, errors


async def test_retries_transient_errors_and_honors_retry_after():
    result, sleeps, errors = await run(failing("timeout", "rate_limited"))
    assert result == "ok"
    assert errors == [("timeout", 1), ("rate_limited", 2)]
    assert sleeps[0] <= 1.0 and sleeps[1] >= 7.0, "a 429's Retry-After is a floor on the wait"


async def test_does_not_retry_what_cannot_succeed():
    with pytest.raises(LLMError) as e:
        await run(failing("bad_request"))
    assert e.value.kind == "bad_request"


async def test_gives_up_after_the_last_attempt():
    with pytest.raises(LLMError) as e:
        await run(failing("server_error", "server_error", "server_error"), attempts=3)
    assert e.value.kind == "server_error"


def test_breaker_opens_probes_and_closes():
    clock = Clock()
    b = CircuitBreaker(failures=3, cooldown_s=60, clock=clock)
    for _ in range(3):
        assert b.allow()
        b.failure()
    assert b.is_open and not b.allow()
    clock.t = 61
    assert b.allow(), "one probe after the cool-down"
    assert not b.allow(), "only one probe at a time"
    b.failure()
    assert b.is_open, "a failed probe opens it again"
    clock.t = 200
    assert b.allow()
    b.success()
    assert b.state == "closed" and b.allow()


async def test_errors_that_prove_the_provider_is_up_do_not_open_the_breaker():
    b = CircuitBreaker(failures=2, cooldown_s=60)
    for _ in range(3):
        with pytest.raises(LLMError):
            await run(failing("bad_request"), breaker=b)
    assert not b.is_open
    with pytest.raises(LLMError) as e:
        await run(failing(*["timeout"] * 5), attempts=5, breaker=b)
    assert e.value.kind == "circuit_open", "two timeouts open it; the third attempt is refused"
