"""The case API client: reading every open case, however many there are."""

from __future__ import annotations

import httpx

from triage.cases import CasesClient


def _server(total: int, *, supports_after_id: bool = True) -> tuple[httpx.AsyncClient, list[dict[str, str]]]:
    """A stand-in case service holding cases 1..total."""
    calls: list[dict[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        calls.append(q)
        limit = int(q["limit"])
        if supports_after_id:
            after = int(q.get("after_id", "0"))
            ids = [i for i in range(1, total + 1) if i > after][:limit]
        else:  # an older case service: newest first, after_id ignored
            ids = list(range(total, 0, -1))[:limit]
        return httpx.Response(200, json={"cases": [{"case_id": i, "logs": "attached"} for i in ids]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), calls


async def test_list_open_pages_through_every_case() -> None:
    client, calls = _server(1234)
    cases = await CasesClient("http://cases", client=client).list_open()
    assert [c["case_id"] for c in cases] == list(range(1, 1235))
    assert [c["after_id"] for c in calls] == ["0", "500", "1000"]
    assert all(c["status"] == "open" and c["limit"] == "500" for c in calls)


async def test_list_open_stops_after_one_short_page() -> None:
    client, calls = _server(7)
    cases = await CasesClient("http://cases", client=client).list_open()
    assert len(cases) == 7
    assert len(calls) == 1


async def test_list_open_with_a_case_service_that_ignores_after_id() -> None:
    # It keeps returning the same newest 500. The client must stop, not loop.
    client, calls = _server(800, supports_after_id=False)
    cases = await CasesClient("http://cases", client=client).list_open()
    assert len(cases) == 500
    assert len(calls) == 2


async def test_list_open_gives_up_at_the_page_cap() -> None:
    client, calls = _server(10_000)
    cases = await CasesClient("http://cases", client=client).list_open(page_size=10, max_pages=3)
    assert len(cases) == 30
    assert len(calls) == 3
