import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from agent_runtime.experiments.bc_tasks import TASKS, check
from agent_runtime.experiments.bc_pilot import SOLUTIONS


def test_private_checks_reject_stubs_and_accept_reference_contracts(tmp_path):
    for task, spec in TASKS.items():
        workspace = tmp_path / task
        workspace.mkdir()
        for name, content in spec["files"].items():
            (workspace / name).write_text(content, encoding="utf-8")
        assert not check(task, workspace)["passed"]
        for index, (_, _, paths) in enumerate(spec["steps"]):
            (workspace / paths[0]).write_text(SOLUTIONS[task][index], encoding="utf-8")
        assert check(task, workspace)["passed"]


def test_real_process_exit_and_paired_snapshot_protocol(tmp_path):
    # CI opts this scripted fixture into the existing always-uploaded evidence
    # directory. Unique lanes avoid replacing previous/concurrent attempt data.
    evidence = os.environ.get("LCC_BC_TEST_ARTIFACTS")
    if evidence:
        tmp_path = Path(evidence).resolve() / uuid.uuid4().hex
        tmp_path.mkdir(parents=True, exist_ok=False)
    root = tmp_path / "pilot"
    run = subprocess.run([sys.executable, "-m", "agent_runtime.experiments.bc_pilot",
                          "--output", str(root)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stdout + run.stderr
    rows = json.loads((root / "results.json").read_text(encoding="utf-8"))
    assert len(rows) == 4
    for task in TASKS:
        pair = [row for row in rows if row["task"] == task]
        assert pair[0]["snapshot_database_sha256"] == pair[1]["snapshot_database_sha256"]
        assert pair[0]["initial_file_hashes"] == pair[1]["initial_file_hashes"]
        assert all(row["final_pass"] and row["completed_parser_unchanged"] for row in pair)
        assert all(row["post_restart_input_tokens"] is None for row in pair)
        assert json.loads((root/task/"prefix"/"process.json").read_text())["exit_code"] == 73
        b = json.loads((root/task/"B"/"model_requests.jsonl").read_text().splitlines()[0])
        c = json.loads((root/task/"C"/"model_requests.jsonl").read_text().splitlines()[0])
        assert "Verified-Scoped recovery context" not in str(b["messages"])
        assert "Verified-Scoped recovery context" in str(c["messages"])
        assert "write-0" in str(b["messages"])
        assert "write-0" not in str(c["messages"])

    continued = tmp_path / "continued"
    rerun = subprocess.run([sys.executable, "-m", "agent_runtime.experiments.bc_pilot",
                            "--output", str(continued), "--resume-from", str(root)],
                           capture_output=True, text=True, timeout=60)
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    later = json.loads((continued / "results.json").read_text(encoding="utf-8"))
    for before, after in zip(rows, later):
        assert before["snapshot_database_sha256"] == after["snapshot_database_sha256"]
        assert after["final_pass"] and after["invariant_violations"] == 0
    for task in TASKS:
        assert (root/task/"prefix"/"model_requests.jsonl").read_bytes() == (continued/task/"prefix"/"model_requests.jsonl").read_bytes()
