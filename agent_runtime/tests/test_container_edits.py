"""Offline protocol/Runtime tests. Fake transport is not Docker acceptance."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent_runtime.container_edits import DockerRepairBridge, EDIT_PROGRAM
from agent_runtime.security import SENSITIVE_BASENAME_PATTERNS


def sha(data):
    return hashlib.sha256(data).hexdigest()


def setup_bridge(tmp_path):
    workspace = tmp_path / "isolated-fixture"
    workspace.mkdir()
    (workspace / "app.py").write_bytes(b"value = 1\n")
    backend = SimpleNamespace(source_id="owned", session="session", image="sha256:" + "a" * 64,
        engine_id="engine", path="/workspace", instance_id="fixture", base_commit="a" * 40)
    bridge = DockerRepairBridge(backend, tmp_path / "receipts")
    def read(request, record):
        data = (workspace / request["args"]["path"]).read_bytes()
        return {"ok": True, "nonce": request["nonce"], "result": {"sha256": sha(data), "text": data.decode()}}
    def edit(request, record):
        result = subprocess.run([sys.executable, "-I", "-c", EDIT_PROGRAM, str(workspace),
            json.dumps(request), json.dumps(SENSITIVE_BASENAME_PATTERNS)], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    bridge.reads._request = read
    bridge.edits._request = edit
    return workspace, bridge


def arguments():
    return {"path": "app.py", "expected_sha256": sha(b"value = 1\n"), "old_text": "1", "new_text": "2"}


def test_read_edit_is_bound_and_unknown_write(tmp_path):
    from agent_runtime.tool_registry import ToolRegistry
    workspace, bridge = setup_bridge(tmp_path)
    bridge.reads.execute("read_file", {"path": "app.py"})
    result = json.loads(bridge.edits.execute(arguments()))
    assert result["after_sha256"] == sha(b"value = 2\n")
    assert (workspace / "app.py").read_bytes() == b"value = 2\n"
    registry = ToolRegistry(tmp_path)
    bridge.install(registry)
    assert registry.get("container_edit_file").effect_kind == "unknown_write"


def test_unread_edit_never_dispatches(tmp_path):
    _, bridge = setup_bridge(tmp_path)
    bridge.edits._request = lambda *a: pytest.fail("unread edit dispatched")
    with pytest.raises(RuntimeError, match="prior bound"):
        bridge.edits.execute(arguments())
    assert bridge.edits.calls == 0


@pytest.mark.parametrize("path", ["../outside", "/etc/passwd", ".git/config", ".env", "x\\y", "x/../app.py"])
def test_edit_paths_fail_before_dispatch(tmp_path, path):
    _, bridge = setup_bridge(tmp_path)
    args = dict(arguments(), path=path)
    with pytest.raises((ValueError, PermissionError)):
        bridge.edits.execute(args)
    assert bridge.edits.calls == 0


def test_stale_container_contents_are_preserved(tmp_path):
    workspace, bridge = setup_bridge(tmp_path)
    bridge.reads.execute("read_file", {"path": "app.py"})
    (workspace / "app.py").write_bytes(b"external change")
    with pytest.raises(RuntimeError, match="STALE_FILE"):
        bridge.edits.execute(arguments())
    assert (workspace / "app.py").read_bytes() == b"external change"


def test_ambiguous_match_and_forged_reply_fail(tmp_path):
    workspace, bridge = setup_bridge(tmp_path)
    (workspace / "app.py").write_bytes(b"11")
    bridge.reads.execute("read_file", {"path": "app.py"})
    with pytest.raises(RuntimeError, match="UNIQUE_MATCH"):
        bridge.edits.execute(dict(arguments(), expected_sha256=sha(b"11")))
    assert (workspace / "app.py").read_bytes() == b"11"
    bridge.edits._request = lambda request, record: {"nonce": "forged", "ok": True}
    with pytest.raises(RuntimeError, match="malformed"):
        bridge.edits.execute(dict(arguments(), expected_sha256=sha(b"11")))


def test_hardlink_cannot_modify_external_inode(tmp_path):
    import os
    workspace, bridge = setup_bridge(tmp_path)
    os.link(workspace / "app.py", tmp_path / "outside.py")
    bridge.reads.execute("read_file", {"path": "app.py"})
    with pytest.raises(RuntimeError, match="single-link"):
        bridge.edits.execute(arguments())
    assert (tmp_path / "outside.py").read_bytes() == b"value = 1\n"


def test_inspect_runtime_container_repair_with_fake_transport(tmp_path):
    pytest.importorskip("inspect_ai")
    from agent_runtime.inspect_runtime_solver import run, ContainerCaseBinding
    from agent_runtime.models import VerifiedSubtaskConfig, VerifierResult
    workspace, bridge = setup_bridge(tmp_path)
    node = VerifiedSubtaskConfig("fixture", "Repair fixture", "Check frozen goal", (), "edit-fixture", "1",
        sha(b"value = 2\n"), lambda ctx: VerifierResult("pass" if (workspace / "app.py").read_bytes() == b"value = 2\n" else "fail", "file goal", []),
        sha(EDIT_PROGRAM.encode()), max_turns=4)
    source = tmp_path / "inputs"
    source.mkdir()
    plan = {"schema_version": "inspect-runtime-plan-v1", "run_id": "repair-fixture", "cases": [{
        "id": "repair", "execution_kind": "container_repair", "prompt": "Read app.py, change 1 to 2, then finish.",
        "files": {"app.py": "HOST_SENTINEL"}, "goal_files": {}, "max_turns": 4,
        "model": [{"tool": "container_read_file", "id": "read", "input": {"path": "app.py"}},
                  {"tool": "container_edit_file", "id": "edit", "input": arguments()}, {"text": "Repaired"}],
        "checks": {"status": "completed", "files": {"app.py": "HOST_SENTINEL"}}}]}
    (source / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    report = run(source / "plan.json", tmp_path / "out", bindings={"repair": ContainerCaseBinding(bridge, node)})
    assert report["processing_ok"], report
    assert report["runtime_cases"] == {"total": 1, "passed": 1}
    calls = json.loads((tmp_path / "out/repair/tool-calls.json").read_text())
    assert next(c for c in calls if c["name"] == "container_edit_file")["effect"] == "unknown_write"
    receipts = "".join(p.read_text() for p in (tmp_path / "out/repair/bridge-calls").glob("*.json"))
    assert "HOST_SENTINEL" not in receipts


def test_interrupted_edit_is_not_replayed(tmp_path):
    from agent_runtime import Runtime
    from agent_runtime.fake_model import ScriptedModel
    from agent_runtime.models import ModelResponse, ToolCall
    from agent_runtime.runtime import InjectedCrash
    workspace, bridge = setup_bridge(tmp_path)
    controller = tmp_path / "controller"
    controller.mkdir()
    policy = controller / "policy.yaml"
    policy.write_text("rules:\n  - effect: allow\n    tools: " + json.dumps(list(bridge.names)) + "\n")
    model = ScriptedModel([
        ModelResponse(tool_calls=[ToolCall("read", "container_read_file", {"path": "app.py"})]),
        ModelResponse(tool_calls=[ToolCall("edit", "container_edit_file", arguments())]), ModelResponse(text="done")])
    def fault(point, **context):
        if point == "after_tool_effect_before_persist" and context.get("tool_use_id") == "edit":
            raise InjectedCrash(point)
    runtime = Runtime(controller, model, tool_setup=bridge.install, tool_scope=bridge.names,
                      policy_path=policy, fault_injector=fault)
    with pytest.raises(InjectedCrash):
        runtime.run("repair")
    task_id = runtime.store.list_tasks()[0]["task_id"]
    resumed = Runtime(controller, model, tool_setup=bridge.install, tool_scope=bridge.names, policy_path=policy)
    result = resumed.resume(task_id)
    assert result.status == "needs_review"
    assert bridge.edits.calls == 1
    assert (workspace / "app.py").read_bytes() == b"value = 2\n"


def test_undeclared_controller_effect_remains_rejected(tmp_path):
    from dataclasses import replace
    from agent_runtime import Runtime
    from agent_runtime.fake_model import ScriptedModel
    _, bridge = setup_bridge(tmp_path)
    def invalid(registry):
        bridge.install(registry)
        entry = registry.get("container_edit_file")
        registry._entries[entry.tool_name] = replace(entry, permission_requirements={"controller_binding": bridge.edits.identity})
    with pytest.raises(ValueError, match="cannot declare side effects"):
        Runtime(tmp_path / "controller", ScriptedModel([]), tool_setup=invalid, tool_scope=bridge.names)


def test_binding_uses_existing_candidate_verifier(tmp_path):
    pytest.importorskip("inspect_ai")
    from agent_runtime.container_edits import repair_binding
    from agent_runtime.workspace_verifier import WorkspaceVerifier
    from contextlib import nullcontext
    _, bridge = setup_bridge(tmp_path)
    backend = bridge.backend
    backend.validator_id = "validator"
    verifier = WorkspaceVerifier(goal="candidate_patch", workspace_id=backend.source_id,
        instance_id=backend.instance_id, base_commit=backend.base_commit, validator_id="validator",
        freeze=nullcontext, capture=lambda: None, validate=lambda _: None)
    backend.verifier = lambda: verifier
    binding = repair_binding(backend, tmp_path / "binding", max_turns=5)
    assert binding.verifier_node.verifier is verifier
    assert binding.verifier_node.max_turns == 5
    assert binding.bridge.names[-1] == "container_edit_file"


def test_read_budget_excludes_container_write(tmp_path):
    from agent_runtime import Runtime
    from agent_runtime.completion import CompletionContract
    from agent_runtime.fake_model import ScriptedModel
    from agent_runtime.models import VerifiedSubtaskConfig, VerifiedSubtaskDAGConfig, VerifierResult
    _, bridge = setup_bridge(tmp_path)
    node = VerifiedSubtaskConfig("fixture", "Goal", "Check", (), "test", "1", sha(b"goal"),
                                lambda ctx: VerifierResult("pass", "ok", []), sha(b"source"), max_turns=4)
    runtime = Runtime(tmp_path / "controller", ScriptedModel([]), tool_setup=bridge.install, tool_scope=bridge.names,
        completion_contract=CompletionContract("verified_task"), verified_subtask_dag=VerifiedSubtaskDAGConfig([node]),
        controller_read_attempt_limit=2)
    assert set(runtime._controller_read_budget.tool_names) == set(bridge.reads.names)
    assert "container_edit_file" not in runtime._controller_read_budget.tool_names
