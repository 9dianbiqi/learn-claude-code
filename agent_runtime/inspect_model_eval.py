"""Bounded model evaluation on local tasks or trusted container repair bindings.

Default CLI action is preflight. --execute explicitly enables the configured
model. Mock mode exercises the same execution path without provider requests.
Provisioning and official scoring are separate; this is not a hostile-code sandbox.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import importlib.metadata
import json
import os
from pathlib import Path

import anyio
from inspect_ai import Task, eval as inspect_eval
from inspect_ai.dataset import Sample
from inspect_ai.log import read_eval_log
from inspect_ai.model import GenerateConfig, ModelOutput, get_model
from inspect_ai.scorer import Score, mean, scorer
from inspect_ai.solver import solver

from .eval_runner import _unsafe_reparse_chain
from .evaluation import _dump, _hash, _json, _read
from .inspect_adapter import INSPECT_VERSION
from .inspect_model_bridge import InspectModelBridge, messages_to_inspect, response_from_inspect, tools_to_inspect
from .inspect_runtime_solver import execute_case, fixture_model, usage_summary, validate_plan, validate_temp_storage


def validate(plan: dict, bindings: dict | None = None) -> list[dict]:
    if (not isinstance(plan, dict) or set(plan) != {"schema_version", "run_id", "mode", "model", "limits", "cases"}
            or plan["schema_version"] != "inspect-model-eval-v1" or plan["mode"] not in {"mock", "live"}):
        raise ValueError("invalid model evaluation plan")
    model = plan["model"]
    if not isinstance(model, str) or "/" not in model or not all(model.split("/", 1)):
        raise ValueError("explicit provider/model required")
    if (plan["mode"] == "mock" and model != "mockllm/runtime-bridge") or (plan["mode"] == "live" and model.startswith("mockllm/")):
        raise ValueError("model/mode mismatch")
    limits = plan["limits"]
    bounds = {"max_requests_per_case": (1, 16), "max_output_tokens": (1, 4096), "request_timeout_seconds": (1, 120)}
    if not isinstance(limits, dict) or set(limits) != set(bounds):
        raise ValueError("explicit finite request/token/time limits required")
    for key, (low, high) in bounds.items():
        if type(limits[key]) is not int or not low <= limits[key] <= high:
            raise ValueError("invalid model limit: " + key)
    if not isinstance(plan["cases"], list):
        raise ValueError("cases must be a list")
    cases = []
    for source in plan["cases"]:
        if not isinstance(source, dict) or set(source) - {"id", "prompt", "files", "goal_files", "max_turns", "model", "execution_kind"}:
            raise ValueError("unexpected model task fields")
        if source.get("execution_kind", "local_fixture") not in {"local_fixture", "container_repair"}:
            raise ValueError("unsupported model execution kind")
        if source.get("execution_kind") == "container_repair" and source.get("goal_files", {}):
            raise ValueError("container repair requires its controller verifier, not local file goals")
        if plan["mode"] == "live" and "model" in source:
            raise ValueError("live tasks cannot carry scripted model answers")
        if source.get("max_turns", 0) > limits["max_requests_per_case"]:
            raise ValueError("turn limit exceeds explicit request budget")
        case = dict(source)
        if plan["mode"] == "mock" and "model" not in case:
            raise ValueError("mock tasks require explicit responses")
        case.setdefault("model", [{"text": "validation placeholder, never sent to a live model"}])
        case["checks"] = {"status": "completed", "files": case.get("goal_files", {})}
        cases.append(case)
    validate_plan({"schema_version": "inspect-runtime-plan-v1", "run_id": plan["run_id"], "cases": cases}, bindings)
    if importlib.metadata.version("inspect-ai") != INSPECT_VERSION:
        raise ValueError("Inspect version differs from pin")
    return cases


@dataclass(frozen=True)
class ModelPolicy:
    model: str
    mode: str
    max_requests: int
    timeout: int


class BoundedModelBridge(InspectModelBridge):
    """Same conversion/audit protocol with explicit model and request bounds."""
    name = "inspect-bounded-model-bridge"

    def __init__(self, model, output, case_id, policy: ModelPolicy):
        if str(model) != policy.model:
            raise ValueError("configured model identity differs from execution model")
        self.model, self.output, self.case_id = model, Path(output), case_id
        self.output.mkdir(parents=True, exist_ok=False)
        self.store, self.calls = None, 0
        self.policy = policy
        self.usage_kind = "provider" if policy.mode == "live" else "synthetic_mock"
        self.generation_timeout = policy.timeout

    def complete(self, messages, tools):
        if self.calls >= self.policy.max_requests:
            raise RuntimeError("explicit model request budget exhausted")
        return super().complete(messages, tools)


@scorer(metrics=[mean()])
def fixture_success():
    async def score(state, target):
        row = state.metadata["model_task_result"]
        return Score(value=int(row["runtime_case_pass"]),
                     explanation="Controller verifier outcome, not official SWE-bench resolved.")
    return score


def model_solver(case, root: Path, policy, binding=None):
    @solver(name="bounded_runtime_model")
    def configured():
        async def solve(state, generate):
            model = get_model()
            def work():
                return execute_case(case, root, model, binding,
                    bridge_factory=lambda m, p, c: BoundedModelBridge(m, p, c, policy))
            with anyio.CancelScope(shield=True):
                row = await anyio.to_thread.run_sync(work, abandon_on_cancel=False)
            state.messages = messages_to_inspect(row.pop("messages"))
            state.output = ModelOutput.from_content(model=policy.model, content=row.pop("final_text"))
            row["fixture_success"] = row["runtime_case_pass"]
            row["model_mode"] = policy.mode
            _dump(root / "result.json", row)
            state.metadata["model_task_result"] = row
            state.completed = True
            return state
        return solve
    return configured()


def run(plan_path: str | Path, output: str | Path, *, execute: bool = False, bindings: dict | None = None) -> dict:
    path, root = Path(os.path.abspath(plan_path)), Path(os.path.abspath(output))
    data = _read(path)
    plan = _json(data)
    cases = validate(plan, bindings)
    limits = plan["limits"]
    if not execute:
        return {"status": "PREFLIGHT_ONLY", "cases": len(cases), "model": plan["model"],
                "mode": plan["mode"], "max_requests": len(cases) * limits["max_requests_per_case"],
                "new_provider_requests": 0, "new_official_evaluations": 0}
    validate_temp_storage()
    if root.exists() or _unsafe_reparse_chain(root) or root.is_relative_to(path.parent):
        raise ValueError("fresh output outside plan directory required")
    root.mkdir(parents=True)
    (root / "plan.json").write_bytes(data)
    binding_manifest = {ident: {"tools": list(binding.bridge.names), "identity": binding.bridge.identity,
                                "verifier_id": binding.verifier_node.verifier_id}
                        for ident, binding in (bindings or {}).items()}
    _dump(root / "bindings.json", binding_manifest)
    policy = ModelPolicy(plan["model"], plan["mode"], limits["max_requests_per_case"], limits["request_timeout_seconds"])
    report = {"schema_version": "inspect-model-result-v1", "run_id": plan["run_id"],
              "plan_sha256": _hash(data), "model": plan["model"], "model_mode": plan["mode"],
              "processing_ok": False, "official_resolved": None, "new_official_evaluations": 0,
              "planned_cases": len(cases), "results": []}
    try:
        tasks = []
        for case in cases:
            case_root = root / case["id"]
            case_root.mkdir()
            model = fixture_model(case, {"calls": 0, "outputs": 0}) if plan["mode"] == "mock" else get_model(
                plan["model"], memoize=False, config=GenerateConfig(max_tokens=limits["max_output_tokens"],
                    max_retries=0, max_connections=1, timeout=limits["request_timeout_seconds"]))
            tasks.append(Task(name="runtime_model_fixture", dataset=[Sample(id=case["id"], input=case["prompt"])],
                              model=model, solver=model_solver(case, case_root, policy, (bindings or {}).get(case["id"])), scorer=fixture_success(),
                              fail_on_error=True, time_limit=limits["max_requests_per_case"] * policy.timeout + 30))
        headers = inspect_eval(tasks, display="none", log_dir=str(root / "logs"), log_format="eval",
                               log_samples=True, log_realtime=False, max_samples=1, max_tasks=1, epochs=1,
                               retry_on_error=0, notification=False, ctl_server=False, acp_server=False, checkpoint=False)
        seen = set()
        for header in headers:
            log_path = Path(header.location).absolute()
            if not log_path.is_relative_to(root) or log_path.suffix != ".eval":
                raise ValueError("unexpected Inspect log location")
            log = read_eval_log(str(log_path), resolve_attachments=True)
            if log.status != "success" or len(log.samples or []) != 1:
                raise ValueError("model evaluation log did not complete")
            sample = log.samples[0]
            ident = sample.id
            if ident in seen or ident not in {c["id"] for c in cases}:
                raise ValueError("duplicate/foreign model evaluation sample")
            seen.add(ident)
            row = _json(_read(root / ident / "result.json"))
            if sample.metadata.get("model_task_result") != row:
                raise ValueError("Inspect sample differs from Runtime result")
            receipts = [_json(_read(p)) for p in sorted((root / ident / "bridge-calls").glob("*.json"))]
            records = _json(_read(root / ident / "model-calls.json"))
            by_id = {r["model_call_id"]: r for r in records}
            receipt_ids = [r["runtime_model_call_id"] for r in receipts]
            if len(by_id) != len(records) or len(set(receipt_ids)) != len(receipts):
                raise ValueError("duplicate model attempt identity")
            events = [event for event in sample.events if event.event == "model"]
            if len(events) != len(receipts) or len(receipts) != row["bridge_calls"]:
                raise ValueError("Inspect/bridge request counts differ")
            for receipt, event in zip(receipts, events):
                record = by_id.get(receipt["runtime_model_call_id"])
                if record is None or record["task_id"] != row["task_id"] or receipt["case_id"] != ident:
                    raise ValueError("foreign model receipt")
                if receipt["ledger_request_sha256"] != _hash(json.dumps(record["request"], sort_keys=True, ensure_ascii=False).encode()):
                    raise ValueError("model receipt differs from durable request")
                projection = record.get("projection") or {}
                expected_request = {"messages": projection["messages"] if projection.get("projection_used") else record["request"]["messages"],
                                    "tools": record["request"]["tools"]}
                if receipt["runtime_request"] != expected_request:
                    raise ValueError("model receipt differs from execution projection")
                request = receipt["runtime_request"]
                fields = {"id", "source", "model"}
                expected_input = [m.model_dump(mode="json", exclude=fields) for m in messages_to_inspect(request["messages"])]
                if [m.model_dump(mode="json", exclude=fields) for m in event.input] != expected_input:
                    raise ValueError("Inspect input differs from Runtime request")
                if [t.model_dump(mode="json") for t in event.tools] != [t.model_dump(mode="json") for t in tools_to_inspect(request["tools"])]:
                    raise ValueError("Inspect tools differ from Runtime schemas")
                if (event.output.model_dump(mode="json") if event.output else None) != receipt.get("inspect_output"):
                    raise ValueError("model output differs from Inspect log")
                if record["status"] == "succeeded" and record["response"] != response_from_inspect(
                        ModelOutput.model_validate(receipt["inspect_output"])).as_dict():
                    raise ValueError("Runtime response differs from Inspect generation")
                if record["status"] not in {"succeeded", "failed", "abandoned"}:
                    raise ValueError("model attempt not terminal")
            if any(r["task_id"] != row["task_id"] or (r["model_call_id"] not in receipt_ids and
                   (r["status"] != "failed" or r.get("response") is not None)) for r in records):
                raise ValueError("unmatched Runtime model attempt")
            usage = usage_summary([(r.get("inspect_output") or {}).get("usage") for r in receipts])
            report["results"].append({**row, "usage_kind": policy.mode, "usage": usage,
                                      "log": log_path.relative_to(root).as_posix(), "log_sha256": _hash(_read(log_path))})
        if seen != {c["id"] for c in cases}:
            raise ValueError("missing planned model evaluation sample")
        report["processing_ok"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    receipts = [p for case in cases for p in (root / case["id"] / "bridge-calls").glob("*.json")]
    report["model_attempts"] = len(receipts)
    report["new_provider_requests"] = len(receipts) if plan["mode"] == "live" else 0
    report["request_count_semantics"] = "bridge generation attempts; not confirmed HTTP deliveries or billing units"
    passed = sum(r["fixture_success"] for r in report["results"])
    report["fixture_outcomes"] = {"planned": len(cases), "passed": passed,
                                   "failed": len(report["results"]) - passed,
                                   "unknown": len(cases) - len(report["results"])}
    _dump(root / "report.json", report)
    _dump(root / "seal.json", [{"path": p.relative_to(root).as_posix(), "sha256": _hash(_read(p))}
                              for p in sorted(root.rglob("*")) if p.is_file()])
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan")
    parser.add_argument("output")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        result = run(args.plan, args.output, execute=args.execute)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(2, f"model evaluation input error: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("processing_ok", result.get("status") == "PREFLIGHT_ONLY") else 1


if __name__ == "__main__":
    raise SystemExit(main())
