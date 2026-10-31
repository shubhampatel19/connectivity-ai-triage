"""Claude through the Anthropic API.

The note comes back as a structured output (output_config.format), so the
API constrains generation to the schema. Thinking stays adaptive at low
effort: the model thinks when a case is hard and skips it when it isn't, and
thinking tokens are billed as output, under max_tokens. Sampling parameters
are omitted; current models reject them. The SDK's own retries are off:
retries happen one level up, where they're counted, logged and budgeted.
"""

from __future__ import annotations

import anthropic

from . import LLMError, Request, Response, Usage


class ClaudeLLM:
    provider = "anthropic"

    def __init__(self, api_key: str, model: str, *, timeout_s: float, effort: str, client=None) -> None:
        self.model = model
        self.effort = effort
        self.client = client or anthropic.AsyncAnthropic(api_key=api_key, max_retries=0, timeout=timeout_s)

    async def complete(self, req: Request) -> Response:
        try:
            msg = await self.client.messages.create(
                model=self.model,
                max_tokens=req.max_tokens,
                # The system prompt is identical for every case, so it's cached:
                # later requests read it at a tenth of the input price.
                system=[{"type": "text", "text": req.system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": req.user}],
                output_config={
                    "format": {"type": "json_schema", "schema": req.schema},
                    "effort": self.effort,
                },
            )
        except anthropic.RateLimitError as e:
            raise LLMError("rate_limited", str(e), _retry_after(e)) from e
        except anthropic.OverloadedError as e:
            raise LLMError("overloaded", str(e), _retry_after(e)) from e
        except anthropic.APITimeoutError as e:
            raise LLMError("timeout", str(e)) from e
        except anthropic.APIConnectionError as e:
            raise LLMError("connection", str(e)) from e
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise LLMError("auth", str(e)) from e
        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                raise LLMError("server_error", str(e), _retry_after(e)) from e
            raise LLMError("bad_request", f"{e.status_code} {e}") from e

        u = msg.usage
        usage = Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_tokens=u.cache_read_input_tokens or 0,
            cache_write_tokens=u.cache_creation_input_tokens or 0,
        )
        stop = msg.stop_reason or ""
        text = "".join(b.text for b in msg.content if b.type == "text")
        if stop == "refusal":
            raise LLMError("refusal", "the model declined", None).with_usage(usage)
        if stop in ("max_tokens", "model_context_window_exceeded"):
            raise LLMError("truncated", f"stopped on {stop} before the note was complete").with_usage(usage)
        return Response(text=text, usage=usage, model=msg.model, stop_reason=stop)


def _retry_after(e: anthropic.APIStatusError) -> float | None:
    try:
        v = e.response.headers.get("retry-after")
        return float(v) if v else None
    except (ValueError, AttributeError):
        return None
