"""Runbook retrieval: index the runbooks, turn a case into a search query,
and return the best few runbooks with their most relevant sections."""

from __future__ import annotations

import asyncio
import re
import time

from .bundle import Evidence, template
from .embed import Embedder
from .metrics import Metrics
from .runbooks import Runbook
from .store import SearchHit, Store
from .tracing import tracer

TS_PREFIX = re.compile(r"^\S+Z\s+\w+\s+")


class Retriever:
    def __init__(self, store: Store, embedder: Embedder, runbooks: list[Runbook], metrics: Metrics) -> None:
        self.store = store
        self.embedder = embedder
        self.runbooks = {rb.id: rb for rb in runbooks}
        self.metrics = metrics

    @property
    def runbook_ids(self) -> list[str]:
        return sorted(self.runbooks)

    async def index(self) -> dict[str, int]:
        chunks = [c for rb in self.runbooks.values() for c in rb.chunks]

        async def embed(texts: list[str]) -> list[list[float]]:
            return await asyncio.to_thread(self.embedder.embed_documents, texts)

        result = await self.store.sync_chunks(chunks, self.embedder.name, embed)
        self.metrics.runbook_chunks.set(result["total"])
        return result

    async def search(
        self, query: str, alert_codes: list[str], *, books: int = 3, per_book: int = 2, max_books: int = 6
    ) -> list[SearchHit]:
        """Runbook sections for a case, best runbook first.

        Two sources, merged. Every runbook tagged with one of the case's alert
        codes is included: together they're the differential, the causes this
        alert can have, and leaving one out because its wording ranked low
        means the model can't pick the cause it describes. Then the top
        `books` runbooks by hybrid search, which brings in anything else the
        logs touch on. Hybrid ranking orders the whole set and picks each
        runbook's most relevant sections.
        """
        with tracer.start_as_current_span("retrieve runbooks") as span:
            start = time.monotonic()
            vec = await asyncio.to_thread(self.embedder.embed_query, query)
            hits = await self.store.search(vec, query, alert_codes, self.embedder.name)
            by_book: dict[str, list[SearchHit]] = {}
            for h in hits:  # best first
                by_book.setdefault(h.runbook_id, []).append(h)
            ranked = list(by_book)
            chosen = ranked[:books]
            codes = set(alert_codes)
            for rid in ranked + sorted(self.runbooks):
                if len(chosen) >= max_books:
                    break
                if rid not in chosen and rid in self.runbooks and codes & set(self.runbooks[rid].alert_codes):
                    chosen.append(rid)
            chosen.sort(key=lambda r: ranked.index(r) if r in ranked else len(ranked))
            out: list[SearchHit] = []
            for rid in chosen:
                found = by_book.get(rid, [])[:per_book]
                if not found:  # tagged, but no section matched: its evidence guide stands in
                    rb = self.runbooks[rid]
                    guide = [c for c in rb.chunks if c.section == "Reading the evidence"] or rb.chunks[:1]
                    found = [SearchHit(c.chunk_id, rid, c.title, c.section, c.content, 0.0) for c in guide]
                out.extend(found)
            self.metrics.retrieval_duration.observe(time.monotonic() - start)
            span.set_attribute("retrieval.runbooks", chosen)
            return out


def query_for(case: dict, evidence: list[Evidence], max_lines: int = 8) -> str:
    """The search query for a case: what it's about, what the alerts said, and
    the device's own warnings and errors.

    The lines come from the end of the log backwards, one per kind of message:
    a bundle is collected when the alert fires, so its last lines are the
    fault, while its first lines are whatever routine noise came hours
    before. Without the dedup, thirty "cpu_temp=... over 80C" lines would
    crowd out the one BMC line that names the cause.
    """
    parts = [str(case.get("title", "")), str(case.get("rule_id", ""))]
    if case.get("scope") == "site":
        parts.append(f"many devices at one site ({case.get('devices', 0)} devices)")
    seen: set[str] = set()
    for a in case.get("alerts") or []:
        for s in (a.get("code"), a.get("message")):
            if s and s not in seen:
                seen.add(s)
                parts.append(str(s))
        if len(seen) >= 6:
            break
    picked: list[str] = []
    templates: set[str] = set()
    for ev in evidence:
        for ln in reversed(ev.lines):
            if ln.level not in ("WARN", "ERROR") or ln.suspicious:
                continue
            t = template(ln)
            if t in templates:
                continue
            templates.add(t)
            picked.append(TS_PREFIX.sub("", ln.text))
            if len(picked) >= max_lines * (evidence.index(ev) + 1) // len(evidence):
                break
    parts.extend(picked[:max_lines])
    return "\n".join(p for p in parts if p)
