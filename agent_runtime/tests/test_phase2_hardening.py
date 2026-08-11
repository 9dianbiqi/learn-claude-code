from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
import yaml

from agent_runtime.eval_runner import run_suite
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, ToolCall
from agent_runtime.permissions import PermissionEngine
from agent_runtime.runtime import InjectedCrash, Runtime
from agent_runtime.store import EventStore, LeaseLost
from agent_runtime.tools import ToolExecutor
from agent_runtime.trace import TraceReporter
import agent_runtime.store as store_module


StaleState = getattr(store_module, "StaleState", RuntimeError)


def _crash_at(point: str):
    def inject(actual: str, **_):
        if actual == point:
            raise InjectedCrash(actual)

    return inject


def _seed_store(tmp_path: Path) -> tuple[EventStore, str]:
    store = EventStore(tmp_path / "runtime.db")
    task_id = "task-seed"
    store.create_task(task_id, str(tmp_path.resolve()), "seed", "fake")
    store.save_checkpoint(task_id, "input_ready", [{"role": "user", "content": "seed"}], {"turn": 0})
    return store, task_id


def test_model_response_persisted_without_checkpoint_is_recovered_without_second_model_call(tmp_path: Path):
    first_model = ScriptedModel([ModelResponse(text="durable answer")])
    first = Runtime(tmp_path, first_model, fault_injector=_crash_at("after_model_response"))
    with pytest.raises(InjectedCrash):
        first.run("finish")

    task_id = first.store.list_tasks()[0]["task_id"]
    assert first_model.call_count == 1
    assert first.store.list_model_calls(task_id)[0]["status"] == "succeeded"

    recovery_model = ScriptedModel([])
    recovered = Runtime(tmp_path, recovery_model)
    result = recovered.resume(task_id)

    assert result.status == "completed"
    assert result.final_text == "durable answer"
    assert recovery_model.call_count == 0
    assert sum(event["type"] == "task_completed" for event in recovered.store.list_events(task_id)) == 1


def test_persisted_model_tool_response_recovers_pending_tool_without_reinvoking_first_turn(tmp_path: Path):
    (tmp_path / "note.txt").write_text("safe", encoding="utf-8")
    first_model = ScriptedModel([ModelResponse(tool_calls=[ToolCall("read", "read_file", {"path": "note.txt"})])])
    first = Runtime(tmp_path, first_model, fault_injector=_crash_at("after_model_response"))
    with pytest.raises(InjectedCrash):
        first.run("read")
    task_id = first.store.list_tasks()[0]["task_id"]
    recovery_model = ScriptedModel([ModelResponse(text="read recovered")])
    recovered = Runtime(tmp_path, recovery_model)
    result = recovered.resume(task_id)
    assert result.status == "completed"
    assert first_model.call_count == 1
    assert recovery_model.call_count == 1
    assert recovered.store.get_tool_call(task_id, "read")["execution_attempts"] == 1


@pytest.mark.parametrize("point", ["completion_after_checkpoint", "completion_after_status", "completion_after_event"])
def test_terminal_completion_boundary_fault_is_atomic(tmp_path: Path, point: str):
    runtime = Runtime(tmp_path, ScriptedModel([ModelResponse(text="done")]), fault_injector=_crash_at(point))
    with pytest.raises(InjectedCrash):
        runtime.run("finish")

    tasks = runtime.store.list_tasks()
    assert len(tasks) == 1
    task = tasks[0]
    assert task["status"] != "completed"
    assert sum(event["type"] == "task_completed" for event in runtime.store.list_events(task["task_id"])) == 0


def test_stale_tool_call_cas_cannot_overwrite_newer_state(tmp_path: Path):
    store, task_id = _seed_store(tmp_path)
    store.create_tool_call(task_id, "call-1", 0, "bash", {"command": "echo x"}, "hash")
    call = store.get_tool_call(task_id, "call-1")
    version = int(call.get("version", 0))
    store.update_tool_call(task_id, "call-1", status="waiting_approval", expected_version=version)

    with pytest.raises(StaleState):
        store.update_tool_call(task_id, "call-1", status="planned", expected_version=version)

    assert store.get_tool_call(task_id, "call-1")["status"] == "waiting_approval"


def test_stale_task_cas_cannot_overwrite_newer_checkpoint_state(tmp_path: Path):
    store, task_id = _seed_store(tmp_path)
    version = int(store.get_task(task_id)["version"])
    store.update_task(task_id, status="running", expected_version=version)
    with pytest.raises(StaleState):
        store.update_task(task_id, status="needs_review", expected_version=version)
    assert store.get_task(task_id)["status"] == "running"


def test_stale_lease_effect_attempt_is_not_written_to_task_store(tmp_path: Path):
    task_id = "task-stale-audit"
    store = EventStore(tmp_path / "runtime.db")
    store.create_task(task_id, str(tmp_path.resolve()), "audit", "fake")
    store.save_checkpoint(task_id, "input_ready", [{"role": "user", "content": "x"}], {"turn": 0})
    repo = str(tmp_path.resolve())
    token = store.acquire_lease(repo, task_id, "owner", ttl=0.01)
    store.bind_lease(repo, "owner", int(token))
    import time

    time.sleep(0.02)
    with pytest.raises(LeaseLost):
        Runtime(tmp_path, ScriptedModel([]), store=store, owner_id="owner")._assert_lease_for_effect(task_id, "call", "after_tool_effect")
    assert not any(event["type"] == "stale_lease_execution_attempt" for event in store.list_events(task_id))


def test_approval_resume_revalidates_current_permission_context(tmp_path: Path):
    target = tmp_path / "new.txt"
    runtime = Runtime(
        tmp_path,
        ScriptedModel([
            ModelResponse(tool_calls=[ToolCall("write", "write_file", {"path": "new.txt", "content": "x"})]),
            ModelResponse(text="blocked"),
        ]),
        interactive=False,
    )
    waiting = runtime.run("write")
    assert waiting.status == "waiting_approval"
    policy = tmp_path / "deny.yaml"
    policy.write_text(yaml.safe_dump({"rules": [{"id": "deny-write", "effect": "deny", "tools": ["write_file"]}]}), encoding="utf-8")
    runtime.permissions = PermissionEngine(tmp_path, policy)
    resumed = runtime.approve("write")
    assert resumed.status == "completed"
    call = runtime.store.get_tool_call(waiting.task_id, "write")
    assert call["status"] == "denied"
    assert not target.exists()


@pytest.mark.parametrize("raw", ["../outside.txt", ".\\..\\outside.txt", "C:relative.txt", "note.txt:secret", r"\\\\server\\share\\x.txt"])
def test_windows_and_traversal_paths_fail_closed(tmp_path: Path, raw: str):
    decision = PermissionEngine(tmp_path).evaluate("write_file", {"path": raw, "content": "x"})
    assert decision.effect == "deny"


def test_symlink_target_is_rejected(tmp_path: Path):
    target = tmp_path / "target.txt"
    target.write_text("safe", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this platform")
    assert PermissionEngine(tmp_path).evaluate("read_file", {"path": "link.txt"}).effect == "deny"
    with pytest.raises(ValueError):
        ToolExecutor(tmp_path).safe_path("link.txt")


def test_edit_preserves_crlf_and_bom(tmp_path: Path):
    target = tmp_path / "note.txt"
    target.write_bytes(b"\xef\xbb\xbfold\r\nline\r\n")
    model = ScriptedModel(
        [
            ModelResponse(tool_calls=[ToolCall("read", "read_file", {"path": "note.txt"})]),
            ModelResponse(tool_calls=[ToolCall("edit", "edit_file", {"path": "note.txt", "old_text": "old", "new_text": "new"})]),
            ModelResponse(text="done"),
        ]
    )
    result = Runtime(tmp_path, model, approval_callback=lambda *_: True).run("edit")
    assert result.status == "completed"
    assert target.read_bytes() == b"\xef\xbb\xbfnew\r\nline\r\n"


def test_delete_or_rename_race_fails_closed(tmp_path: Path):
    target = tmp_path / "note.txt"
    target.write_text("old", encoding="utf-8")
    model = ScriptedModel([
        ModelResponse(tool_calls=[ToolCall("read", "read_file", {"path": "note.txt"})]),
        ModelResponse(tool_calls=[ToolCall("edit", "edit_file", {"path": "note.txt", "old_text": "old", "new_text": "new"})]),
    ])

    def rename_after_start(point, tool_use_id=None, **_):
        if point == "after_tool_started" and tool_use_id == "edit":
            target.rename(tmp_path / "moved.txt")

    result = Runtime(tmp_path, model, approval_callback=lambda *_: True, fault_injector=rename_after_start).run("edit")
    assert result.status == "needs_review"
    assert (tmp_path / "moved.txt").read_text(encoding="utf-8") == "old"


def test_shell_composition_cannot_become_auto_allow_under_wildcard_policy(tmp_path: Path):
    policy = tmp_path / "policy.yaml"
    policy.write_text(yaml.safe_dump({"rules": [{"id": "wild", "effect": "allow", "tools": ["*"]}]}), encoding="utf-8")
    engine = PermissionEngine(tmp_path, policy)
    for command in ("echo x | cat", "echo x > marker", "echo x && echo y", "echo x; echo y", "echo $(whoami)"):
        assert engine.evaluate("bash", {"command": command}).effect in {"ask", "deny"}
    assert engine.evaluate("bash", {"command": "echo $RUNTIME_PATH"}).effect == "deny"
    assert engine.evaluate("bash", {"command": "del .agent_*"}).effect == "deny"


@pytest.mark.parametrize(
    "command",
    ["cmd /c echo x", "powershell -EncodedCommand ZQBjAGgAbwAgAHg=", "python -c 'print(1)'", "node -e 'console.log(1)'"],
)
def test_shell_wrappers_are_denied(tmp_path: Path, command: str):
    assert PermissionEngine(tmp_path).evaluate("bash", {"command": command}).effect == "deny"


def test_nonzero_shell_result_is_needs_review_and_persists_metadata(tmp_path: Path):
    model = ScriptedModel([ModelResponse(tool_calls=[ToolCall("bad", "bash", {"command": "echo partial > marker.txt & exit 7"})])])
    runtime = Runtime(tmp_path, model, approval_callback=lambda *_: True)
    result = runtime.run("shell")
    assert result.status == "needs_review"
    call = runtime.store.get_tool_call(result.task_id, "bad")
    assert call["status"] == "needs_review"
    assert call["returncode"] == 7
    assert call["timed_out"] == 0
    assert call["execution_status"] == "nonzero"
    assert (tmp_path / "marker.txt").read_text(encoding="utf-8").strip() == "partial"


def test_shell_timeout_is_structured_and_not_success(tmp_path: Path):
    executor = ToolExecutor(tmp_path)
    executor.shell_timeout = 0.01
    result = executor.run_bash("python -c \"import time; time.sleep(0.2)\"")
    assert result.timed_out is True
    assert result.status == "timed_out"
    assert result.returncode is None


def test_shell_stdout_and_stderr_are_bounded(tmp_path: Path):
    executor = ToolExecutor(tmp_path)
    result = executor.run_bash("python -c \"import sys; print('o'*60000); print('e'*60000, file=sys.stderr)\"")
    assert len(result.stdout.encode("utf-8")) <= 50_000 + len("... [truncated]".encode("utf-8"))
    assert len(result.stderr.encode("utf-8")) <= 50_000 + len("... [truncated]".encode("utf-8"))
    assert result.returncode == 0


def test_unknown_shell_effect_attempt_is_counted_without_claiming_exactly_once(tmp_path: Path):
    model = ScriptedModel([ModelResponse(tool_calls=[ToolCall("shell", "bash", {"command": "echo x >> marker.txt"})])])
    first = Runtime(tmp_path, model, approval_callback=lambda *_: True, fault_injector=_crash_at("after_tool_effect_before_persist"))
    with pytest.raises(InjectedCrash):
        first.run("shell")
    task_id = first.store.list_tasks()[0]["task_id"]
    resumed = Runtime(tmp_path, ScriptedModel([]))
    assert resumed.resume(task_id).status == "needs_review"
    call = resumed.store.get_tool_call(task_id, "shell")
    assert call["execution_attempts"] == 1
    assert call["effect_attempts"] == 1
    summary = TraceReporter(resumed.store).summary(task_id)
    assert summary["effect_attempts"] == 1
    assert summary["duplicate_effect_attempts"] == 0
    assert summary["confirmed_duplicate_side_effects"] == 0


def test_duplicate_effect_metrics_are_not_dedup_event_metrics(tmp_path: Path):
    store, task_id = _seed_store(tmp_path)
    for tool_id in ("write-a", "write-b"):
        store.create_tool_call(
            task_id,
            tool_id,
            0,
            "write_file",
            {"path": "same.txt", "content": "x"},
            "same-hash",
            effect="file_write",
            effect_key="same-effect",
        )
        store.start_tool_call(task_id, tool_id, "file_write")
        store.update_tool_call(task_id, tool_id, status="succeeded", effect_confirmed=1, finished_at=1.0)
    summary = TraceReporter(store).summary(task_id)
    assert summary["duplicate_tool_calls"] == 0
    assert summary["duplicate_effect_attempts"] == 1
    assert summary["confirmed_duplicate_side_effects"] == 1


def test_store_invariant_checker_detects_pointer_mismatch(tmp_path: Path):
    store, task_id = _seed_store(tmp_path)
    assert store.scan_invariants(task_id) == []
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE tasks SET checkpoint_id = 999 WHERE task_id = ?", (task_id,))
    violations = store.scan_invariants(task_id)
    assert any("checkpoint" in item for item in violations)
    with pytest.raises(RuntimeError, match="invariant"):
        store.assert_invariants(task_id)


def test_invariant_checker_rejects_aborted_executable_call(tmp_path: Path):
    store, task_id = _seed_store(tmp_path)
    store.create_tool_call(task_id, "running", 0, "bash", {"command": "echo x"}, "hash")
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE tasks SET status = 'aborted' WHERE task_id = ?", (task_id,))
    violations = store.scan_invariants(task_id)
    assert any("aborted task has executable" in item for item in violations)


def test_unsupported_schema_fails_closed(tmp_path: Path):
    db = tmp_path / "runtime.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)")
        conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (999, 0)")
    with pytest.raises(RuntimeError, match="schema"):
        EventStore(db)


def test_trace_contains_attempt_and_review_metrics_and_redacts_secrets(tmp_path: Path):
    runtime = Runtime(tmp_path, ScriptedModel([ModelResponse(text="done")]))
    result = runtime.run("token=super-secret")
    summary = TraceReporter(runtime.store).summary(result.task_id)
    for key in (
        "tool_execution_attempts", "effect_attempts", "duplicate_effect_attempts", "confirmed_duplicate_side_effects",
        "permission_bypass_count", "invariant_violation_count", "needs_review_correctness", "stale_lease_execution_attempts",
    ):
        assert key in summary
    output = tmp_path / "trace.jsonl"
    TraceReporter(runtime.store).export_jsonl(result.task_id, output)
    assert "super-secret" not in output.read_text(encoding="utf-8")


def test_eval_fault_must_trigger_and_interruption_is_not_inferred(tmp_path: Path):
    suite = {
        "name": "fault-negative",
        "tasks": [{"id": "never", "model": [{"text": "done"}], "fault": {"point": "does_not_exist"}}],
    }
    suite_path = tmp_path / "suite.yaml"
    suite_path.write_text(yaml.safe_dump(suite), encoding="utf-8")
    report = run_suite(suite_path, tmp_path / "runs")
    assert report["passed"] == 0
    assert report["actual_interruption_count"] == 0
    assert report["recovery_cases"] == 0


def test_eval_rejects_existing_unmarked_run_root(tmp_path: Path):
    root = tmp_path / "runs"
    root.mkdir()
    (root / "do-not-delete.txt").write_text("keep", encoding="utf-8")
    suite_path = tmp_path / "suite.yaml"
    suite_path.write_text(yaml.safe_dump({"tasks": [{"id": "one", "model": [{"text": "done"}]}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="marker"):
        run_suite(suite_path, root)


def test_eval_rejects_fixture_and_check_traversal(tmp_path: Path):
    fixture_suite = tmp_path / "fixture.yaml"
    fixture_suite.write_text(
        yaml.safe_dump({"tasks": [{"id": "fixture", "fixture": {"files": {"../escape.txt": "x"}}, "model": [{"text": "done"}]}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Fixture path"):
        run_suite(fixture_suite, tmp_path / "fixture-runs")

    check_suite = tmp_path / "check.yaml"
    check_suite.write_text(
        yaml.safe_dump({"tasks": [{"id": "check", "model": [{"text": "done"}], "checks": {"files": {"../outside": "x"}}}]}),
        encoding="utf-8",
    )
    report = run_suite(check_suite, tmp_path / "check-runs")
    assert report["passed"] == 0


def test_eval_rejects_repository_root(tmp_path: Path):
    suite_path = tmp_path / "suite.yaml"
    suite_path.write_text(yaml.safe_dump({"tasks": [{"id": "repo", "model": [{"text": "done"}]}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="repository"):
        run_suite(suite_path, Path.cwd())


def test_read_file_has_byte_limit(tmp_path: Path):
    (tmp_path / "large.txt").write_bytes(b"x" * (2 * 1024 * 1024))
    with pytest.raises(ValueError, match="size"):
        ToolExecutor(tmp_path).run_read_file("large.txt")


def test_oversized_read_fails_without_leaving_running_tool(tmp_path: Path):
    (tmp_path / "large.txt").write_bytes(b"x" * (2 * 1024 * 1024))
    runtime = Runtime(tmp_path, ScriptedModel([ModelResponse(tool_calls=[ToolCall("large", "read_file", {"path": "large.txt"})])]))
    result = runtime.run("read large")
    assert result.status == "failed"
    call = runtime.store.get_tool_call(result.task_id, "large")
    assert call["status"] == "failed"
    assert runtime.store.scan_invariants(result.task_id) == []


def test_checkpoint_and_model_request_size_guards(tmp_path: Path):
    store, task_id = _seed_store(tmp_path)
    with pytest.raises(ValueError, match="checkpoint messages"):
        store.save_checkpoint(task_id, "input_ready", [{"role": "user", "content": "x" * (5 * 1024 * 1024)}], {"turn": 0})
    with pytest.raises(ValueError, match="model request"):
        store.create_model_call(task_id, 0, {"messages": [{"role": "user", "content": "x" * (5 * 1024 * 1024)}]})


def test_model_response_size_guard_fails_closed(tmp_path: Path):
    result = Runtime(tmp_path, ScriptedModel([ModelResponse(text="x" * (2 * 1024 * 1024))])).run("large")
    assert result.status == "failed"
