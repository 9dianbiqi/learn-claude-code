"""One pinned SWE instance through an already provisioned controller backend.

No implicit image downloads, provisioning, paid calls or official harness runs.
The caller owns backend construction/cleanup. Default is preflight only.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .benchmark_dataset import verify
from .container_edits import DockerRepairBridge, repair_binding
from .eval_runner import _unsafe_reparse_chain
from .evaluation import _dump, _hash, _read, _json
from .inspect_model_eval import run as model_run, validate
from .inspect_runtime_solver import validate_mock_responses


def instance_input(bundle, manifest_sha256, instance_id, backend):
    root = Path(os.path.abspath(bundle))
    manifest = verify(root, manifest_sha256)
    if instance_id not in manifest["instance_ids"]:
        raise ValueError("instance not in frozen dataset")
    rows = _json(_read(root / "solver/dataset.json"))
    matches = [r for r in rows if r["instance_id"] == instance_id]
    if len(matches) != 1:
        raise ValueError("unique solver instance required")
    row = matches[0]
    if backend.instance_id != instance_id or backend.base_commit != row["base_commit"]:
        raise ValueError("container backend does not match selected instance/base")
    return manifest, row


def candidate_patch(root: Path, backend) -> str:
    """Export only the candidate that passed the existing workspace verifier."""
    evidence = _json(_read(root / "repair/workspace/.agent_runtime/verification/completion.json"))
    if evidence.get("goal") != "candidate_patch":
        raise ValueError("candidate verifier evidence required")
    snapshot, validation = evidence["workspace"], evidence["validation"]
    for key, expected in (("workspace_id", backend.source_id), ("instance_id", backend.instance_id),
                          ("base_commit", backend.base_commit)):
        if snapshot.get(key) != expected:
            raise ValueError("candidate workspace identity mismatch")
    patch = snapshot.get("patch")
    if not isinstance(patch, str) or not patch.strip() or snapshot.get("patch_applies") is not True:
        raise ValueError("nonempty applicable candidate required")
    if (validation.get("revision") != snapshot.get("revision")
            or validation.get("patch_sha256") != _hash(patch.encode())
            or validation.get("validator_id") != backend.validator_id
            or type(validation.get("exit_code")) is not int or validation["exit_code"] != 0
            or type(validation.get("tests_run")) is not int or validation["tests_run"] < 1):
        raise ValueError("candidate validation mismatch")
    return patch


def run_instance(bundle, manifest_sha256, instance_id, backend, output, *, model,
                 mode="live", max_turns=8, max_output_tokens=2048, timeout=60,
                 responses=None, execute=False):
    """Caller supplies the exact provider and a trusted isolated backend capability."""
    manifest, row = instance_input(bundle, manifest_sha256, instance_id, backend)
    root = Path(os.path.abspath(output))
    if root.exists() or _unsafe_reparse_chain(root) or root.is_relative_to(Path(os.path.abspath(bundle))):
        raise ValueError("fresh output outside prepared dataset required")
    if mode == "live" and responses is not None:
        raise ValueError("live execution cannot receive scripted responses")
    if mode not in {"mock", "live"} or (mode == "mock" and not responses):
        raise ValueError("explicit model mode and mock responses required")
    if mode == "mock":
        validate_mock_responses(responses, DockerRepairBridge.names)
    # Validate scalar/model limits without constructing a container bridge.
    check = {"schema_version": "inspect-model-eval-v1", "run_id": manifest["run_id"], "mode": mode,
        "model": model, "limits": {"max_requests_per_case": max_turns, "max_output_tokens": max_output_tokens,
                                  "request_timeout_seconds": timeout},
        "cases": [{"id": "preflight", "prompt": row["problem_statement"], "max_turns": max_turns,
                   "files": {"validation.txt": "x"}, "goal_files": {"validation.txt": "x"}}]}
    if mode == "mock":
        # Container tool scripts are validated after binding; this placeholder is
        # exclusively schema preflight, never a model response used for execution.
        check["cases"][0]["model"] = [{"text": "schema validation"}]
    validate(check)
    if not execute:
        return {"status": "PREFLIGHT_ONLY", "instance_id": instance_id, "model": model,
                "max_requests": max_turns, "dataset_manifest_sha256": manifest_sha256,
                "new_provider_requests": 0, "new_official_evaluations": 0}
    root.mkdir(parents=True)
    inputs = root / "inputs"
    inputs.mkdir()
    binding = repair_binding(backend, root / "container-tools", max_turns=max_turns)
    case = {"id": "repair", "execution_kind": "container_repair", "prompt": row["problem_statement"],
            "files": {}, "goal_files": {}, "max_turns": max_turns}
    if mode == "mock":
        case["model"] = responses
    plan = {**check, "cases": [case]}
    validate(plan, {"repair": binding})
    _dump(inputs / "plan.json", plan)
    _dump(inputs / "provenance.json", {"dataset_manifest_sha256": manifest_sha256,
        "dataset_revision": manifest["dataset_revision"], "harness_commit": manifest["harness_commit"],
        "instance_id": instance_id, "repo": row["repo"], "base_commit": row["base_commit"]})
    patch, error = "", None
    try:
        report = model_run(inputs / "plan.json", root / "model-run", execute=True, bindings={"repair": binding})
    except Exception as exc:
        # Preserve the planned instance even when logging/startup cannot finish.
        # Live request count is unknown after an unreturned execution, not zero.
        error = f"{type(exc).__name__}: {exc}"
        report = {"processing_ok": False, "new_provider_requests": 0 if mode == "mock" else None}
    if report["processing_ok"] and report["fixture_outcomes"]["passed"] == 1:
        try:
            patch = candidate_patch(root / "model-run", backend)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            error = f"{type(exc).__name__}: {exc}"
    prediction = {"instance_id": instance_id, "model_name_or_path": model, "model_patch": patch}
    (root / "predictions.jsonl").write_text(json.dumps(prediction, ensure_ascii=False) + "\n", encoding="utf-8")
    statuses = [r.get("runtime_status") for r in report.get("results", []) if r.get("id") == "repair"]
    runtime_status = statuses[0] if len(statuses) == 1 else None
    result = {"schema_version": "inspect-swe-candidate-v1", "instance_id": instance_id,
        "runtime_status": runtime_status,
        "manual_recovery_required": runtime_status not in {"completed", "failed"} or not report["processing_ok"] or error is not None,
        "processing_ok": report["processing_ok"] and error is None, "candidate_validated": bool(patch),
        "prediction_sha256": _hash(_read(root / "predictions.jsonl")), "dataset_manifest_sha256": manifest_sha256,
        "official_resolved": None, "new_provider_requests": report["new_provider_requests"],
        "new_official_evaluations": 0, "error": error or report.get("error")}
    _dump(root / "result.json", result)
    _dump(root / "seal.json", [{"path": p.relative_to(root).as_posix(), "sha256": _hash(_read(p))}
                              for p in sorted(root.rglob("*")) if p.is_file()])
    return result
