"""JSON logs in the same shape as the Go services' (time, level, msg,
service), with trace_id and span_id when a sampled span is active, so Loki
can parse them and Grafana can link a line to its trace."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

from opentelemetry import trace

SERVICE = "ai-triage"
# Standard LogRecord attributes, and uvicorn's color_message (the message again,
# with terminal color codes), are not fields worth a log line.
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "color_message",
}


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.replace("WARNING", "WARN"),
            "msg": record.getMessage(),
            "service": SERVICE,
        }
        sc = trace.get_current_span().get_span_context()
        if sc.is_valid and sc.trace_flags.sampled:
            out["trace_id"] = format(sc.trace_id, "032x")
            out["span_id"] = format(sc.span_id, "016x")
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                out[k] = v
        if record.exc_info:
            out["err"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup(level: str = "info") -> None:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JSONFormatter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(
        {"debug": logging.DEBUG, "warn": logging.WARNING, "error": logging.ERROR}.get(level, logging.INFO)
    )
    # Per-request access logs and library chatter would drown the service's own lines.
    for noisy in ("uvicorn.access", "httpx", "httpcore", "botocore", "urllib3", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


log = logging.getLogger("triage")
