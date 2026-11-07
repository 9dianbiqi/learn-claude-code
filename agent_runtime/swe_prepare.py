"""Check prepared SWE assets and optionally inspect a host; never create a container.

Offline by default. --host makes only Docker info/image inspection requests.
This planner does not download/build images, install dependencies or run probes.
"""
import argparse
import json
from pathlib import Path
import sys

from .benchmark_dataset import _fresh
from .evaluation import _dump, _hash, _read
from .swe_acceptance import load_config, run


def inspect_host(client, image, expected_engine_id=None):
    if client.api.timeout is None or not 0 < client.api.timeout <= 5:
        raise ValueError("bounded Docker HTTP timeout required")
    info = client.info()
    if info.get("OSType") != "linux" or not info.get("ID"):
        raise ValueError("identified Linux Docker engine required")
    if expected_engine_id is not None and info["ID"] != expected_engine_id:
        raise ValueError("Docker engine differs from the explicit execution pin")
    if client.images.get(image).id != image:
        raise ValueError("local image identity mismatch")
    return {"engine_id": info["ID"], "os": "linux", "image": image,
            "status": "PINNED_IMAGE_PRESENT" if expected_engine_id else "ENGINE_PIN_REQUIRED",
            "probes_executed": False}


def plan(config_path, run_output, *, client=None, expected_engine_id=None, official=False):
    path = Path(config_path).absolute()
    data = _read(path)
    config = load_config(path)
    result = {"schema_version": "swe-preparation-v1", "config_sha256": _hash(data),
              "instance_id": config["instance_id"], "status": "BLOCKED", "checks": [],
              "host": None, "new_provider_requests": 0, "new_official_evaluations": 0,
              "containers_created": 0}
    def check(name, operation):
        try:
            value = operation()
            result["checks"].append({"name": name, "status": "PASSED", "error": None})
            return value
        except Exception as exc:
            result["checks"].append({"name": name, "status": "BLOCKED", "error": f"{type(exc).__name__}: {exc}"})
            return None
    offline = check("offline_assets_and_script", lambda: run(**config, output=run_output,
        official=official, expected_engine_id=expected_engine_id))
    # Do not contact a host when even the frozen input preflight is invalid.
    if offline and client is not None:
        result["host"] = check("engine_pin_and_local_image", lambda: inspect_host(
            client, config["image"], expected_engine_id))
    if official:
        check("official_execution_platform", lambda: _linux())
    blocked = any(c["status"] == "BLOCKED" for c in result["checks"])
    if not blocked:
        result["status"] = ("PREPARED_ASSETS_AND_HOST" if result["host"] and expected_engine_id else
                            "ENGINE_PIN_REQUIRED" if result["host"] else "OFFLINE_READY_HOST_PENDING")
    result["remaining"] = [
        "Provision a fresh owned source and execute the frozen task-relevant probes.",
        "Run the bounded scripted read/edit acceptance and retain its candidate/lifecycle evidence.",
        "On a prepared Linux Python host, explicitly invoke the clean pinned official harness.",
        "General image builds/dependency installation and autonomous model repair remain separate."]
    return result


def _linux():
    if sys.platform != "linux":
        raise ValueError("official execution requires a prepared Linux Python host")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config")
    parser.add_argument("run_output", help="future fresh acceptance directory, not created by this command")
    parser.add_argument("--host", action="store_true")
    parser.add_argument("--docker-host")
    parser.add_argument("--expected-engine-id")
    parser.add_argument("--official", action="store_true")
    parser.add_argument("--report", help="optional fresh directory for the preparation report")
    args = parser.parse_args()
    if args.host and not args.docker_host:
        parser.error("--host requires an explicit --docker-host endpoint")
    client = None
    try:
        # Preflight before constructing the SDK client, which may negotiate API.
        result = plan(args.config, args.run_output, expected_engine_id=args.expected_engine_id, official=args.official)
        if args.host and result["status"] != "BLOCKED":
            import docker
            client = docker.DockerClient(base_url=args.docker_host, timeout=3)
            result = plan(args.config, args.run_output, client=client,
                          expected_engine_id=args.expected_engine_id, official=args.official)
        if args.report:
            config = load_config(args.config)
            output = Path(args.report).absolute()
            _fresh(output, [Path(args.config).absolute().parent, Path(args.run_output).absolute(),
                           Path(config["bundle"]), Path(config["base_archive"]).parent,
                           *([Path(config["harness"])] if config.get("harness") else [])])
            output.mkdir(parents=True)
            _dump(output / "preparation.json", result)
        print(json.dumps(result, indent=2))
        return 2 if result["status"] == "BLOCKED" else 0
    except Exception as exc:
        parser.exit(2, f"preparation error: {type(exc).__name__}: {exc}\n")
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
