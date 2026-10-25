"""Independent-review regression: validator deletion failure is a durable blocker."""
import json
from types import SimpleNamespace

import pytest

from agent_runtime import Runtime
from agent_runtime.completion import CompletionContract
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, VerifiedSubtaskConfig, VerifiedSubtaskDAGConfig
from agent_runtime.store import EffectBlocked
from agent_runtime.tests.test_docker_workspace import backend
import agent_runtime.docker_workspace as module


def test_validator_remove_failure_is_recorded_blocks_fresh_runtime_and_releases_parent(tmp_path, monkeypatch):
    worker, source = backend(tmp_path, {"math.py": b"answer = 4\n"})
    worker.engine_id = "fixture-engine"
    class Box:
        id = "owned-validator"
        attrs = {"State": {"Running": False, "ExitCode": 0}}
        def start(self): pass
        def reload(self): pass
        def logs(self, **kwargs): return iter([b"ok\n"] if kwargs["stdout"] else [])
        def remove(self, **kwargs): raise RuntimeError("injected validator removal failure")
    worker.client.containers.create = lambda *args, **kwargs: Box()
    monkeypatch.setattr(module, "load_tmpfs_archive", lambda *args, **kwargs: None)
    verifier = worker.verifier()
    node = VerifiedSubtaskConfig("candidate", "candidate", "fixture", (verifier.evidence_path,),
        worker.validator_id, "1", worker.validator_id, verifier, "b"*64, max_turns=3)
    runtime = Runtime(tmp_path/"controller", ScriptedModel([ModelResponse(text="ready")]),
        verified_subtask_dag=VerifiedSubtaskDAGConfig([node]), completion_contract=CompletionContract("candidate_patch", 1))
    result = runtime.run("offline deletion failure")
    assert result.status == "failed"
    rows = runtime.store.list_blocking_reservations(str(runtime.repo_root))
    assert len(rows) == 1 and rows[0]["state"] == "unknown"
    resource = rows[0]["details"]["resource"]
    assert resource["kind"] == "docker_validator" and resource["source_id"] == "owned-validator"
    assert resource["parent_source_id"] == source.id
    assert not source.attrs["State"]["Paused"]
    records = list(worker.output.glob("*-validation-cleanup.json"))
    assert len(records) == 1
    cleanup = json.loads(records[0].read_text())
    assert not cleanup["removed"] and cleanup["log_readers_finished"]
    fresh = Runtime(runtime.repo_root, ScriptedModel([ModelResponse(text="must not run")]))
    with pytest.raises(EffectBlocked): fresh.run("new task")
    assert fresh.model.call_count == 0
    runtime.store.assert_invariants()


@pytest.mark.parametrize("change", ["parent_source_id", "engine_id", "session", "owner"])
def test_parent_drain_cannot_bypass_an_unrelated_cleanup_blocker(tmp_path, change):
    import sqlite3
    runtime = Runtime(tmp_path, ScriptedModel([ModelResponse(text="bootstrap")]))
    task = runtime.run("bootstrap").task_id
    runtime._acquire(task)
    child = {"kind": "docker_validator", "source_id": "validator", "image": "image",
             "parent_source_id": "source", "session": "session", "engine_id": "engine"}
    if change != "owner": child[change] = "different"
    reservation = runtime.store.begin_workspace_cleanup(child)
    runtime.store.finish_effect_reservation(reservation, "unknown", {"resource": child})
    if change == "owner":
        with sqlite3.connect(runtime.store.path) as connection:
            connection.execute("UPDATE effect_reservations SET owner_id='different' WHERE reservation_id=?", (reservation,))
    parent = {"kind": "docker_workspace", "source_id": "source", "image": "image", "session": "session", "engine_id": "engine"}
    try:
        with pytest.raises(EffectBlocked): runtime.store.begin_workspace_cleanup(parent)
    finally:
        runtime._release()
