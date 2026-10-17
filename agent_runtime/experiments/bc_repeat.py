"""Frozen five-pair continuation replication; no new prefixes or automatic retries."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time

from .bc_pilot import digest, input_tokens, requests, save
from .bc_tasks import TASKS

CLASSIFICATION = {
    "marker_format_error": "Text contains SUBTASK_COMPLETE but no line equals that exact marker.",
    "marker_with_tools": "A response contains an exact marker line and tool calls; runtime rejects it.",
    "text_without_marker": "Nonempty text-only reply without any marker; a missing-completion candidate, not proof of claimed completion.",
    "api_error": "Recorded model call has error_type (includes transport/provider/adapter errors).",
    "completed_file_read": "read_file targeting the already verified parser; may be legitimate verification.",
    "repeated_read": "Read a path already read after restart; not automatically wasted work.",
    "marker_valid": "Text-only reply with an exact marker line; a verification trigger, not acceptance.",
}


def classify(calls, completed_path):
    events = []
    seen = set()
    for call in calls:
        blocks = call.get("response", {}).get("content", [])
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        tools = [b for b in blocks if b.get("type") == "tool_use"]
        exact = "SUBTASK_COMPLETE" in text.splitlines()
        kinds = []
        if call.get("error_type"):
            kinds.append("api_error")
        if exact:
            kinds.append("marker_with_tools" if tools else "marker_valid")
        elif "SUBTASK_COMPLETE" in text:
            kinds.append("marker_format_error")
        elif text.strip() and not tools:
            kinds.append("text_without_marker")
        for tool in tools:
            if tool.get("name") == "read_file":
                path = str(tool.get("input", {}).get("path", "")).replace("\\", "/")
                if path.startswith("./"):
                    path = path[2:]
                if path == completed_path:
                    kinds.append("completed_file_read")
                if path in seen:
                    kinds.append("repeated_read")
                seen.add(path)
        for kind in kinds:
            events.append({"call": call["call"], "kind": kind})
    return events


def analyze(root):
    rows = []
    for repeat in range(1, 6):
        directory = root / f"repeat-{repeat}"
        result_file = directory / "results.json"
        if not result_file.exists():
            continue
        for row in json.loads(result_file.read_text(encoding="utf-8")):
            if "arm" not in row:
                rows.append({**row, "repeat": repeat})
                continue
            calls = requests(directory / row["task"] / row["arm"])
            events = classify(calls, TASKS[row["task"]]["steps"][0][2][0])
            rows.append({**row, "repeat": repeat, "events": events,
                         "counts": {kind: sum(e["kind"] == kind for e in events) for kind in CLASSIFICATION},
                         "first_input_tokens": input_tokens(calls[0]) if calls else None,
                         "uncached_input_tokens": sum(c.get("usage", {}).get("input_tokens", 0) for c in calls),
                         "cache_read_input_tokens": sum((c.get("provider_usage") or {}).get("cache_read_input_tokens", 0) or 0 for c in calls),
                         "empty_message_count": sum(not m.get("content") for c in calls for m in c["messages"]),
                         "missing_usage_calls": sum(not c.get("usage") for c in calls)})
    aggregate = {}
    for arm in ("B", "C"):
        selected = [r for r in rows if r.get("arm") == arm]
        aggregate[arm] = {
            "runs": len(selected), "passed": sum(r["final_pass"] and r["status"] == "completed" for r in selected),
            "input_tokens": sum(r["post_restart_input_tokens"] or 0 for r in selected),
            "output_tokens": sum(r["post_restart_output_tokens"] or 0 for r in selected),
            "model_calls": sum(r["post_restart_calls"] for r in selected),
            "first_input_tokens": sum(r["first_input_tokens"] or 0 for r in selected),
            "events": {kind: sum(r["counts"][kind] for r in selected) for kind in CLASSIFICATION},
            "runs_with_protocol_error": sum(any(r["counts"][k] for k in ("marker_format_error", "marker_with_tools", "text_without_marker")) for r in selected),
        }
    pairs = []
    for task in TASKS:
        for repeat in range(1, 6):
            pair = {r.get("arm"): r for r in rows if r["task"] == task and r["repeat"] == repeat}
            if "B" in pair and "C" in pair:
                b, c = pair["B"], pair["C"]
                assert b["snapshot_database_sha256"] == c["snapshot_database_sha256"]
                assert b["initial_file_hashes"] == c["initial_file_hashes"]
                assert b["initial_file_identities"] == c["initial_file_identities"]
                pairs.append({"task": task, "repeat": repeat,
                              "input_delta_C_minus_B": c["post_restart_input_tokens"]-b["post_restart_input_tokens"],
                              "calls_delta_C_minus_B": c["post_restart_calls"]-b["post_restart_calls"]})
    save(root / "analysis.json", {"rows": rows, "aggregate": aggregate, "pairs": pairs,
                                  "missing_cells": 20-len([r for r in rows if "arm" in r]),
                                  "unit_of_generalization": "2 tasks; repeats are conditional on the same two snapshots"})
    return aggregate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--resume-from", required=True)
    parser.add_argument("--env-file", required=True)
    args = parser.parse_args()
    root, source = Path(args.output).resolve(), Path(args.resume_from).resolve()
    if root.exists():
        raise ValueError("Use a new output directory; no overwrites or retries")
    root.mkdir(parents=True)
    code_root = Path(__file__).resolve().parents[2]
    schedule = []
    for repeat in range(1, 6):
        for task in TASKS:
            order = ["B", "C"] if task == "duration" else ["C", "B"]
            if repeat % 2 == 0:
                order.reverse()
            schedule.extend({"repeat": repeat, "task": task, "arm": arm} for arm in order)
    hashes = {str(p.relative_to(code_root)): digest(p) for p in (code_root/"agent_runtime").rglob("*.py") if "tests" not in p.parts}
    save(root / "protocol.json", {
        "version": "bc-f3-repeat-v2", "schedule": schedule, "classification": CLASSIFICATION,
        "restoration": "Identity-preserving hard links for atomic-replace file tools; private database copy; per-arm observation and snapshot integrity checks.",
        "runtime_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "prefix_source": str(source), "prefix_manifest_sha256": digest(source/"manifest.json"),
        "source_hashes": hashes, "automatic_retries": 0, "started_at": time.time(),
        "analysis": "Report all failures, per-task paired deltas and protocol events. No inferential significance claim with two tasks.",
        "unchanged": "Runtime, task/verifier bytes, prompts, tool set and budgets from completed pilot. Only order alternates.",
    })
    for relative, expected in hashes.items():
        dst = root / "frozen-source" / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(code_root/relative, dst)
        assert digest(dst) == expected
    for repeat in range(1, 6):
        # Stop if implementation changes mid-experiment, rather than mix versions.
        assert all(digest(code_root/relative) == expected for relative, expected in hashes.items())
        command = [sys.executable, "-m", "agent_runtime.experiments.bc_pilot", "--mode", "live",
                   "--env-file", str(Path(args.env_file).resolve()), "--resume-from", str(source),
                   "--output", str(root/f"repeat-{repeat}")]
        if repeat % 2 == 0:
            command.append("--reverse-order")
        print(f"Starting repetition {repeat}/5", flush=True)
        with (root/f"repeat-{repeat}.log").open("w", encoding="utf-8") as stream:
            result = subprocess.run(command, stdout=stream, stderr=stream)
        save(root/f"repeat-{repeat}-process.json", {"exit_code": result.returncode})
        print(json.dumps(analyze(root)), flush=True)
    save(root/"completed.json", {"finished_at": time.time()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
