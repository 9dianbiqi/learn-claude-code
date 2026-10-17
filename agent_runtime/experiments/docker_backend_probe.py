"""Real-container, offline fixture integration. No model API or SWE-bench grading."""
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tarfile
import time
import uuid

import docker

from agent_runtime import Runtime
import agent_runtime.docker_workspace as backend_module
from agent_runtime.completion import CompletionContract
from agent_runtime.docker_workspace import CommandProbe, DockerWorkspaceBackend, digest, load_tmpfs_archive
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, VerifiedSubtaskConfig, VerifiedSubtaskDAGConfig
from agent_runtime.store import EventStore, LeaseLost
from agent_runtime.trace import TraceReporter


IMAGE = os.environ.get("LCC_DOCKER_IMAGE", "sha256:d6cf11fb58af6b71dcda13f4c5d0c7bbceb036b09d968893b2570c1c53be72e2")
PYTHON = os.environ.get("LCC_CONTAINER_PYTHON", "/opt/miniconda3/envs/testbed/bin/python")


def local_client():
    """Check the explicitly selected local image before SDK run can auto-pull."""
    if not IMAGE.startswith("sha256:") or len(IMAGE) != 71:
        raise ValueError("LCC_DOCKER_IMAGE must be an immutable local sha256 ID")
    client = docker.from_env(timeout=2)
    try:
        if client.images.get(IMAGE).id != IMAGE:
            raise ValueError("Local image identity mismatch")
    except Exception:
        client.close()
        raise
    return client


def save(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, default=str)
        stream.write("\n")


def files_tar(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as tar:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size, member.uid, member.gid, member.mode = len(data), 65534, 65534, 0o644
            tar.addfile(member, io.BytesIO(data))
    return output.getvalue()


def run_case(root, name, base, base_commit, *, sleep=0, timeout=6, takeover=False, output_limit=False, active_writer=False):
    directory = root/name
    directory.mkdir()
    client = local_client()
    token = uuid.uuid4().hex
    source = client.containers.run(IMAGE, command=["sleep", "180"], detach=True,
        user="65534:65534", network_mode="none", read_only=True, cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"], mem_limit="256m", nano_cpus=1000000000,
        pids_limit=64, working_dir="/workspace", labels={"org.lcc.backend-session": token},
        environment={"HOME": "/tmp"},
        tmpfs={"/workspace": "rw,nosuid,nodev,uid=65534,gid=65534,mode=0700,size=32m",
               "/tmp": "rw,nosuid,nodev,uid=65534,gid=65534,mode=1777,size=16m"})
    state = {"case": name, "status": "FAILED", "real_model_calls": 0,
             "official_harness_runs": 0, "source_container": source.id, "image": IMAGE}
    backend = runtime = None
    try:
        # Controller-authored fixture edits, deliberately not an Agent patch.
        load_tmpfs_archive(client, source.id, "/workspace", files_tar({"math.py": b"answer = 4\n", "added.py": b"added = True\n"}))
        if active_writer:
            writer = client.api.exec_create(source.id, cmd=[PYTHON, "-I", "-c",
                "import pathlib,time; p=pathlib.Path('/workspace/counter.txt'); i=0\n"
                "while True:\n p.write_text(str(i)); i+=1; time.sleep(.01)"], user="65534:65534")["Id"]
            client.api.exec_start(writer, detach=True)
            time.sleep(.1)
        code = ("import time,importlib.util; time.sleep("+str(sleep)+"); "
                "s=importlib.util.spec_from_file_location('fixture_module','/workspace/math.py'); "
                "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(m.answer)")
        if output_limit:
            code = "print('x'*100000)"
        probe = CommandProbe((PYTHON, "-I", "-c", code), digest(b"4\n"), timeout)
        backend = DockerWorkspaceBackend(client=client, source_id=source.id, session=token,
            image=IMAGE, workspace_path="/workspace", instance_id="offline-fixture",
            base_commit=base_commit, base_archive=base, base_archive_sha256=digest(base.read_bytes()),
            probes=[probe], output=directory/"backend", scope_timeout_seconds=25)
        verifier = backend.verifier()
        config = VerifiedSubtaskConfig("candidate", "Verify fixture candidate", "Trusted fixture probe",
            (verifier.evidence_path,), backend.validator_id, "1", backend.validator_id, verifier,
            digest(Path(backend_module.__file__).read_bytes()),
            max_turns=4)
        frozen_observations = []
        def fault(point, **context):
            if point in {"verified_subtask_f2_pre", "verified_subtask_f2_mid", "verified_subtask_f3"}:
                source.reload()
                frozen_observations.append({"point": point, "paused": source.attrs["State"]["Paused"]})
                assert source.attrs["State"]["Paused"]
        runtime = Runtime(directory/"controller", ScriptedModel([ModelResponse(text="fixture candidate ready")]),
            verified_subtask_dag=VerifiedSubtaskDAGConfig([config]),
            completion_contract=CompletionContract("candidate_patch", 1), lease_ttl=2,
            fault_injector=fault)
        heartbeats = []
        took_over = []
        def heartbeat():
            heartbeats.append(time.time())
            if takeover and len(heartbeats) == 7:
                with sqlite3.connect(runtime.store.path) as connection:
                    connection.execute("UPDATE leases SET expires_at=0")
                other = EventStore(runtime.store.path)
                task_id = other.list_tasks()[0]["task_id"]
                assert other.acquire_lease(str(runtime.repo_root), task_id, "replacement-owner", ttl=30)
                took_over.append(True)
            runtime.heartbeat_verification()
        backend.heartbeat = heartbeat
        started = time.monotonic()
        if takeover:
            try:
                runtime.run("Offline fixture verification, no model network")
            except LeaseLost:
                pass
            else:
                raise AssertionError("Stale owner was not rejected")
            task_id = runtime.store.list_tasks()[0]["task_id"]
            assert took_over and runtime.store.list_verified_subtask_checkpoints(task_id) == []
            source.reload()
            assert source.attrs["State"]["Paused"]
            with sqlite3.connect(runtime.store.path) as connection:
                assert connection.execute("SELECT owner_id FROM leases").fetchone()[0] == "replacement-owner"
            state.update(status="PASS_STALE_OWNER_REJECTED_SOURCE_RETAINED_PAUSED", task_id=task_id)
        else:
            result = runtime.run("Offline fixture verification, no model network")
            state["runtime_result"] = result.__dict__
            TraceReporter(runtime.store).export_jsonl(result.task_id, directory/"trace.jsonl")
            save(directory/"runtime-summary.json", TraceReporter(runtime.store).summary(result.task_id))
            if output_limit or timeout < sleep:
                assert result.status == "failed" and json.loads(result.error)["code"] == "VERIFICATION_UNCERTAIN"
                assert runtime.store.list_verified_subtask_checkpoints(result.task_id) == []
                state["status"] = "PASS_BOUNDED_VALIDATION_FAILURE"
            else:
                assert result.status == "completed"
                assert len(frozen_observations) == 3 and all(v["paused"] for v in frozen_observations)
                assert backend._validation_containers == set()
                state["status"] = "PASS_REAL_FREEZE_CAPTURE_VALIDATE_COMMIT"
                if active_writer:
                    assert backend._stopped
                    resumed_writer = source.exec_run([PYTHON, "-I", "-c",
                        "import pathlib,time; p=pathlib.Path('/workspace/counter.txt'); "
                        "a=p.read_text(); time.sleep(.15); b=p.read_text(); assert a!=b; print('WRITER_RESUMED')"])
                    assert resumed_writer.exit_code == 0
                    pids = [entry["pid"] for entry in backend._stopped]
                    source.exec_run([PYTHON, "-I", "-c", "import os,signal; "
                                     + ";".join(f"os.kill({pid},signal.SIGKILL)" for pid in pids)])
                    state["background_writer_quiesced_and_resumed"] = True
                # A new mutation scenario, not a repeat of the successful test.
                load_tmpfs_archive(client, source.id, "/workspace", files_tar({"math.py": b"answer = 9\n"}))
                runtime.model = ScriptedModel([])
                stale = runtime.resume(result.task_id)
                assert stale.status == "failed" and json.loads(stale.error)["code"] == "EVIDENCE_STALE"
                assert runtime.model.call_count == 0
                state["changed_workspace_resume"] = stale.__dict__
            runtime.store.assert_invariants(result.task_id)
            source.reload()
            assert not source.attrs["State"]["Paused"]
        state.update(seconds=time.monotonic()-started, heartbeats=len(heartbeats),
                     commit_pause_observations=frozen_observations)
    except Exception as exc:
        state["error_type"], state["error"] = type(exc).__name__, str(exc)
        raise
    finally:
        if backend:
            state["validation_containers_before_fixture_cleanup"] = sorted(backend._validation_containers)
        # Explicit fixture owner cleanup, including the intentionally retained
        # paused source after a stale lease. This is not an old worker unpause.
        for box in client.containers.list(all=True, filters={"label": [
                "org.lcc.validation-session="+token, "org.lcc.validation-source="+source.id]}):
            box.remove(force=True)
            if backend:
                backend._validation_containers.discard(box.id)
        state["validation_containers_remaining"] = sorted(backend._validation_containers) if backend else []
        source.remove(force=True)
        state["fixture_owner_cleanup"] = True
        save(directory/"result.json", state)
        client.close()
    return state


def main(output, selected=None):
    output.mkdir(parents=True, exist_ok=False)
    seed = output/"trusted-base"
    seed.mkdir()
    (seed/"math.py").write_text("answer = 3\n")
    commands = [["git", "init"], ["git", "add", "math.py"],
        ["git", "-c", "user.name=OfflineFixture", "-c", "user.email=fixture@local.invalid", "commit", "-m", "fixture base"]]
    for argv in commands:
        subprocess.run(argv, cwd=seed, check=True, capture_output=True, timeout=10)
    base_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=seed, text=True).strip()
    base = output/"trusted-base.tar"
    base.write_bytes(subprocess.check_output(["git", "archive", "HEAD"], cwd=seed, timeout=10))
    cases = []
    for name, settings in [("success-and-drift", {"sleep": 4}),
            ("timeout", {"sleep": 4, "timeout": .6}),
            ("output-limit", {"output_limit": True}),
            ("lease-takeover", {"sleep": 4, "takeover": True}),
            ("active-writer", {"sleep": 1, "active_writer": True})]:
        if selected and name != selected:
            continue
        cases.append(run_case(output, name, base, base_commit, **settings))
    save(output/"summary.json", {"status": "PASS_OFFLINE_DOCKER_BACKEND", "cases": cases,
         "real_model_calls": 0, "official_harness_runs": 0, "fixture_patches_not_agent_generated": True,
         "base_commit": base_commit, "base_archive_sha256": digest(base.read_bytes())})
    print(json.dumps({"status": "PASS_OFFLINE_DOCKER_BACKEND", "cases": len(cases)}))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", choices=["success-and-drift", "timeout", "output-limit", "lease-takeover", "active-writer"])
    args = parser.parse_args()
    main(args.output, args.case)
