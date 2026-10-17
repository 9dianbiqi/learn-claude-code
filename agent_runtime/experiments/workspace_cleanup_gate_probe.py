"""Cross-entry cleanup blocker + exact Docker removal reconciliation, offline."""
import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

import docker

from agent_runtime import Runtime
from agent_runtime.docker_workspace import DockerWorkspaceBackend, digest, load_tmpfs_archive
from agent_runtime.experiments.docker_backend_probe import IMAGE, PYTHON, files_tar, save, local_client
from agent_runtime.experiments.docker_release_probe import configured
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse
from agent_runtime.store import EventStore, EffectBlocked
from agent_runtime.trace import TraceReporter
from agent_runtime.workspace_verifier import WorkspaceCleanupError


def child(path):
    runtime, backend, client = configured(path, True)
    try:
        runtime.run("offline cleanup gate fixture; no model API")
        raise AssertionError("Expected release fault")
    except WorkspaceCleanupError as exc:
        save(path.parent/"child-result.json", {"exception": type(exc).__name__, "cleanup": exc.report})
    finally:
        client.close()


def run_case(output, name, fault, base, commit):
    root = output/name
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
    state = {"case": name, "status": "FAILED", "source_id": source.id,
             "real_model_api_calls": 0, "paid_calls": 0, "official_harness_runs": 0}
    removed = False
    try:
        load_tmpfs_archive(client, source.id, "/workspace", files_tar({"math.py": b"answer = 4\n"}))
        writer = client.api.exec_create(source.id, cmd=[PYTHON, "-I", "-c",
            "import pathlib,time; p=pathlib.Path('/workspace/counter'); i=0\n"
            "while True:\n p.write_text(str(i)); i+=1; time.sleep(.01)"], user="65534:65534")["Id"]
        client.api.exec_start(writer, detach=True)
        time.sleep(.15)
        config = root/"config.json"
        save(config, {"directory": str(root), "source": source.id, "session": session, "case": fault,
            "base": str(base), "base_commit": commit, "base_sha256": digest(base.read_bytes())})
        process = subprocess.run([sys.executable, __file__, "--child", str(config)], capture_output=True, timeout=45)
        (root/"child-stdout.log").write_bytes(process.stdout)
        (root/"child-stderr.log").write_bytes(process.stderr)
        assert process.returncode == (73 if name == "crash-owner" else 0), process.stderr.decode(errors="replace")
        controller_root = root/"controller"
        store = EventStore(controller_root/".agent_runtime/runtime.db")
        original_task = store.list_tasks()[0]["task_id"]
        all_reservations = store.list_effect_reservations(original_task)
        reservations = [row for row in all_reservations
            if row["effect"] == "workspace_cleanup"
            and row["details"].get("resource", {}).get("kind") == "docker_workspace"
            and row["details"]["resource"]["source_id"] == source.id]
        assert len(reservations) == 1
        assert all(row["state"] == "completed" for row in all_reservations
                   if row["reservation_id"] != reservations[0]["reservation_id"])
        assert reservations[0]["state"] == ("running" if name == "crash-owner" else "unknown")
        original_resource = reservations[0]["details"]["resource"]
        assert original_resource["source_id"] == source.id and original_resource["engine_id"] == client.info()["ID"]
        with sqlite3.connect(store.path) as connection:
            expiry = connection.execute("SELECT expires_at FROM leases").fetchone()[0]
        while time.time() <= expiry: time.sleep(.03)
        blocked_model = ScriptedModel([ModelResponse(text="must not execute")])
        fresh = Runtime(controller_root, blocked_model)
        try: fresh.run("new unrelated ordinary task in same repository")
        except EffectBlocked: pass
        else: raise AssertionError("New ordinary task bypassed cleanup blocker")
        assert blocked_model.call_count == 0
        row = store.list_blocking_reservations(str(fresh.repo_root))[0]
        assert row["state"] == "unknown" and row["details"]["resource"] == original_resource
        # Deliberately omit the original recovery journal and construct a new
        # backend: the repository gate must stop even this former bypass.
        from agent_runtime.experiments.docker_release_probe import configured as make_runtime
        resume, replacement, resume_client = make_runtime(config, False)
        replacement.recovery_journal = None
        try:
            try: resume.resume(original_task)
            except EffectBlocked: pass
            else: raise AssertionError("Fresh backend returned cached completion")
            assert resume.model.call_count == 0
            assert not list(replacement.output.glob("*-capture.json"))
            assert not list(replacement.output.glob("*-freeze.json"))
        finally:
            resume_client.close()
        # Operator inspection is read-only and cannot clear a present resource.
        operator = Runtime(controller_root, ScriptedModel([]))
        try:
            operator.resolve_workspace_cleanup(row["reservation_id"],
                lambda resource: DockerWorkspaceBackend.observe_removed(client, resource), "inspect source before removal")
        except RuntimeError as exc:
            assert "still exists" in str(exc)
        else: raise AssertionError("Present resource incorrectly reconciled")
        assert store.list_blocking_reservations(str(operator.repo_root))
        # Explicit fixture-owner removal is separate from ledger reconciliation.
        source.remove(force=True)
        removed = True
        observations = []
        def observe(resource):
            result = DockerWorkspaceBackend.observe_removed(client, resource)
            observations.append(result)
            return result
        operator.resolve_workspace_cleanup(row["reservation_id"], observe, "fixture owner removed exact source; verify daemon 404")
        assert not store.list_blocking_reservations(str(operator.repo_root))
        assert store.get_task(original_task)["status"] == "completed"
        assert len(store.list_verified_subtask_checkpoints(original_task)) == 1
        follow_model = ScriptedModel([ModelResponse(text="offline followup allowed")])
        follow = Runtime(controller_root, follow_model)
        follow_result = follow.run("read-only offline followup after confirmed removal")
        assert follow_result.status == "completed" and follow_model.call_count == 1
        store.assert_invariants()
        TraceReporter(store).export_jsonl(original_task, root/"original-trace.jsonl")
        TraceReporter(store).export_jsonl(follow_result.task_id, root/"followup-trace.jsonl")
        save(root/"removal-observation.json", observations[0])
        save(root/"reservation-final.json", store.list_effect_reservations(original_task))
        save(root/"runtime-summary.json", TraceReporter(store).summary(original_task))
        state.update(status="PASS_GLOBAL_BLOCK_AND_TRUSTED_RECONCILIATION", child_exit=process.returncode,
            original_task=original_task, original_status="completed", checkpoint_count=1,
            reservation_before_takeover=reservations[0]["state"], reservation_after_takeover="unknown",
            reservation_final="completed", blocked_model_calls=0, blocked_capture_calls=0,
            same_resource_preserved=True, existing_source_probe_rejected=True,
            followup_scripted_calls=1, resolved_after_actual_removal=True)
    except Exception as exc:
        state.update(error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        for box in client.containers.list(all=True, filters={"label": "org.lcc.validation-session="+session}):
            box.remove(force=True)
        if not removed: source.remove(force=True)
        state["fixture_owner_cleanup"] = True
        save(root/"result.json", state)
        client.close()
    print(json.dumps({"case": name, "status": state["status"]}), flush=True)
    return state


def main(output):
    output.mkdir(parents=True, exist_ok=False)
    seed = output/"trusted-base"
    seed.mkdir()
    (seed/"math.py").write_text("answer = 3\n")
    for argv in [["git", "init"], ["git", "add", "math.py"], ["git", "-c", "user.name=Fixture",
                 "-c", "user.email=fixture@local.invalid", "commit", "-m", "offline fixture base"]]:
        subprocess.run(argv, cwd=seed, check=True, capture_output=True, timeout=10)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=seed, text=True).strip()
    base = output/"trusted-base.tar"
    base.write_bytes(subprocess.check_output(["git", "archive", "HEAD"], cwd=seed, timeout=10))
    cases = [run_case(output, name, fault, base, commit) for name, fault in
             [("ignored-error", "thaw-nonzero"), ("crash-owner", "crash-after-unpause")]]
    save(output/"summary.json", {"status": "PASS_OFFLINE_WORKSPACE_CLEANUP_GATE", "cases": cases,
        "real_model_api_calls": 0, "paid_calls": 0, "official_harness_runs": 0, "api_tokens": 0,
        "fixture_changes_not_agent_generated": True})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.child: child(args.child)
    else: main(args.output)
