"""New repository-wide cleanup gate and trusted reconciliation tests."""
import pytest

from agent_runtime import Runtime
from agent_runtime.completion import CompletionContract
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, VerifiedSubtaskConfig, VerifiedSubtaskDAGConfig
from agent_runtime.store import EffectBlocked
from agent_runtime.workspace_verifier import ValidationEvidence, WorkspaceCleanupError
from agent_runtime.tests.test_docker_workspace import backend


def configured(root, worker, model):
    worker.engine_id = "unit-engine"  # Trusted daemon identity in the in-memory fixture.
    worker.validate = lambda snapshot: ValidationEvidence(snapshot.revision, snapshot.patch_sha256,
                                                         worker.validator_id, 0, 1)
    verifier = worker.verifier()
    node = VerifiedSubtaskConfig("candidate", "candidate", "fixture", (verifier.evidence_path,),
        worker.validator_id, "1", worker.validator_id, verifier, "b"*64, max_turns=3)
    return Runtime(root, model, verified_subtask_dag=VerifiedSubtaskDAGConfig([node]),
                   completion_contract=CompletionContract("candidate_patch", 1))


def pending(tmp_path):
    directory = tmp_path/"first"
    directory.mkdir()
    worker, source = backend(directory, {"math.py": b"answer = 4\n"})
    worker._thaw = lambda _: (_ for _ in ()).throw(RuntimeError("injected thaw failure"))
    runtime = configured(tmp_path/"controller", worker, ScriptedModel([ModelResponse(text="ready")]))
    with pytest.raises(WorkspaceCleanupError): runtime.run("offline fixture")
    task = runtime.store.list_tasks()[0]["task_id"]
    return runtime, task, source


@pytest.mark.parametrize("entry", ["new_backend_resume", "new_chat_task"])
def test_ignored_cleanup_failure_blocks_new_runtime_before_model_or_capture(tmp_path, entry):
    old, task, source = pending(tmp_path)
    model = ScriptedModel([ModelResponse(text="must not run")])
    if entry == "new_backend_resume":
        directory = tmp_path/"replacement"
        directory.mkdir()
        worker, _ = backend(directory, {"math.py": b"answer = 4\n"})
        calls = []
        original = worker.capture
        worker.capture = lambda: (calls.append(True), original())[1]
        new = configured(old.repo_root, worker, model)
        action = lambda: new.resume(task)
    else:
        calls = []
        new = Runtime(old.repo_root, model)
        action = lambda: new.run("new ordinary task")
    with pytest.raises(EffectBlocked): action()
    assert model.call_count == 0 and calls == []
    assert old.store.get_task(task)["status"] == "completed"


def removed(resource):
    return {"outcome": "removed", "resource": resource, "method": "docker-get-404", "engine_id": resource["engine_id"]}


def test_trusted_reconciliation_unblocks_same_repo_without_rewriting_task(tmp_path):
    runtime, task, _ = pending(tmp_path)
    reservation = runtime.store.list_blocking_reservations(str(runtime.repo_root))[0]
    operator = Runtime(runtime.repo_root, ScriptedModel([]))
    operator.resolve_workspace_cleanup(reservation["reservation_id"], removed, "fixture owner confirmed removal")
    assert not operator.store.list_blocking_reservations(str(operator.repo_root))
    assert operator.store.get_task(task)["status"] == "completed"
    assert len(operator.store.list_verified_subtask_checkpoints(task)) == 1
    operator.store.assert_invariants(task)
    new = Runtime(runtime.repo_root, ScriptedModel([ModelResponse(text="read-only followup")]))
    assert new.run("followup").status == "completed"


@pytest.mark.parametrize("invalid", ["exists", "wrong_resource", "wrong_engine", "missing_method", "blank_reason"])
def test_bad_operator_observation_never_clears_blocker(tmp_path, invalid):
    runtime, task, _ = pending(tmp_path)
    reservation = runtime.store.list_blocking_reservations(str(runtime.repo_root))[0]
    def observe(resource):
        value = removed(resource)
        if invalid == "exists": value["outcome"] = "exists"
        if invalid == "wrong_resource": value["resource"] = {**resource, "source_id": "other"}
        if invalid == "wrong_engine": value["engine_id"] = "other"
        if invalid == "missing_method": value.pop("method")
        return value
    with pytest.raises(ValueError):
        Runtime(runtime.repo_root, ScriptedModel([])).resolve_workspace_cleanup(
            reservation["reservation_id"], observe, "" if invalid == "blank_reason" else "inspect")
    assert runtime.store.list_blocking_reservations(str(runtime.repo_root))[0]["state"] == "unknown"
    runtime.store.assert_invariants(task)


def test_duplicate_reconciliation_rejected_without_probe(tmp_path):
    from agent_runtime.store import StaleState
    runtime, _, _ = pending(tmp_path)
    row = runtime.store.list_blocking_reservations(str(runtime.repo_root))[0]
    operator = Runtime(runtime.repo_root, ScriptedModel([]))
    operator.resolve_workspace_cleanup(row["reservation_id"], removed, "removed")
    calls = []
    with pytest.raises(StaleState):
        operator.resolve_workspace_cleanup(row["reservation_id"], lambda _: calls.append(True), "again")
    assert not calls


def test_lease_expiry_during_probe_rejects_resolution(tmp_path):
    import sqlite3
    from agent_runtime.store import LeaseLost
    runtime, _, _ = pending(tmp_path)
    row = runtime.store.list_blocking_reservations(str(runtime.repo_root))[0]
    def observe(resource):
        with sqlite3.connect(runtime.store.path) as connection:
            connection.execute("UPDATE leases SET expires_at=0")
        return removed(resource)
    with pytest.raises(LeaseLost):
        Runtime(runtime.repo_root, ScriptedModel([])).resolve_workspace_cleanup(row["reservation_id"], observe, "removed")
    assert runtime.store.list_blocking_reservations(str(runtime.repo_root))


def test_clean_release_does_not_block_and_unrelated_repo_is_independent(tmp_path):
    runtime, _, _ = pending(tmp_path)
    unrelated = Runtime(tmp_path/"independent", ScriptedModel([ModelResponse(text="ok")]))
    assert unrelated.run("independent").status == "completed"
    directory = tmp_path/"clean-backend"
    directory.mkdir()
    worker, _ = backend(directory, {"math.py": b"answer = 4\n"})
    clean = configured(tmp_path/"clean-controller", worker, ScriptedModel([ModelResponse(text="ready")]))
    assert clean.run("clean fixture").status == "completed"
    assert not clean.store.list_blocking_reservations(str(clean.repo_root))
    clean.store.assert_invariants()


@pytest.mark.parametrize("mode", ["exists", "wrong_engine", "timeout", "removed"])
def test_docker_removal_probe_does_not_confuse_uncertainty_with_absence(mode):
    from types import SimpleNamespace
    from agent_runtime.docker_workspace import DockerWorkspaceBackend
    resource = {"source_id": "full-id", "engine_id": "engine"}
    def get(_):
        if mode == "exists": return object()
        if mode == "timeout": raise TimeoutError("daemon unreachable")
        error = RuntimeError("typed HTTP response fixture")
        error.response = SimpleNamespace(status_code=404)
        raise error
    client = SimpleNamespace(info=lambda: {"ID": "other" if mode == "wrong_engine" else "engine"},
                             containers=SimpleNamespace(get=get))
    if mode == "removed":
        assert DockerWorkspaceBackend.observe_removed(client, resource)["outcome"] == "removed"
    else:
        with pytest.raises((RuntimeError, ValueError, TimeoutError)):
            DockerWorkspaceBackend.observe_removed(client, resource)


def test_blocked_entry_does_not_replace_active_task_context(tmp_path):
    old, _, _ = pending(tmp_path)
    new = Runtime(old.repo_root, ScriptedModel([]))
    assert new._active_task_id is None
    with pytest.raises(EffectBlocked): new.run("must block before entering task")
    assert new._active_task_id is None


def test_old_projection_validation_stays_strict_for_resource_rows(tmp_path):
    from agent_runtime.migrations import _legacy_projection_violations
    runtime, task, _ = pending(tmp_path)
    runtime.store.assert_invariants(task)
    connection = runtime.store._connect()
    try:
        violations = _legacy_projection_violations(connection, task)
        assert any("missing tool call" in item for item in violations)
    finally:
        connection.close()


def test_cleanup_exception_does_not_exempt_unbound_or_tool_reservations(tmp_path):
    import sqlite3
    from agent_runtime.store import InvariantViolation
    runtime, _, _ = pending(tmp_path)
    with sqlite3.connect(runtime.store.path) as connection:
        connection.execute("UPDATE effect_reservations SET tool_use_id='unbound'")
    with pytest.raises(InvariantViolation, match="workspace cleanup binding"):
        runtime.store.assert_invariants()
    with sqlite3.connect(runtime.store.path) as connection:
        connection.execute("UPDATE effect_reservations SET effect='unknown_write'")
    with pytest.raises(InvariantViolation, match="missing tool call"):
        runtime.store.assert_invariants()
