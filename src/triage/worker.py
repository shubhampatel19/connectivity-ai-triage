"""The reconcile loop: every POLL_INTERVAL, list the open cases, work out
which need a note, and triage them a few at a time.

Polling the case API instead of consuming an event stream keeps this service
decoupled from the case service's internals, and makes it self-healing: a
crash, a restart or a missed event just means the next pass picks the case
up. Leases in Postgres keep replicas from triaging the same case twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import time
from datetime import UTC, datetime
from typing import Any

from .cases import CasesClient
from .config import Settings
from .logs import log
from .metrics import Metrics
from .pipeline import Triager, parse_time
from .store import Store

# Cases worth a note: those whose logs have arrived, and those that will never
# get any (an alert in its device's bundle cooldown), which get a note from
# their alerts alone. "waiting" means a bundle is on its way: worth waiting
# for rather than paying for a note twice.
TRIAGE_LOGS = ("attached", "none")


def needs_triage(case: dict[str, Any], note: dict[str, Any] | None, max_attempts: int, now: datetime) -> bool:
    if case.get("logs") not in TRIAGE_LOGS:
        return False
    if note is None:
        return True
    changed = parse_time(case.get("last_alert_at")) != note["case_last_alert"]
    if note["status"] == "done":
        # New alerts may mean new bundles. Fetching the case tells; if its
        # bundles are the same, the triage is a cheap "unchanged".
        return changed
    due = note["next_attempt_at"] is None or note["next_attempt_at"] <= now
    return (note["attempts"] < max_attempts and due) or changed


class Worker:
    def __init__(
        self, settings: Settings, store: Store, cases: CasesClient, triager: Triager, metrics: Metrics
    ) -> None:
        self.s = settings
        self.store = store
        self.cases = cases
        self.triager = triager
        self.m = metrics
        self.owner = f"{socket.gethostname()}-{os.getpid()}"
        self.sem = asyncio.Semaphore(settings.llm_concurrency)
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        log.info("worker started", extra={"owner": self.owner, "poll_interval_s": self.s.poll_interval_s})
        while not self._stop.is_set():
            try:
                await self.reconcile_once()
            except Exception as e:  # keep the loop alive; the next pass retries
                log.error("reconcile failed", extra={"err": f"{type(e).__name__}: {e}"})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.s.poll_interval_s)

    async def reconcile_once(self) -> list[int]:
        cases = await self.cases.list_open()
        self.m.reconcile_last_success.set(time.time())
        with_logs = [c for c in cases if c.get("logs") in TRIAGE_LOGS]
        notes = await self.store.note_index([c["case_id"] for c in with_logs])
        now = datetime.now(UTC)
        todo = [
            c["case_id"]
            for c in with_logs
            if needs_triage(c, notes.get(c["case_id"]), self.s.max_case_attempts, now)
        ]
        self.m.pending.set(len(todo))
        if not todo:
            return []
        done: list[int] = []
        async with asyncio.TaskGroup() as tg:
            for case_id in todo:
                tg.create_task(self._one(case_id, done))
        return done

    async def _one(self, case_id: int, done: list[int]) -> None:
        async with self.sem:
            if self._stop.is_set() or not await self.store.claim(case_id, self.owner, self.s.lease_s):
                return
            try:
                res = await self.triager.triage(case_id)
                if res.status in ("done", "failed", "unchanged"):
                    done.append(case_id)
            except Exception as e:
                log.error("triage crashed", extra={"case_id": case_id, "err": f"{type(e).__name__}: {e}"})
            finally:
                await self.store.release(case_id, self.owner)
