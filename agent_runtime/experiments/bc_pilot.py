"""Paired F3 continuation pilot. Run with --help; live calls require --mode live."""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

from agent_runtime.experiments.bc_tasks import TASKS, check, make_dag, prompt
from agent_runtime.models import ModelResponse, ToolCall
from agent_runtime.projector import ContextProjection, estimate_tokens
from agent_runtime.runtime import Runtime
from agent_runtime.trace import TraceReporter

ALLOWED = {"read_file", "glob", "write_file", "edit_file"}
CALL_LIMIT = 32
TOKEN_LIMIT = 150_000
SCOPED_BUDGET = 16_000
F3_EXIT = 73


def save(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def input_tokens(entry: dict) -> int:
    usage = entry.get("provider_usage") or entry.get("usage", {})
    return sum(int(usage.get(key) or 0) for key in
               ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


class FullHistory:
    """Disable default ContextProjector truncation, equally before both arms."""
    def project(self, task_id, messages, checkpoint_id):
        return ContextProjection(messages, checkpoint_id, {
            "basis": "full_history", "projection_used": False,
            "token_estimate": estimate_tokens(json.dumps(messages, ensure_ascii=False))})


SOLUTIONS = {
    "duration": (
        "import re\ndef parse_duration(text):\n    if not isinstance(text,str): raise TypeError('string required')\n    m=re.fullmatch(r'([0-9]+)(ms|s|m)',text.strip())\n    if not m: raise ValueError('invalid duration')\n    return int(m[1])*{'ms':1,'s':1000,'m':60000}[m[2]]\n",
        "from duration import parse_duration\ndef load_timeout(settings):\n    if 'timeout' not in settings: return 30000\n    value=parse_duration(settings['timeout'])\n    if value<=0: raise ValueError('positive timeout required')\n    return value\n"),
    "inventory": (
        "import re\ndef parse_record(line):\n    if not isinstance(line,str): raise TypeError('string required')\n    parts=line.split(',')\n    if len(parts)!=2: raise ValueError('two fields required')\n    sku, qty=(x.strip() for x in parts)\n    if not sku or not re.fullmatch(r'[0-9]+',qty): raise ValueError('invalid record')\n    return sku,int(qty)\n",
        "from records import parse_record\ndef aggregate(lines):\n    result={}\n    for line in lines:\n        sku,qty=parse_record(line)\n        result[sku]=result.get(sku,0)+qty\n    return result\n"),
}


class MeasuredModel:
    timeout = 90.0

    def __init__(self, args, directory: Path, prior: list[dict]):
        self.args, self.directory = args, directory
        self.used_calls = len(prior)
        self.used_tokens = sum(input_tokens(x) + x.get("usage", {}).get("output_tokens", 0) for x in prior)
        self.local_calls = 0
        if args.mode == "live":
            from dotenv import load_dotenv
            from agent_runtime.providers import AnthropicModel
            load_dotenv(args.env_file, override=True)
            self.provider = AnthropicModel(timeout=self.timeout)
            self.provider.client = self.provider.client.with_options(max_retries=0)
            self.name = self.provider.name
        else:
            self.name = "scripted-protocol-check"
            self.timeout = 0.1

    def complete(self, messages, tools):
        if self.used_calls >= CALL_LIMIT or self.used_tokens >= TOKEN_LIMIT:
            raise RuntimeError("pilot cumulative budget exhausted")
        tools = [t for t in tools if t["name"] in ALLOWED]
        self.used_calls += 1
        self.local_calls += 1
        started = time.monotonic()
        entry = {"call": self.local_calls, "messages": messages, "tools": tools,
                 "usage": {}, "provider_tokens": self.args.mode == "live"}
        try:
            if self.args.mode == "live":
                response = self.provider.complete(messages, tools)
            else:
                stage = 0 if self.args.phase == "prefix" else 1
                path = TASKS[self.args.task]["steps"][stage][2][0]
                index = self.local_calls - 1
                if index == 0:
                    response = ModelResponse(tool_calls=[ToolCall(f"read-{stage}", "read_file", {"path": path})])
                elif index == 1:
                    response = ModelResponse(tool_calls=[ToolCall(f"write-{stage}", "write_file", {"path": path, "content": SOLUTIONS[self.args.task][stage]})])
                else:
                    response = ModelResponse(text="Implemented the published contract.\nSUBTASK_COMPLETE")
            if any(call.name not in ALLOWED for call in response.tool_calls):
                raise ValueError("model requested a tool outside the frozen pilot scope")
            entry["usage"] = response.usage
            if response.raw and isinstance(response.raw.get("usage"), dict):
                entry["provider_usage"] = response.raw["usage"]
            entry["response"] = response.as_dict()
            self.used_tokens += input_tokens(entry) + response.usage.get("output_tokens", 0)
            return response
        except Exception as exc:
            entry["error_type"] = type(exc).__name__
            raise
        finally:
            entry["seconds"] = time.monotonic() - started
            with (self.directory / "model_requests.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")


def requests(directory: Path) -> list[dict]:
    path = directory / "model_requests.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def worker(args) -> int:
    workspace = Path(args.workspace)
    directory = Path(args.directory)
    prior = requests(directory.parent / "prefix") if args.phase != "prefix" else []
    model = MeasuredModel(args, directory, prior)

    def fault(point, **context):
        if args.phase == "prefix" and point == "verified_subtask_f3":
            save(directory / "fault.json", {"point": point, "context": context,
                                          "pid": os.getpid(), "time": time.time()})
            os._exit(F3_EXIT)

    runtime = Runtime(workspace, model, verified_subtask_dag=make_dag(args.task),
                      scoped_context_budget=SCOPED_BUDGET if args.phase == "C" else None,
                      tool_scope=ALLOWED, fault_injector=fault,
                      approval_callback=lambda name, *_: name in ALLOWED,
                      # A one-second lease measures host I/O jitter rather than
                      # this protocol on Windows CI. Keep fencing unchanged and
                      # still wait for natural expiry after the real F3 exit.
                      lease_ttl=100 if args.mode == "live" else 5)
    runtime.projector = FullHistory()
    started = time.monotonic()
    try:
        result = (runtime.run(prompt(args.task)) if args.phase == "prefix"
                  else runtime.resume(runtime.store.list_tasks()[0]["task_id"]))
        task_id = result.task_id
        trace = TraceReporter(runtime.store)
        trace.export_jsonl(task_id, directory / "trace.jsonl")
        save(directory / "result.json", {"result": dataclasses.asdict(result),
              "trace": trace.summary(task_id), "seconds": time.monotonic() - started,
              "final_check": check(args.task, workspace)})
        return 0
    except Exception as exc:
        save(directory / "error.json", {"type": type(exc).__name__, "message": str(exc)[:1000]})
        return 2


def launch(args, task, phase, workspace, directory):
    directory.mkdir()
    command = [sys.executable, "-m", "agent_runtime.experiments.bc_pilot", "--worker", "--mode", args.mode,
               "--task", task, "--phase", phase, "--workspace", str(workspace),
               "--directory", str(directory)]
    if args.env_file:
        command += ["--env-file", str(Path(args.env_file).resolve())]
    started = time.monotonic()
    with (directory / "process.log").open("w", encoding="utf-8") as log:
        try:
            result = subprocess.run(command, stdout=log, stderr=log, timeout=600,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            code = result.returncode
        except subprocess.TimeoutExpired:
            code = -1
    save(directory / "process.json", {"exit_code": code, "seconds": time.monotonic()-started})
    return code


def file_hashes(workspace):
    return {p.relative_to(workspace).as_posix(): digest(p) for p in workspace.rglob("*")
            if p.is_file() and ".agent_runtime" not in p.parts and "__pycache__" not in p.parts}


def clone_snapshot(snapshot: Path, workspace: Path) -> None:
    # This pilot exposes only atomic-replace file writers, never shell/MCP.
    # Hard links retain persisted st_dev/st_ino observations. Atomic replacement
    # detaches writes from the frozen source. Databases must NOT be hard-linked.
    def copy_file(source, target):
        source = Path(source)
        if source.is_symlink():
            raise ValueError("Snapshot symlinks are unsupported")
        if source.relative_to(snapshot).parts[0] == ".agent_runtime":
            return shutil.copy2(source, target)
        os.link(source, target)
        return str(target)
    shutil.copytree(snapshot, workspace, copy_function=copy_file)


def verify_observations(workspace: Path) -> dict:
    from agent_runtime.tools import ToolExecutor
    executor = ToolExecutor(workspace)
    database = workspace / ".agent_runtime/runtime.db"
    conn = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        for row in conn.execute("SELECT * FROM file_observations"):
            relative = Path(row["path"]).relative_to(workspace).as_posix()
            actual = executor.file_state(relative)
            expected_identity = json.loads(row["identity_json"]) if row["identity_json"] else None
            if actual["sha256"] != row["sha256"] or (expected_identity is not None and actual["identity"] != expected_identity):
                raise ValueError(f"Restored observation mismatch: {relative}")
    finally:
        conn.close()
    return {path: executor.file_state(path)["identity"] for path in file_hashes(workspace)}


def supervise(args):
    root = Path(args.output).resolve()
    if root.exists():
        raise ValueError("Output must be a new directory; previous attempts are never overwritten")
    root.mkdir(parents=True)
    source_root = Path(__file__).resolve().parents[2]
    manifest = {"protocol": "bc-f3-paired-pilot-v4", "mode": args.mode,
                "tasks": list(TASKS), "arms": ["B", "C"], "fault": "verified_subtask_f3",
                "design": "Shared live prefix; restore identical snapshot at same absolute path for each arm",
                "call_limit_per_trajectory": CALL_LIMIT, "token_stop_threshold": TOKEN_LIMIT,
                "budget_note": "One response can cross token threshold; SDK max_tokens=8000. No automatic retries.",
                "phase_timeout_seconds": 600, "scoped_context_budget_estimate": SCOPED_BUDGET,
                "baseline_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source_root, text=True).strip(),
                "source_hashes": {str(p.relative_to(source_root)): digest(p)
                                  for p in (source_root / "agent_runtime").rglob("*.py") if "tests" not in p.parts},
                "python": sys.version, "platform": sys.platform, "started_at": time.time()}
    if args.mode == "live":
        from dotenv import dotenv_values
        config = {**os.environ, **dotenv_values(args.env_file)}
        manifest["model"] = config.get("MODEL_ID")
        manifest["endpoint"] = config.get("ANTHROPIC_BASE_URL")
        manifest["temperature"] = "provider default (not seeded)"
    source = Path(args.resume_from).resolve() if args.resume_from else None
    if source:
        previous = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
        for key in ("mode", "model", "endpoint"):
            if previous.get(key) != manifest.get(key):
                raise ValueError(f"Snapshot configuration mismatch: {key}")
        fixture_key = str(Path("agent_runtime") / "experiments" / "bc_tasks.py")
        if previous["source_hashes"][fixture_key] != manifest["source_hashes"][fixture_key]:
            raise ValueError("Frozen task/verifier changed; cannot reuse snapshot")
        manifest["prefix_source"] = str(source)
        manifest["prefix_source_manifest_sha256"] = digest(source / "manifest.json")
    save(root / "manifest.json", manifest)
    rows = []
    for task, spec in TASKS.items():
        case = root / task
        case.mkdir()
        workspace = (source / task / "workspace") if source else (case / "workspace")
        db = workspace / ".agent_runtime" / "runtime.db"
        if source:
            if workspace.exists():
                raise ValueError(f"Refusing to replace existing workspace: {workspace}")
            prefix = source / task / "prefix"
            if json.loads((prefix / "process.json").read_text())["exit_code"] != F3_EXIT:
                raise ValueError("Source did not reach a real F3 termination")
            if json.loads((prefix / "fault.json").read_text())["point"] != "verified_subtask_f3":
                raise ValueError("Source fault point mismatch")
            shutil.copytree(prefix, case / "prefix")
            snapshot = source / task / "snapshot"
            print(f"{task}: reusing preserved F3 snapshot; no prefix model calls", flush=True)
        else:
            workspace.mkdir()
            for name, content in spec["files"].items():
                (workspace / name).write_text(content, encoding="utf-8")
            print(f"{task}: running common prefix", flush=True)
            code = launch(args, task, "prefix", workspace, case / "prefix")
            if code != F3_EXIT or not (case / "prefix" / "fault.json").exists():
                rows.append({"task": task, "status": "prefix_failed_or_fault_not_reached", "exit_code": code})
                save(root / "results.json", rows)
                continue
            # The child is dead. Recover SQLite WAL through its own API, then snapshot.
            with sqlite3.connect(db) as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                expires = conn.execute("SELECT max(expires_at) FROM leases").fetchone()[0] or 0
            conn.close()
            print(f"{task}: F3 terminated process; waiting for natural lease expiry", flush=True)
            while time.time() <= expires:
                time.sleep(min(1, max(0.01, expires-time.time()+0.05)))
            snapshot = case / "snapshot"
            workspace.rename(snapshot)
        snap_db_hash = digest(snapshot / ".agent_runtime" / "runtime.db")
        snapshot_file_hashes = file_hashes(snapshot)
        prefix_usage = requests(case / "prefix")
        order = ["B", "C"] if task == "duration" else ["C", "B"]
        if getattr(args, "reverse_order", False):
            order.reverse()
        for arm in order:
            clone_snapshot(snapshot, workspace)
            assert digest(db) == snap_db_hash
            identities = verify_observations(workspace)
            before = file_hashes(workspace)
            print(f"{task}: resuming {arm}", flush=True)
            code = launch(args, task, arm, workspace, case / arm)
            result_path = case / arm / "result.json"
            result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
            calls = requests(case / arm)
            def tokens(items, key):
                if args.mode != "live":
                    return None
                return sum(input_tokens(x) if key == "input_tokens" else x.get("usage", {}).get(key, 0) for x in items)
            row = {"task": task, "arm": arm, "exit_code": code,
                   "snapshot_database_sha256": snap_db_hash, "initial_file_hashes": before,
                   "initial_file_identities": identities,
                   "status": result.get("result", {}).get("status", "process_error"),
                   "final_pass": result.get("final_check", {}).get("passed", False),
                   "post_restart_calls": len(calls),
                   "post_restart_input_tokens": tokens(calls, "input_tokens"),
                   "post_restart_output_tokens": tokens(calls, "output_tokens"),
                   "total_input_tokens": tokens(prefix_usage + calls, "input_tokens"),
                   "total_output_tokens": tokens(prefix_usage + calls, "output_tokens"),
                   "resume_seconds": result.get("seconds"),
                   "completed_parser_unchanged": before[TASKS[task]["steps"][0][2][0]] == digest(workspace / TASKS[task]["steps"][0][2][0]),
                   "invariant_violations": result.get("trace", {}).get("invariant_violation_count"),
                   "scope": "synthetic fixed-plan pilot, not SWE-bench; one paired continuation per task"}
            rows.append(row)
            save(root / "results.json", rows)
            workspace.rename(case / f"{arm}-workspace")
            assert file_hashes(snapshot) == snapshot_file_hashes, "Frozen snapshot contents changed"
            assert digest(snapshot / ".agent_runtime/runtime.db") == snap_db_hash
            print(json.dumps(row, ensure_ascii=False), flush=True)
    manifest["finished_at"] = time.time()
    save(root / "manifest.json", manifest)
    return 0 if len(rows) == 4 and all(r.get("final_pass") for r in rows) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["scripted", "live"], default="scripted")
    parser.add_argument("--output")
    parser.add_argument("--env-file")
    parser.add_argument("--resume-from", help="Reuse existing F3 snapshots at their original workspace paths")
    parser.add_argument("--reverse-order", action="store_true", help="Reverse both paired continuation orders")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--task", choices=list(TASKS))
    parser.add_argument("--phase", choices=["prefix", "B", "C"])
    parser.add_argument("--workspace")
    parser.add_argument("--directory")
    args = parser.parse_args()
    if args.mode == "live" and not args.env_file:
        parser.error("live mode requires --env-file")
    if not args.worker and not args.output:
        parser.error("--output is required")
    return worker(args) if args.worker else supervise(args)


if __name__ == "__main__":
    raise SystemExit(main())
