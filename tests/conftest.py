"""Shared fixtures. Tests that need Postgres (with pgvector) use
TRIAGE_TEST_DATABASE_URL and skip without it; CI sets it and fails if any
skipped. Each one starts from an empty triage schema."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import psycopg
import pytest

from triage import runbooks
from triage.config import Settings
from triage.embed import HashEmbedder
from triage.evals import dataset
from triage.evals.run import StaticCases
from triage.llm.fake import FakeLLM
from triage.llm.resilience import CircuitBreaker
from triage.metrics import Metrics
from triage.objectstore import MemoryStore
from triage.pipeline import Triager
from triage.retrieval import Retriever
from triage.store import Store

ROOT = Path(__file__).resolve().parents[1]
DSN = os.environ.get("TRIAGE_TEST_DATABASE_URL", "")


@pytest.fixture(scope="session")
def cases() -> dict[str, dataset.EvalCase]:
    return {c.id: c for c in dataset.load(ROOT / "evals" / "cases")}


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url=DSN,
        llm_provider="fake",
        runbooks_dir=str(ROOT / "runbooks"),
        embedder="hash",
        llm_retry_base_s=0.001,
        daily_budget_usd=5.0,
    )


@pytest.fixture
async def store():
    if not DSN:
        pytest.skip("TRIAGE_TEST_DATABASE_URL not set")
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as c:
        await c.execute("DROP SCHEMA IF EXISTS triage CASCADE")
        await c.execute("DROP TABLE IF EXISTS public.triage_migrations")
    s = await Store.open(DSN)
    yield s
    await s.close()


@dataclasses.dataclass
class Rig:
    settings: Settings
    store: Store
    retriever: Retriever
    llm: FakeLLM
    breaker: CircuitBreaker
    cases: StaticCases
    objects: MemoryStore
    metrics: Metrics
    triager: Triager

    def add(self, ec: dataset.EvalCase) -> int:
        self.cases.cases[ec.case["case_id"]] = ec.case
        self.objects.objects.update(ec.objects)
        return ec.case["case_id"]


@pytest.fixture
async def rig(store, settings) -> Rig:
    m = Metrics()
    retriever = Retriever(store, HashEmbedder(), runbooks.load(settings.runbooks_dir), m)
    await retriever.index()
    llm = FakeLLM()
    breaker = CircuitBreaker(settings.breaker_failures, settings.breaker_cooldown_s)
    cases_api, objects = StaticCases({}), MemoryStore()
    triager = Triager(settings, store, cases_api, objects, retriever, llm, breaker, m)  # type: ignore[arg-type]
    return Rig(settings, store, retriever, llm, breaker, cases_api, objects, m, triager)
