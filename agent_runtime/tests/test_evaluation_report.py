import json
from pathlib import Path

import pytest

from agent_runtime.evaluation import _dump, _hash
from agent_runtime.evaluation_report import render


def manifest(tmp_path, data, kind="junit", scope="offline_simulated"):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    source = inputs / "evidence"
    source.write_bytes(data)
    path = inputs / "manifest.json"
    _dump(path, {"schema_version": "evaluation-report-input-v1", "entries": [
        {"id": "proof", "kind": kind, "scope": scope, "path": str(source), "sha256": _hash(data)}]})
    return path, source


def test_existing_junit_preserves_failures_skips_and_paths(tmp_path):
    path, source = manifest(tmp_path, b'<testsuites><testsuite><testcase/><testcase><failure/></testcase>'
        b'<testcase><skipped/></testcase></testsuite></testsuites>')
    result = render(path, tmp_path / "out")
    row = result["rows"][0]
    assert row["status"] == "FAILED" and row["metrics"] == dict(passed=1, failed=1, errors=0, skipped=1)
    assert row["path"] == str(source) and result["overall_success_rate"] is None
    markdown = (tmp_path / "out/report.md").read_text()
    assert "real_container: no evidence supplied" in markdown
    assert "official: no evidence supplied" in markdown
    assert result["new_official_evaluations"] == 0


def test_synthetic_official_result_does_not_promote_scope(tmp_path):
    data = json.dumps(dict(schema_version="swe-acceptance-v1", status="REPORTED",
                          official_resolved={"synthetic": True})).encode()
    path, _ = manifest(tmp_path, data, "acceptance")
    result = render(path, tmp_path / "out")
    assert result["rows"][0]["scope"] == "offline_simulated"
    assert "official: no evidence supplied" in (tmp_path / "out/report.md").read_text()


def test_changed_evidence_rejected_without_output(tmp_path):
    path, source = manifest(tmp_path, b'<testsuite><testcase/></testsuite>')
    source.write_bytes(b"changed")
    with pytest.raises(ValueError, match="digest"):
        render(path, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_unknown_kind_without_schema_cannot_be_rendered_as_evidence(tmp_path):
    path, _ = manifest(tmp_path, b"{}", "unsupported")
    with pytest.raises(ValueError, match="schema/kind"):
        render(path, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_skipped_only_is_not_a_pass_and_existing_output_is_preserved(tmp_path):
    path, _ = manifest(tmp_path, b'<testsuite><testcase><skipped/></testcase></testsuite>')
    out = tmp_path / "out"
    assert render(path, out)["rows"][0]["status"] == "SKIPPED_ONLY"
    raw = (out / "report.json").read_bytes()
    with pytest.raises(ValueError, match="fresh"):
        render(path, out)
    assert (out / "report.json").read_bytes() == raw
