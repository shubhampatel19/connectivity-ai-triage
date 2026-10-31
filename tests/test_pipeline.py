"""The triage pipeline end to end, with real generated cases, the fake model,
and Postgres."""

import dataclasses
import json

from triage.llm import Response, Usage
from triage.llm.fake import triage as fake_triage


async def calls(rig, outcome=None) -> int:
    async with rig.store.pool.connection() as c:
        q = "SELECT count(*) FROM triage.llm_calls" + (" WHERE outcome = %s" if outcome else "")
        row = await (await c.execute(q, (outcome,) if outcome else ())).fetchone()
        return row[0]


async def test_triages_a_case_into_a_grounded_note(rig, cases):
    cid = rig.add(cases["thermal-fan_failure-01"])
    res = await rig.triager.triage(cid)
    assert res.status == "done" and res.category == "fan_failure"
    row = await rig.store.get_note(cid)
    assert row["checks"]["grounded"], row["checks"]
    assert row["retrieved"][0] == "thermal-fan-failure" or "thermal-fan-failure" in row["retrieved"]
    assert row["checks"]["evidence_selection"][0]["lines_shown"] > 0
    assert float(row["cost_usd"]) > 0 and row["input_tokens"] > 0
    assert await calls(rig, "ok") == 1


async def test_unchanged_case_costs_nothing_new_bundles_do(rig, cases):
    ec = cases["psu-psu_failing-01"]
    cid = rig.add(ec)
    await rig.triager.triage(cid)
    # More alerts on the case, same bundles: no new call.
    rig.cases.cases[cid] = {**ec.case, "alert_count": 9, "last_alert_at": "2026-10-03T00:00:00Z"}
    assert (await rig.triager.triage(cid)).status == "unchanged"
    assert await calls(rig) == 1
    note = await rig.store.get_note(cid)
    assert note["case_last_alert"].isoformat().startswith("2026-10-03"), "the new alert time is recorded"
    # A new bundle: a new note.
    extra = dict(ec.case["bundles"][0], bundle_id="b-new", uploaded_at="2026-10-03T00:00:00Z")
    rig.cases.cases[cid] = {**rig.cases.cases[cid], "bundles": [*ec.case["bundles"], extra]}
    assert (await rig.triager.triage(cid)).status == "done"
    assert await calls(rig) == 2


async def test_budget_exhausted_skips_without_calling(rig, cases):
    rig.triager.s = dataclasses.replace(rig.settings, daily_budget_usd=0.0)
    cid = rig.add(cases["disk-disk_wear-01"])
    res = await rig.triager.triage(cid)
    assert res.status == "skipped_budget"
    note = await rig.store.get_note(cid)
    assert note["status"] == "skipped" and "budget" in note["error"] and note["next_attempt_at"] is not None
    assert await calls(rig) == 0


async def test_failures_are_retried_then_recorded(rig, cases):
    rig.llm.set_faults("server_error=1")
    cid = rig.add(cases["disk-disk_failing-01"])
    res = await rig.triager.triage(cid)
    assert res.status == "failed"
    note = await rig.store.get_note(cid)
    assert note["status"] == "failed" and note["attempts"] == 1 and "server_error" in note["error"]
    assert await calls(rig, "server_error") == rig.settings.llm_max_attempts
    # The next failure on the same input counts a second attempt.
    rig.breaker.success()
    await rig.triager.triage(cid)
    assert (await rig.store.get_note(cid))["attempts"] == 2


async def test_open_breaker_skips_without_writing(rig, cases):
    for _ in range(rig.settings.breaker_failures):
        rig.breaker.failure()
    cid = rig.add(cases["thermal-high_ambient-01"])
    assert (await rig.triager.triage(cid)).status == "skipped_breaker"
    assert await rig.store.get_note(cid) is None


class Flaky:
    """Malformed once, then a real note: the malformed one is retried and paid for."""

    provider, model = "fake", "flaky"

    def __init__(self):
        self.n = 0

    async def complete(self, req):
        self.n += 1
        text = '{"summary": "cut' if self.n == 1 else json.dumps(fake_triage(req.user))
        return Response(text, Usage(1000, 100), self.model, "end_turn")


async def test_malformed_output_is_retried(rig, cases):
    rig.triager.llm = Flaky()
    cid = rig.add(cases["thermal-blocked_airflow-01"])
    assert (await rig.triager.triage(cid)).status == "done"
    assert await calls(rig, "invalid_output") == 1 and await calls(rig, "ok") == 1
    note = await rig.store.get_note(cid)
    assert note["input_tokens"] == 2000, "both attempts are counted in the note's cost"


async def test_injection_is_flagged_and_does_not_change_the_answer(rig, cases):
    ec = next(c for c in cases.values() if c.expect.injection and not c.variant)
    cid = rig.add(ec)
    res = await rig.triager.triage(cid)
    note = await rig.store.get_note(cid)
    assert res.category == ec.label["cause"]
    assert note["note"]["injection_detected"] and note["checks"]["suspicious_lines"] >= 1
    assert "no fault found" not in json.dumps(note["note"]).lower()
    assert rig.metrics.injection_suspected.labels("filter")._value.get() >= 1


async def test_unreadable_bundle_falls_back_to_the_alerts(rig, cases):
    ec = cases["thermal-fan_failure-03"]
    cid = rig.add(ec)
    rig.objects.objects[ec.case["bundles"][0]["object_key"]] = b"corrupt"
    assert (await rig.triager.triage(cid)).status == "done"
    note = await rig.store.get_note(cid)
    assert note["note"]["category"] == "insufficient_evidence"
    assert "not a gzipped tar" in note["checks"]["bundle_errors"][0]


async def test_log_storm_stays_within_the_evidence_budget(rig, cases):
    ec = next(c for c in cases.values() if c.variant == "log-storm")
    cid = rig.add(ec)
    assert (await rig.triager.triage(cid)).category == ec.expect.categories[0]
    note = await rig.store.get_note(cid)
    sel = note["checks"]["evidence_selection"][0]
    assert sel["lines_total"] > 3000 and sel["omitted_by_budget"] > 0 and sel["trace_bytes"] >= 8 << 20
    assert note["input_tokens"] < rig.settings.evidence_token_budget + 4000
