"""A local model through Ollama: free, offline, and slower and less capable
than Claude. Ollama constrains output to the JSON schema passed as `format`."""

from __future__ import annotations

import httpx

from . import LLMError, Request, Response, Usage


class OllamaLLM:
    provider = "ollama"

    def __init__(
        self, url: str, model: str, *, timeout_s: float, client: httpx.AsyncClient | None = None
    ) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.client = client or httpx.AsyncClient(timeout=timeout_s)

    async def complete(self, req: Request) -> Response:
        body = {
            "model": self.model,
            "stream": False,
            "format": req.schema,
            "options": {"temperature": 0, "num_ctx": 16384, "num_predict": req.max_tokens},
            "messages": [{"role": "system", "content": req.system}, {"role": "user", "content": req.user}],
        }
        try:
            r = await self.client.post(f"{self.url}/api/chat", json=body)
        except httpx.TimeoutException as e:
            raise LLMError("timeout", str(e) or "timed out") from e
        except httpx.TransportError as e:
            raise LLMError("connection", str(e) or type(e).__name__) from e
        if r.status_code == 404:
            raise LLMError("bad_request", f"model {self.model} not found; run: ollama pull {self.model}")
        if r.status_code >= 500:
            raise LLMError("server_error", f"{r.status_code} {r.text[:200]}")
        if r.status_code >= 400:
            raise LLMError("bad_request", f"{r.status_code} {r.text[:200]}")
        d = r.json()
        usage = Usage(input_tokens=d.get("prompt_eval_count", 0), output_tokens=d.get("eval_count", 0))
        if d.get("done_reason") == "length":
            raise LLMError("truncated", "stopped at num_predict").with_usage(usage)
        return Response(
            text=d.get("message", {}).get("content", ""),
            usage=usage,
            model=self.model,
            stop_reason="end_turn",
        )
