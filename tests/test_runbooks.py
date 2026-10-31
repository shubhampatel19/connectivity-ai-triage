from pathlib import Path

from triage import runbooks
from triage.llm.fake import RUNBOOK
from triage.schema import CATEGORIES

ROOT = Path(__file__).resolve().parents[1]


def test_every_cause_has_exactly_one_runbook_and_it_explains_the_evidence():
    books = {b.id: b for b in runbooks.load(ROOT / "runbooks")}
    for cat in CATEGORIES:
        owners = [b for b in books.values() if cat in b.categories]
        assert len(owners) == 1, f"{cat}: {[b.id for b in owners]}"
        assert RUNBOOK[cat] == owners[0].id
        if cat != "insufficient_evidence":
            assert any(c.section == "Reading the evidence" for c in owners[0].chunks), owners[0].id
            assert owners[0].alert_codes, f"{owners[0].id} needs the alert code it explains"


def test_chunks_carry_their_title_and_have_stable_ids(tmp_path):
    (tmp_path / "x-y.md").write_text(
        "---\nid: x-y\ntitle: Thing\nalert_codes: [a.b]\n---\n# Thing\nIntro.\n## Fix\nDo it.\n"
    )
    rb = runbooks.parse(tmp_path / "x-y.md")
    assert [c.chunk_id for c in rb.chunks] == ["x-y#overview", "x-y#fix"]
    assert rb.chunks[1].text == "Thing: Fix\nDo it."
    assert rb.chunks[0].content_hash != rb.chunks[1].content_hash


def test_mismatched_id_is_rejected(tmp_path):
    (tmp_path / "a.md").write_text("---\nid: b\n---\ntext\n")
    try:
        runbooks.parse(tmp_path / "a.md")
    except ValueError as e:
        assert "must match" in str(e)
    else:
        raise AssertionError("expected a ValueError")
