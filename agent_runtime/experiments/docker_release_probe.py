"""Offline release-failure injection on real Docker containers; no model API."""
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
from requests.exceptions import ReadTimeout

from agent_runtime import Runtime
import agent_runtime.docker_workspace as backend_module
from agent_runtime.completion import CompletionContract
from agent_runtime.docker_workspace import CommandProbe, DockerWorkspaceBackend, digest, load_tmpfs_archive
from agent_runtime.experiments.docker_backend_probe import IMAGE, PYTHON, files_tar, save, local_client
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, VerifiedSubtaskConfig, VerifiedSubtaskDAGConfig
from agent_runtime.store import EventStore, EffectBlocked
from agent_runtime.trace import TraceReporter
from agent_runtime.workspace_verifier import WorkspaceCleanupError


CASES = ("unpause-rejected", "unpause-response-lost", "thaw-nonzero",
         "crash-before-unpause", "crash-after-unpause", "crash-after-thaw")
CRASH_POINTS = {"crash-before-unpause": "release_before_unpause",
                "crash-after-unpause": "release_after_unpause", "crash-after-thaw": "release_after_thaw"}


def configured(config_path, child):
    config = json.loads(config_path.read_text())
    root = Path(config["directory"])
    client = local_client()
    runtime = None
    def fault(point):
        if child and point == CRASH_POINTS.get(config["case"]):
            save(root/"crash-observation.json", {"point": point, "pid": os.getpid(), "exit": 73})
            os._exit(73)
    backend = DockerWorkspaceBackend(client=client, source_id=config["source"], session=config["session"],
        image=IMAGE, workspace_path="/workspace", instance_id="offline-release-fixture",
        base_commit=config["base_commit"], base_archive=config["base"],
        base_archive_sha256=config["base_sha256"],
        probes=[CommandProbe((PYTHON, "-I", "-c", "exec(open('/workspace/math.py').read()); print(answer)"), digest(b"4\n"), 6)],
        output=root/("child-backend" if child else "resume-backend"),
        heartbeat=lambda: runtime.heartbeat_verification(),
        lease_identity=lambda: runtime.verification_lease_identity(),
        recovery_journal=None if child else root/"child-backend/freeze-journal.json",
        fault_injector=fault, scope_timeout_seconds=25)
    if child and config["case"] in {"unpause-rejected", "unpause-response-lost"}:
        original = client.api.unpause
        def injected_unpause(container):
            assert container == config["source"]
            if config["case"] == "unpause-rejected":
                save(root/"injection.json", {"kind": "SDK APIError before daemon action", "daemon_unpause_called": False})
                raise docker.errors.APIError("injected unpause rejection")
            original(container)  # Real Docker action; only its acknowledgement is faulted.
            save(root/"injection.json", {"kind": "SDK ReadTimeout after real daemon action", "daemon_unpause_called": True})
            raise ReadTimeout("injected response loss after daemon unpause")
        client.api.unpause = injected_unpause
    if child and config["case"] == "thaw-nonzero":
        original = client.api.exec_create
        def injected_exec(*args, **kwargs):
            command = kwargs.get("cmd", args[1] if len(args) > 1 else None)
            if command and len(command) >= 4 and "entries=json.loads" in command[3]:
                command = list(command)
                command[3] = "raise SystemExit(41)"
                if "cmd" in kwargs: kwargs["cmd"] = command
                else: args = (args[0], command, *args[2:])
                save(root/"injection.json", {"kind": "replacement thaw program", "real_container_exit": 41})
            return original(*args, **kwargs)
        client.api.exec_create = injected_exec
    verifier = backend.verifier()
    node = VerifiedSubtaskConfig("candidate", "Verify candidate", "Fixed offline probe", (verifier.evidence_path,),
        backend.validator_id, "1", backend.validator_id, verifier, digest(Path(backend_module.__file__).read_bytes()), max_turns=4)
    runtime = Runtime(root/"controller", ScriptedModel([ModelResponse(text="fixture ready")] if child else []),
        verified_subtask_dag=VerifiedSubtaskDAGConfig([node]), completion_contract=CompletionContract("candidate_patch", 1), lease_ttl=2)
    return runtime, backend, client


def child(config_path):
    runtime, backend, client = configured(config_path, True)
    try:
        runtime.run("Controller-authored offline candidate; release fault fixture")
        raise AssertionError("Release failure was silently swallowed")
    except WorkspaceCleanupError as exc:
        task = runtime.store.list_tasks()[0]
        assert task["status"] == "completed"
        save(config_path.parent/"child-result.json", {"exception": type(exc).__name__, "report": exc.report,
             "task_status": task["status"], "real_model_calls": 0})
    finally:
        client.close()


def run_case(output, case, base, commit):
    root = output/case
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
    state = {"case": case, "status": "FAILED", "source_id": source.id,
             "real_model_api_calls": 0, "paid_calls": 0, "official_harness_runs": 0}
    try:
        load_tmpfs_archive(client, source.id, "/workspace", files_tar({"math.py": b"answer = 4\n"}))
        writer = client.api.exec_create(source.id, cmd=[PYTHON, "-I", "-c",
            "import pathlib,time; p=pathlib.Path('/workspace/counter'); i=0\n"
            "while True:\n p.write_text(str(i)); i+=1; time.sleep(.01)"], user="65534:65534")["Id"]
        client.api.exec_start(writer, detach=True)
        time.sleep(.15)
        save(root/"config.json", {"directory": str(root), "source": source.id, "session": session, "case": case,
             "base": str(base), "base_commit": commit, "base_sha256": digest(base.read_bytes())})
        result = subprocess.run([sys.executable, __file__, "--child", str(root/"config.json")], capture_output=True, timeout=45)
        (root/"child-stdout.log").write_bytes(result.stdout)
        (root/"child-stderr.log").write_bytes(result.stderr)
        assert result.returncode == (73 if case in CRASH_POINTS else 0), result.stderr.decode(errors="replace")
        store = EventStore(root/"controller/.agent_runtime/runtime.db")
        task = store.list_tasks()[0]
        assert task["status"] == "completed"
        assert len(store.list_verified_subtask_checkpoints(task["task_id"])) == 1
        store.assert_invariants(task["task_id"])
        with sqlite3.connect(store.path) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert connection.execute("SELECT COUNT(*) FROM verifier_runs WHERE authoritative=1").fetchone()[0] == 1
            expiry = connection.execute("SELECT expires_at FROM leases").fetchone()[0]
        journal_path = root/"child-backend/freeze-journal.json"
        journal = json.loads(journal_path.read_text())
        assert journal["state"] == "releasing" and journal["stopped"]
        source.reload()
        paused = source.attrs["State"]["Paused"]
        assert paused == (case in {"unpause-rejected", "crash-before-unpause"})
        reports = list((root/"child-backend").glob("*-freeze-release.json"))
        if case not in CRASH_POINTS:
            assert len(reports) == 1
            report = json.loads(reports[0].read_text())
            assert report["cleanup_status"] == "requires_review" and report["manual_recovery_required"]
            assert report["observed_paused"] == paused and not report["processes_resumed"]
        else:
            assert not reports  # os._exit cannot execute finally; persistent phase is the evidence.
        while time.time() <= expiry: time.sleep(.03)
        runtime, backend, resume_client = configured(root/"config.json", False)
        try:
            try: runtime.resume(task["task_id"])
            except EffectBlocked as exc:
                assert any(row["effect"] == "workspace_cleanup" for row in exc.reservations)
                state["resume_refusal"] = str(exc)
            else: raise AssertionError("Release-in-progress source silently returned cached completion")
            assert runtime.model.call_count == 0
        finally:
            resume_client.close()
        assert len(store.list_verified_subtask_checkpoints(task["task_id"])) == 1
        assert store.get_task(task["task_id"])["status"] == "completed"
        if not paused:
            inspect = source.exec_run([PYTHON, "-I", "-c", "import pathlib,time,json; p=pathlib.Path('/workspace/counter'); "
                "a=p.read_text(); time.sleep(.15); b=p.read_text(); print(json.dumps({'changed':a!=b}))"])
            assert inspect.exit_code == 0
            changed = json.loads(inspect.output)["changed"]
            assert changed == (case == "crash-after-thaw")
            state["writer_changed_after_failure"] = changed
        assert not client.containers.list(all=True, filters={"label": "org.lcc.validation-session="+session})
        TraceReporter(store).export_jsonl(task["task_id"], root/"trace.jsonl")
        save(root/"runtime-summary.json", TraceReporter(store).summary(task["task_id"]))
        state.update(status="PASS_RELEASE_FAILURE_EXPLICIT_REVIEW", child_exit=result.returncode,
            task_id=task["task_id"], task_status="completed", authoritative_checkpoints=1,
            journal_state=journal["state"], release_stage=journal.get("release_stage"), paused=paused,
            recovery_model_calls=0, automatic_release_retry=False, validators_remaining=0)
    except Exception as exc:
        state.update(error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        for box in client.containers.list(all=True, filters={"label": "org.lcc.validation-session="+session}):
            box.remove(force=True)
        source.remove(force=True)
        state["fixture_owner_cleanup"] = True
        save(root/"result.json", state)
        client.close()
    print(json.dumps({"case": case, "status": state["status"]}), flush=True)
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
    results = [run_case(output, case, base, commit) for case in (selected or CASES)]
    save(output/"summary.json", {"status": "PASS_OFFLINE_RELEASE_FAILURE_BOUNDARIES", "cases": results,
        "real_model_api_calls": 0, "paid_calls": 0, "official_harness_runs": 0,
        "fixture_patches_not_agent_generated": True})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--case", choices=CASES, action="append")
    args = parser.parse_args()
    if args.child: child(args.child)
    else: main(args.output, args.case)
