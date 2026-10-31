"""Postgres: migrations, triage notes, leases, the spend ledger, and the
runbook index with hybrid (vector + keyword + alert code) search."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from importlib import resources
from typing import Any

import psycopg
from pgvector.psycopg import register_vector_async
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .runbooks import Chunk

MIGRATION_LOCK = 0x7472_6961_6765  # "triage"
STOPWORDS = set(
    "the a an and or of to in on at for with by is are was were be been it its this that from as not no "
    "case device devices alert alerts over under".split()
)


async def migrate(dsn: str) -> None:
    """Applies migrations in order under an advisory lock, so replicas starting
    together take turns. pgvector's types must exist before a pooled
    connection can register them, which is why this runs on its own
    connection first."""
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK,))
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS public.triage_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())"
            )
            done = {
                r[0]
                for r in await (await conn.execute("SELECT name FROM public.triage_migrations")).fetchall()
            }
            files = sorted(
                f for f in resources.files("triage.migrations").iterdir() if f.name.endswith(".sql")
            )
            for f in files:
                if f.name in done:
                    continue
                async with conn.transaction():
                    await conn.execute(f.read_text())
                    await conn.execute("INSERT INTO public.triage_migrations (name) VALUES (%s)", (f.name,))
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK,))


@dataclass
class SearchHit:
    chunk_id: str
    runbook_id: str
    title: str
    section: str
    content: str
    score: float

    @property
    def text(self) -> str:
        return f"{self.title}: {self.section}\n{self.content}"


def tsquery(text: str, limit: int = 24) -> str:
    """An OR query over the distinctive words in text. to_tsquery's AND would
    demand every word appear in one chunk, which a long query never does."""
    words: list[str] = []
    for w in re.findall(r"[a-z][a-z0-9]{2,}", text.lower()):
        if w not in STOPWORDS and w not in words:
            words.append(w)
    return " | ".join(words[:limit])


class Store:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    @classmethod
    async def open(cls, dsn: str, *, min_size: int = 1, max_size: int = 8) -> Store:
        await migrate(dsn)

        async def configure(conn: psycopg.AsyncConnection) -> None:
            await register_vector_async(conn)
            await conn.set_autocommit(True)

        pool = AsyncConnectionPool(dsn, min_size=min_size, max_size=max_size, configure=configure, open=False)
        await pool.open(wait=True, timeout=30)
        return cls(pool)

    async def close(self) -> None:
        await self.pool.close()

    async def ping(self) -> None:
        async with self.pool.connection() as c:
            await c.execute("SELECT 1")

    # ---------- leases ----------

    async def claim(self, case_id: int, owner: str, lease_s: float) -> bool:
        """Takes the case's lease if it's free or expired. Atomic: of two
        replicas racing, exactly one gets a row back."""
        async with self.pool.connection() as c:
            row = await (
                await c.execute(
                    """INSERT INTO triage.leases (case_id, owner, lease_until)
                       VALUES (%s, %s, now() + make_interval(secs => %s))
                       ON CONFLICT (case_id) DO UPDATE
                         SET owner = EXCLUDED.owner, lease_until = EXCLUDED.lease_until
                         WHERE triage.leases.lease_until < now() OR triage.leases.owner = EXCLUDED.owner
                       RETURNING owner""",
                    (case_id, owner, lease_s),
                )
            ).fetchone()
            return row is not None

    async def release(self, case_id: int, owner: str) -> None:
        async with self.pool.connection() as c:
            await c.execute("DELETE FROM triage.leases WHERE case_id = %s AND owner = %s", (case_id, owner))

    # ---------- notes ----------

    # Row factories go on the cursor, never the connection: a pooled
    # connection would keep a dict row factory for whoever borrows it next.
    async def _one(self, sql: str, params: tuple) -> dict[str, Any] | None:
        async with self.pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchone()

    async def _all(self, sql: str, params: tuple) -> list[dict[str, Any]]:
        async with self.pool.connection() as c, c.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()

    async def get_note(self, case_id: int) -> dict[str, Any] | None:
        return await self._one("SELECT * FROM triage.notes WHERE case_id = %s", (case_id,))

    async def note_index(self, case_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Status, input hash and retry schedule for many cases at once."""
        if not case_ids:
            return {}
        rows = await self._all(
            """SELECT case_id, status, input_hash, case_last_alert, attempts, next_attempt_at
               FROM triage.notes WHERE case_id = ANY(%s)""",
            (case_ids,),
        )
        return {r["case_id"]: r for r in rows}

    async def list_notes(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self._all(
            """SELECT case_id, status, note->>'category' AS category, note->>'confidence' AS confidence,
                      note->>'summary' AS summary, model, cost_usd, latency_ms, error, updated_at
               FROM triage.notes ORDER BY updated_at DESC LIMIT %s""",
            (limit,),
        )

    async def save_note(self, **f: Any) -> None:
        f.setdefault("note", None)
        f.setdefault("checks", {})
        f.setdefault("retrieved", [])
        f.setdefault("bundle_ids", [])
        f.setdefault("error", "")
        f.setdefault("next_attempt_at", None)
        f.setdefault("trace_id", "")
        f.setdefault("case_last_alert", None)
        for k in ("input_tokens", "output_tokens", "latency_ms", "attempts"):
            f.setdefault(k, 0)
        f.setdefault("cost_usd", Decimal(0))
        f["note"] = Jsonb(f["note"]) if f["note"] is not None else None
        f["checks"] = Jsonb(f["checks"])
        async with self.pool.connection() as c:
            await c.execute(
                """INSERT INTO triage.notes (case_id, status, input_hash, case_last_alert, bundle_ids, note, checks,
                       retrieved, provider, model, prompt_version, input_tokens, output_tokens, cost_usd, latency_ms,
                       attempts, error, next_attempt_at, trace_id)
                   VALUES (%(case_id)s, %(status)s, %(input_hash)s, %(case_last_alert)s, %(bundle_ids)s, %(note)s,
                       %(checks)s, %(retrieved)s, %(provider)s, %(model)s, %(prompt_version)s, %(input_tokens)s,
                       %(output_tokens)s, %(cost_usd)s, %(latency_ms)s, %(attempts)s, %(error)s,
                       %(next_attempt_at)s, %(trace_id)s)
                   ON CONFLICT (case_id) DO UPDATE SET
                       status = EXCLUDED.status, input_hash = EXCLUDED.input_hash,
                       case_last_alert = EXCLUDED.case_last_alert, bundle_ids = EXCLUDED.bundle_ids,
                       note = EXCLUDED.note, checks = EXCLUDED.checks, retrieved = EXCLUDED.retrieved,
                       provider = EXCLUDED.provider, model = EXCLUDED.model,
                       prompt_version = EXCLUDED.prompt_version, input_tokens = EXCLUDED.input_tokens,
                       output_tokens = EXCLUDED.output_tokens, cost_usd = EXCLUDED.cost_usd,
                       latency_ms = EXCLUDED.latency_ms, attempts = EXCLUDED.attempts, error = EXCLUDED.error,
                       next_attempt_at = EXCLUDED.next_attempt_at, trace_id = EXCLUDED.trace_id,
                       updated_at = now()""",
                f,
            )

    async def touch_note(self, case_id: int, case_last_alert: datetime) -> None:
        """Records that a case changed (new alerts) without needing a new note."""
        async with self.pool.connection() as c:
            await c.execute(
                "UPDATE triage.notes SET case_last_alert = %s WHERE case_id = %s", (case_last_alert, case_id)
            )

    async def forget(self, case_id: int) -> None:
        async with self.pool.connection() as c:
            await c.execute("DELETE FROM triage.notes WHERE case_id = %s", (case_id,))

    # ---------- spend ----------

    async def record_call(self, **f: Any) -> None:
        async with self.pool.connection() as c:
            await c.execute(
                """INSERT INTO triage.llm_calls (case_id, provider, model, outcome, input_tokens, output_tokens,
                       cost_usd, latency_ms)
                   VALUES (%(case_id)s, %(provider)s, %(model)s, %(outcome)s, %(input_tokens)s, %(output_tokens)s,
                       %(cost_usd)s, %(latency_ms)s)""",
                f,
            )

    async def spent_24h(self) -> float:
        async with self.pool.connection() as c:
            row = await (
                await c.execute(
                    "SELECT coalesce(sum(cost_usd), 0) FROM triage.llm_calls WHERE at > now() - interval '24 hours'"
                )
            ).fetchone()
            return float(row[0]) if row else 0.0

    # ---------- runbook index ----------

    async def sync_chunks(self, chunks: list[Chunk], embedder_name: str, embed) -> dict[str, int]:
        """Brings the index in line with the runbooks on disk. Only chunks whose
        text or embedder changed are embedded again; chunks no longer on disk
        are deleted. `embed` takes a list of texts and returns vectors."""
        async with self.pool.connection() as c:
            existing = {
                r[0]: (r[1], r[2])
                for r in await (
                    await c.execute("SELECT chunk_id, content_hash, embedder FROM triage.runbook_chunks")
                ).fetchall()
            }
            stale = [ch for ch in chunks if existing.get(ch.chunk_id) != (ch.content_hash, embedder_name)]
            vectors = await embed([ch.text for ch in stale]) if stale else []
            async with c.transaction():
                for ch, vec in zip(stale, vectors, strict=True):
                    await c.execute(
                        """INSERT INTO triage.runbook_chunks (chunk_id, runbook_id, title, section, content,
                               alert_codes, content_hash, embedder, embedding)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (chunk_id) DO UPDATE SET runbook_id = EXCLUDED.runbook_id,
                               title = EXCLUDED.title, section = EXCLUDED.section, content = EXCLUDED.content,
                               alert_codes = EXCLUDED.alert_codes, content_hash = EXCLUDED.content_hash,
                               embedder = EXCLUDED.embedder, embedding = EXCLUDED.embedding, indexed_at = now()""",
                        (
                            ch.chunk_id,
                            ch.runbook_id,
                            ch.title,
                            ch.section,
                            ch.content,
                            list(ch.alert_codes),
                            ch.content_hash,
                            embedder_name,
                            _vector(vec),
                        ),
                    )
                keep = [ch.chunk_id for ch in chunks]
                gone = await c.execute(
                    "DELETE FROM triage.runbook_chunks WHERE NOT (chunk_id = ANY(%s))", (keep,)
                )
            return {"embedded": len(stale), "deleted": gone.rowcount, "total": len(chunks)}

    async def search(
        self,
        query_vec: list[float],
        query_text: str,
        alert_codes: list[str],
        embedder_name: str,
        limit: int = 50,
    ) -> list[SearchHit]:
        """Hybrid search, fused with reciprocal rank fusion: each signal
        contributes 1/(60 + rank). Vectors catch paraphrase ("fan stopped" vs
        "tachometer reads zero"), keywords catch exact terms (part numbers,
        "12V rail"), and a matching alert code is a weak prior that ranks
        relevant runbooks up without deciding between them."""
        sql = """
            WITH v AS (
                SELECT chunk_id, row_number() OVER (ORDER BY embedding <=> %(qv)s) AS r
                FROM triage.runbook_chunks WHERE embedder = %(emb)s
                ORDER BY embedding <=> %(qv)s LIMIT 50),
            k AS (
                SELECT chunk_id, row_number() OVER (ORDER BY ts_rank_cd(tsv, q) DESC) AS r
                FROM triage.runbook_chunks, to_tsquery('english', %(tq)s) q
                WHERE %(tq)s <> '' AND tsv @@ q
                ORDER BY ts_rank_cd(tsv, q) DESC LIMIT 50),
            m AS (
                SELECT chunk_id FROM triage.runbook_chunks WHERE alert_codes && %(codes)s::text[])
            SELECT c.chunk_id, c.runbook_id, c.title, c.section, c.content,
                   coalesce(1.0 / (60 + v.r), 0) + coalesce(1.0 / (60 + k.r), 0)
                   + CASE WHEN m.chunk_id IS NULL THEN 0 ELSE 0.5 / 61 END AS score
            FROM triage.runbook_chunks c
            LEFT JOIN v USING (chunk_id) LEFT JOIN k USING (chunk_id) LEFT JOIN m USING (chunk_id)
            WHERE v.chunk_id IS NOT NULL OR k.chunk_id IS NOT NULL
            ORDER BY score DESC, c.chunk_id LIMIT %(limit)s"""
        async with self.pool.connection() as c:
            rows = await (
                await c.execute(
                    sql,
                    {
                        "qv": _vector(query_vec),
                        "tq": tsquery(query_text),
                        "codes": alert_codes,
                        "emb": embedder_name,
                        "limit": limit,
                    },
                )
            ).fetchall()
        return [SearchHit(r[0], r[1], r[2], r[3], r[4], float(r[5])) for r in rows]

    async def chunk_count(self) -> int:
        async with self.pool.connection() as c:
            row = await (await c.execute("SELECT count(*) FROM triage.runbook_chunks")).fetchone()
            return int(row[0]) if row else 0


def _vector(v: list[float]):
    import numpy as np

    return np.asarray(v, dtype=np.float32)
