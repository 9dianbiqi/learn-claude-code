"""Single-instance acceptance: owned environment -> mock repair -> optional grading.

Default is preflight. This entry point cannot call a paid model. Scripted repair
measures plumbing only, never autonomous coding ability. No automatic retries.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

from .benchmark_dataset import verify, _fresh
from .docker_workspace import CommandProbe
from .evaluation import _dump, _hash, _json, _read
from .inspect_swe_runner import run_instance
from .swe_environment import checked_archive, provision
from .swe_grading import grade, harness_identity


class RetainEnvironment(RuntimeError):
    pass


def run(bundle, manifest_sha256, instance_id, output, *, client=None, image,
        base_archive, base_archive_sha256, probes, responses, max_turns=8,
        execute=False, official=False, harness=None, expected_engine_id=None):
    bundle, root = Path(bundle).absolute(), Path(output).absolute()
    manifest = verify(bundle, manifest_sha256)
    if manifest["instance_ids"] != [instance_id]:
        raise ValueError("acceptance requires exactly one planned instance")
    sources = [bundle, Path(base_archive).absolute().parent]
    if harness is not None:
        harness = Path(harness).absolute()
        sources.append(harness)
    _fresh(root, sources)
    if expected_engine_id is not None and (not isinstance(expected_engine_id, str)
            or not re.fullmatch(r"[A-Za-z0-9:_.-]{1,128}", expected_engine_id)):
        raise ValueError("invalid engine identity pin")
    if not isinstance(image, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("immutable local image required")
    checked_archive(base_archive, base_archive_sha256)
    probes = tuple(probes)
    if not probes or not all(isinstance(p, CommandProbe) for p in probes):
        raise ValueError("trusted probes required")
    row = _json(_read(bundle / "solver/dataset.json"))[0]
    model = "mockllm/runtime-bridge"
    run_instance(bundle, manifest_sha256, instance_id,
                 SimpleNamespace(instance_id=instance_id, base_commit=row["base_commit"]),
                 root / "candidate", model=model, mode="mock", responses=responses, max_turns=max_turns)
    if official:
        if harness is None:
            raise ValueError("explicit pinned harness required")
        harness_identity(harness, manifest["harness_commit"])
    result = {"schema_version": "swe-acceptance-v1", "instance_id": instance_id,
              "status": "PREFLIGHT_ONLY", "stage": "preflight", "candidate_validated": False,
              "official_resolved": None, "new_provider_requests": 0,
              "new_official_evaluations": 0, "manual_recovery_required": False,
              "dataset_manifest_sha256": manifest_sha256, "error": None}
    if not execute:
        return result
    if client is None:
        raise ValueError("explicit Docker client required")
    root.mkdir(parents=True)
    _dump(root / "invocation.json", {**result, "image": image,
        "base_archive_sha256": base_archive_sha256, "responses": responses,
        "max_turns": max_turns, "official_requested": official,
        "harness_commit": manifest["harness_commit"], "expected_engine_id": expected_engine_id})
    prediction = {"instance_id": instance_id, "model_name_or_path": model, "model_patch": ""}
    try:
        result["stage"] = "provision"
        with provision(bundle, manifest_sha256, instance_id, client=client, image=image,
                       base_archive=base_archive, base_archive_sha256=base_archive_sha256,
                       probes=probes, output=root / "environment",
                       expected_engine_id=expected_engine_id) as backend:
            result["stage"] = "candidate"
            candidate = run_instance(bundle, manifest_sha256, instance_id, backend,
                root / "candidate", model=model, mode="mock", responses=responses,
                max_turns=max_turns, execute=True)
            if candidate.get("manual_recovery_required", True) or not candidate["processing_ok"]:
                raise RetainEnvironment("candidate requires review; preserve source and skip scoring: "
                                        + str(candidate.get("error") or candidate.get("runtime_status")))
            raw = _read(root / "candidate/predictions.jsonl")
            if _hash(raw) != candidate["prediction_sha256"]:
                raise RetainEnvironment("candidate prediction changed")
            value = _json(raw)
            if (value.get("instance_id") != instance_id or value.get("model_name_or_path") != model
                    or not isinstance(value.get("model_patch"), str)
                    or bool(value["model_patch"].strip()) != candidate["candidate_validated"]):
                raise RetainEnvironment("candidate prediction identity/outcome mismatch")
            prediction = value
            result["candidate_validated"] = candidate["candidate_validated"]
            result["stage"] = "cleanup"
        result["status"] = "CANDIDATE_FINISHED"
    except Exception as exc:
        result.update(status="BLOCKED", error=f"{type(exc).__name__}: {exc}", manual_recovery_required=True)
        lifecycle = root / "environment/lifecycle.json"
        if lifecycle.exists():
            result["manual_recovery_required"] = _json(_read(lifecycle))["manual_recovery_required"]
    # Even provision/startup failures remain in the denominator. Preserve any
    # validated candidate after cleanup failure, but never score a blocked run.
    predictions = root / "predictions.jsonl"
    predictions.write_text(json.dumps(prediction) + "\n", encoding="utf-8")
    result["prediction_sha256"] = _hash(_read(predictions))
    if result["status"] != "BLOCKED" and official:
        result["stage"] = "grading"
        try:
            # grade() requires output disjoint from its prediction input directory.
            grading_inputs = root / "grading-inputs"
            grading_inputs.mkdir()
            grading_prediction = grading_inputs / "predictions.jsonl"
            grading_prediction.write_bytes(_read(predictions))
            scored = grade(bundle, manifest_sha256, grading_prediction, result["prediction_sha256"],
                           harness, root / "official", execute=True)
            result.update(official_resolved=scored["official_resolved"],
                          new_official_evaluations=scored["new_official_evaluations"],
                          manual_recovery_required=scored["manual_recovery_required"],
                          status=scored["status"], error=scored["error"])
        except Exception as exc:
            result.update(status="BLOCKED", error=f"{type(exc).__name__}: {exc}",
                          new_official_evaluations=None, manual_recovery_required=True)
    _dump(root / "result.json", result)
    return result


def load_config(path):
    """Load only controller inputs; CLI execution switches cannot live in JSON."""
    config = _json(_read(Path(path)))
    required = {"bundle", "manifest_sha256", "instance_id", "image", "base_archive",
                "base_archive_sha256", "probes", "responses"}
    if not isinstance(config, dict) or not required <= set(config) or set(config) - required - {"max_turns", "harness"}:
        raise ValueError("invalid acceptance config fields")
    for key in ("bundle", "base_archive", "harness"):
        if key in config and (not isinstance(config[key], str) or not Path(config[key]).is_absolute()):
            raise ValueError("config asset paths must be absolute")
    if not isinstance(config["probes"], list):
        raise ValueError("probes must be a list")
    probes = []
    for probe in config["probes"]:
        if (not isinstance(probe, dict) or not {"argv", "stdout_sha256"} <= set(probe)
                or set(probe) - {"argv", "stdout_sha256", "timeout_seconds"}
                or not isinstance(probe["argv"], list)):
            raise ValueError("invalid command probe")
        probes.append(CommandProbe(tuple(probe["argv"]), probe["stdout_sha256"], probe.get("timeout_seconds", 5)))
    config["probes"] = probes
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", help="trusted controller JSON with absolute local paths")
    parser.add_argument("output")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--official", action="store_true")
    parser.add_argument("--docker-host", help="explicit Docker SDK endpoint; CLI contexts are not implicitly used")
    parser.add_argument("--expected-engine-id", help="pin the engine before any container creation")
    args = parser.parse_args()
    if args.execute and (not args.docker_host or not args.expected_engine_id):
        parser.error("--execute requires --docker-host and --expected-engine-id")
    if args.execute and args.official and sys.platform != "linux":
        parser.error("official execution requires a prepared Linux Python host")
    client = None
    original_docker_env = None
    try:
        config = load_config(args.config)
        # Complete input preflight before even constructing a Docker client.
        preflight = run(**config, output=args.output, official=args.official,
                        expected_engine_id=args.expected_engine_id)
        if not args.execute:
            print(json.dumps(preflight, indent=2))
            return 0
        import docker
        # The official harness is a child Python process using docker.from_env.
        # Keep it on the explicit SDK endpoint instead of an inherited default.
        keys = ("DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")
        original_docker_env = {key: os.environ.get(key) for key in keys}
        os.environ["DOCKER_HOST"] = args.docker_host
        for key in keys[1:]:
            os.environ.pop(key, None)
        client = docker.DockerClient(base_url=args.docker_host, timeout=3)
        result = run(**config, output=args.output, client=client, execute=True, official=args.official,
                     expected_engine_id=args.expected_engine_id)
        print(json.dumps(result, indent=2))
        return 2 if result["status"] in {"BLOCKED", "FAILED"} or result["manual_recovery_required"] else 0
    except Exception as exc:
        parser.exit(2, f"acceptance input/startup error: {exc}\n")
    finally:
        try:
            if client is not None:
                client.close()
        finally:
            if original_docker_env is not None:
                for key, value in original_docker_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


if __name__ == "__main__":
    raise SystemExit(main())
