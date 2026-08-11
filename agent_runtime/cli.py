from __future__ import annotations

import argparse
import json
from pathlib import Path

from .eval_runner import run_suite, write_report
from .providers import AnthropicModel
from .runtime import Runtime
from .store import EventStore
from .trace import TraceReporter


def _runtime(args: argparse.Namespace) -> Runtime:
    repo = Path(args.repo).resolve()
    return Runtime(repo, AnthropicModel(), policy_path=getattr(args, "policy", None), interactive=True)


def _print_result(result) -> int:
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    return 0 if result.status == "completed" else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-runtime")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run")
    run.add_argument("--repo", default=".")
    run.add_argument("--policy")
    run.add_argument("--prompt", required=True)

    for name in ("resume", "status", "approve", "deny", "resolve-call", "trace"):
        command = sub.add_parser(name)
        command.add_argument("id", nargs="?")
        command.add_argument("--repo", default=".")
        command.add_argument("--output")
        command.add_argument("--action", choices=("retry", "complete", "abort"))

    evaluation = sub.add_parser("eval")
    evaluation.add_argument("--suite", required=True)
    evaluation.add_argument("--runs")
    evaluation.add_argument("--output")
    evaluation.add_argument("--live", action="store_true", help="Reserved for a future live benchmark; MVP eval is deterministic.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "eval":
        if args.live:
            raise SystemExit("Live eval is intentionally outside the MVP runner; omit --live.")
        report = run_suite(args.suite, args.runs)
        if args.output:
            write_report(report, args.output)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["passed"] == report["total"] else 2

    if args.command == "run":
        return _print_result(_runtime(args).run(args.prompt))
    if args.command == "resume":
        return _print_result(_runtime(args).resume(args.id))
    if args.command == "approve":
        return _print_result(_runtime(args).approve(args.id))
    if args.command == "deny":
        return _print_result(_runtime(args).deny(args.id))
    if args.command == "resolve-call":
        return _print_result(_runtime(args).resolve_call(args.id, args.action))
    if args.command == "status":
        store = EventStore(Path(args.repo).resolve() / ".agent_runtime" / "runtime.db")
        if not args.id:
            raise SystemExit("status requires TASK_ID")
        print(json.dumps(store.get_task(args.id), ensure_ascii=False, indent=2))
        return 0
    if args.command == "trace":
        store = EventStore(Path(args.repo).resolve() / ".agent_runtime" / "runtime.db")
        reporter = TraceReporter(store)
        summary = reporter.summary(args.id)
        if args.output:
            reporter.export_jsonl(args.id, args.output)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    raise SystemExit(f"Unsupported command: {args.command}")
