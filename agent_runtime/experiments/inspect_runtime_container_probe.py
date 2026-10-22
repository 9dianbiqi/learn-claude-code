"""Real Inspect mock → Runtime → existing isolated Docker read bridge probe."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import uuid

from agent_runtime.container_reads import DockerReadBridge
from agent_runtime.docker_workspace import CommandProbe, DockerWorkspaceBackend, digest, load_tmpfs_archive
from agent_runtime.evaluation import _dump, _read
from agent_runtime.experiments.docker_backend_probe import IMAGE, PYTHON, files_tar, local_client
from agent_runtime.inspect_runtime_solver import ContainerCaseBinding, run
from agent_runtime.models import VerifierResult, VerifiedSubtaskConfig


def probe(output: Path) -> dict:
    output = output.absolute()
    if output.exists():
        raise ValueError("fresh container probe output required")
    output.mkdir(parents=True, exist_ok=False)
    base = output / "base"
    base.mkdir()
    (base / "seed.py").write_text("seed = True\n", encoding="utf-8")
    for command in (["git", "init"], ["git", "add", "seed.py"],
                    ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@local.invalid", "commit", "-m", "base fixture"]):
        subprocess.run(command, cwd=base, check=True, capture_output=True, timeout=10)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=base, text=True).strip()
    archive = output / "base.tar"
    archive.write_bytes(subprocess.check_output(["git", "archive", "HEAD"], cwd=base, timeout=10))
    client = local_client()  # Validates immutable cached image, never auto-pulls.
    session = uuid.uuid4().hex
    source = None
    state = {"processing_ok": False, "real_provider_requests": 0, "official_evaluations": 0,
             "model_mode": "inspect_mock", "image": IMAGE, "base_commit": commit,
             "instance_id": "inspect-container-read-fixture", "fixture_cleanup": False}
    try:
        source = client.containers.run(IMAGE, command=["sleep", "300"], detach=True, user="65534:65534",
            network_mode="none", read_only=True, cap_drop=["ALL"], security_opt=["no-new-privileges:true"],
            mem_limit="256m", nano_cpus=1000000000, pids_limit=64, working_dir="/workspace",
            labels={"org.lcc.backend-session": session}, environment={"HOME": "/tmp"}, tmpfs={
                "/workspace": "rw,nosuid,nodev,uid=65534,gid=65534,mode=0700,size=32m",
                "/tmp": "rw,nosuid,nodev,uid=65534,gid=65534,mode=1777,size=16m"})
        state["source_id"] = source.id
        implementation = b"def inspected_symbol(): return 9\n"
        load_tmpfs_archive(client, source.id, "/workspace", files_tar({"src/actual_impl.py": implementation}))
        backend = DockerWorkspaceBackend(client=client, source_id=source.id, session=session, image=IMAGE,
            workspace_path="/workspace", instance_id=state["instance_id"], base_commit=commit,
            base_archive=archive, base_archive_sha256=digest(archive.read_bytes()),
            probes=[CommandProbe((PYTHON, "-I", "-c", "print('not used')"), digest(b"not used\n"))],
            output=output / "backend")
        reads = DockerReadBridge(backend, output / "container-reads", max_calls=6)
        def verify(context):
            records = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(reads.output.glob("*.json"))]
            exact = [r for r in records if r.get("ok") and r["request"]["operation"] == "read_file"
                     and r["request"]["args"]["path"] == "src/actual_impl.py"
                     and r["response"]["result"]["sha256"] == digest(implementation)
                     and r["binding"]["source_id"] == source.id]
            return VerifierResult("pass" if exact else "fail", "Bound container read proof", [])
        inputs = output / "inputs"
        inputs.mkdir()
        plan = {"schema_version": "inspect-runtime-plan-v1", "run_id": "inspect-container-read-probe", "cases": [{
            "id": "container-discovery", "execution_kind": "container_read", "max_turns": 6,
            "prompt": "Locate and read the implementation using only the bound container tools.",
            "files": {"src/guess.py": "HOST_SENTINEL_MUST_NOT_ENTER_MODEL"}, "goal_files": {},
            "model": [
                {"tool": "container_read_file", "id": "missing", "input": {"path": "src/guess.py"}},
                {"tool": "container_find_files", "id": "discover", "input": {"pattern": "**/*.py"}, "expect_feedback": "PATH_NOT_FOUND"},
                {"tool": "container_read_file", "id": "read", "input": {"path": "src/actual_impl.py"}, "expect_feedback": "src/actual_impl.py"},
                {"text": "Located the bound container implementation.", "expect_feedback": "inspected_symbol"}],
            "checks": {"status": "completed", "files": {"src/guess.py": "HOST_SENTINEL_MUST_NOT_ENTER_MODEL"}}}]}
        _dump(inputs / "plan.json", plan)
        node = VerifiedSubtaskConfig("fixture", "Locate the container implementation", "Exact bound read proof", (),
            "inspect-container-read-fixture", "1", digest(json.dumps({"binding": reads.identity,
                "expected_content_sha256": digest(implementation)}, sort_keys=True).encode()),
            verify, digest(Path(__file__).read_bytes()), max_turns=6)
        report = run(inputs / "plan.json", output / "inspect-run",
                     bindings={"container-discovery": ContainerCaseBinding(reads, node)})
        if not report["processing_ok"] or report["runtime_cases"] != {"total": 1, "passed": 1}:
            raise RuntimeError("Inspect/container execution or verifier failed: " + str(report.get("error")))
        result = report["results"][0]
        if result["inspect_model_events"] != 4 or result["bridge_calls"] != 4 or reads.calls != 3:
            raise RuntimeError("unexpected model or container dispatch count")
        case_root = output / "inspect-run" / "container-discovery"
        receipts = [_read(p).decode("utf-8") for p in sorted((case_root / "bridge-calls").glob("*.json"))]
        if "HOST_SENTINEL_MUST_NOT_ENTER_MODEL" in "".join(receipts):
            raise RuntimeError("host sentinel leaked into Inspect model context")
        tools = json.loads((case_root / "tool-calls.json").read_text(encoding="utf-8"))
        if tools[0]["error"] != "PATH_NOT_FOUND" or any(t["effect"] != "read_only" for t in tools):
            raise RuntimeError("container-only recovery path not preserved")
        records = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(reads.output.glob("*.json"))]
        if any(r["binding"]["source_id"] != source.id or "exec_id" not in r for r in records):
            raise RuntimeError("container exec binding evidence missing")
        state.update(processing_ok=True, container_exec_reads=3, inspect_model_events=4,
                     runtime_model_records=result["runtime_model_records"],
                     host_sentinel_not_read=True, verifier_authority="existing Runtime Verifier",
                     task_success=None, official_resolved=None)
    except Exception as exc:
        state["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            if source is not None:
                source.reload()
                if source.attrs["Config"]["Labels"].get("org.lcc.backend-session") != session:
                    raise RuntimeError("refusing cleanup of a container outside owned session")
                source.remove(force=True)
                state["fixture_cleanup"] = True
        except Exception as exc:
            state.update(processing_ok=False, cleanup_error=f"{type(exc).__name__}: {exc}")
        client.close()
        _dump(output / "probe-result.json", state)
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    result = probe(parser.parse_args().output)
    print(json.dumps(result))
    raise SystemExit(0 if result["processing_ok"] else 1)
