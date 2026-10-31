"""OpenTelemetry, the same contract as go-kit's: W3C trace context always,
OTLP/HTTP export only when OTEL_EXPORTER_OTLP_ENDPOINT is set."""

from __future__ import annotations

import os

from opentelemetry import propagate, trace
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from .logs import SERVICE, log

tracer = trace.get_tracer("connectivity-ai-triage")


def enabled() -> bool:
    return bool(
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    )


def setup() -> TracerProvider | None:
    propagate.set_global_textmap(CompositePropagator([TraceContextTextMapPropagator()]))
    if not enabled():
        log.info("tracing: propagating trace context only; set OTEL_EXPORTER_OTLP_ENDPOINT to export spans")
        return None
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    # The OTEL_* variables (sampler, resource attributes, service name) apply.
    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    log.info("tracing: exporting spans over OTLP/HTTP")
    return provider


def inject_headers(headers: dict[str, str]) -> dict[str, str]:
    """Adds the current trace context to outgoing HTTP headers."""
    propagate.inject(headers)
    return headers


def current_trace_id() -> str:
    sc = trace.get_current_span().get_span_context()
    return format(sc.trace_id, "032x") if sc.is_valid and sc.trace_flags.sampled else ""
