"""Triage of one case, start to finish: fetch the case and its log bundles,
pick the evidence, retrieve runbooks, ask the model, check the answer, store
the note. Every outcome, including failures and skips, is a row in
triage.notes, so "why has case 12 no note?" has an answer."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from opentelemetry.trace import Status, StatusCode

from . import schema
from .bundle import BundleError, Evidence, evidence_from
from .cases import CasesClient
from .config import Settings
from .llm import LLM, LLMError, Request, Response, Usage, cost
from .llm.resilience import CircuitBreaker, with_retries
from .logs import log
from .metrics import Metrics
from .objectstore import ObjectStore
from .prompt import CANARY, PROMPT_VERSION, build
from .retrieval import Retriever, query_for
from .store import Store
from .tracing import current_trace_id, tracer


@dataclass
class Result:
    case_id: int
    status: str  # done, failed, skipped_budget, skipped_breaker, unchanged
    category: str = ""
    error: str = ""


def parse_time(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if not v:
        return None
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def choose_bundles(case: dict, limit: int) -> list[dict]:
    """The newest arrived bundle of each device, newest devices first, up to
    limit. A site case with fifty devices doesn't need fifty bundles to see
    a pattern, and each one costs tokens."""
    arrived = [b for b in case.get("bundles") or [] if b.get("state") == "attached" and b.get("object_key")]
    arrived.sort(key=lambda b: str(b.get("uploaded_at") or ""), reverse=True)
    per_device: dict[str, dict] = {}
    for b in arrived:
        per_device.setdefault(b["device_id"], b)
    return list(per_device.values())[:limit]


def case_devices(case: dict) -> set[str]:
    devs = {a["device_id"] for a in case.get("alerts") or [] if a.get("device_id")}
    if case.get("device_id"):
        devs.add(case["device_id"])
    for child in case.get("children") or []:
        if child.get("device_id"):
            devs.add(child["device_id"])
    return devs


class Triager:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        cases: CasesClient,
        objects: ObjectStore,
        retriever: Retriever,
        llm: LLM,
        breaker: CircuitBreaker,
        metrics: Metrics,
    ) -> None:
        self.s = settings
        self.store = store
        self.cases = cases
        self.objects = objects
        self.retriever = retriever
        self.llm = llm
        self.breaker = breaker
        self.m = metrics
        self.m.budget_limit.set(settings.daily_budget_usd)

    def input_hash(self, bundles: list[dict]) -> str:
        """What a note depends on. Not the alert count: a case that keeps
        collecting alerts would otherwise be triaged, and paid for, on every
        one."""
        key = [PROMPT_VERSION, self.llm.provider, self.llm.model, sorted(b["bundle_id"] for b in bundles)]
        return hashlib.sha256(json.dumps(key).encode()).hexdigest()[:24]

    async def triage(self, case_id: int, *, force: bool = False) -> Result:
        with tracer.start_as_current_span("triage case", attributes={"case.id": case_id}) as span:
            start = time.monotonic()
            res = await self._triage(case_id, force=force)
            span.set_attribute("triage.status", res.status)
            if res.category:
                span.set_attribute("triage.category", res.category)
            if res.status == "failed":
                span.set_status(Status(StatusCode.ERROR, res.error))
            self.m.cases.labels(res.status).inc()
            if res.status in ("done", "failed"):
                self.m.case_duration.observe(time.monotonic() - start)
            return res

    async def _triage(self, case_id: int, *, force: bool) -> Result:
        if self.breaker.is_open and not force:
            return Result(case_id, "skipped_breaker", error="LLM circuit breaker open")
        case = await self.cases.get(case_id)
        bundles = choose_bundles(case, self.s.max_bundles_per_case)
        ihash = self.input_hash(bundles)
        last_alert = parse_time(case.get("last_alert_at"))
        prev = await self.store.get_note(case_id)
        if prev and prev["status"] == "done" and prev["input_hash"] == ihash and not force:
            if last_alert:
                await self.store.touch_note(case_id, last_alert)
            return Result(case_id, "unchanged", category=(prev["note"] or {}).get("category", ""))
        same_input = prev is not None and prev["input_hash"] == ihash
        attempt = (prev["attempts"] if same_input and prev["status"] != "done" else 0) + 1
        base = {
            "case_id": case_id,
            "input_hash": ihash,
            "case_last_alert": last_alert,
            "bundle_ids": [b["bundle_id"] for b in bundles],
            "provider": self.llm.provider,
            "model": self.llm.model,
            "prompt_version": PROMPT_VERSION,
            "attempts": attempt,
            "trace_id": current_trace_id(),
        }

        evidence, bundle_errors = await self._evidence(case, bundles)
        hits = await self.retriever.search(
            query_for(case, evidence), sorted({a.get("code", "") for a in case.get("alerts") or []})
        )
        retrieved = list(dict.fromkeys(h.runbook_id for h in hits))  # best first
        prompt = build(case, evidence, hits)
        req = Request(
            prompt.system,
            prompt.user,
            schema.output_schema(self.retriever.runbook_ids),
            self.s.llm_max_tokens,
        )
        base["retrieved"] = retrieved

        spent = await self.store.spent_24h()
        self.m.budget_spent.set(spent)
        estimate = cost(
            self.llm.provider,
            self.llm.model,
            Usage(input_tokens=len(req.system + req.user) // 3, output_tokens=1500),
        )
        if spent + estimate > self.s.daily_budget_usd:
            msg = f"daily LLM budget exhausted: ${spent:.4f} spent of ${self.s.daily_budget_usd:.2f} in the last 24h"
            log.warning(msg, extra={"case_id": case_id})
            await self.store.save_note(
                # A skip isn't an attempt: the case gets its full retries once
                # the budget allows.
                **{**base, "attempts": attempt - 1},
                status="skipped",
                error=msg,
                next_attempt_at=datetime.now(UTC) + timedelta(minutes=15),
            )
            return Result(case_id, "skipped_budget", error=msg)

        totals = {"usage": Usage(), "cost": 0.0}

        async def call() -> tuple[Response, schema.TriageOutput]:
            t0 = time.monotonic()
            with tracer.start_as_current_span(
                f"chat {self.llm.model}",
                attributes={
                    "gen_ai.system": self.llm.provider,
                    "gen_ai.request.model": self.llm.model,
                    "gen_ai.request.max_tokens": req.max_tokens,
                },
            ) as span:
                try:
                    resp = await self.llm.complete(req)
                except LLMError as e:
                    e.latency = time.monotonic() - t0  # type: ignore[attr-defined]
                    span.set_attribute("error.type", e.kind)
                    raise
                span.set_attribute(
                    "gen_ai.usage.input_tokens",
                    resp.usage.input_tokens + resp.usage.cache_read_tokens + resp.usage.cache_write_tokens,
                )
                span.set_attribute("gen_ai.usage.output_tokens", resp.usage.output_tokens)
                span.set_attribute("gen_ai.response.finish_reasons", [resp.stop_reason])
                try:
                    out = schema.parse(resp.text)
                except schema.InvalidOutput as e:
                    err = LLMError("invalid_output", str(e)).with_usage(resp.usage)
                    err.latency = time.monotonic() - t0  # type: ignore[attr-defined]
                    raise err from e
            await self._account(case_id, "ok", resp.usage, time.monotonic() - t0, totals)
            return resp, out

        async def on_error(e: LLMError, n: int) -> None:
            await self._account(case_id, e.kind, e.usage or Usage(), getattr(e, "latency", 0.0), totals)
            log.warning(
                "LLM attempt failed", extra={"case_id": case_id, "attempt": n, "kind": e.kind, "err": str(e)}
            )

        t_llm = time.monotonic()
        try:
            _, out = await with_retries(
                call,
                attempts=self.s.llm_max_attempts,
                breaker=self.breaker,
                on_error=on_error,
                base_delay=self.s.llm_retry_base_s,
            )
        except LLMError as e:
            self.m.breaker_open.set(1 if self.breaker.is_open else 0)
            if e.kind == "circuit_open":
                return Result(case_id, "skipped_breaker", error=str(e))
            backoff = timedelta(seconds=min(3600, 60 * 2 ** (attempt - 1)))
            await self.store.save_note(
                **base,
                status="failed",
                error=str(e),
                checks={"bundle_errors": bundle_errors},
                input_tokens=totals["usage"].input_tokens,
                output_tokens=totals["usage"].output_tokens,
                cost_usd=Decimal(str(round(totals["cost"], 6))),
                latency_ms=int((time.monotonic() - t_llm) * 1000),
                next_attempt_at=datetime.now(UTC) + backoff,
            )
            log.error("triage failed", extra={"case_id": case_id, "attempt": attempt, "err": str(e)})
            return Result(case_id, "failed", error=str(e))
        self.m.breaker_open.set(1 if self.breaker.is_open else 0)

        checks = schema.check(out, shown=prompt.shown, case_devices=case_devices(case), retrieved=retrieved)
        flagged = sum(ev.suspicious_lines for ev in evidence)
        checks.update(
            {
                "bundle_errors": bundle_errors,
                "suspicious_lines": flagged,
                "canary_leaked": CANARY in json.dumps(out.model_dump()),
                "evidence_selection": [
                    {
                        "device_id": ev.device_id,
                        "bundle_id": ev.bundle_id,
                        "lines_total": ev.total_lines,
                        "lines_shown": len(ev.lines),
                        "omitted_by_budget": ev.omitted_by_budget,
                        "collapsed": ev.collapsed,
                        "trace_bytes": ev.trace_bytes,
                    }
                    for ev in evidence
                ],
            }
        )
        if checks["ungrounded_evidence"]:
            self.m.grounding_failures.labels("evidence").inc()
        if checks["unknown_devices"]:
            self.m.grounding_failures.labels("device").inc()
        if checks["runbooks_not_retrieved"]:
            self.m.grounding_failures.labels("runbook").inc()
        if flagged:
            self.m.injection_suspected.labels("filter").inc(flagged)
        if out.injection_detected:
            self.m.injection_suspected.labels("model").inc()

        await self.store.save_note(
            **base,
            status="done",
            note=out.model_dump(),
            checks=checks,
            input_tokens=totals["usage"].input_tokens,
            output_tokens=totals["usage"].output_tokens,
            cost_usd=Decimal(str(round(totals["cost"], 6))),
            latency_ms=int((time.monotonic() - t_llm) * 1000),
        )
        log.info(
            "case triaged",
            extra={
                "case_id": case_id,
                "category": out.category,
                "confidence": out.confidence,
                "grounded": checks["grounded"],
            },
        )
        return Result(case_id, "done", category=out.category)

    async def _evidence(self, case: dict, bundles: list[dict]) -> tuple[list[Evidence], list[str]]:
        if not bundles:
            return [], []
        budget = max(500, self.s.evidence_token_budget // len(bundles))
        evidence, errors = [], []
        for b in bundles:
            alert_times = [
                t
                for a in case.get("alerts") or []
                if a.get("device_id") == b["device_id"] and (t := parse_time(a.get("observed_at")))
            ]
            with tracer.start_as_current_span(
                "read bundle", attributes={"bundle.id": b["bundle_id"], "device.id": b["device_id"]}
            ) as span:
                try:
                    data = await self.objects.get(b["object_key"], self.s.max_bundle_bytes)
                    ev = evidence_from(
                        data,
                        bundle_id=b["bundle_id"],
                        device_id=b["device_id"],
                        alert_times=alert_times,
                        token_budget=budget,
                        max_bytes=self.s.max_bundle_bytes,
                        max_uncompressed=self.s.max_uncompressed_bytes,
                    )
                except (BundleError, KeyError, OSError) as e:
                    # One unreadable bundle shouldn't sink the case; the note says
                    # which bundle couldn't be used.
                    errors.append(f"{b['bundle_id']}: {e}")
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    log.warning("bundle unreadable", extra={"bundle_id": b["bundle_id"], "err": str(e)})
                    continue
                except Exception as e:  # botocore raises its own hierarchy
                    errors.append(f"{b['bundle_id']}: {type(e).__name__}: {e}")
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    continue
                span.set_attribute("evidence.lines", len(ev.lines))
                self.m.evidence_lines.observe(len(ev.lines))
                if ev.truncated:
                    self.m.evidence_truncated.inc()
                evidence.append(ev)
        return evidence, errors

    async def _account(self, case_id: int, outcome: str, u: Usage, latency_s: float, totals: dict) -> None:
        p, model = self.llm.provider, self.llm.model
        c = cost(p, model, u)
        totals["usage"] = Usage(
            totals["usage"].input_tokens + u.input_tokens + u.cache_read_tokens + u.cache_write_tokens,
            totals["usage"].output_tokens + u.output_tokens,
        )
        totals["cost"] += c
        self.m.llm_requests.labels(p, model, outcome).inc()
        if latency_s:
            self.m.llm_duration.labels(p, model).observe(latency_s)
        for kind, n in (
            ("input", u.input_tokens),
            ("output", u.output_tokens),
            ("cache_read", u.cache_read_tokens),
            ("cache_write", u.cache_write_tokens),
        ):
            if n:
                self.m.llm_tokens.labels(p, model, kind).inc(n)
        if c:
            self.m.llm_cost.labels(p, model).inc(c)
        await self.store.record_call(
            case_id=case_id,
            provider=p,
            model=model,
            outcome=outcome,
            input_tokens=u.input_tokens + u.cache_read_tokens + u.cache_write_tokens,
            output_tokens=u.output_tokens,
            cost_usd=Decimal(str(round(c, 6))),
            latency_ms=int(latency_s * 1000),
        )
