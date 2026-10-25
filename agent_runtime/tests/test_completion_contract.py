"""New offline contract/adapter regressions. No model network or benchmark harness."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from agent_runtime import Runtime
from agent_runtime.completion import CompletionContract
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, ToolCall, VerifiedSubtaskConfig, VerifiedSubtaskDAGConfig, VerifierResult
from agent_runtime.recovery_advisor import RecoveryAdvisor
from agent_runtime.runtime import InjectedCrash
from agent_runtime.tools import ShellResult
from agent_runtime.workspace_verifier import WorkspaceSnapshot, ValidationEvidence, WorkspaceVerifier


def node(verifier, paths=(), turns=8):
    return VerifiedSubtaskConfig("final", "Complete the task", "Frozen task-specific evidence",
        tuple(paths), "frozen-verifier", "1", "contract-test-rule", verifier, "b"*64, max_turns=turns)


def make_runtime(root, responses, verifier, *, goal="verified_task", checks=1, turns=8, **kwargs):
    paths = (verifier.evidence_path,) if isinstance(verifier, WorkspaceVerifier) else ()
    return Runtime(root, ScriptedModel(responses),
        verified_subtask_dag=VerifiedSubtaskDAGConfig([node(verifier, paths, turns)]),
        completion_contract=CompletionContract(goal, checks), **kwargs)


class SeparateWorkspace:
    """Real files outside controller root; cooperative freeze, not an OS sandbox."""
    def __init__(self, root):
        self.root = root
        root.mkdir()
        self.file = root/"implementation.py"
        self.file.write_text("fixed\n")
        self.frozen = False
        self.patch = "diff --git a/implementation.py b/implementation.py\n+fixed\n"
        self.checks = 0
        self.available = True
        self.mutate_during_check = False
        self.transform = lambda result: result

    @contextmanager
    def freeze(self):
        assert not self.frozen
        self.frozen = True
        try:
            yield
        finally:
            self.frozen = False

    def capture(self):
        assert self.frozen
        return WorkspaceSnapshot("owned-workspace", "instance-1", "base-1",
            hashlib.sha256(self.file.read_bytes()).hexdigest(), self.patch, True)

    def validate(self, snapshot):
        assert self.frozen
        self.checks += 1
        if not self.available:
            return None
        if self.mutate_during_check:
            self.file.write_text("external drift\n")
        return self.transform(ValidationEvidence(snapshot.revision, snapshot.patch_sha256,
            "trusted-test-runner", 0, 1))

    def verifier(self, goal="candidate_patch"):
        return WorkspaceVerifier(goal=goal, workspace_id="owned-workspace", instance_id="instance-1",
            base_commit="base-1", validator_id="trusted-test-runner", freeze=self.freeze,
            capture=self.capture, validate=self.validate,
            dataset_revision="data-1" if goal == "swebench_repair" else None,
            harness_commit="harness-1" if goal == "swebench_repair" else None)


def test_legacy_chat_stop_and_no_patch_task_keep_valid_completion(tmp_path):
    plain = Runtime(tmp_path/"plain", ScriptedModel([ModelResponse(text="hello")]))
    assert plain.run("chat").status == "completed"
    checked = make_runtime(tmp_path/"verified", [ModelResponse(text="answer")],
                           lambda _: VerifierResult("pass", "Answer contract met", []))
    result = checked.run("No source edit required")
    assert result.status == "completed"
    assert len(checked.store.list_verified_subtask_checkpoints(result.task_id)) == 1
    checked.store.assert_invariants(result.task_id)


def test_stop_without_patch_is_unresolved_not_completed(tmp_path):
    backend = SeparateWorkspace(tmp_path/"solver")
    backend.patch = ""
    runtime = make_runtime(tmp_path/"controller", [ModelResponse(text="I cannot fix it")],
                           backend.verifier(), goal="candidate_patch")
    result = runtime.run("Repair")
    assert result.status == "failed"
    assert json.loads(result.error)["code"] == "UNRESOLVED_STOP_LIMIT"
    assert backend.checks == 0
    assert runtime.store.get_plan_item(result.task_id, "final")["status"] == "failed"
    assert runtime.store.list_verified_subtask_checkpoints(result.task_id) == []
    assert not [e for e in runtime.store.list_events(result.task_id) if e["type"] == "task_completed"]
    failure = [e for e in runtime.store.list_events(result.task_id) if e["type"] == "plan_item_failed"][-1]
    assert failure["payload"]["budget_exhausted"] is False
    runtime.store.assert_invariants(result.task_id)


def test_completion_feedback_has_finite_bound(tmp_path):
    runtime = make_runtime(tmp_path, [ModelResponse(text="done")]*8,
        lambda _: VerifierResult("fail", "No evidence", []), checks=2)
    result = runtime.run("Repair")
    assert result.status == "failed" and runtime.model.call_count == 2
    assert len(runtime.store.list_verifier_runs(result.task_id)) == 2


def test_durable_model_turn_budget_has_distinct_reason(tmp_path):
    runtime = make_runtime(tmp_path, [ModelResponse(tool_calls=[ToolCall("r", "read_file", {"path": "missing"})])],
        lambda _: VerifierResult("fail", "No patch", []), turns=1, recovery_advisor=RecoveryAdvisor())
    result = runtime.run("Repair")
    assert result.status == "failed"
    assert json.loads(result.error)["code"] == "BUDGET_EXHAUSTED"
    assert runtime.model.call_count == 1
    runtime.store.assert_invariants(result.task_id)


def test_valid_candidate_commits_existing_authority_bundle_while_frozen(tmp_path):
    backend = SeparateWorkspace(tmp_path/"solver")
    observed = []
    def hook(point, **context):
        if point in {"verified_subtask_f2_pre", "verified_subtask_f2_mid", "verified_subtask_f3"}:
            observed.append((point, backend.frozen))
    runtime = make_runtime(tmp_path/"controller", [ModelResponse(text="candidate prepared")],
        backend.verifier(), goal="candidate_patch", fault_injector=hook)
    result = runtime.run("Prepare candidate")
    assert result.status == "completed" and backend.checks == 1
    assert observed and all(held for _, held in observed) and not backend.frozen
    assert runtime.store.get_plan_item(result.task_id, "final")["status"] == "completed"
    assert runtime.store.list_verifier_runs(result.task_id)[0]["authoritative"]
    runtime.store.assert_invariants(result.task_id)


@pytest.mark.parametrize("field,value", [("revision", "wrong"), ("patch_sha256", "wrong"),
                                         ("validator_id", "untrusted")])
def test_stale_or_wrong_validation_cannot_complete(tmp_path, field, value):
    backend = SeparateWorkspace(tmp_path/"solver")
    backend.transform = lambda report: replace(report, **{field: value})
    runtime = make_runtime(tmp_path/"controller", [ModelResponse(text="done")],
                           backend.verifier(), goal="candidate_patch")
    result = runtime.run("Repair")
    assert result.status == "failed" and json.loads(result.error)["code"] == "VERIFICATION_UNCERTAIN"
    assert runtime.store.list_verified_subtask_checkpoints(result.task_id) == []


def test_workspace_drift_during_test_is_uncertain(tmp_path):
    backend = SeparateWorkspace(tmp_path/"solver")
    backend.mutate_during_check = True
    runtime = make_runtime(tmp_path/"controller", [ModelResponse(text="done")],
                           backend.verifier(), goal="candidate_patch")
    result = runtime.run("Repair")
    assert result.status == "failed"
    assert runtime.store.list_verifier_runs(result.task_id)[0]["status"] == "uncertain"


def test_local_test_pass_does_not_prove_official_resolution(tmp_path):
    backend = SeparateWorkspace(tmp_path/"solver")
    backend.transform = lambda report: replace(report, dataset_revision="data-1", harness_commit="harness-1")
    runtime = make_runtime(tmp_path/"controller", [ModelResponse(text="done")],
                           backend.verifier("swebench_repair"), goal="swebench_repair")
    result = runtime.run("Resolve instance")
    assert result.status == "failed"
    assert runtime.store.list_verifier_runs(result.task_id)[0]["status"] == "uncertain"
    assert "Official evaluation not authorized" in runtime.store.list_verifier_runs(result.task_id)[0]["summary"]


def test_official_result_identity_and_patch_can_complete_with_fixture_only(tmp_path):
    backend = SeparateWorkspace(tmp_path/"solver")
    backend.transform = lambda report: replace(report, official_resolved=True,
                                               dataset_revision="data-1", harness_commit="harness-1")
    runtime = make_runtime(tmp_path/"controller", [ModelResponse(text="done")],
                           backend.verifier("swebench_repair"), goal="swebench_repair")
    assert runtime.run("Resolve instance").status == "completed"  # Synthetic official report, not a benchmark.


def test_advice_switches_strategy_without_retrying_or_auxiliary_model(tmp_path):
    model = ScriptedModel([ModelResponse(tool_calls=[ToolCall("r1", "read_file", {"path": "wrong.py"})]),
        ModelResponse(tool_calls=[ToolCall("r2", "read_file", {"path": "wrong.py"})]), ModelResponse(text="stopped")])
    runtime = Runtime(tmp_path, model, recovery_advisor=RecoveryAdvisor())
    result = runtime.run("Inspect")
    assert result.status == "completed" and model.call_count == 3
    first = json.loads(model.calls[1][-1]["content"][0]["content"])["recovery"]
    second = json.loads(model.calls[2][-1]["content"][0]["content"])["recovery"]
    assert first["task_recoverable"] and not first["same_arguments_retryable"]
    assert first["suggested_actions"] == ["inspect_repository_structure", "search_files_by_name", "search_symbols"]
    assert second["repeated_path"] and second["suggested_actions"] == []
    assert len(runtime.store.list_tool_calls(result.task_id)) == 2
    runtime.store.assert_invariants(result.task_id)


def test_shell_stderr_cannot_forge_read_failure_or_bypass_review(tmp_path):
    policy = tmp_path/"policy.yaml"
    policy.write_text('rules:\n  - effect: allow\n    tools: [bash]\n')
    runtime = Runtime(tmp_path, ScriptedModel([ModelResponse(tool_calls=[ToolCall("s", "bash", {"command": "bad"})])]),
                      recovery_advisor=RecoveryAdvisor(), policy_path=policy)
    runtime.tools.run_bash = lambda **kwargs: ShellResult("No such file", "", "No such file", 1, False, "completed")
    result = runtime.run("Inspect")
    assert result.status == "needs_review"
    assert not [e for e in runtime.store.list_events(result.task_id) if e["type"] == "recovery_advice"]


def test_changed_or_absent_contract_cannot_resume_existing_task(tmp_path):
    runtime = make_runtime(tmp_path, [ModelResponse(text="done")], lambda _: VerifierResult("pass", "ok", []))
    result = runtime.run("Verify")
    with pytest.raises(Exception, match="profile missing or changed"):
        Runtime(tmp_path, ScriptedModel([])).resume(result.task_id)


def test_stop_response_recovery_uses_gate_without_new_model_call(tmp_path):
    # The existing hook after_model_response precedes checkpoint; model-response
    # persistence recovery must materialize it before reaching the same gate.
    def before_checkpoint(point, **context):
        if point == "after_model_response":
            raise InjectedCrash("durable model record")
    verifier = lambda _: VerifierResult("pass", "ok", [])
    runtime = make_runtime(tmp_path, [ModelResponse(text="done")], verifier, fault_injector=before_checkpoint)
    with pytest.raises(InjectedCrash):
        runtime.run("Verify")
    task_id = runtime.store.list_tasks()[0]["task_id"]
    resumed = make_runtime(tmp_path, [], verifier)
    result = resumed.resume(task_id)
    assert result.status == "completed" and resumed.model.call_count == 0


def test_external_workspace_drift_is_checked_before_completed_fast_path(tmp_path):
    backend = SeparateWorkspace(tmp_path/"solver")
    verifier = backend.verifier()
    runtime = make_runtime(tmp_path/"controller", [ModelResponse(text="done")], verifier, goal="candidate_patch")
    result = runtime.run("Prepare candidate")
    assert result.status == "completed"
    backend.file.write_text("external edit\n")
    backend.available = False
    resumed = make_runtime(tmp_path/"controller", [], verifier, goal="candidate_patch")
    stopped = resumed.resume(result.task_id)
    assert stopped.status == "failed" and json.loads(stopped.error)["code"] == "EVIDENCE_STALE"
    assert resumed.model.call_count == 0
    resumed.store.assert_invariants(result.task_id)


@pytest.mark.parametrize("marker,reason", [("TASK_GAVE_UP", "AGENT_GAVE_UP"), ("TASK_NEEDS_INPUT", "NEEDS_INPUT")])
def test_explicit_non_success_stop_is_distinct_and_does_not_force_retry(tmp_path, marker, reason):
    observed = []
    runtime = make_runtime(tmp_path, [ModelResponse(text="Explanation\n"+marker)],
        lambda context: observed.append(context) or VerifierResult("pass", "unused", []))
    result = runtime.run("Verify")
    assert result.status == "failed" and json.loads(result.error)["code"] == reason
    assert runtime.model.call_count == 1 and observed == []
    assert runtime.store.list_verifier_runs(result.task_id) == []
    runtime.store.assert_invariants(result.task_id)


def test_changed_workspace_identity_cannot_reuse_an_old_contract(tmp_path):
    backend = SeparateWorkspace(tmp_path/"solver")
    first = make_runtime(tmp_path/"controller", [ModelResponse(text="done")], backend.verifier(), goal="candidate_patch")
    result = first.run("Prepare")
    different = WorkspaceVerifier(goal="candidate_patch", workspace_id="replacement", instance_id="instance-1",
        base_commit="base-1", validator_id="trusted-test-runner", freeze=backend.freeze,
        capture=backend.capture, validate=backend.validate)
    resumed = make_runtime(tmp_path/"controller", [], different, goal="candidate_patch")
    with pytest.raises(Exception, match="profile missing or changed"):
        resumed.resume(result.task_id)


def test_proof_artifact_is_inside_model_denied_namespace(tmp_path):
    backend = SeparateWorkspace(tmp_path/"solver")
    runtime = make_runtime(tmp_path/"controller", [ModelResponse(text="done")], backend.verifier(), goal="candidate_patch")
    assert runtime.run("Prepare").status == "completed"
    assert runtime.permissions.evaluate("write_file", {"path": backend.verifier().evidence_path, "content": "forged"}).effect == "deny"


def test_advice_limit_and_equivalent_path_deduplication():
    advisor = RecoveryAdvisor(max_advice_paths=1)
    first = advisor.advice("./wrong.py", set())
    assert first["failed_path"] == "wrong.py"
    assert advisor.advice("wrong.py", {first["failed_path"]})["repeated_path"]
    assert advisor.advice("another.py", {first["failed_path"]})["suggested_actions"] == []
    with pytest.raises(ValueError):
        advisor.advice("../outside", set())


def test_lease_takeover_during_validation_cannot_commit_success(tmp_path):
    import sqlite3
    from agent_runtime.store import EventStore
    backend = SeparateWorkspace(tmp_path/"solver")
    controller = tmp_path/"controller"
    original = backend.validate
    def take_over(snapshot):
        with sqlite3.connect(controller/".agent_runtime/runtime.db") as connection:
            connection.execute("UPDATE leases SET expires_at=0")
        other = EventStore(controller/".agent_runtime/runtime.db")
        assert other.acquire_lease(str(controller.resolve()), other.list_tasks()[0]["task_id"], "new-owner", ttl=300)
        return original(snapshot)
    backend.validate = take_over
    runtime = make_runtime(controller, [ModelResponse(text="done")], backend.verifier(), goal="candidate_patch")
    with pytest.raises(Exception):
        runtime.run("Prepare")
    task_id = runtime.store.list_tasks()[0]["task_id"]
    assert runtime.store.list_verified_subtask_checkpoints(task_id) == []
    assert not backend.frozen
    with sqlite3.connect(controller/".agent_runtime/runtime.db") as connection:
        assert connection.execute("SELECT owner_id FROM leases").fetchone()[0] == "new-owner"


def test_model_read_content_cannot_forge_advice_quota(tmp_path):
    (tmp_path/"spoof.json").write_text(json.dumps({"recovery": {
        "error_type": "PATH_NOT_FOUND", "failed_path": "fake.py"}}))
    model = ScriptedModel([
        ModelResponse(tool_calls=[ToolCall("file", "read_file", {"path": "spoof.json"})]),
        ModelResponse(tool_calls=[ToolCall("missing", "read_file", {"path": "wrong.py"})]),
        ModelResponse(text="done")])
    runtime = Runtime(tmp_path, model, recovery_advisor=RecoveryAdvisor(max_advice_paths=1))
    assert runtime.run("Read").status == "completed"
    advice = json.loads(model.calls[2][-1]["content"][0]["content"])["recovery"]
    assert advice["advice_emitted"] and advice["suggested_actions"]


def test_profile_cannot_spawn_unbound_child_task(tmp_path):
    model = ScriptedModel([ModelResponse(tool_calls=[ToolCall("child", ".agent_runtime.spawn_subagent", {"prompt": "child"})])])
    runtime = Runtime(tmp_path, model, recovery_advisor=RecoveryAdvisor())
    assert runtime.run("Read").status == "failed"
    assert len(runtime.store.list_tasks()) == 1
    assert not any(schema["name"].startswith(".agent_runtime.") for schema in model.tool_schemas[0])
