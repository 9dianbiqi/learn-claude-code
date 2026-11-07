"""Read-only M1 example verification; requires the original local source bundles.

Run from any directory. Does not rerun tests, Runtime, models or Harness.
Missing local sources are a blocker, not a reason to replace recorded pins.
"""
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
    manifest = ROOT / "agent_runtime/docs/evaluation-v2-m1-example-input.json"
    _, sources, report = project_manifest(manifest)
    example = ROOT / "agent_runtime/examples/evaluation-v2-m1-final"
    assert digest(report) == digest(read(example / "report.json")), "read-only reprojection differs"
    assert aggregate([adapt(s) for s in sources] * 2) == report["metrics"], "duplicate imports inflate metrics"
    for row in read(example / "seal.json"):
        assert sha(example / row["path"]) == row["sha256"], "example evidence changed"
    checks = read(Path(__file__).with_name("checks.json"))
    assert sha(example / "seal.json") == checks["example_seal_sha256"]
    assert report["metrics"]["unique_case_observations"] == 10
    assert report["metrics"]["unique_attempt_records"] == 52
    for row in read(Path(__file__).with_name("seal.json")):
        assert sha(Path(__file__).parent / row["path"]) == row["sha256"], "validation evidence changed"
    print("VERIFIED: 7 existing bundles / 129 members; 10 observations / 52 attempts; duplicate imports unchanged")


if __name__ == "__main__":
    main()
