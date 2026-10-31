"""HTTP API: triage notes, runbook search, health and metrics."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from opentelemetry import propagate
from opentelemetry.trace import SpanKind
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .config import Settings
from .llm.fake import FakeLLM
from .logs import log
from .service import Service
from .tracing import tracer


def _jsonable(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: (float(v) if isinstance(v, Decimal) else v) for k, v in row.items()}


def create_app(settings: Settings, service: Service | None = None, **start_kwargs: Any) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        svc = service or await Service.start(settings, **start_kwargs)
        app.state.svc = svc
        task = asyncio.create_task(svc.worker.run()) if settings.worker_enabled else None
        try:
            yield
        finally:
            svc.worker.stop()
            if task:
                await task
            if service is None:
                await svc.close()

    app = FastAPI(title="connectivity-ai-triage", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def trace_requests(request: Request, call_next):
        # A server span per request, continuing the caller's trace.
        if request.url.path in ("/healthz", "/readyz", "/metrics"):
            return await call_next(request)
        route = request.scope.get("route")
        name = f"{request.method} {getattr(route, 'path', request.url.path)}"
        ctx = propagate.extract(dict(request.headers))
        with tracer.start_as_current_span(name, context=ctx, kind=SpanKind.SERVER) as span:
            resp = await call_next(request)
            span.set_attribute("http.response.status_code", resp.status_code)
            return resp

    def svc(request: Request) -> Service:
        return request.app.state.svc

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        # Ready means Postgres answers. The LLM and the case API are deliberately
        # not part of it: if they're down, notes wait, but the API still serves
        # the notes it has, and pulling the pod out of rotation fixes nothing.
        try:
            await asyncio.wait_for(svc(request).store.ping(), timeout=2)
        except Exception as e:
            return JSONResponse({"status": "unavailable", "err": str(e)}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.get("/metrics")
    async def metrics(request: Request) -> Response:
        return Response(generate_latest(svc(request).metrics.registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/v1/triage")
    async def list_notes(request: Request, limit: int = 50) -> dict[str, Any]:
        rows = await svc(request).store.list_notes(max(1, min(limit, 500)))
        return {"notes": [_jsonable(r) for r in rows]}

    @app.get("/v1/triage/{case_id}")
    async def get_note(request: Request, case_id: int) -> dict[str, Any]:
        row = await svc(request).store.get_note(case_id)
        if row is None:
            raise HTTPException(404, f"no triage for case {case_id} yet")
        return _jsonable(row) or {}

    @app.post("/v1/triage/{case_id}/rerun")
    async def rerun(request: Request, case_id: int) -> dict[str, Any]:
        s = svc(request)
        if not await s.store.claim(case_id, s.worker.owner, settings.lease_s):
            raise HTTPException(409, f"case {case_id} is being triaged right now")
        try:
            res = await s.triager.triage(case_id, force=True)
        except httpx.HTTPStatusError as e:
            raise HTTPException(e.response.status_code, f"case API: {e.response.text[:200]}") from e
        finally:
            await s.store.release(case_id, s.worker.owner)
        return {"case_id": res.case_id, "status": res.status, "category": res.category, "error": res.error}

    @app.get("/v1/runbooks")
    async def list_runbooks(request: Request) -> dict[str, Any]:
        books = svc(request).retriever.runbooks.values()
        return {
            "runbooks": [
                {
                    "id": b.id,
                    "title": b.title,
                    "alert_codes": b.alert_codes,
                    "sections": [c.section for c in b.chunks],
                }
                for b in books
            ]
        }

    @app.get("/v1/runbooks/search")
    async def search(request: Request, q: str, code: str = "") -> dict[str, Any]:
        hits = await svc(request).retriever.search(q, [code] if code else [])
        return {
            "query": q,
            "hits": [
                {"runbook_id": h.runbook_id, "section": h.section, "score": round(h.score, 5)} for h in hits
            ],
        }

    @app.get("/debug/llm-faults")
    async def get_faults(request: Request) -> dict[str, Any]:
        llm = svc(request).llm
        if not isinstance(llm, FakeLLM):
            raise HTTPException(404, "fault injection exists only with LLM_PROVIDER=fake")
        return llm.faults.as_dict()

    @app.post("/debug/llm-faults")
    async def set_faults(request: Request, spec: str = "") -> dict[str, Any]:
        """For drills: ?spec=rate_limit=0.5,latency_ms=2000 (empty clears)."""
        llm = svc(request).llm
        if not isinstance(llm, FakeLLM):
            raise HTTPException(404, "fault injection exists only with LLM_PROVIDER=fake")
        try:
            llm.set_faults(spec)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        log.warning("fake LLM faults changed", extra={"faults": llm.faults.as_dict()})
        return llm.faults.as_dict()

    return app
