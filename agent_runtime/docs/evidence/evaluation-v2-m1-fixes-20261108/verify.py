"""Read-only verification of M1 fixes. Requires the original local bundles."""
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
    _, sources, result = project_manifest(ROOT / "agent_runtime/docs/evaluation-v2-m1-fixes-input-20261108.json")
    output = ROOT / "agent_runtime/examples/evaluation-v2-m1-fixes-20261108"
    assert digest(result) == digest(read(output / "report.json"))
    assert aggregate([adapt(s) for s in sources] * 2) == result["metrics"]
    assert result["metrics"]["unique_case_observations"] == 11
    assert result["metrics"]["unique_attempt_records"] == 53
    original = ROOT / "agent_runtime/examples/evaluation-v2-m1-final"
    _, _, old_projection = project_manifest(ROOT / "agent_runtime/docs/evaluation-v2-m1-example-input.json")
    assert old_projection["metrics"] == read(original / "report.json")["metrics"]
    for directory in (output, original, Path(__file__).parent):
        for member in read(directory / "seal.json"):
            assert sha(directory / member["path"]) == member["sha256"], str(directory / member["path"])
    checks = read(Path(__file__).with_name("checks.json"))
    assert sha(output / "seal.json") == checks["example_seal_sha256"]
    print("VERIFIED: mixed imports add no samples; 11 observations / 53 attempt records; original example seal intact")


if __name__ == "__main__":
    main()
