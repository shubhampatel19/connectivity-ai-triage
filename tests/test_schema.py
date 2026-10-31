import json

import pytest

from triage import schema


def note(**over):
    base = {
        "summary": "s",
        "category": "fan_failure",
        "confidence": "high",
        "evidence": [{"line": "fan1 tachometer reads 0 RPM, fan1 marked failed", "why": "dead fan"}],
        "runbooks": ["thermal-fan-failure"],
        "next_steps": ["ship a fan"],
        "devices": ["site-a-00001"],
        "injection_detected": False,
    }
    base.update(over)
    return json.dumps(base)


def test_schema_is_closed_and_fully_required():
    s = schema.output_schema(["b", "a"])

    def walk(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
            for v in node["properties"].values():
                walk(v)
        if node.get("type") == "array":
            walk(node["items"])

    walk(s)
    assert s["properties"]["runbooks"]["items"]["enum"] == ["a", "b"]
    assert s["properties"]["category"]["enum"] == list(schema.CATEGORIES)


def test_parse_tolerates_enum_case():
    out = schema.parse(note(category="Fan_Failure", confidence="HIGH", runbooks=["Thermal-Fan-Failure"]))
    assert (out.category, out.confidence, out.runbooks) == ("fan_failure", "high", ["thermal-fan-failure"])


@pytest.mark.parametrize("text", ['{"summary": "trunc', "[]", note(category="gremlins")])
def test_parse_rejects_bad_output(text):
    with pytest.raises(schema.InvalidOutput):
        schema.parse(text)


def test_grounding_checks():
    shown = ["2026-10-01T12:00:00.000Z ERROR [bmc] fan1 tachometer reads 0 RPM, fan1 marked failed"]
    ok = schema.check(
        schema.parse(note()), shown=shown, case_devices={"site-a-00001"}, retrieved=["thermal-fan-failure"]
    )
    assert ok["grounded"] and ok["evidence_grounded"] == 1

    bad = schema.parse(
        note(
            evidence=[
                {"line": "fan2 reads 0 RPM", "why": "invented"},
                {"line": "0 RPM", "why": "too short to count"},
            ],
            devices=["site-a-00001", "site-b-00099"],
            runbooks=["disk-wear"],
        )
    )
    c = schema.check(bad, shown=shown, case_devices={"site-a-00001"}, retrieved=["thermal-fan-failure"])
    assert not c["grounded"]
    assert c["ungrounded_evidence"] == ["fan2 reads 0 RPM", "0 RPM"]
    assert c["unknown_devices"] == ["site-b-00099"]
    assert c["runbooks_not_retrieved"] == ["disk-wear"]
