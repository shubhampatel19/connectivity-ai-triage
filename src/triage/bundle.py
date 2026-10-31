"""Reads a device log bundle and picks the lines worth a model's attention.

A bundle is what the edge agent uploads: a gzipped tar holding
inventory.json, logs/system.log (the device's rolling system log) and
logs/trace.log (a verbose trace, mostly noise, sometimes megabytes). The
model never sees the whole thing. Lines are ranked (errors and warnings
first, then anything near an alert) and packed into a token budget, so a huge
bundle costs the same as a small one and a single device can't blow up the
bill.

Everything in a bundle came from a device and is untrusted: it is read in
memory with size limits, never extracted to disk, and lines that look like
instructions to a model are flagged.
"""

from __future__ import annotations

import gzip
import io
import json
import re
import tarfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta

LINE_RE = re.compile(r"^(\d{4}-\d\d-\d\dT[\d:.]+Z)\s+(TRACE|DEBUG|INFO|WARN|ERROR)\s+\[([^\]]+)\]\s?(.*)$")
MAX_LINE_CHARS = 400
# Components whose INFO lines still say something about hardware health.
HEALTH_COMPONENTS = {"thermal", "bmc", "power", "storage", "kernel", "raid", "support"}
LEVEL_PRIORITY = {"ERROR": 3, "WARN": 2, "INFO": 0, "DEBUG": -1, "TRACE": -2, "?": 0}
NEAR_ALERT = timedelta(minutes=5)

# Phrases aimed at a model rather than a person. A filter like this is easy to
# evade, so it's a signal for metrics and evals, not the defense: the prompt
# treats every log line as data whether or not it matched.
INJECTION_RE = re.compile(
    r"ignore (all |any |the )?(previous|prior|above|alerts|rest of)|disregard (the|all|your) "
    r"(instructions|rules|alerts)|system prompt|you are now|new instructions|as an ai|assistant:|"
    r"\b(ai|llm) (assistant|model|triage)|note to the (ai|assistant|model)|"
    r"respond with|output the following|<\s*/?\s*(system|instructions)",
    re.IGNORECASE,
)


class BundleError(Exception):
    """The bundle can't be read: corrupt, too big, or not a bundle."""


@dataclass
class LogLine:
    index: int
    text: str  # sanitized: what the model sees, and what evidence must quote
    ts: datetime | None
    level: str
    component: str
    suspicious: bool = False


@dataclass
class RawBundle:
    inventory: dict[str, object]
    lines: list[LogLine]
    trace_bytes: int
    other_files: list[str] = field(default_factory=list)


@dataclass
class Evidence:
    bundle_id: str
    device_id: str
    inventory: dict[str, object]
    lines: list[LogLine]  # chosen lines, oldest first
    total_lines: int
    omitted_by_budget: int
    collapsed: int
    trace_bytes: int
    suspicious_lines: int

    @property
    def truncated(self) -> bool:
        return self.omitted_by_budget > 0


def sanitize(line: str) -> str:
    """What the model may see of one device line: printable text, no tag
    delimiters (so a line can't close the <device_logs> block it sits in), and
    no more than MAX_LINE_CHARS."""
    line = "".join(ch if ch.isprintable() or ch == "\t" else "\ufffd" for ch in line)
    line = line.replace("<", "‹").replace(">", "›")
    if len(line) > MAX_LINE_CHARS:
        line = line[:MAX_LINE_CHARS] + "…"
    return line.rstrip()


def parse_line(index: int, raw: str) -> LogLine:
    text = sanitize(raw)
    m = LINE_RE.match(text)
    ts = None
    level, component = "?", "?"
    if m:
        try:
            ts = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
        except ValueError:
            ts = None
        level, component = m.group(2), m.group(3)
    return LogLine(index, text, ts, level, component, bool(INJECTION_RE.search(text)))


def read_bundle(data: bytes, *, max_bytes: int, max_uncompressed: int) -> RawBundle:
    if len(data) > max_bytes:
        raise BundleError(f"bundle is {len(data)} bytes, over the {max_bytes}-byte limit")
    try:
        # Decompress in bounded steps: a small gzip can expand to gigabytes.
        out = io.BytesIO()
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
            while chunk := gz.read(1 << 20):
                out.write(chunk)
                if out.tell() > max_uncompressed:
                    raise BundleError(f"bundle expands past {max_uncompressed} bytes")
        out.seek(0)
        inventory, system, trace_bytes, other = _members(out, max_uncompressed)
    except BundleError:
        raise
    except (OSError, EOFError, tarfile.TarError) as e:
        raise BundleError(f"not a gzipped tar: {e}") from e
    text = system.decode("utf-8", errors="replace")
    lines = [parse_line(i, raw) for i, raw in enumerate(text.splitlines()) if raw.strip()]
    return RawBundle(inventory=inventory, lines=lines, trace_bytes=trace_bytes, other_files=other)


def _members(tar_bytes: io.BytesIO, max_uncompressed: int) -> tuple[dict[str, object], bytes, int, list[str]]:
    inventory: dict[str, object] = {}
    system = b""
    trace_bytes = 0
    other: list[str] = []
    with tarfile.open(fileobj=tar_bytes, mode="r:") as tar:
        for member in tar:
            if not member.isfile():
                continue
            name = member.name.lstrip("./")
            if name == "logs/trace.log":
                # Never read in full; only its size matters to the model.
                trace_bytes = member.size
                continue
            f = tar.extractfile(member)
            if f is None:
                continue
            body = f.read(min(member.size, max_uncompressed))
            if name == "inventory.json":
                try:
                    inv = json.loads(body)
                    inventory = inv if isinstance(inv, dict) else {}
                except ValueError:
                    inventory = {}
            elif name == "logs/system.log":
                system = body
            else:
                other.append(name)
    return inventory, system, trace_bytes, other


def estimate_tokens(text: str) -> int:
    """A deliberately high estimate: log lines are dense with digits and
    punctuation, which tokenize worse than prose."""
    return len(text) // 3 + 1


def template(line: LogLine) -> str:
    """A line's message with its numbers and hex IDs blanked: lines that say the
    same thing about different readings share a template."""
    msg = line.text.split("] ", 1)[-1]
    return f"{line.level}|{line.component}|{re.sub(r'[0-9a-f]{8,}|\d+(\.\d+)?', '#', msg)}"


def select_lines(
    raw: RawBundle, *, alert_times: list[datetime], token_budget: int
) -> tuple[list[LogLine], int, int]:
    """Picks lines for the model: returns (chosen lines oldest first,
    lines omitted for budget, lines collapsed as repeats).

    Long runs of the same message with different numbers ("upload failed, 412
    samples buffered", "..., 418 samples buffered") keep their first and last
    line. Then lines are ranked by level, by being near an alert, and by
    recency, and taken until the budget runs out.
    """
    lines = raw.lines
    keep: list[LogLine] = []
    collapsed = 0
    i = 0
    while i < len(lines):
        j = i
        t = template(lines[i])
        while j + 1 < len(lines) and template(lines[j + 1]) == t:
            j += 1
        if j - i >= 2:
            keep.extend((lines[i], lines[j]))
            collapsed += j - i - 1
        else:
            keep.extend(lines[i : j + 1])
        i = j + 1

    def score(line: LogLine) -> tuple[int, int]:
        s = LEVEL_PRIORITY.get(line.level, 0)
        if line.level == "INFO" and line.component in HEALTH_COMPONENTS:
            s += 1
        if line.ts and any(abs(line.ts - t) <= NEAR_ALERT for t in alert_times):
            s += 2
        return (s, line.index)  # ties go to the more recent line

    chosen: list[LogLine] = []
    used = 0
    for line in sorted(keep, key=score, reverse=True):
        cost = estimate_tokens(line.text)
        if used + cost > token_budget:
            continue
        chosen.append(line)
        used += cost
    chosen.sort(key=lambda ln: ln.index)
    return chosen, len(keep) - len(chosen), collapsed


def evidence_from(
    data: bytes,
    *,
    bundle_id: str,
    device_id: str,
    alert_times: list[datetime],
    token_budget: int,
    max_bytes: int,
    max_uncompressed: int,
) -> Evidence:
    raw = read_bundle(data, max_bytes=max_bytes, max_uncompressed=max_uncompressed)
    chosen, omitted, collapsed = select_lines(raw, alert_times=alert_times, token_budget=token_budget)
    return Evidence(
        bundle_id=bundle_id,
        device_id=device_id,
        inventory=raw.inventory,
        lines=chosen,
        total_lines=len(raw.lines),
        omitted_by_budget=omitted,
        collapsed=collapsed,
        trace_bytes=raw.trace_bytes,
        suspicious_lines=sum(1 for ln in raw.lines if ln.suspicious),
    )
