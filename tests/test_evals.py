"""The eval runner, with the fake model: every case must pass, so this is the
pipeline's regression test against the whole labelled set."""

import os

from triage.evals import run


async def test_eval_suite_passes_with_the_fake_model(store, tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["TRIAGE_TEST_DATABASE_URL"])
    args = run.parse_args(["--provider", "fake", "--embedder", "hash", "--out", str(tmp_path)])
    assert await run.run(args) == 0
    reports = list(tmp_path.glob("*-fake.md"))
    assert reports and "Thresholds: met" in reports[0].read_text()
