"""Prometheus metrics, prefix connectivity_triage_, on the service's own
registry so tests can build as many as they like."""

from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    GCCollector,
    Histogram,
    PlatformCollector,
    ProcessCollector,
)

P = "connectivity_triage_"


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        r = self.registry = registry or CollectorRegistry()
        ProcessCollector(registry=r)
        PlatformCollector(registry=r)
        GCCollector(registry=r)

        self.cases = Counter(
            P + "cases_total",
            "Triage runs by outcome: done, failed, skipped_budget, skipped_breaker, unchanged.",
            ["outcome"],
            registry=r,
        )
        self.case_duration = Histogram(
            P + "case_duration_seconds",
            "Time to triage one case, end to end.",
            buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300),
            registry=r,
        )
        self.pending = Gauge(
            P + "pending_cases", "Open cases with logs that have no current triage note.", registry=r
        )
        self.reconcile_last_success = Gauge(
            P + "reconcile_last_success_timestamp_seconds",
            "When the worker last listed cases successfully.",
            registry=r,
        )

        self.llm_requests = Counter(
            P + "llm_requests_total",
            "LLM requests by outcome (each attempt counts).",
            ["provider", "model", "outcome"],
            registry=r,
        )
        self.llm_duration = Histogram(
            P + "llm_request_duration_seconds",
            "LLM request latency, per attempt.",
            ["provider", "model"],
            buckets=(0.25, 0.5, 1, 2, 4, 8, 15, 30, 60),
            registry=r,
        )
        self.llm_tokens = Counter(
            P + "llm_tokens_total",
            "Tokens billed, by kind: input, output, cache_read, cache_write.",
            ["provider", "model", "kind"],
            registry=r,
        )
        self.llm_cost = Counter(
            P + "llm_cost_usd_total", "Estimated spend in US dollars.", ["provider", "model"], registry=r
        )
        self.breaker_open = Gauge(
            P + "llm_circuit_open", "1 while the LLM circuit breaker is open.", registry=r
        )
        self.budget_spent = Gauge(P + "budget_spent_usd", "Spend over the last 24 hours.", registry=r)
        self.budget_limit = Gauge(P + "budget_limit_usd", "DAILY_BUDGET_USD.", registry=r)

        self.evidence_lines = Histogram(
            P + "evidence_lines",
            "Log lines sent to the model per case.",
            buckets=(0, 5, 10, 25, 50, 100, 200, 400),
            registry=r,
        )
        self.evidence_truncated = Counter(
            P + "evidence_truncated_total", "Cases whose logs didn't fit the evidence budget.", registry=r
        )
        self.grounding_failures = Counter(
            P + "grounding_failures_total",
            "Notes citing something the model wasn't given: evidence, device or runbook.",
            ["kind"],
            registry=r,
        )
        self.injection_suspected = Counter(
            P + "injection_suspected_total",
            "Log lines that look like instructions to a model, by who noticed: filter or model.",
            ["detector"],
            registry=r,
        )
        self.retrieval_duration = Histogram(
            P + "retrieval_duration_seconds",
            "Runbook search latency.",
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1),
            registry=r,
        )
        self.runbook_chunks = Gauge(P + "runbook_chunks", "Runbook chunks indexed.", registry=r)
