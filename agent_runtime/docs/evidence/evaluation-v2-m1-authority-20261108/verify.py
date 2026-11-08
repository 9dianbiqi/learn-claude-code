"""Read-only second-review verification; requires original local source bundles."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from agent_runtime.evaluation_adapters import adapt
from agent_runtime.evaluation_contract import aggregate, digest
from agent_runtime.evaluation_v2 import project_manifest


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    _, sources, report = project_manifest(ROOT / "agent_runtime/docs/evaluation-v2-m1-authority-input-20261108.json")
    output = ROOT / "agent_runtime/examples/evaluation-v2-m1-authority-20261108"
    assert digest(report) == digest(read(output / "report.json"))
    assert aggregate([adapt(s) for s in sources] * 2) == report["metrics"]
    assert report["metrics"]["unique_case_observations"] == 11
    assert report["metrics"]["unique_attempt_records"] == 53
    previous = [("evaluation-v2-m1-example-input.json", "evaluation-v2-m1-final"),
                ("evaluation-v2-m1-fixes-input-20261108.json", "evaluation-v2-m1-fixes-20261108")]
    directories = [output, Path(__file__).parent]
    for manifest, name in previous:
        old_output = ROOT / "agent_runtime/examples" / name
        _, _, projected = project_manifest(ROOT / "agent_runtime/docs" / manifest)
        assert projected["metrics"] == read(old_output / "report.json")["metrics"]
        directories.append(old_output)
    for directory in directories:
        for member in read(directory / "seal.json"):
            assert sha(directory / member["path"]) == member["sha256"], str(directory / member["path"])
    checks = read(Path(__file__).with_name("checks.json"))
    assert sha(output / "seal.json") == checks["example_seal_sha256"]
    print("VERIFIED: 11 observations / 53 attempts; duplicate imports unchanged; both historical example seals intact")


if __name__ == "__main__":
    main()
