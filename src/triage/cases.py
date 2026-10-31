"""The case service's API: the only way this service learns about cases. It
never reads the case service's tables."""

from __future__ import annotations

from typing import Any

import httpx

from .tracing import inject_headers


class CasesClient:
    def __init__(
        self, base_url: str, client: httpx.AsyncClient | None = None, timeout_s: float = 10.0
    ) -> None:
        self.base = base_url.rstrip("/")
        self.client = client or httpx.AsyncClient(timeout=timeout_s)

    async def _get(self, path: str, **params: Any) -> Any:
        r = await self.client.get(self.base + path, params=params, headers=inject_headers({}))
        r.raise_for_status()
        return r.json()

    async def list_open(self, limit: int = 500) -> list[dict[str, Any]]:
        return (await self._get("/v1/cases", status="open", limit=limit))["cases"]

    async def get(self, case_id: int) -> dict[str, Any]:
        return await self._get(f"/v1/cases/{case_id}")

    async def close(self) -> None:
        await self.client.aclose()
