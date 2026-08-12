from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from dotenv import find_dotenv, load_dotenv

from . import __version__
from .eval_runner import run_suite, write_report
from .permissions import PermissionEngine
from .providers import AnthropicModel
from .runtime import Runtime
from .store import EventStore
from .trace import TraceReporter


TASK_STATUSES = (
    "created", "running", "waiting_approval", "needs_review", "completed", "failed", "aborted"
)


def _repo(args: argparse.Namespace) -> Path:
    return Path(args.repo).resolve()


def _db_path(repo: Path) -> Path:
    return repo / ".agent_runtime" / "runtime.db"


def _store(args: argparse.Namespace, *, must_exist: bool = True) -> EventStore:
    path = _db_path(_repo(args))
    if must_exist and not path.exists():
        raise SystemExit(f"Runtime database does not exist: {path}")
    return EventStore(path)


def _runtime(args: argparse.Namespace) -> Runtime:
    return Runtime(
        _repo(args),
        AnthropicModel(),
        policy_path=getattr(args, "policy", None),
        interactive=True,
    )


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _print_result(result: Any) -> int:
    _print_json(result.__dict__)
    return 0 if result.status == "completed" else 2


def _add_repo(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", default=".", help="Repository root (default: current directory).")


def _add_policy(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--policy", help="Optional allow/ask/deny YAML policy.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-runtime", description="Durable local coding-agent runtime.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Create and execute a new task.")
    _add_repo(run)
    _add_policy(run)
    run.add_argument("--prompt", required=True)

    resume = sub.add_parser("resume", help="Resume a non-terminal task from its latest checkpoint.")
    resume.add_argument("task_id")
    _add_repo(resume)
    _add_policy(resume)

    status = sub.add_parser("status", help="Show the durable task row.")
    status.add_argument("task_id")
    _add_repo(status)

    list_command = sub.add_parser("list", help="List tasks in the repository Runtime database.")
    _add_repo(list_command)
    list_command.add_argument("--status", choices=TASK_STATUSES)

    show = sub.add_parser("show", help="Show task, checkpoint, tools, and unresolved effects.")
    show.add_argument("task_id")
    _add_repo(show)

    pending = sub.add_parser("pending", help="List calls awaiting approval or reconciliation.")
    _add_repo(pending)
    pending.add_argument("--task-id")

    events = sub.add_parser("events", help="Show recent append-only task events.")
    events.add_argument("task_id")
    _add_repo(events)
    events.add_argument("--type", dest="event_type")
    events.add_argument("--limit", type=int, default=100)

    for name, help_text in (
        ("approve", "Approve a call in waiting_approval."),
        ("deny", "Deny a call in waiting_approval."),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("tool_use_id")
        _add_repo(command)
        _add_policy(command)

    resolve = sub.add_parser("resolve-call", help="Resolve an ambiguous effect after operator review.")
    resolve.add_argument("tool_use_id")
    _add_repo(resolve)
    _add_policy(resolve)
    resolve.add_argument("--action", required=True, choices=("retry", "complete", "abort"))

    trace = sub.add_parser("trace", help="Summarize and optionally export a task trace.")
    trace.add_argument("task_id")
    _add_repo(trace)
    trace.add_argument("--output")

    doctor = sub.add_parser("doctor", help="Check disk, configuration, policy, temp, and database health.")
    _add_repo(doctor)
    _add_policy(doctor)
    doctor.add_argument("--minimum-free-mb", type=int, default=512)

    db_check = sub.add_parser("db-check", help="Run SQLite integrity and Runtime invariant checks.")
    _add_repo(db_check)

    evaluation = sub.add_parser("eval", help="Run a deterministic fixed-task suite.")
    evaluation.add_argument("--suite", required=True)
    evaluation.add_argument("--runs")
    evaluation.add_argument("--output")
    evaluation.add_argument(
        "--live", action="store_true", help="Reserved for a future live benchmark; MVP eval is deterministic."
    )
    return parser


def _task_list(args: argparse.Namespace) -> list[dict[str, Any]]:
    tasks = _store(args).list_tasks()
    if args.status:
        tasks = [task for task in tasks if task["status"] == args.status]
    return [
        {
            "task_id": task["task_id"],
            "status": task["status"],
            "model": task["model"],
            "checkpoint_id": task["checkpoint_id"],
            "created_at": task["created_at"],
            "updated_at": task["updated_at"],
            "last_error": task["last_error"],
        }
        for task in tasks
    ]


def _task_show(args: argparse.Namespace) -> dict[str, Any]:
    store = _store(args)
    task = store.get_task(args.task_id)
    checkpoint = store.get_checkpoint(task["checkpoint_id"]) if task.get("checkpoint_id") else None
    checkpoint_summary = None if checkpoint is None else {
        "checkpoint_id": checkpoint["checkpoint_id"],
        "phase": checkpoint["phase"],
        "cursor": checkpoint["cursor"],
        "created_at": checkpoint["created_at"],
    }
    tools = []
    for call in store.list_tool_calls(args.task_id):
        tools.append({
            key: call.get(key)
            for key in (
                "tool_use_id", "name", "status", "permission", "permission_rule", "effect",
                "execution_attempts", "effect_attempts", "effect_confirmed", "error",
            )
        })
    return {
        "task": task,
        "checkpoint": checkpoint_summary,
        "tool_calls": tools,
        "blocking_reservations": store.list_blocking_reservations(str(_repo(args)), args.task_id),
    }


def _pending(args: argparse.Namespace) -> list[dict[str, Any]]:
    calls = _store(args).list_pending_tool_calls(args.task_id)
    result = []
    for call in calls:
        if call["status"] == "waiting_approval":
            next_commands = [
                f"python -m agent_runtime approve {call['tool_use_id']} --repo \"{call['repo_root']}\"",
                f"python -m agent_runtime deny {call['tool_use_id']} --repo \"{call['repo_root']}\"",
            ]
        else:
            next_commands = [
                f"python -m agent_runtime resolve-call {call['tool_use_id']} --repo \"{call['repo_root']}\" --action complete",
                f"python -m agent_runtime resolve-call {call['tool_use_id']} --repo \"{call['repo_root']}\" --action retry",
                f"python -m agent_runtime resolve-call {call['tool_use_id']} --repo \"{call['repo_root']}\" --action abort",
            ]
        result.append({
            "task_id": call["task_id"],
            "task_status": call["task_status"],
            "tool_use_id": call["tool_use_id"],
            "tool": call["name"],
            "args": call["args"],
            "status": call["status"],
            "reason": call.get("permission_reason") or call.get("error"),
            "next_commands": next_commands,
        })
    return result


def _events(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.limit < 1:
        raise SystemExit("events --limit must be positive")
    events = _store(args).list_events(args.task_id)
    if args.event_type:
        events = [event for event in events if event["type"] == args.event_type]
    return events[-args.limit:]


def _check(name: str, status: str, message: str, **details: Any) -> dict[str, Any]:
    return {"name": name, "status": status, "message": message, **details}


def _disk_check(name: str, path: Path, minimum_free_mb: int) -> dict[str, Any]:
    try:
        free_mb = shutil.disk_usage(path).free / (1024 * 1024)
    except OSError as exc:
        return _check(name, "fail", str(exc), path=str(path))
    status = "pass" if free_mb >= minimum_free_mb else "fail"
    return _check(
        name,
        status,
        f"{free_mb:.0f} MiB free (minimum {minimum_free_mb} MiB)",
        path=str(path),
        free_mb=round(free_mb, 1),
    )


def _doctor(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    repo = _repo(args)
    checks: list[dict[str, Any]] = []
    if args.minimum_free_mb < 0:
        raise SystemExit("doctor --minimum-free-mb must be non-negative")
    if repo.exists() and repo.is_dir():
        checks.append(_check("repository", "pass", "Repository directory exists", path=str(repo)))
        checks.append(_disk_check("repository_disk", repo, args.minimum_free_mb))
    else:
        checks.append(_check("repository", "fail", "Repository directory does not exist", path=str(repo)))

    temp_root = Path(tempfile.gettempdir()).resolve()
    checks.append(_disk_check("temporary_disk", temp_root, args.minimum_free_mb))
    try:
        with tempfile.NamedTemporaryFile(prefix="agent-runtime-doctor-", dir=temp_root):
            pass
        checks.append(_check("temporary_write", "pass", "Temporary directory is writable", path=str(temp_root)))
    except OSError as exc:
        checks.append(_check("temporary_write", "fail", str(exc), path=str(temp_root)))

    try:
        PermissionEngine(repo, policy_path=args.policy)
        checks.append(_check("policy", "pass", "Permission policy is valid", path=args.policy))
    except Exception as exc:
        checks.append(_check("policy", "fail", str(exc), path=args.policy))

    dotenv_path = find_dotenv(usecwd=True)
    if dotenv_path:
        load_dotenv(dotenv_path=dotenv_path, override=True)
    model_id = os.getenv("MODEL_ID")
    credential_present = bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))
    checks.append(_check(
        "model_configuration", "pass" if model_id else "warn",
        f"MODEL_ID is configured ({model_id})" if model_id else "MODEL_ID is not configured; deterministic Eval still works",
    ))
    checks.append(_check(
        "provider_credential", "pass" if credential_present else "warn",
        "Provider credential is configured" if credential_present else "No provider credential; real-model run/resume is unavailable",
    ))
    try:
        timeout = float(os.getenv("AGENT_RUNTIME_MODEL_TIMEOUT_SECONDS", "120"))
        status = "pass" if 0 < timeout < 300 else "fail"
        message = f"Model timeout {timeout:g}s is below default lease TTL 300s" if status == "pass" else (
            "Model timeout must be positive and below default lease TTL 300s"
        )
        checks.append(_check("timeout_margin", status, message, timeout_seconds=timeout))
    except ValueError:
        checks.append(_check("timeout_margin", "fail", "AGENT_RUNTIME_MODEL_TIMEOUT_SECONDS is not numeric"))

    database = _db_path(repo)
    if database.exists():
        try:
            store = EventStore(database)
            integrity = store.integrity_check()
            invariants = store.scan_invariants()
            status = "pass" if not integrity and not invariants else "fail"
            checks.append(_check(
                "database", status, "SQLite and Runtime invariants are healthy" if status == "pass" else "Database check failed",
                path=str(database), integrity_errors=integrity, invariant_violations=invariants,
            ))
        except Exception as exc:
            checks.append(_check("database", "fail", str(exc), path=str(database)))
    else:
        checks.append(_check("database", "warn", "Runtime database has not been created yet", path=str(database)))

    ok = not any(item["status"] == "fail" for item in checks)
    report = {
        "ok": ok,
        "version": __version__,
        "repository": str(repo),
        "checks": checks,
        "warnings": sum(item["status"] == "warn" for item in checks),
        "failures": sum(item["status"] == "fail" for item in checks),
    }
    return report, 0 if ok else 2


def _db_check(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    store = _store(args)
    integrity = store.integrity_check()
    invariants = store.scan_invariants()
    report = {
        "ok": not integrity and not invariants,
        "database": str(store.path),
        "schema_integrity_errors": integrity,
        "runtime_invariant_violations": invariants,
        "task_count": len(store.list_tasks()),
    }
    return report, 0 if report["ok"] else 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "eval":
        if args.live:
            raise SystemExit("Live eval is intentionally outside the MVP runner; omit --live.")
        report = run_suite(args.suite, args.runs)
        if args.output:
            write_report(report, args.output)
        _print_json(report)
        return 0 if report["passed"] == report["total"] else 2

    if args.command == "run":
        return _print_result(_runtime(args).run(args.prompt))
    if args.command == "resume":
        return _print_result(_runtime(args).resume(args.task_id))
    if args.command == "approve":
        return _print_result(_runtime(args).approve(args.tool_use_id))
    if args.command == "deny":
        return _print_result(_runtime(args).deny(args.tool_use_id))
    if args.command == "resolve-call":
        return _print_result(_runtime(args).resolve_call(args.tool_use_id, args.action))
    if args.command == "status":
        _print_json(_store(args).get_task(args.task_id))
        return 0
    if args.command == "list":
        _print_json(_task_list(args))
        return 0
    if args.command == "show":
        _print_json(_task_show(args))
        return 0
    if args.command == "pending":
        _print_json(_pending(args))
        return 0
    if args.command == "events":
        _print_json(_events(args))
        return 0
    if args.command == "trace":
        store = _store(args)
        reporter = TraceReporter(store)
        summary = reporter.summary(args.task_id)
        if args.output:
            reporter.export_jsonl(args.task_id, args.output)
        _print_json(summary)
        return 0
    if args.command == "doctor":
        report, exit_code = _doctor(args)
        _print_json(report)
        return exit_code
    if args.command == "db-check":
        report, exit_code = _db_check(args)
        _print_json(report)
        return exit_code
    raise SystemExit(f"Unsupported command: {args.command}")
