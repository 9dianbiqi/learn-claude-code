"""Real process death + same-source recovery, offline fixtures only."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

import docker

from agent_runtime import Runtime
import agent_runtime.docker_workspace as backend_module
from agent_runtime.completion import CompletionContract
from agent_runtime.docker_workspace import CommandProbe, DockerWorkspaceBackend, digest, load_tmpfs_archive
from agent_runtime.experiments.docker_backend_probe import IMAGE, PYTHON, files_tar, save, local_client
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, VerifiedSubtaskConfig, VerifiedSubtaskDAGConfig
from agent_runtime.store import EventStore
from agent_runtime.trace import TraceReporter


POINTS = ("export_started", "export_durable", "source_paused", "validation_started",
          "verified_subtask_f2_pre", "verified_subtask_f2_mid", "verified_subtask_f3")


def controller(config_path, *, child=False):
    config = json.loads(config_path.read_text())
    root = Path(config["directory"])
    client = local_client()
    runtime = None
    def fault(point, **context):
        if child and point == config["point"]:
            save(root/"crash-observation.json", {"point": point, "pid": os.getpid(), "exit": 73})
            os._exit(73)  # No Python finally blocks, lease release or Docker cleanup.
    backend = DockerWorkspaceBackend(client=client, source_id=config["source"], session=config["session"],
        image=IMAGE, workspace_path="/workspace", instance_id="offline-crash-fixture",
        base_commit=config["base_commit"], base_archive=config["base"],
        base_archive_sha256=config["base_sha256"],
        probes=[CommandProbe((PYTHON, "-I", "-c", "import time; time.sleep(1); "
            "exec(open('/workspace/math.py').read()); print(answer)"), digest(b"4\n"), 6)],
        output=root/("child-backend" if child else "recovery-backend"),
        heartbeat=lambda: runtime.heartbeat_verification(),
        lease_identity=lambda: runtime.verification_lease_identity(),
        recovery_journal=None if child else root/"child-backend/freeze-journal.json",
        fault_injector=fault, scope_timeout_seconds=25)
    verifier = backend.verifier()
    node = VerifiedSubtaskConfig("candidate", "Verify fixture candidate", "Trusted fixture probe",
        (verifier.evidence_path,), backend.validator_id, "1", backend.validator_id, verifier,
        digest(Path(backend_module.__file__).read_bytes()), max_turns=4)
    runtime = Runtime(root/"controller", ScriptedModel([ModelResponse(text="fixture ready")] if child else []),
        verified_subtask_dag=VerifiedSubtaskDAGConfig([node]),
        completion_contract=CompletionContract("candidate_patch", 1), lease_ttl=2, fault_injector=fault)
    if child:
        runtime.run("Offline crash fixture; controller-authored candidate, no model network")
        raise AssertionError("Crash point was not reached")
    task = runtime.store.list_tasks()[0]["task_id"]
    result = runtime.resume(task)
    return result, runtime, backend


def run_case(output, point, base, commit):
    root = output/point
    root.mkdir()
    client = local_client()
    session = uuid.uuid4().hex
    source = client.containers.run(IMAGE, command=["sleep", "300"], detach=True,
        user="65534:65534", network_mode="none", read_only=True, cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"], mem_limit="256m", nano_cpus=1000000000,
        pids_limit=64, working_dir="/workspace", labels={"org.lcc.backend-session": session},
        environment={"HOME": "/tmp"}, tmpfs={
            "/workspace": "rw,nosuid,nodev,uid=65534,gid=65534,mode=0700,size=32m",
            "/tmp": "rw,nosuid,nodev,uid=65534,gid=65534,mode=1777,size=16m"})
    state = {"point": point, "status": "FAILED", "source_id": source.id,
             "real_model_calls": 0, "official_harness_runs": 0}
    try:
        load_tmpfs_archive(client, source.id, "/workspace", files_tar({"math.py": b"answer = 4\n"}))
        if point == "source_paused":
            writer = client.api.exec_create(source.id, cmd=[PYTHON, "-I", "-c",
                "import pathlib,time; p=pathlib.Path('/workspace/counter'); i=0\n"
                "while True:\n p.write_text(str(i)); i+=1; time.sleep(.01)"], user="65534:65534")["Id"]
            client.api.exec_start(writer, detach=True)
            time.sleep(.1)
        config = {"directory": str(root), "source": source.id, "session": session,
                  "point": point, "base": str(base), "base_commit": commit,
                  "base_sha256": digest(base.read_bytes())}
        save(root/"config.json", config)
        process = subprocess.run([sys.executable, __file__, "--child", str(root/"config.json")],
            capture_output=True, timeout=45)
        (root/"child-stdout.log").write_bytes(process.stdout)
        (root/"child-stderr.log").write_bytes(process.stderr)
        assert process.returncode == 73, process.stderr.decode(errors="replace")
        store = EventStore(root/"controller/.agent_runtime/runtime.db")
        task = store.list_tasks()[0]["task_id"]
        with sqlite3.connect(store.path) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            old_lease = connection.execute("SELECT owner_id,fencing_token,expires_at FROM leases").fetchone()
            authoritative = connection.execute("SELECT COUNT(*) FROM verifier_runs WHERE authoritative=1").fetchone()[0]
            semantic = connection.execute("SELECT COUNT(*) FROM semantic_checkpoints").fetchone()[0]
        expected = int(point == "verified_subtask_f3")
        assert semantic == authoritative == expected
        source.reload()
        before = source.attrs["State"]["Paused"]
        assert before == (point not in {"export_started", "export_durable"})
        state.update(child_exit=process.returncode, paused_after_crash=before,
                     authority_before_resume=authoritative, semantic_before_resume=semantic,
                     old_owner=old_lease[0], old_fence=old_lease[1])
        while time.time() <= old_lease[2]: time.sleep(.03)
        if not before:
            try:
                controller(root/"config.json")
            except RuntimeError as exc:
                assert "quarantine" in str(exc)
                state.update(status="PASS_PARTIAL_EXPORT_QUARANTINED", recovery_error=str(exc))
            else:
                raise AssertionError("Incomplete freeze was silently recovered")
            assert len(store.list_verified_subtask_checkpoints(task)) == 0
        else:
            result, runtime, backend = controller(root/"config.json")
            assert result.status == "completed", result
            assert runtime.model.call_count == 0
            assert len(runtime.store.list_verified_subtask_checkpoints(task)) == 1
            runtime.store.assert_invariants(task)
            TraceReporter(runtime.store).export_jsonl(task, root/"trace.jsonl")
            save(root/"runtime-summary.json", TraceReporter(runtime.store).summary(task))
            validations = list((root/"recovery-backend").glob("*-validation.json"))
            assert len(validations) == (0 if expected else 1)
            source.reload()
            assert not source.attrs["State"]["Paused"]
            journal = json.loads((root/"child-backend/freeze-journal.json").read_text())
            assert journal["lease"]["fencing_token"] > old_lease[1]
            assert journal["state"] == "released"
            if point == "source_paused":
                assert journal["stopped"]
                writer_check = source.exec_run([PYTHON, "-I", "-c",
                    "import pathlib,time; p=pathlib.Path('/workspace/counter'); "
                    "a=p.read_text(); time.sleep(.15); assert a!=p.read_text(); print('RESUMED')"])
                assert writer_check.exit_code == 0
                state["writer_resumed_by_new_owner"] = True
            state.update(status="PASS_SAME_SOURCE_FENCED_RECOVERY", runtime_result=result.__dict__,
                         recovery_model_calls=runtime.model.call_count, recovery_validations=len(validations),
                         new_owner=journal["lease"]["owner_id"], new_fence=journal["lease"]["fencing_token"])
        assert client.containers.list(all=True, filters={"label": "org.lcc.validation-session="+session}) == []
        state["orphan_validators_remaining"] = 0
    except Exception as exc:
        state.update(error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        # Fixture owner cleanup, including quarantined sources; no global prune.
        for box in client.containers.list(all=True, filters={"label": "org.lcc.validation-session="+session}):
            box.remove(force=True)
        source.remove(force=True)
        state["fixture_owner_cleanup"] = True
        save(root/"result.json", state)
        client.close()
    print(json.dumps({"point": point, "status": state["status"]}), flush=True)
    return state


def main(output, selected):
    output.mkdir(parents=True, exist_ok=False)
    seed = output/"trusted-base"
    seed.mkdir()
    (seed/"math.py").write_text("answer = 3\n")
    for command in [["git", "init"], ["git", "add", "math.py"], ["git", "-c", "user.name=Fixture",
            "-c", "user.email=fixture@local.invalid", "commit", "-m", "offline fixture base"]]:
        subprocess.run(command, cwd=seed, check=True, capture_output=True, timeout=10)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=seed, text=True).strip()
    base = output/"trusted-base.tar"
    base.write_bytes(subprocess.check_output(["git", "archive", "HEAD"], cwd=seed, timeout=10))
    cases = [run_case(output, point, base, commit) for point in (selected or POINTS)]
    save(output/"summary.json", {"status": "PASS_OFFLINE_DOCKER_PROCESS_CRASH_RECOVERY", "cases": cases,
        "real_model_calls": 0, "official_harness_runs": 0, "fixture_patches_not_agent_generated": True})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--case", choices=POINTS, action="append")
    args = parser.parse_args()
    if args.child: controller(args.child, child=True)
    else: main(args.output, args.case)
