"""New review-blocker repair integration: real validator deletion failure."""
import argparse
import json
from pathlib import Path
import subprocess
import time
import uuid

import docker

from agent_runtime import Runtime
from agent_runtime.docker_workspace import DockerWorkspaceBackend, digest, load_tmpfs_archive
from agent_runtime.experiments.docker_backend_probe import IMAGE, PYTHON, files_tar, save, local_client
from agent_runtime.experiments.docker_release_probe import configured
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse
from agent_runtime.store import EffectBlocked
from agent_runtime.trace import TraceReporter


def main(output):
    output.mkdir(parents=True, exist_ok=False)
    seed = output/"base"
    seed.mkdir()
    (seed/"math.py").write_text("answer = 3\n")
    for argv in [["git", "init"], ["git", "add", "math.py"], ["git", "-c", "user.name=Fixture",
                 "-c", "user.email=fixture@local.invalid", "commit", "-m", "offline fixture"]]:
        subprocess.run(argv, cwd=seed, check=True, capture_output=True, timeout=10)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=seed, text=True).strip()
    base = output/"base.tar"
    base.write_bytes(subprocess.check_output(["git", "archive", "HEAD"], cwd=seed, timeout=10))
    client = local_client()
    session = uuid.uuid4().hex
    source = client.containers.run(IMAGE, command=["sleep", "300"], detach=True, user="65534:65534",
        network_mode="none", read_only=True, cap_drop=["ALL"], security_opt=["no-new-privileges:true"],
        mem_limit="256m", nano_cpus=1000000000, pids_limit=64, working_dir="/workspace",
        labels={"org.lcc.backend-session": session}, environment={"HOME": "/tmp"},
        tmpfs={"/workspace": "rw,nosuid,nodev,uid=65534,gid=65534,mode=0700,size=32m",
               "/tmp": "rw,nosuid,nodev,uid=65534,gid=65534,mode=1777,size=16m"})
    state = {"status": "FAILED", "real_model_api_calls": 0, "paid_calls": 0, "api_tokens": 0,
             "official_harness_runs": 0, "source_id": source.id}
    target = None
    controller_client = None
    try:
        load_tmpfs_archive(client, source.id, "/workspace", files_tar({"math.py": b"answer = 4\n"}))
        writer = client.api.exec_create(source.id, cmd=[PYTHON, "-I", "-c",
            "import pathlib,time; p=pathlib.Path('/workspace/counter'); i=0\n"
            "while True:\n p.write_text(str(i)); i+=1; time.sleep(.01)"], user="65534:65534")["Id"]
        client.api.exec_start(writer, detach=True)
        time.sleep(.1)
        config = output/"config.json"
        save(config, {"directory": str(output), "source": source.id, "session": session,
            "case": "validator-delete", "base": str(base), "base_commit": commit,
            "base_sha256": digest(base.read_bytes())})
        runtime, backend, controller_client = configured(config, True)
        original_remove = controller_client.api.remove_container
        def fail_validator_remove(container, **kwargs):
            nonlocal target
            if container in backend._validation_containers:
                target = container
                save(output/"delete-injection.json", {"container_id": target,
                    "kind": "SDK APIError before validator removal", "daemon_remove_called": False})
                raise docker.errors.APIError("injected validator deletion failure")
            return original_remove(container, **kwargs)
        controller_client.api.remove_container = fail_validator_remove
        result = runtime.run("Offline validator removal failure; no model network")
        assert result.status == "failed" and json.loads(result.error)["code"] == "VERIFICATION_UNCERTAIN"
        assert not runtime.store.list_verified_subtask_checkpoints(result.task_id)
        rows = runtime.store.list_blocking_reservations(str(runtime.repo_root))
        assert len(rows) == 1 and rows[0]["state"] == "unknown"
        resource = rows[0]["details"]["resource"]
        assert resource["kind"] == "docker_validator" and resource["source_id"] == target
        assert resource["parent_source_id"] == source.id
        source.reload()
        assert not source.attrs["State"]["Paused"]
        changed = source.exec_run([PYTHON, "-I", "-c", "import pathlib,time; p=pathlib.Path('/workspace/counter'); "
            "a=p.read_text(); time.sleep(.15); assert a!=p.read_text(); print('PARENT_RELEASED')"])
        assert changed.exit_code == 0
        cleanup = json.loads(next(backend.output.glob("*-validation-cleanup.json")).read_text())
        assert not cleanup["removed"] and cleanup["log_readers_finished"]
        assert client.containers.get(target).id == target
        fresh = Runtime(runtime.repo_root, ScriptedModel([ModelResponse(text="must not run")]))
        try: fresh.run("blocked followup")
        except EffectBlocked: pass
        else: raise AssertionError("Orphan validator did not block fresh Runtime")
        assert fresh.model.call_count == 0
        operator = Runtime(runtime.repo_root, ScriptedModel([]))
        try:
            operator.resolve_workspace_cleanup(rows[0]["reservation_id"],
                lambda value: DockerWorkspaceBackend.observe_removed(client, value), "inspect retained validator")
        except RuntimeError as exc: assert "still exists" in str(exc)
        else: raise AssertionError("Existing validator falsely reconciled")
        client.containers.get(target).remove(force=True)
        operator.resolve_workspace_cleanup(rows[0]["reservation_id"],
            lambda value: DockerWorkspaceBackend.observe_removed(client, value), "fixture owner removed exact validator")
        assert not runtime.store.list_blocking_reservations(str(runtime.repo_root))
        assert runtime.store.get_task(result.task_id)["status"] == "failed"
        follow = Runtime(runtime.repo_root, ScriptedModel([ModelResponse(text="offline followup")]))
        follow_result = follow.run("followup after validator removal")
        assert follow_result.status == "completed"
        runtime.store.assert_invariants()
        TraceReporter(runtime.store).export_jsonl(result.task_id, output/"trace.jsonl")
        TraceReporter(runtime.store).export_jsonl(follow_result.task_id, output/"followup-trace.jsonl")
        save(output/"reservations-final.json", runtime.store.list_effect_reservations(result.task_id))
        save(output/"runtime-summary.json", TraceReporter(runtime.store).summary(result.task_id))
        state.update(status="PASS_VALIDATOR_UNKNOWN_PARENT_RELEASED_RECONCILED", validator_id=target,
            result=result.__dict__, blocked_model_calls=0, parent_writer_resumed=True,
            log_readers_finished=True, false_success_checkpoints=0, existing_validator_probe_rejected=True,
            exact_validator_removed_before_reconciliation=True)
    except Exception as exc:
        state.update(error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        for box in client.containers.list(all=True, filters={"label": ["org.lcc.validation-session="+session,
                                                     "org.lcc.validation-source="+source.id]}):
            box.remove(force=True)
        source.remove(force=True)
        state["fixture_owner_cleanup"] = True
        save(output/"result.json", state)
        if controller_client: controller_client.close()
        client.close()
    print(json.dumps(state), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args().output)
