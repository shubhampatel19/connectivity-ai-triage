"""Configuration from environment variables. Every setting has a default that
works in the platform's compose stack, except DATABASE_URL."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# The default model for each provider. LLM_MODEL overrides it.
DEFAULT_MODELS = {
    "anthropic": "claude-haiku-5-5",
    "ollama": "qwen2.5:3b",
    "fake": "fake-triage-1",
}


def _env(name: str, default: str) -> str:
    v = os.environ.get(name, "")
    return v if v != "" else default


def _int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    return _env(name, "true" if default else "false").lower() in ("1", "true", "yes", "on")


def _default_runbooks_dir() -> str:
    # In the image the runbooks sit at /app/runbooks; in a checkout, next to src/.
    here = Path(__file__).resolve()
    for candidate in (here.parents[2] / "runbooks", Path("/app/runbooks")):
        if candidate.is_dir():
            return str(candidate)
    return "runbooks"


@dataclass(frozen=True)
class Settings:
    database_url: str
    cases_url: str = "http://localhost:8085"
    http_host: str = "0.0.0.0"
    http_port: int = 8086

    s3_endpoint: str = "localhost:8333"
    s3_access_key: str = "connectivity"
    s3_secret_key: str = "connectivity-secret"
    s3_bucket: str = "log-bundles"

    # auto: anthropic when ANTHROPIC_API_KEY is set, else ollama when
    # OLLAMA_URL is set, else fake.
    llm_provider: str = "auto"
    llm_model: str = ""
    anthropic_api_key: str = field(default="", repr=False)
    ollama_url: str = ""
    llm_timeout_s: float = 30.0
    # Thinking counts toward max_tokens, so this is a hard ceiling on what one
    # request can cost, not the size of the note.
    llm_max_tokens: int = 4096
    llm_effort: str = "low"
    llm_max_attempts: int = 4
    llm_retry_base_s: float = 1.0
    llm_concurrency: int = 2
    breaker_failures: int = 5
    breaker_cooldown_s: float = 60.0
    daily_budget_usd: float = 2.0
    fake_llm_faults: str = ""

    poll_interval_s: float = 10.0
    lease_s: float = 300.0
    max_case_attempts: int = 5
    worker_enabled: bool = True

    evidence_token_budget: int = 6000
    max_bundles_per_case: int = 3
    max_bundle_bytes: int = 64 << 20
    max_uncompressed_bytes: int = 256 << 20

    embedder: str = "fastembed"
    embed_model: str = "BAAI/bge-small-en-v1.5"
    runbooks_dir: str = ""
    retrieval_top_k: int = 3

    log_level: str = "info"

    @property
    def provider(self) -> str:
        """The provider actually used, with auto resolved."""
        if self.llm_provider != "auto":
            return self.llm_provider
        if self.anthropic_api_key:
            return "anthropic"
        if self.ollama_url:
            return "ollama"
        return "fake"

    @property
    def model(self) -> str:
        return self.llm_model or DEFAULT_MODELS.get(self.provider, "")

    @classmethod
    def from_env(cls) -> Settings:
        db = os.environ.get("DATABASE_URL", "")
        if not db:
            raise SystemExit("DATABASE_URL is required")
        return cls(
            database_url=db,
            cases_url=_env("CASES_URL", cls.cases_url).rstrip("/"),
            http_host=_env("HTTP_HOST", cls.http_host),
            http_port=_int("HTTP_PORT", cls.http_port),
            s3_endpoint=_env("S3_ENDPOINT", cls.s3_endpoint),
            s3_access_key=_env("S3_ACCESS_KEY", cls.s3_access_key),
            s3_secret_key=_env("S3_SECRET_KEY", cls.s3_secret_key),
            s3_bucket=_env("S3_BUCKET", cls.s3_bucket),
            llm_provider=_env("LLM_PROVIDER", cls.llm_provider).lower(),
            llm_model=_env("LLM_MODEL", ""),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            ollama_url=_env("OLLAMA_URL", "").rstrip("/"),
            llm_timeout_s=_float("LLM_TIMEOUT", cls.llm_timeout_s),
            llm_max_tokens=_int("LLM_MAX_TOKENS", cls.llm_max_tokens),
            llm_effort=_env("LLM_EFFORT", cls.llm_effort),
            llm_max_attempts=_int("LLM_MAX_ATTEMPTS", cls.llm_max_attempts),
            llm_retry_base_s=_float("LLM_RETRY_BASE", cls.llm_retry_base_s),
            llm_concurrency=_int("LLM_CONCURRENCY", cls.llm_concurrency),
            breaker_failures=_int("BREAKER_FAILURES", cls.breaker_failures),
            breaker_cooldown_s=_float("BREAKER_COOLDOWN", cls.breaker_cooldown_s),
            daily_budget_usd=_float("DAILY_BUDGET_USD", cls.daily_budget_usd),
            fake_llm_faults=_env("FAKE_LLM_FAULTS", ""),
            poll_interval_s=_float("POLL_INTERVAL", cls.poll_interval_s),
            lease_s=_float("LEASE", cls.lease_s),
            max_case_attempts=_int("MAX_CASE_ATTEMPTS", cls.max_case_attempts),
            worker_enabled=_bool("WORKER_ENABLED", cls.worker_enabled),
            evidence_token_budget=_int("EVIDENCE_TOKEN_BUDGET", cls.evidence_token_budget),
            max_bundles_per_case=_int("MAX_BUNDLES_PER_CASE", cls.max_bundles_per_case),
            max_bundle_bytes=_int("MAX_BUNDLE_BYTES", cls.max_bundle_bytes),
            max_uncompressed_bytes=_int("MAX_UNCOMPRESSED_BYTES", cls.max_uncompressed_bytes),
            embedder=_env("EMBEDDER", cls.embedder).lower(),
            embed_model=_env("EMBED_MODEL", cls.embed_model),
            runbooks_dir=_env("RUNBOOKS_DIR", _default_runbooks_dir()),
            retrieval_top_k=_int("RETRIEVAL_TOP_K", cls.retrieval_top_k),
            log_level=_env("LOG_LEVEL", cls.log_level).lower(),
        )
