"""Builds the service from settings: one place that knows how the parts fit."""

from __future__ import annotations

from dataclasses import dataclass

from . import embed, runbooks
from .cases import CasesClient
from .config import Settings
from .llm import LLM
from .llm.claude import ClaudeLLM
from .llm.fake import FakeLLM
from .llm.ollama import OllamaLLM
from .llm.resilience import CircuitBreaker
from .logs import log
from .metrics import Metrics
from .objectstore import ObjectStore, S3Store
from .pipeline import Triager
from .retrieval import Retriever
from .store import Store
from .worker import Worker


def make_llm(s: Settings) -> LLM:
    p = s.provider
    if p == "anthropic":
        if not s.anthropic_api_key:
            raise SystemExit("LLM_PROVIDER=anthropic needs ANTHROPIC_API_KEY")
        return ClaudeLLM(s.anthropic_api_key, s.model, timeout_s=s.llm_timeout_s, effort=s.llm_effort)
    if p == "ollama":
        return OllamaLLM(s.ollama_url or "http://localhost:11434", s.model, timeout_s=s.llm_timeout_s)
    if p == "fake":
        return FakeLLM(s.model, s.fake_llm_faults)
    raise SystemExit(f"LLM_PROVIDER must be auto, anthropic, ollama or fake, not {p!r}")


@dataclass
class Service:
    settings: Settings
    store: Store
    cases: CasesClient
    retriever: Retriever
    llm: LLM
    breaker: CircuitBreaker
    triager: Triager
    worker: Worker
    metrics: Metrics

    @classmethod
    async def start(
        cls,
        s: Settings,
        *,
        metrics: Metrics | None = None,
        objects: ObjectStore | None = None,
        cases: CasesClient | None = None,
        llm: LLM | None = None,
        embedder: embed.Embedder | None = None,
    ) -> Service:
        m = metrics or Metrics()
        store = await Store.open(s.database_url)
        books = runbooks.load(s.runbooks_dir)
        retriever = Retriever(store, embedder or embed.make(s.embedder, s.embed_model), books, m)
        indexed = await retriever.index()
        log.info(
            "runbooks indexed", extra={**indexed, "runbooks": len(books), "embedder": retriever.embedder.name}
        )
        llm = llm or make_llm(s)
        log.info("model", extra={"provider": llm.provider, "model": llm.model})
        breaker = CircuitBreaker(s.breaker_failures, s.breaker_cooldown_s)
        cases = cases or CasesClient(s.cases_url)
        objects = objects or S3Store(s.s3_endpoint, s.s3_access_key, s.s3_secret_key, s.s3_bucket)
        triager = Triager(s, store, cases, objects, retriever, llm, breaker, m)
        worker = Worker(s, store, cases, triager, m)
        return cls(s, store, cases, retriever, llm, breaker, triager, worker, m)

    async def close(self) -> None:
        self.worker.stop()
        await self.cases.close()
        await self.store.close()
