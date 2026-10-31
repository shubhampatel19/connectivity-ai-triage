"""Runs the eval suite.

    python -m triage eval [--cases evals/cases] [--provider fake|anthropic|ollama] [--repeat N]

Needs DATABASE_URL pointing at a scratch database: the run writes notes,
spend and a runbook index into its "triage" schema, and must not share them
with a live service (case IDs would collide, and eval spend would count
against the service's budget).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from .. import embed, runbooks
from ..config import Settings
from ..llm.resilience import CircuitBreaker
from ..metrics import Metrics
from ..objectstore import MemoryStore
from ..pipeline import Triager
from ..prompt import PROMPT_VERSION
from ..retrieval import Retriever
from ..service import make_llm
from ..store import Store
from . import dataset, score

REPO = Path(__file__).resolve().parents[3]
# In a checkout the cases sit in evals/ at the repo root; the image sets
# EVALS_DIR, and EVALS_OUT to somewhere writable.
EVALS = Path(os.environ.get("EVALS_DIR") or REPO / "evals")


class StaticCases:
    """The case API, answered from the dataset."""

    def __init__(self, cases: dict[int, dict[str, Any]]) -> None:
        self.cases = cases

    async def get(self, case_id: int) -> dict[str, Any]:
        return self.cases[case_id]

    async def list_open(self, limit: int = 500) -> list[dict[str, Any]]:
        return list(self.cases.values())[:limit]

    async def close(self) -> None:
        pass


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m triage eval")
    p.add_argument("--cases", default=str(EVALS / "cases"))
    p.add_argument("--provider", default=os.environ.get("LLM_PROVIDER", "fake"))
    p.add_argument("--model", default=os.environ.get("LLM_MODEL", ""))
    p.add_argument("--embedder", default=os.environ.get("EMBEDDER", "fastembed"))
    p.add_argument("--repeat", type=int, default=1, help="runs per case: real models aren't deterministic")
    p.add_argument("--only", default="", help="run cases whose id contains this")
    p.add_argument("--no-variants", action="store_true", help="skip the derived no-logs and log-storm cases")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--thresholds", default=str(EVALS / "thresholds.yaml"))
    p.add_argument("--out", default=os.environ.get("EVALS_OUT") or str(EVALS / "results"))
    return p.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    base = dict(os.environ)
    base.setdefault("DATABASE_URL", "")
    if not base["DATABASE_URL"]:
        print("DATABASE_URL must point at a scratch database for evals", file=sys.stderr)
        return 2
    os.environ["LLM_PROVIDER"] = args.provider
    if args.model:
        os.environ["LLM_MODEL"] = args.model
    s = Settings.from_env()
    s = dataclasses.replace(
        s,
        embedder=args.embedder,
        # Evals have their own budget, and need no worker or case API.
        daily_budget_usd=float(os.environ.get("EVAL_BUDGET_USD", "5")),
        worker_enabled=False,
    )
    cases = dataset.load(args.cases, variants=not args.no_variants)
    if args.only:
        cases = [c for c in cases if args.only in c.id]
    if not cases:
        print("no cases selected", file=sys.stderr)
        return 2

    m = Metrics()
    store = await Store.open(s.database_url)
    try:
        books = runbooks.load(s.runbooks_dir)
        retriever = Retriever(store, embed.make(s.embedder, s.embed_model), books, m)
        await retriever.index()
        llm = make_llm(s)
        objects = MemoryStore({k: v for c in cases for k, v in c.objects.items()})
        cases_api = StaticCases({c.case["case_id"]: c.case for c in cases})
        # A generous breaker: one flaky stretch shouldn't fail the rest of the run.
        breaker = CircuitBreaker(failures=20, cooldown_s=30)
        triager = Triager(s, store, cases_api, objects, retriever, llm, breaker, m)  # type: ignore[arg-type]

        sem = asyncio.Semaphore(args.concurrency)
        results: list[score.Graded] = []
        started = time.monotonic()

        async def one(ec: dataset.EvalCase) -> None:
            async with sem:
                for _ in range(args.repeat):
                    await store.forget(ec.case["case_id"])
                    await triager.triage(ec.case["case_id"], force=True)
                    g = score.grade(ec, await store.get_note(ec.case["case_id"]))
                    results.append(g)
                    mark = "pass" if g.passed else "FAIL"
                    print(f"{mark}  {ec.id:<40} {g.got:<22} {'; '.join(g.problems)[:150]}", flush=True)

        await asyncio.gather(*(one(c) for c in cases))
        elapsed = time.monotonic() - started
    finally:
        await store.close()

    results.sort(key=lambda r: r.case)
    summary = score.summarize(results)
    generators = sorted({c.label.get("generator", "?") for c in cases})
    meta = {
        "provider": llm.provider,
        "model": llm.model,
        "embedder": retriever.embedder.name,
        "prompt_version": PROMPT_VERSION,
        "cases": len(cases),
        "repeat": args.repeat,
        "generator": ", ".join(generators),
        "elapsed_s": round(elapsed, 1),
    }
    thresholds = _thresholds(args.thresholds, llm.provider, retriever.embedder.name)
    failures = score.check_thresholds(summary, thresholds)
    report = score.markdown(summary, results, meta, failures)
    print("\n" + report)

    payload = {
        "meta": meta,
        "summary": summary,
        "thresholds": thresholds,
        "failures": failures,
        "results": [dataclasses.asdict(r) for r in results],
    }
    await asyncio.to_thread(_write, Path(args.out), llm.provider, report, payload)
    if os.environ.get("GITHUB_ACTIONS"):
        _annotate(summary, results, meta, failures)
    return 1 if failures else 0


def _write(out: Path, provider: str, report: str, payload: dict[str, Any]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    (out / f"{stamp}-{provider}.json").write_text(json.dumps(payload, indent=1))
    (out / f"{stamp}-{provider}.md").write_text(report)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a") as f:
            f.write(report)


def _annotate(
    summary: dict[str, Any], results: list[score.Graded], meta: dict[str, Any], failures: list[str]
) -> None:
    """The headline numbers as a CI annotation: shown on the commit's checks
    and readable through the API, unlike the job summary and raw logs."""

    def pct(k: str) -> str:
        return f"{summary[k]:.0%}"

    head = (
        f"{summary['cases']} runs: pass {pct('pass_rate')}, category {pct('category_accuracy')}, "
        f"runbook {pct('runbook_accuracy')}, retrieval {pct('retrieval_recall')} (top-1 {pct('retrieval_top1')}), "
        f"grounded {pct('grounded_rate')}, injection {pct('injection_accuracy')}, safety {pct('safety')}; "
        f"p95 {summary['latency_p95_ms']} ms, largest prompt {summary['max_input_tokens']} tokens, ${summary['cost_usd']:.4f}"
    )

    # Workflow commands treat % as an escape in the message, and , and : as
    # separators in properties such as the title.
    def data(v: str) -> str:
        return v.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")

    def prop(v: str) -> str:
        return data(v).replace(":", "%3A").replace(",", "%2C")

    title = prop(f"evals {meta['provider']}/{meta['model']}, {meta['embedder']}")
    if failures:
        print(f"::error title={title}::{data(head)} | below threshold: {data('; '.join(failures))}")
    else:
        print(f"::notice title={title}::{data(head)}")
    for r in [r for r in results if not r.passed][:10]:
        print(f"::warning title={prop('eval ' + r.case)}::{data('; '.join(r.problems)[:900])}")


def _thresholds(path: str, provider: str, embedder: str) -> dict[str, float]:
    if not Path(path).exists():
        return {}
    data = yaml.safe_load(Path(path).read_text()) or {}
    t = dict(data.get("providers", {}).get(provider, {}))
    kind = embedder.split(":", 1)[0].split("-", 1)[0]
    t.update(data.get("retrieval", {}).get(kind, {}))
    return {k: float(v) for k, v in t.items()}


def main(argv: list[str]) -> int:
    from .. import logs

    logs.setup(os.environ.get("LOG_LEVEL", "warn"))
    return asyncio.run(run(parse_args(argv)))
