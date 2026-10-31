import asyncio
from datetime import UTC, datetime, timedelta

from triage.worker import Worker, needs_triage

NOW = datetime(2026, 10, 30, tzinfo=UTC)
T1 = "2026-10-30T00:00:00Z"


def note(**kw):
    base = {
        "status": "done",
        "case_last_alert": datetime(2026, 10, 30, tzinfo=UTC),
        "attempts": 1,
        "next_attempt_at": None,
    }
    base.update(kw)
    return base


def test_needs_triage():
    case = {"logs": "attached", "last_alert_at": T1}
    assert needs_triage(case, None, 5, NOW)
    assert not needs_triage({**case, "logs": "waiting"}, None, 5, NOW), "a bundle is on its way: wait for it"
    assert needs_triage({**case, "logs": "none"}, None, 5, NOW), "no bundle is coming: triage the alerts"
    assert not needs_triage(case, note(), 5, NOW), "done and unchanged"
    assert needs_triage({**case, "last_alert_at": "2026-10-30T01:00:00Z"}, note(), 5, NOW), "new alerts"
    assert not needs_triage(case, note(status="failed", next_attempt_at=NOW + timedelta(minutes=5)), 5, NOW)
    assert needs_triage(case, note(status="failed", next_attempt_at=NOW - timedelta(minutes=5)), 5, NOW)
    assert not needs_triage(case, note(status="failed", attempts=5), 5, NOW), "out of attempts"
    assert needs_triage(
        {**case, "last_alert_at": "2026-10-30T02:00:00Z"}, note(status="failed", attempts=5), 5, NOW
    )


async def test_two_workers_triage_each_case_once(rig, cases):
    ids = [rig.add(c) for c in list(cases.values())[:6]]
    for cid in ids:
        rig.cases.cases[cid]["logs"] = "attached" if rig.cases.cases[cid]["bundles"] else "none"
    a = Worker(rig.settings, rig.store, rig.cases, rig.triager, rig.metrics)
    b = Worker(rig.settings, rig.store, rig.cases, rig.triager, rig.metrics)
    b.owner = a.owner + "-b"
    await asyncio.gather(a.reconcile_once(), b.reconcile_once())
    async with rig.store.pool.connection() as c:
        per_case = await (
            await c.execute("SELECT case_id, count(*) FROM triage.llm_calls GROUP BY case_id")
        ).fetchall()
        leases = await (await c.execute("SELECT count(*) FROM triage.leases")).fetchone()
    assert sorted(r[0] for r in per_case) == sorted(ids)
    assert all(r[1] == 1 for r in per_case), f"a case was paid for twice: {per_case}"
    assert leases[0] == 0, "leases are released after each case"
    # Nothing changed, so the next pass has nothing to do.
    assert await a.reconcile_once() == []


async def test_lease_is_exclusive_until_it_expires(rig):
    assert await rig.store.claim(1, "a", 60)
    assert not await rig.store.claim(1, "b", 60)
    assert await rig.store.claim(1, "a", 60), "the holder can renew"
    assert await rig.store.claim(2, "b", 0.0)
    await asyncio.sleep(0.05)
    assert await rig.store.claim(2, "a", 60), "an expired lease can be taken"
