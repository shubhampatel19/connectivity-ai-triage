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

    async def list_open(self, page_size: int = 500, max_pages: int = 200) -> list[dict[str, Any]]:
        """Every open case, oldest first.

        The case API returns at most 500 cases a call. Asking once would mean
        that past 500 open cases some are never seen, and so never triaged,
        with nothing to say so. This pages by case ID (``after_id``) until a
        short page.

        A case service from before ``after_id`` existed ignores it and
        returns the same newest cases every time; the loop notices a page
        with nothing new and stops, which is the old behaviour.
        """
        seen: dict[int, dict[str, Any]] = {}
        after = 0
        for _ in range(max_pages):
            page = (await self._get("/v1/cases", status="open", limit=page_size, after_id=after))["cases"]
            new = [c for c in page if c["case_id"] not in seen]
            for c in new:
                seen[c["case_id"]] = c
            if len(page) < page_size or not new:
                break
            after = max(c["case_id"] for c in page)
        return [seen[k] for k in sorted(seen)]

    async def get(self, case_id: int) -> dict[str, Any]:
        return await self._get(f"/v1/cases/{case_id}")

    async def close(self) -> None:
        await self.client.aclose()
