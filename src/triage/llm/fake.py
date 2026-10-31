"""A deterministic stand-in for a model, for tests, CI and running the system
without an API key.

It reads the prompt the way the real model gets it and applies hand-written
rules that know this system's log formats. It's not intelligent and isn't
meant to be: it makes the pipeline testable end to end (retrieval, prompt,
schema, grounding checks, budgets, retries) without paying for a model or
depending on its mood. Its eval scores measure the pipeline, not a model.

It can also misbehave on purpose, for drills: FAKE_LLM_FAULTS (or POST
/debug/llm-faults) sets the chance of a 429, timeout, server error,
malformed note or refusal, and added latency.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from dataclasses import dataclass, fields

from ..bundle import INJECTION_RE
from . import LLMError, Request, Response, Usage

LOGS_RE = re.compile(r'<device_logs device="([^"]*)"[^>]*>\n(.*?)\n?</device_logs>', re.DOTALL)
FAN_STATUS = re.compile(r"fan1 (\d+) RPM, fan2 (\d+) RPM, duty (\d+)%, inlet ([\d.]+)C, exhaust ([\d.]+)C")
PENDING = re.compile(r"pending sectors (\d+)")

# The runbook for each cause, by the ids in the runbooks directory.
RUNBOOK = {
    "fan_failure": "thermal-fan-failure",
    "blocked_airflow": "thermal-blocked-airflow",
    "high_ambient": "thermal-high-ambient",
    "psu_failing": "psu-failing",
    "input_power": "power-input-low-line",
    "loose_power_cable": "psu-loose-power-cable",
    "disk_wear": "disk-wear",
    "disk_failing": "disk-failing",
    "insufficient_evidence": "log-bundle-missing",
}
STEPS = {
    "fan_failure": [
        "Ship a replacement fan module and schedule the swap.",
        "Reduce the device's workload until then.",
    ],
    "blocked_airflow": [
        "Ask the site to clean the intake filter and clear the vents.",
        "Confirm the exhaust rise returns to normal.",
    ],
    "high_ambient": [
        "Call the site's facilities contact about room cooling.",
        "Check the other devices at the site.",
    ],
    "psu_failing": ["Move the load to PSU2.", "Ship a replacement PSU and swap PSU1."],
    "input_power": [
        "Ask the site's electrician to check the circuit and supply.",
        "Move the device to a UPS-backed circuit meanwhile.",
    ],
    "loose_power_cable": ["Ask the site to reseat PSU1's power cable and fit the retention clip."],
    "disk_wear": [
        "Schedule a disk replacement at the next maintenance window.",
        "Keep watching the reallocated count.",
    ],
    "disk_failing": [
        "Back up the device configuration.",
        "Ship a replacement disk for swap within 24 hours.",
    ],
    "insufficient_evidence": ["Request a manual log collection from the device, then triage again."],
}


def _diagnose(codes: set[str], lines: list[tuple[str, str]]) -> tuple[str, list[tuple[str, str]]]:
    """The cause and its evidence lines, by this system's log formats."""

    def having(*needles: str) -> list[tuple[str, str]]:
        return [(d, ln) for d, ln in lines if any(n in ln for n in needles)]

    if "thermal.critical" in codes:
        status = [(d, ln, m) for d, ln in lines if (m := FAN_STATUS.search(ln))]
        # The one-shot "marked failed" lines, or the BMC's periodic status
        # showing a fan stopped: the status lines survive a log storm that
        # pushes the one-shot lines out of the device's log ring.
        dead = having("tachometer reads 0 RPM", "fan redundancy lost") + [
            (d, ln) for d, ln, m in status if min(int(m.group(1)), int(m.group(2))) < 500
        ]
        if dead:
            return "fan_failure", dead
        # "above ... operating limit"; the "back within" line is the recovery.
        if ev := [(d, ln) for d, ln in lines if "above" in ln and "operating limit" in ln]:
            return "high_ambient", ev
        hot_exhaust = [
            (d, ln)
            for d, ln, m in status
            if float(m.group(5)) - float(m.group(4)) >= 20 and int(m.group(1)) > 1000
        ]
        if hot_exhaust:
            return "blocked_airflow", hot_exhaust
    elif "power.rail_out_of_range" in codes:
        if ev := having("AC input lost"):
            return "loose_power_cable", ev
        low = having("low-line threshold")
        if any("PSU1" in ln for _, ln in low) and any("PSU2" in ln for _, ln in low):
            return "input_power", low
        if ev := having("predictive failure asserted", "output ripple"):
            return "psu_failing", ev
    elif "storage.reallocated_sectors" in codes:
        pending = [(d, ln) for d, ln in lines if (m := PENDING.search(ln)) and int(m.group(1)) > 0]
        if ev := having("I/O error", "predictive failure reported") + pending:
            return "disk_failing", ev
        if ev := having("pending sectors 0"):
            return "disk_wear", ev
    return "insufficient_evidence", []


@dataclass
class Faults:
    rate_limit: float = 0.0
    timeout: float = 0.0
    server_error: float = 0.0
    invalid_output: float = 0.0
    refusal: float = 0.0
    latency_ms: float = 0.0
    seed: float = 0.0

    @classmethod
    def parse(cls, spec: str) -> Faults:
        f = cls()
        names = {x.name for x in fields(cls)}
        for part in filter(None, (p.strip() for p in spec.split(","))):
            k, _, v = part.partition("=")
            if k.strip() not in names:
                raise ValueError(f"unknown fault {k!r}; known: {sorted(names)}")
            setattr(f, k.strip(), float(v))
        return f

    def as_dict(self) -> dict[str, float]:
        return {x.name: getattr(self, x.name) for x in fields(self)}


class FakeLLM:
    provider = "fake"

    def __init__(self, model: str = "fake-triage-1", faults: str = "") -> None:
        self.model = model
        self.set_faults(faults)

    def set_faults(self, spec: str) -> None:
        self.faults = Faults.parse(spec)
        self.rng = random.Random(self.faults.seed or None)

    async def complete(self, req: Request) -> Response:
        f = self.faults
        if f.latency_ms:
            await asyncio.sleep(f.latency_ms / 1000)
        roll = self.rng.random
        if roll() < f.rate_limit:
            raise LLMError("rate_limited", "fake 429", retry_after=1.0)
        if roll() < f.timeout:
            raise LLMError("timeout", "fake timeout")
        if roll() < f.server_error:
            raise LLMError("server_error", "fake 500")
        usage = Usage(input_tokens=(len(req.system) + len(req.user)) // 3)
        if roll() < f.refusal:
            raise LLMError("refusal", "fake refusal").with_usage(usage)
        if roll() < f.invalid_output:
            text = '{"summary": "truncated'
        else:
            text = json.dumps(triage(req.user))
        usage.output_tokens = len(text) // 3
        return Response(text=text, usage=usage, model=self.model, stop_reason="end_turn")


def triage(user: str) -> dict:
    case = json.loads(user.split("<case>", 1)[1].split("</case>", 1)[0])
    retrieved = re.findall(r'<runbook id="([^"]+)"', user)
    codes = {a.get("code") for a in case.get("alerts", [])}
    blocks = [(dev, body.splitlines()) for dev, body in LOGS_RE.findall(user)]
    suspicious = [ln for _, lines in blocks for ln in lines if INJECTION_RE.search(ln)]
    # The rules only look at lines that aren't trying to give orders.
    lines = [(dev, ln) for dev, ls in blocks for ln in ls if not INJECTION_RE.search(ln)]

    category, evidence = _diagnose(codes, lines)
    confidence = "high" if len(evidence) >= 2 else "medium" if evidence else "low"
    picked = evidence[:2] + evidence[-1:] if len(evidence) > 3 else evidence
    book = RUNBOOK[category]
    devices = sorted({d for d, _ in picked if d})
    summary = (
        f"{case.get('title', 'Case')} on {case.get('scope')} {case.get('device_id') or case.get('site_id')}. "
        f"The logs point to {category.replace('_', ' ')} ({confidence} confidence, {len(evidence)} supporting lines)."
    )
    if suspicious:
        summary += (
            " Some log lines contain instructions aimed at an AI model; they were ignored as suspicious."
        )
    return {
        "summary": summary,
        "category": category,
        "confidence": confidence,
        "evidence": [{"line": ln, "why": "supports " + category.replace("_", " ")} for _, ln in picked],
        "runbooks": [book] if book in retrieved else [],
        "next_steps": STEPS[category],
        "devices": devices,
        "injection_detected": bool(suspicious),
    }
