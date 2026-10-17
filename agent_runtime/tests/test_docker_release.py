"""New release failure regressions. No Docker daemon or external model calls."""
import json
import time

import pytest

from agent_runtime import Runtime
import agent_runtime.docker_workspace as module
from agent_runtime.completion import CompletionContract
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, VerifiedSubtaskConfig, VerifiedSubtaskDAGConfig
from agent_runtime.workspace_verifier import ValidationEvidence
from agent_runtime.tests.test_docker_workspace import backend


@pytest.mark.parametrize("failure", ["unpause", "thaw"])
def test_release_failure_is_recorded_and_does_not_rewrite_committed_task(tmp_path, failure):
    worker, source = backend(tmp_path, {"math.py": b"answer = 4\n"})
    worker.validate = lambda snapshot: ValidationEvidence(snapshot.revision, snapshot.patch_sha256,
                                                         worker.validator_id, 0, 1)
    def fail(*args): raise RuntimeError("injected "+failure+" failure")
    if failure == "unpause": source.unpause = fail
    else: worker._thaw = fail
    verifier = worker.verifier()
    node = VerifiedSubtaskConfig("candidate", "candidate", "fixture check", (verifier.evidence_path,),
        worker.validator_id, "1", worker.validator_id, verifier, "b"*64, max_turns=3)
    runtime = Runtime(tmp_path/"controller", ScriptedModel([ModelResponse(text="ready")]),
        verified_subtask_dag=VerifiedSubtaskDAGConfig([node]),
        completion_contract=CompletionContract("candidate_patch", 1))
    caught = None
    try: runtime.run("offline release fixture")
    except RuntimeError as exc: caught = exc
    task = runtime.store.list_tasks()[0]
    assert task["status"] == "completed"  # Cleanup must not overwrite committed verification authority.
    assert caught is not None and type(caught).__name__ == "WorkspaceCleanupError"
    assert len(runtime.store.list_verified_subtask_checkpoints(task["task_id"])) == 1
    runtime.store.assert_invariants(task["task_id"])
    records = list(worker.output.glob("*-freeze-release.json"))
    assert len(records) == 1
    report = json.loads(records[0].read_text())
    assert report["cleanup_status"] == "requires_review"
    assert report["manual_recovery_required"] and not report["processes_resumed"]
    assert report["observed_paused"] == (failure == "unpause")


def release_report(worker):
    return json.loads(next(worker.output.glob("*-freeze-release.json")).read_text())


def test_unpause_applied_but_response_lost_is_not_reported_as_still_paused(tmp_path):
    worker, source = backend(tmp_path, {})
    original = source.unpause
    def ambiguous():
        original()
        raise TimeoutError("response lost after unpause")
    source.unpause = ambiguous
    thaw = []
    worker._thaw = lambda source: thaw.append(True)
    with pytest.raises(module.WorkspaceCleanupError):
        with worker.freeze(): pass
    record = release_report(worker)
    assert record["observed_paused"] is False and record["unpaused"] is False
    assert not record["processes_resumed"] and not thaw


def test_primary_verification_error_is_not_masked_by_release_failure(tmp_path):
    worker, source = backend(tmp_path, {})
    source.unpause = lambda: (_ for _ in ()).throw(RuntimeError("cleanup also failed"))
    with pytest.raises(ValueError, match="primary verification error") as caught:
        with worker.freeze(): raise ValueError("primary verification error")
    assert release_report(worker)["cleanup_status"] == "requires_review"
    assert any("cleanup requires review" in note for note in caught.value.__notes__)


def test_lease_loss_between_unpause_and_thaw_never_sends_sigcont(tmp_path):
    from agent_runtime.store import LeaseLost
    worker, source = backend(tmp_path, {})
    thaw = []
    worker._thaw = lambda source: thaw.append(True)
    worker.fault_injector = lambda point: setattr(worker, "_heartbeat_error", "injected takeover") if point == "release_after_unpause" else None
    with pytest.raises(module.WorkspaceCleanupError):
        with worker.freeze(): pass
    assert not thaw and not source.attrs["State"]["Paused"]
    assert release_report(worker)["error_type"] == "LeaseLost"


def test_heartbeat_remains_active_during_slow_release(tmp_path):
    worker, source = backend(tmp_path, {})
    beats = []
    worker.interval = .02
    worker.heartbeat = lambda: beats.append(time.monotonic())
    original = source.unpause
    def slow():
        start = len(beats)
        time.sleep(.15)
        assert len(beats) >= start+3
        original()
    source.unpause = slow
    with worker.freeze(): pass
    assert release_report(worker)["cleanup_status"] == "released"


def test_failure_after_sigcont_still_requires_release_confirmation(tmp_path):
    worker, source = backend(tmp_path, {})
    def fault(point):
        if point == "release_after_thaw": raise OSError("release journal confirmation unavailable")
    worker.fault_injector = fault
    with pytest.raises(module.WorkspaceCleanupError):
        with worker.freeze(): pass
    record = release_report(worker)
    assert record["processes_resumed"] and record["manual_recovery_required"]
    assert record["cleanup_status"] == "requires_review" and not record["observed_paused"]


def test_releasing_journal_cannot_be_automatically_adopted(tmp_path, monkeypatch):
    from agent_runtime.tests.test_docker_recovery import frozen
    journal, _ = frozen(tmp_path, monkeypatch)
    journal.update(state="releasing")
    before = journal.path.read_bytes()
    with pytest.raises(RuntimeError, match="quarantine"):
        journal.adopt(paused=True, max_bytes=1024, digest=module.digest)
    assert journal.path.read_bytes() == before
