"""Optional Inspect adapter for sealed historical/offline evaluation bundles.

No Runtime, solver tools, provider, sandbox or official harness is executed.
Install agent_runtime/requirements-inspect.txt before using this module.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path

from inspect_ai import Task, eval as inspect_eval
from inspect_ai.dataset import Sample
from inspect_ai.log import read_eval_log
from inspect_ai.model import GenerateConfig, ModelOutput, get_model
from inspect_ai.scorer import Score, mean, scorer
from inspect_ai.solver import solver

from .eval_runner import _unsafe_reparse_chain
from .evaluation import _dump, _hash, _read
from .evaluation_bundle import OUTCOME_FIELDS, score_counters, verify_bundle

INSPECT_VERSION = "0.3.283"
COUNTERS = ("success_lower_bound", "failure_fraction", "unknown_fraction", "known_fraction")


@solver
def archived_result():
    async def solve(state, generate):
        # Do not invoke generate. Metadata is a verified result projection only.
        state.output = ModelOutput.from_content(
            model="mockllm/offline-archive", content="Archived evidence import; no model generation.")
        state.completed = True
        return state
    return solve


@scorer(metrics={name: [mean()] for name in COUNTERS})
def archived_outcome():
    async def score(state, target):
        row = state.metadata["archived_result"]
        return Score(value=score_counters(row), metadata={"archived_result": row},
                     explanation="Imported evidence; success_lower_bound is not a strict success rate.")
    return score


def guarded_model(attempts: dict):
    def forbid_generation(*_):
        attempts["count"] += 1
        raise RuntimeError("offline evidence adapter forbids model generation")
    return get_model("mockllm/offline-archive", config=GenerateConfig(
        max_tokens=1, max_retries=0, max_connections=1),
        memoize=False, custom_outputs=forbid_generation)


def build_tasks(projected: dict) -> list[Task]:
    tasks = []
    for kind in OUTCOME_FIELDS:
        rows = [row for row in projected["results"] if row["kind"] == kind]
        if not rows:
            continue
        samples = [Sample(id=row["id"], input=f"Import archived {kind} result {row['id']}.",
                          metadata={"archived_result": row}) for row in rows]
        tasks.append(Task(name="offline_archive_" + kind, version=1, dataset=samples,
                          solver=archived_result(), scorer=archived_outcome(),
                          metadata={"offline_import": True, "kind": kind,
                                    "source_seal_sha256": projected["source_seal_sha256"],
                                    "source_aggregate": projected["source_aggregates"][kind]},
                          message_limit=2, token_limit=1, time_limit=30, fail_on_error=True))
    return tasks


def _check_logs(logs, projected: dict, root: Path) -> list[dict]:
    expected_groups = {kind: {row["id"]: row for row in projected["results"] if row["kind"] == kind}
                       for kind in OUTCOME_FIELDS}
    seen = set()
    receipts = []
    for header in logs:
        log_path = Path(header.location).absolute()
        if not log_path.is_relative_to(root) or log_path.suffix != ".eval":
            raise ValueError("Inspect log escaped output or used unexpected format")
        log = read_eval_log(str(log_path))
        kind = (log.eval.metadata or {}).get("kind")
        if kind not in expected_groups or kind in seen or not expected_groups[kind]:
            raise ValueError("unexpected or duplicate Inspect group")
        seen.add(kind)
        if log.status != "success" or log.error is not None:
            raise ValueError("Inspect evaluation/log failed")
        samples = log.samples or []
        expected = expected_groups[kind]
        if len(samples) != len(expected) or {s.id for s in samples} != set(expected):
            raise ValueError("Inspect sample denominator or identity mismatch")
        for sample in samples:
            row = expected[sample.id]
            if sample.error is not None or sample.metadata.get("archived_result") != row:
                raise ValueError("Inspect sample projection mismatch or error")
            score = (sample.scores or {}).get("archived_outcome")
            if score is None or score.value != score_counters(row):
                raise ValueError("Inspect score differs from source outcome")
        metrics = {}
        for result in log.results.scores if log.results else []:
            if result.name not in COUNTERS or set(result.metrics) != {"mean"}:
                raise ValueError("unexpected Inspect metric")
            metrics[result.name] = result.metrics["mean"].value
        expected_metrics = {name: sum(score_counters(r)[name] for r in expected.values()) / len(expected)
                            for name in COUNTERS}
        if metrics != expected_metrics:
            raise ValueError("Inspect aggregate metrics differ from planned denominator")
        # No model usage is expected for this offline path, including mock usage.
        if log.stats.model_usage:
            raise ValueError("offline import unexpectedly recorded model usage")
        receipts.append({"kind": kind, "samples": len(samples), "metrics": metrics,
                         "strict_source_aggregate": projected["source_aggregates"][kind],
                         "log": log_path.relative_to(root).as_posix(), "sha256": _hash(_read(log_path))})
    if seen != {kind for kind, rows in expected_groups.items() if rows}:
        raise ValueError("missing Inspect group log")
    return receipts


def run(bundle: str | Path, output: str | Path, seal_sha256: str) -> dict:
    projected = verify_bundle(bundle, seal_sha256)
    installed = importlib.metadata.version("inspect-ai")
    if installed != INSPECT_VERSION:
        raise ValueError("Inspect version differs from optional dependency pin")
    root = Path(os.path.abspath(output))
    if root.is_relative_to(Path(os.path.abspath(bundle))):
        raise ValueError("Inspect output must be outside the source bundle")
    if _unsafe_reparse_chain(root) or root.exists():
        raise ValueError("Inspect output must be a fresh directory without reparse parents")
    root.mkdir(parents=True, exist_ok=False)
    _dump(root / "projected-input.json", projected)
    attempts = {"count": 0}
    status = {"schema_version": "inspect-offline-result-v1", "processing_ok": False,
              "inspect_version": installed, "source_run_id": projected["source_run_id"],
              "source_seal_sha256": seal_sha256, "source_aggregates": projected["source_aggregates"],
              "source_processing_ok": projected["source_processing_ok"],
              "new_provider_requests": 0, "new_official_evaluations": 0,
              "new_provider_input_tokens": 0, "new_provider_output_tokens": 0}
    try:
        logs = inspect_eval(build_tasks(projected), model=guarded_model(attempts),
                            display="none", log_dir=str(root / "logs"), log_format="eval",
                            log_samples=True, log_realtime=False, max_samples=1, max_tasks=1,
                            epochs=1, retry_on_error=0, notification=False, ctl_server=False,
                            acp_server=False, checkpoint=False)
        if attempts["count"]:
            raise ValueError("offline adapter attempted model generation")
        status["logs"] = _check_logs(logs, projected, root)
        status["processing_ok"] = True
    except Exception as exc:  # noqa: BLE001 - retain Inspect logs and diagnostic state
        status["error"] = f"{type(exc).__name__}: {exc}"
    status["model_generation_attempts"] = attempts["count"]
    _dump(root / "adapter-result.json", status)
    inventory = [{"path": p.relative_to(root).as_posix(), "sha256": _hash(_read(p))}
                 for p in sorted(root.rglob("*")) if p.is_file()]
    _dump(root / "seal.json", inventory)
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seal-sha256", required=True)
    args = parser.parse_args()
    try:
        result = run(args.bundle, args.output, args.seal_sha256)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(2, f"Inspect input error: {exc}\n")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["processing_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
