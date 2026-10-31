import json

import anthropic
import httpx
import httpx2
import pytest

from triage.llm import LLMError, Request, Usage, cost
from triage.llm.claude import ClaudeLLM
from triage.llm.fake import FakeLLM, Faults
from triage.llm.ollama import OllamaLLM

REQ = Request(system="sys", user="usr", schema={"type": "object"}, max_tokens=4096)


# The Anthropic SDK ships its own fork of httpx, httpx2: its client and
# transports come from there.
def claude(handler) -> tuple[ClaudeLLM, list[dict]]:
    sent: list[dict] = []

    def record(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return handler(request)

    client = anthropic.AsyncAnthropic(
        api_key="test", max_retries=0, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(record))
    )
    return ClaudeLLM("unused", "claude-haiku-5-5", timeout_s=5, effort="low", client=client), sent


def message(text: str, stop: str = "end_turn") -> dict:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": text}],
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 1200,
            "output_tokens": 300,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 0,
        },
    }


def error(status: int, kind: str, headers: dict | None = None) -> httpx2.Response:
    return httpx2.Response(
        status, json={"type": "error", "error": {"type": kind, "message": kind}}, headers=headers or {}
    )


async def test_claude_request_shape_and_usage():
    llm, sent = claude(lambda r: httpx2.Response(200, json=message('{"ok": true}')))
    resp = await llm.complete(REQ)
    assert resp.text == '{"ok": true}', "thinking blocks are not part of the note"
    assert (resp.usage.input_tokens, resp.usage.output_tokens, resp.usage.cache_read_tokens) == (
        1200,
        300,
        900,
    )
    body = sent[0]
    assert body["model"] == "claude-haiku-5-5" and body["max_tokens"] == 4096
    assert body["output_config"] == {
        "format": {"type": "json_schema", "schema": {"type": "object"}},
        "effort": "low",
    }
    assert body["system"] == [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}]
    for p in ("temperature", "top_p", "top_k"):
        assert p not in body, f"{p} is rejected by current models"


@pytest.mark.parametrize(
    ("response", "kind", "retryable", "retry_after"),
    [
        (error(429, "rate_limit_error", {"retry-after": "3"}), "rate_limited", True, 3.0),
        (error(529, "overloaded_error"), "overloaded", True, None),
        (error(500, "api_error"), "server_error", True, None),
        (error(503, "api_error"), "server_error", True, None),
        (error(400, "invalid_request_error"), "bad_request", False, None),
        (error(401, "authentication_error"), "auth", False, None),
        (error(404, "not_found_error"), "bad_request", False, None),
    ],
)
async def test_claude_errors_are_classified(response, kind, retryable, retry_after):
    llm, _ = claude(lambda r: response)
    with pytest.raises(LLMError) as e:
        await llm.complete(REQ)
    assert (e.value.kind, e.value.retryable, e.value.retry_after) == (kind, retryable, retry_after)


@pytest.mark.parametrize(("stop", "kind"), [("refusal", "refusal"), ("max_tokens", "truncated")])
async def test_claude_unusable_stops_are_errors_that_still_cost(stop, kind):
    llm, _ = claude(lambda r: httpx2.Response(200, json=message("{", stop)))
    with pytest.raises(LLMError) as e:
        await llm.complete(REQ)
    assert e.value.kind == kind and not e.value.retryable
    assert e.value.usage and e.value.usage.output_tokens == 300, (
        "a refused or truncated request is still billed"
    )


async def test_claude_timeout_and_connection_errors():
    def boom(r):
        raise httpx2.ConnectError("refused")

    llm, _ = claude(boom)
    with pytest.raises(LLMError) as e:
        await llm.complete(REQ)
    assert e.value.kind == "connection" and e.value.retryable


async def test_ollama_request_and_errors():
    sent = []

    def ok(r: httpx.Request) -> httpx.Response:
        sent.append(json.loads(r.content))
        return httpx.Response(
            200,
            json={
                "message": {"content": "{}"},
                "prompt_eval_count": 50,
                "eval_count": 7,
                "done_reason": "stop",
            },
        )

    llm = OllamaLLM(
        "http://ollama:11434",
        "qwen2.5:3b",
        timeout_s=5,
        client=httpx.AsyncClient(transport=httpx.MockTransport(ok)),
    )
    resp = await llm.complete(REQ)
    assert (
        resp.usage.input_tokens == 50
        and sent[0]["format"] == {"type": "object"}
        and sent[0]["stream"] is False
    )

    llm.client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, text="model not found"))
    )
    with pytest.raises(LLMError, match=r"ollama pull qwen2\.5:3b"):
        await llm.complete(REQ)


def test_cost_tiers_and_free_local_models():
    u = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost("anthropic", "claude-haiku-5-5", u) == pytest.approx(0.50 + 2.50), (
        "over 100k-token prompts cost more"
    )
    small = Usage(input_tokens=10_000, output_tokens=1_000, cache_read_tokens=5_000)
    assert cost("anthropic", "claude-haiku-5-5", small) == pytest.approx(
        (10_000 * 0.10 + 1_000 * 0.50 + 5_000 * 0.01) / 1e6
    )
    assert cost("ollama", "qwen2.5:3b", u) == 0
    assert cost("fake", "fake-triage-1", small) == cost("anthropic", "claude-haiku-5-5", small)


async def test_fake_faults():
    with pytest.raises(ValueError, match="unknown fault"):
        Faults.parse("meteor=1")
    llm = FakeLLM(faults="rate_limit=1")
    with pytest.raises(LLMError) as e:
        await llm.complete(REQ)
    assert e.value.kind == "rate_limited" and e.value.retry_after == 1.0
    llm.set_faults("invalid_output=1")
    resp = await llm.complete(Request("s", '<case>{"alerts": []}</case>', {}, 100))
    with pytest.raises(json.JSONDecodeError):
        json.loads(resp.text)


def test_fake_finds_a_dead_fan_in_status_lines_alone():
    # After a log storm only the BMC's periodic status lines may be left.
    user = (
        '<case>{"alerts": [{"code": "thermal.critical", "device_id": "d1", "message": "cpu_temp=91C"}]}</case>\n'
        '<runbooks>\n<runbook id="thermal-fan-failure" title="t">\n[Symptoms]\nx\n</runbook>\n</runbooks>\n'
        '<device_logs device="d1" bundle="b">\n'
        "2026-10-01T12:00:00.000Z WARN  [bmc] fan status: fan1 0 RPM, fan2 7190 RPM, duty 100%, inlet 24.1C, exhaust 41.0C\n"
        "</device_logs>"
    )
    from triage.llm.fake import triage as fake_triage

    note = fake_triage(user)
    assert note["category"] == "fan_failure" and note["runbooks"] == ["thermal-fan-failure"]
