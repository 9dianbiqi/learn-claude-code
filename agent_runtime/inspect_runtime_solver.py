"""Controlled Runtime-as-Solver fixtures using real Inspect mock generation."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import difflib
import importlib.metadata
import json
import os
from pathlib import Path
import tempfile
from zipfile import ZipFile

import anyio
from inspect_ai import Task, eval as inspect_eval
from inspect_ai.dataset import Sample
from inspect_ai.log import read_eval_log
from inspect_ai.model import GenerateConfig, ModelOutput, get_model
from inspect_ai.scorer import Score, mean, scorer
from inspect_ai.solver import solver

from .completion import CompletionContract
from .eval_runner import _response_from_entry, _unsafe_reparse_chain
from .evaluation import SAFE_TOOLS, _dump, _hash, _identifier, _json, _read, _relative, _validate_script
from .inspect_adapter import INSPECT_VERSION
from .inspect_model_bridge import InspectModelBridge, messages_to_inspect, mock_output, response_from_inspect, tools_to_inspect
from .models import VerifiedSubtaskConfig, VerifiedSubtaskDAGConfig, VerifierResult
from .recovery_advisor import RecoveryAdvisor
from .runtime import InjectedCrash, Runtime
from .trace import TraceReporter

FAULT_POINTS = {"after_model_response", "after_model_response_before_persist", "after_tool_persist"}


@dataclass(frozen=True)
class ContainerCaseBinding:
    """Trusted controller-only capability; unavailable through CLI plan data."""
    bridge: object
    verifier_node: VerifiedSubtaskConfig


def validate_plan(plan: dict, bindings: dict | None = None) -> None:
    if not isinstance(plan, dict) or plan.get("schema_version") != "inspect-runtime-plan-v1":
        raise ValueError("unsupported Runtime solver plan schema")
    if set(plan) != {"schema_version", "run_id", "cases"}:
        raise ValueError("unexpected plan fields")
    _identifier(plan["run_id"])
    cases = plan["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= 8:
        raise ValueError("1..8 controlled cases required")
    ids, container_ids = set(), set()
    for case in cases:
        if not isinstance(case, dict) or set(case) - {
            "id", "prompt", "files", "goal_files", "model", "max_turns", "checks", "fault_point", "fault_tool_id", "execution_kind"}:
            raise ValueError("unexpected case fields")
        ident = _identifier(case["id"])
        if len(ident) > 24 or ident.casefold() in ids:
            raise ValueError("unique portable case identifiers of at most 24 characters required")
        ids.add(ident.casefold())
        if not isinstance(case.get("prompt"), str) or not case["prompt"].strip():
            raise ValueError("explicit fixture prompt required")
        if type(case.get("max_turns")) is not int or not 1 <= case["max_turns"] <= 16:
            raise ValueError("max_turns must be an integer in 1..16")
        if case.get("fault_point") is not None and case["fault_point"] not in FAULT_POINTS:
            raise ValueError("unsupported durable-boundary fault")
        if case.get("fault_tool_id") is not None:
            _identifier(case["fault_tool_id"])
            if case.get("fault_point") != "after_tool_persist":
                raise ValueError("tool fault selector requires after_tool_persist")
        kind = case.get("execution_kind", "local_fixture")
        if kind not in {"local_fixture", "container_read"}:
            raise ValueError("unknown execution kind")
        if kind == "local_fixture":
            _validate_script(case)
        else:
            container_ids.add(ident)
            binding = (bindings or {}).get(ident)
            if not isinstance(binding, ContainerCaseBinding):
                raise ValueError("container case requires a trusted controller binding")
            if tuple(binding.bridge.names) != ("container_read_file", "container_find_files", "container_search_symbols"):
                raise ValueError("existing container-only read bridge required")
            if (not isinstance(binding.verifier_node, VerifiedSubtaskConfig)
                    or binding.verifier_node.subtask_id != "fixture"
                    or binding.verifier_node.max_turns != case["max_turns"]):
                raise ValueError("container verifier node must bind the same durable turn limit")
        if len(json.dumps(case).encode()) > 64 * 1024:
            raise ValueError("case exceeds 64 KiB")
        model = case.get("model")
        if not isinstance(model, list) or not 1 <= len(model) <= 16:
            raise ValueError("1..16 frozen mock responses required")
        for entry in model:
            if not isinstance(entry, dict) or set(entry) - {"tool", "id", "input", "text", "usage", "expect_feedback"}:
                raise ValueError("unsupported response fields")
            if ("tool" in entry) == ("text" in entry):
                raise ValueError("each response must contain exactly one tool or text")
            if "text" in entry and not isinstance(entry["text"], str):
                raise ValueError("response text must be a string")
            if "tool" in entry:
                _identifier(entry["id"])
                if not isinstance(entry.get("input"), dict):
                    raise ValueError("tool input must be an object")
            if "usage" in entry:
                usage = entry["usage"]
                if (not isinstance(usage, dict) or set(usage) != {"input_tokens", "output_tokens"}
                        or any(type(v) is not int or not 0 <= v <= 128 for v in usage.values())):
                    raise ValueError("explicit synthetic usage must have two counts in 0..128")
            if entry.get("expect_feedback") is not None and not isinstance(entry["expect_feedback"], str):
                raise ValueError("feedback assertion must be text")
            if kind == "container_read" and "tool" in entry and entry["tool"] not in binding.bridge.names:
                raise ValueError("container fixture may not request native tools")
            _response_from_entry(entry)
        goals = case.get("goal_files", {})
        if not isinstance(goals, dict) or (kind == "local_fixture" and not 1 <= len(goals) <= 8):
            raise ValueError("1..8 frozen fixture goals required")
        checks = case.get("checks")
        if not isinstance(checks, dict) or checks.get("status") not in {"completed", "failed", "needs_review"}:
            raise ValueError("explicit Runtime status oracle required")
        for mapping in (case.get("files", {}), goals, checks.get("files", {})):
            if not isinstance(mapping, dict):
                raise ValueError("file maps must be objects")
            portable = set()
            for name, text in mapping.items():
                _relative(name)
                if name.casefold() in portable or not isinstance(text, str):
                    raise ValueError("file paths must be unique and contents text")
                portable.add(name.casefold())
        if kind == "local_fixture" and not set(goals) <= set(case.get("files", {})):
            raise ValueError("goals must name frozen fixture files")
        fixture_names = [p.casefold().split("/") for p in case.get("files", {})]
        if any(a != b and len(a) < len(b) and b[:len(a)] == a for a in fixture_names for b in fixture_names):
            raise ValueError("fixture file/directory collision")
    if set(bindings or {}) != container_ids:
        raise ValueError("unexpected trusted bindings")


def fixture_model(case: dict, counts: dict):
    entries = list(case["model"])
    def respond(messages, tools, tool_choice, config):
        counts["calls"] += 1
        if not entries:
            raise RuntimeError("frozen Inspect mock script exhausted")
        entry = entries.pop(0)
        expected = entry.get("expect_feedback")
        if expected and expected not in json.dumps([m.model_dump(mode="json") for m in messages], ensure_ascii=False):
            raise RuntimeError("expected Runtime tool feedback not delivered")
        counts["outputs"] += 1
        return mock_output(_response_from_entry(entry))
    model = get_model("mockllm/runtime-bridge", memoize=False, custom_outputs=respond,
                      config=GenerateConfig(max_tokens=2048, max_retries=0, max_connections=1))
    # The callback is already installed on MockLLM. Task.model serialization must
    # contain JSON configuration, not a Python function; the frozen plan is its recipe.
    model.model_args.pop("custom_outputs", None)
    return model


def execute_case(case: dict, root: Path, model, binding: ContainerCaseBinding | None = None) -> dict:
    """Worker-thread entry; each fresh case has exactly one durable Runtime task."""
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=False)
    for name, content in case.get("files", {}).items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    bridge = InspectModelBridge(model, root / "bridge-calls", case["id"])
    fault_triggered = False

    def verify(context):
        entries = []
        for name, expected in case["goal_files"].items():
            path = workspace / name
            if not path.is_file() or path.read_text(encoding="utf-8") != expected:
                return VerifierResult("fail", "Frozen fixture goal is not satisfied", [])
            entries.append({"path": name, "sha256": _hash(path.read_bytes())})
        return VerifierResult("pass", "Frozen fixture goal satisfied", entries)

    node = binding.verifier_node if binding else VerifiedSubtaskConfig(
        "fixture", "Complete the controlled fixture", "Satisfy the frozen verifier",
        tuple(case["goal_files"]), "inspect-fixture-verifier", "1",
        _hash(json.dumps(case.get("goal_files", {}), sort_keys=True).encode()), verify,
        _hash(Path(__file__).read_bytes()), max_turns=case["max_turns"])
    policy = None
    if binding:
        policy = root / "controller-policy.yaml"
        policy.write_text("rules:\n  - effect: allow\n    tools: [container_read_file, container_find_files, container_search_symbols]\n", encoding="utf-8")

    def inject(point, tool_use_id=None, **_):
        nonlocal fault_triggered
        if (not fault_triggered and point == case.get("fault_point")
                and (not case.get("fault_tool_id") or case["fault_tool_id"] == tool_use_id)):
            fault_triggered = True
            raise InjectedCrash(point)

    def engine(with_fault=False):
        runtime = Runtime(workspace, bridge, tool_scope=binding.bridge.names if binding else SAFE_TOOLS,
            tool_setup=binding.bridge.install if binding else None, policy_path=policy,
            verified_subtask_dag=VerifiedSubtaskDAGConfig([node]),
            completion_contract=CompletionContract("verified_task", 1), recovery_advisor=RecoveryAdvisor(),
            approval_callback=lambda *_: True, fault_injector=inject if with_fault else None)
        bridge.store = runtime.store
        if binding:
            binding.bridge.backend.heartbeat = runtime.heartbeat_verification
        return runtime

    runtime = engine(bool(case.get("fault_point")))
    try:
        try:
            outcome = runtime.run(case["prompt"])
        except InjectedCrash:
            tasks = runtime.store.list_tasks()
            if len(tasks) != 1:
                raise ValueError("fault did not preserve exactly one task")
            runtime = engine()
            outcome = runtime.resume(tasks[0]["task_id"])
        tasks = runtime.store.list_tasks()
        if len(tasks) != 1:
            raise ValueError("solver created more than one Runtime task")
        task_id = outcome.task_id
        runtime.store.assert_invariants(task_id)
        passed = outcome.status == case["checks"]["status"]
        for name, expected in case["checks"].get("files", {}).items():
            path = workspace / name
            passed = passed and path.is_file() and path.read_text(encoding="utf-8") == expected
        if case.get("fault_point") and not fault_triggered:
            passed = False
        checkpoint = runtime.store.get_checkpoint(runtime.store.get_task(task_id)["checkpoint_id"])
        row = {"id": case["id"], "task_id": task_id, "runtime_status": outcome.status,
               "error": outcome.error, "runtime_case_pass": passed, "task_success": None, "official_resolved": None,
               "fault_triggered": fault_triggered, "bridge_calls": bridge.calls,
               "consumed_turns": runtime.store.get_plan_item(task_id, "fixture")["consumed_turns"],
               "messages": checkpoint["messages"], "final_text": outcome.final_text,
               "execution_kind": case.get("execution_kind", "local_fixture")}
        return row
    finally:
        tasks = runtime.store.list_tasks()
        if len(tasks) == 1:
            task_id = tasks[0]["task_id"]
            reporter = TraceReporter(runtime.store)
            _dump(root / "runtime-summary.json", reporter.summary(task_id))
            _dump(root / "model-calls.json", runtime.store.list_model_calls(task_id))
            _dump(root / "tool-calls.json", runtime.store.list_tool_calls(task_id))
            _dump(root / "verifier-runs.json", runtime.store.list_verifier_runs(task_id))
            reporter.export_jsonl(task_id, root / "trace.jsonl")
        patch = []
        paths = set(case.get("files", {}))
        if not binding:
            paths.update(e["input"]["path"] for e in case["model"] if e.get("tool") in SAFE_TOOLS)
        for name in sorted(paths):
            before = case.get("files", {}).get(name, "")
            path = workspace / name
            after = path.read_text(encoding="utf-8") if path.is_file() else ""
            patch.extend(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                            fromfile="a/" + name if name in case.get("files", {}) else "/dev/null",
                                            tofile="b/" + name))
        (root / "workspace-diff.txt").write_text("".join(patch), encoding="utf-8")


@scorer(metrics=[mean()])
def runtime_regression():
    async def score(state, target):
        row = state.metadata["runtime_result"]
        return Score(value=int(row["runtime_case_pass"]), metadata={"runtime_result": row},
                     explanation="Controlled Runtime regression oracle only; no Agent or SWE-bench score.")
    return score


def runtime_solver(case: dict, root: str, binding: ContainerCaseBinding | None = None):
    # Inspect serializes registered factory arguments. Keep controller capabilities
    # and hidden goals in a trusted closure; frozen plan/hash supplies provenance.
    @solver(name="controlled_runtime_solver")
    def configured():
        async def solve(state, generate):
            model = get_model()
            # Cancellation is deferred across the finite Runtime execution, not
            # allowed to leave its worker/model callback orphaned after return.
            with anyio.CancelScope(shield=True):
                result = await anyio.to_thread.run_sync(lambda: execute_case(case, Path(root), model, binding),
                                                       abandon_on_cancel=False)
            messages = result.pop("messages")
            final_text = result.pop("final_text")
            _dump(Path(root) / "result.json", result)
            _dump(Path(root) / "final-messages.json", messages)
            state.metadata["runtime_result"] = result
            state.messages = messages_to_inspect(messages)
            state.output = ModelOutput.from_content(model="mockllm/runtime-bridge", content=final_text)
            state.completed = True
            return state
        return solve
    return configured()


def usage_summary(usages: list[dict | None]) -> dict:
    known = [u for u in usages if isinstance(u, dict)
             and all(type(u.get(k)) is int and u[k] >= 0 for k in ("input_tokens", "output_tokens"))]
    complete = len(known) == len(usages)
    result = {"complete": complete, "known_calls": len(known), "missing_calls": len(usages) - len(known)}
    for key in ("input_tokens", "output_tokens"):
        subtotal = sum(u[key] for u in known)
        result["known_" + key] = subtotal
        result[key] = subtotal if complete else None
    return result


def validate_temp_storage() -> None:
    """Inspect's .eval recorder appends/truncates an anonymous temporary ZIP."""
    try:
        with tempfile.TemporaryFile() as stream:
            with ZipFile(stream, "w") as archive:
                archive.writestr("first", "temporary storage probe")
            with ZipFile(stream, "a") as archive:
                archive.writestr("second", "append/truncate probe")
    except OSError as exc:
        raise ValueError("Inspect .eval logging requires anonymous seek/truncate support; use a native temporary directory") from exc


def check_logs(logs, cases: list[dict], root: Path, counts: dict) -> list[dict]:
    expected = {c["id"]: c for c in cases}
    seen, records = set(), []
    for header in logs:
        path = Path(header.location).absolute()
        if not path.is_relative_to(root) or path.suffix != ".eval":
            raise ValueError("unexpected Inspect log path")
        log = read_eval_log(str(path), resolve_attachments=True)
        ident = (log.eval.metadata or {}).get("case_id")
        if ident not in expected or ident in seen or log.status != "success" or len(log.samples or []) != 1:
            raise ValueError("Inspect case identity, status or denominator mismatch")
        seen.add(ident)
        sample = log.samples[0]
        result = _json(_read(root / ident / "result.json"))
        model_calls = _json(_read(root / ident / "model-calls.json"))
        receipts = [_json(_read(p)) for p in sorted((root / ident / "bridge-calls").glob("*.json"))]
        if sample.id != ident or sample.error or sample.metadata.get("runtime_result") != result:
            raise ValueError("Inspect sample/result mismatch")
        if (sample.scores or {}).get("runtime_regression") is None or sample.scores["runtime_regression"].value != int(result["runtime_case_pass"]):
            raise ValueError("Inspect regression score mismatch")
        metrics = log.results.scores if log.results else []
        if (len(metrics) != 1 or metrics[0].name != "runtime_regression"
                or set(metrics[0].metrics) != {"mean"}
                or metrics[0].metrics["mean"].value != int(result["runtime_case_pass"])):
            raise ValueError("Inspect regression aggregate mismatch")
        actual_messages = [m.model_dump(mode="json", exclude={"id", "source", "model"}) for m in sample.messages]
        final = _json(_read(root / ident / "final-messages.json"))
        expected_messages = [m.model_dump(mode="json", exclude={"id", "source", "model"}) for m in messages_to_inspect(final)]
        if actual_messages != expected_messages:
            raise ValueError("Inspect final conversation differs from Runtime checkpoint")
        events = [event for event in sample.events if event.event == "model"]
        if len(events) != len(receipts) or counts[ident]["calls"] != len(receipts) or result["bridge_calls"] != len(receipts):
            raise ValueError("Inspect generation/Runtime bridge attempt mismatch")
        by_id = {c["model_call_id"]: c for c in model_calls}
        receipt_ids = [r["runtime_model_call_id"] for r in receipts]
        if (len(by_id) != len(model_calls) or len(set(receipt_ids)) != len(receipt_ids)
                or any(c["task_id"] != result["task_id"] for c in model_calls)):
            raise ValueError("duplicate or foreign Runtime/bridge model record")
        pre_generation_failures = [c for c in model_calls if c["model_call_id"] not in receipt_ids]
        if any(c["status"] != "failed" or c.get("response") is not None for c in pre_generation_failures):
            raise ValueError("unmatched nonfailed Runtime model record")
        for receipt, event in zip(receipts, events):
            recorded = by_id.get(receipt["runtime_model_call_id"])
            if (recorded is None or recorded["task_id"] != receipt["task_id"] or receipt["case_id"] != ident
                    or recorded["turn"] != receipt["turn"]):
                raise ValueError("bridge receipt/Runtime ledger linkage mismatch")
            ledger_hash = _hash(json.dumps(recorded["request"], sort_keys=True, ensure_ascii=False).encode())
            projection = recorded.get("projection") or {}
            projected_messages = projection.get("messages") if projection.get("projection_used") else recorded["request"]["messages"]
            expected_request = {"messages": projected_messages, "tools": recorded["request"]["tools"]}
            if (receipt.get("ledger_request_sha256") != ledger_hash or receipt["runtime_request"] != expected_request
                    or receipt.get("projection") != recorded.get("projection")
                    or receipt.get("source_checkpoint_id") != recorded.get("source_checkpoint_id")):
                raise ValueError("bridge request differs from durable Runtime projection/request")
            request = json.dumps(receipt["runtime_request"], sort_keys=True, ensure_ascii=False).encode()
            if _hash(request) != receipt["request_sha256"]:
                raise ValueError("bridge request digest mismatch")
            output = event.output.model_dump(mode="json") if event.output is not None else None
            if output != receipt.get("inspect_output"):
                raise ValueError("Inspect ModelEvent output differs from durable bridge receipt")
            if recorded["status"] == "succeeded":
                if not receipt["ok"] or recorded.get("response") != response_from_inspect(
                        ModelOutput.model_validate(receipt["inspect_output"])).as_dict():
                    raise ValueError("Runtime confirmed response differs from Inspect response")
            elif recorded["status"] not in {"failed", "abandoned"} or recorded.get("response") is not None:
                raise ValueError("unconfirmed Runtime response has inconsistent status/data")
            expected_input = [m.model_dump(mode="json", exclude={"id", "source", "model"})
                              for m in messages_to_inspect(receipt["runtime_request"]["messages"])]
            actual_input = [m.model_dump(mode="json", exclude={"id", "source", "model"}) for m in event.input]
            if actual_input != expected_input:
                raise ValueError("Inspect ModelEvent input differs from Runtime request")
            expected_tools = [t.model_dump(mode="json") for t in tools_to_inspect(receipt["runtime_request"]["tools"])]
            if [t.model_dump(mode="json") for t in event.tools] != expected_tools:
                raise ValueError("Inspect ModelEvent tool schemas differ from Runtime request")
        synthetic_usage = usage_summary([(r.get("inspect_output") or {}).get("usage") for r in receipts])
        stats = log.stats.model_usage
        if set(stats) - {"mockllm/runtime-bridge"}:
            raise ValueError("unexpected model in controlled Inspect log")
        for key in ("input_tokens", "output_tokens"):
            if sum(getattr(u, key) for u in stats.values()) != synthetic_usage["known_" + key]:
                raise ValueError("Inspect usage differs from bridge receipts")
        row = {**result, "inspect_model_events": len(events), "runtime_model_records": len(model_calls),
               "runtime_pre_generation_failures": len(pre_generation_failures),
               "runtime_abandoned_records": sum(c["status"] == "abandoned" for c in model_calls),
               "log": path.relative_to(root).as_posix(), "log_sha256": _hash(_read(path)),
               "synthetic_inspect_usage": synthetic_usage,
               "runtime_confirmed_synthetic_usage": usage_summary([(c.get("response") or {}).get("usage") for c in model_calls]),
               "provider_input_tokens": None, "provider_output_tokens": None}
        records.append(row)
    if seen != set(expected):
        raise ValueError("missing Inspect Runtime case logs")
    return sorted(records, key=lambda r: list(expected).index(r["id"]))


def run(plan_path: str | Path, output: str | Path, *, bindings: dict | None = None) -> dict:
    plan_path = Path(os.path.abspath(plan_path))
    data = _read(plan_path)
    plan = _json(data)
    validate_plan(plan, bindings)
    if importlib.metadata.version("inspect-ai") != INSPECT_VERSION:
        raise ValueError("Inspect package version does not match pin")
    validate_temp_storage()
    root = Path(os.path.abspath(output))
    if root.exists() or _unsafe_reparse_chain(root) or root.is_relative_to(plan_path.parent):
        raise ValueError("fresh output outside the plan directory required")
    root.mkdir(parents=True, exist_ok=False)
    (root / "plan.json").write_bytes(data)
    counts, tasks = {}, []
    report = {"schema_version": "inspect-runtime-result-v1", "run_id": plan["run_id"],
              "plan_sha256": _hash(data), "processing_ok": False,
              "new_provider_requests": 0, "new_official_evaluations": 0, "model_mode": "inspect_mock",
              "task_success": None, "official_resolved": None}
    try:
        for case in plan["cases"]:
            counts[case["id"]] = {"calls": 0, "outputs": 0}
            case_root = root / case["id"]
            case_root.mkdir()
            tasks.append(Task(name="controlled_runtime", version=1,
                dataset=[Sample(id=case["id"], input=case["prompt"], metadata={"case_id": case["id"]})],
                solver=runtime_solver(case, str(case_root), (bindings or {}).get(case["id"])),
                scorer=runtime_regression(), model=fixture_model(case, counts[case["id"]]),
                metadata={"case_id": case["id"], "usage_kind": "synthetic_mock", "plan_sha256": _hash(data)},
                time_limit=90, token_limit=4096, fail_on_error=True))
        logs = inspect_eval(tasks, model=None, display="none", log_dir=str(root / "logs"), log_format="eval",
            log_samples=True, log_realtime=False, max_samples=1, max_tasks=1, epochs=1,
            retry_on_error=0, notification=False, ctl_server=False, acp_server=False, checkpoint=False)
        _dump(root / "inspect-headers.json", json.loads(json.dumps(
            [log.model_dump(mode="python") for log in logs], default=lambda obj: "<" + type(obj).__name__ + ">")))
        report["results"] = check_logs(logs, plan["cases"], root, counts)
        report["runtime_cases"] = {"total": len(plan["cases"]),
            "passed": sum(r["runtime_case_pass"] for r in report["results"])}
        report["processing_ok"] = True
    except Exception as exc:  # noqa: BLE001 - keep failed Inspect logs and Runtime evidence
        report["error"] = f"{type(exc).__name__}: {exc}"
    report["mock_attempts"] = counts
    _dump(root / "report.json", report)
    _dump(root / "seal.json", [{"path": p.relative_to(root).as_posix(), "sha256": _hash(_read(p))}
                               for p in sorted(root.rglob("*")) if p.is_file()])
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        report = run(args.plan, args.output)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(2, f"controlled Runtime input error: {exc}\n")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["processing_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
