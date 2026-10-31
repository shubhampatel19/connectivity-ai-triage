import dataclasses

from fastapi.testclient import TestClient

from triage.api import create_app
from triage.service import Service
from triage.worker import Worker


def client(rig) -> TestClient:
    s = dataclasses.replace(rig.settings, worker_enabled=False)
    worker = Worker(s, rig.store, rig.cases, rig.triager, rig.metrics)
    svc = Service(
        s, rig.store, rig.cases, rig.retriever, rig.llm, rig.breaker, rig.triager, worker, rig.metrics
    )  # type: ignore[arg-type]
    return TestClient(create_app(s, service=svc))


async def test_api(rig, cases):
    cid = rig.add(cases["disk-disk_failing-02"])
    with client(rig) as c:
        assert c.get("/healthz").json() == {"status": "ok"}
        assert c.get("/readyz").status_code == 200
        assert c.get(f"/v1/triage/{cid}").status_code == 404
        r = c.post(f"/v1/triage/{cid}/rerun")
        assert r.status_code == 200 and r.json()["category"] == "disk_failing"
        note = c.get(f"/v1/triage/{cid}").json()
        assert note["status"] == "done" and isinstance(note["cost_usd"], float)
        assert c.get("/v1/triage").json()["notes"][0]["case_id"] == cid
        hits = c.get(
            "/v1/runbooks/search", params={"q": "PSU1 AC input lost", "code": "power.rail_out_of_range"}
        ).json()["hits"]
        assert hits[0]["runbook_id"] == "psu-loose-power-cable"
        assert len(c.get("/v1/runbooks").json()["runbooks"]) >= 14
        assert c.post("/debug/llm-faults", params={"spec": "latency_ms=1"}).json()["latency_ms"] == 1
        assert c.post("/debug/llm-faults", params={"spec": "meteor=1"}).status_code == 400
        assert b"connectivity_triage_cases_total" in c.get("/metrics").content


async def test_rerun_refuses_a_case_being_triaged(rig, cases):
    cid = rig.add(cases["disk-disk_wear-02"])
    await rig.store.claim(cid, "someone-else", 60)
    with client(rig) as c:
        assert c.post(f"/v1/triage/{cid}/rerun").status_code == 409
