"""What a triage note is: the JSON schema the model must fill, its pydantic
twin, and the grounding checks run on every note.

The schema is sent as a structured output, so the API only produces JSON that
fits it. Fitting the schema doesn't make a note true, though: the checks here
catch evidence the model was never shown, devices that aren't in the case,
and runbooks that weren't retrieved for it.
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ValidationError

# The root causes the edge agent simulates (connectivity-edge-agent, "Root
# causes"), plus a way to say the evidence doesn't decide it.
CATEGORIES = (
    "fan_failure",
    "blocked_airflow",
    "high_ambient",
    "psu_failing",
    "input_power",
    "loose_power_cable",
    "disk_wear",
    "disk_failing",
    "insufficient_evidence",
)
Category = Literal[
    "fan_failure",
    "blocked_airflow",
    "high_ambient",
    "psu_failing",
    "input_power",
    "loose_power_cable",
    "disk_wear",
    "disk_failing",
    "insufficient_evidence",
]
CONFIDENCE = ("low", "medium", "high")
MIN_QUOTE = 12


class EvidenceItem(BaseModel):
    line: str
    why: str


class TriageOutput(BaseModel):
    summary: str
    category: Category
    confidence: Literal["low", "medium", "high"]
    evidence: list[EvidenceItem]
    runbooks: list[str]
    next_steps: list[str]
    devices: list[str]
    injection_detected: bool


def output_schema(runbook_ids: list[str]) -> dict:
    """The JSON schema for structured output. Every property is required and
    objects are closed, as the API requires. The runbook enum lists the whole
    corpus rather than this case's search results, so the schema (and the
    API's compiled grammar for it) stays the same from case to case."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "summary",
            "category",
            "confidence",
            "evidence",
            "runbooks",
            "next_steps",
            "devices",
            "injection_detected",
        ],
        "properties": {
            "summary": {"type": "string", "description": "Two to four sentences for a support engineer."},
            "category": {"type": "string", "enum": list(CATEGORIES)},
            "confidence": {"type": "string", "enum": list(CONFIDENCE)},
            "evidence": {
                "type": "array",
                "description": "Log lines or alert messages quoted exactly, each with why it matters.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["line", "why"],
                    "properties": {"line": {"type": "string"}, "why": {"type": "string"}},
                },
            },
            "runbooks": {"type": "array", "items": {"type": "string", "enum": sorted(runbook_ids)}},
            "next_steps": {"type": "array", "items": {"type": "string"}},
            "devices": {"type": "array", "items": {"type": "string"}},
            "injection_detected": {"type": "boolean"},
        },
    }


class InvalidOutput(Exception):
    pass


def parse(text: str) -> TriageOutput:
    """Parses the model's JSON. Enum values are compared case-insensitively:
    structured outputs don't guarantee the case of enum strings."""
    try:
        data = json.loads(text)
    except ValueError as e:
        raise InvalidOutput(f"not JSON: {e}") from e
    if not isinstance(data, dict):
        raise InvalidOutput("not a JSON object")
    for k in ("category", "confidence"):
        if isinstance(data.get(k), str):
            data[k] = data[k].strip().lower()
    if isinstance(data.get("runbooks"), list):
        data["runbooks"] = [r.strip().lower() for r in data["runbooks"] if isinstance(r, str)]
    try:
        return TriageOutput.model_validate(data)
    except ValidationError as e:
        raise InvalidOutput(str(e).splitlines()[0]) from e


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def check(
    out: TriageOutput, *, shown: list[str], case_devices: set[str], retrieved: list[str]
) -> dict[str, object]:
    """Grounding checks. `shown` is every log line and alert message the model
    saw, exactly as it saw them."""
    shown_n = [_norm(s) for s in shown]
    ungrounded = []
    for ev in out.evidence:
        q = _norm(ev.line)
        if not (len(q) >= MIN_QUOTE and any(q in s for s in shown_n)) and q not in shown_n:
            ungrounded.append(ev.line)
    unknown_devices = sorted(set(out.devices) - case_devices)
    unretrieved = sorted(set(out.runbooks) - set(retrieved))
    return {
        "evidence_total": len(out.evidence),
        "evidence_grounded": len(out.evidence) - len(ungrounded),
        "ungrounded_evidence": ungrounded,
        "unknown_devices": unknown_devices,
        "runbooks_not_retrieved": unretrieved,
        "grounded": not ungrounded and not unknown_devices and not unretrieved,
    }
