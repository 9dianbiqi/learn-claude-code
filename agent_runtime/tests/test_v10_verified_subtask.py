from __future__ import annotations

import sqlite3
import time
import json
from pathlib import Path

import pytest

from agent_runtime import Runtime
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.migrations import (
    SchemaManager,
    V5_CHECKSUM,
    V6_CHECKSUM,
    V7_CHECKSUM,
    V8_CHECKSUM,
    V9_CHECKSUM,
    V10_CHECKSUM,
    _BASE_SCHEMA,
    _apply_v6_durable_context,
    _apply_v7_background_jobs,
    _apply_v8_tool_registry,
    _apply_v9_subagents_mailbox,
)
from agent_runtime.models import ModelResponse, VerifierContext, VerifierResult, VerifiedSubtaskConfig
from agent_runtime.runtime import InjectedCrash
from agent_runtime.store import EventStore, InvariantViolation
from agent_runtime.trace import TraceReporter


def _config(verifier, evidence_paths: list[str] | tuple[str, ...] = ("artifact.txt",)) -> VerifiedSubtaskConfig:
    return VerifiedSubtaskConfig(
        subtask_id="s1",
        description="Create the artifact",
        completion_criteria="The artifact exists with the expected content.",
        evidence_paths=evidence_paths,
        verifier_id="artifact-verifier",
        verifier_version="1.0",
        verification_rule="artifact-v1",
        verifier=verifier,
    )


def _v9_database(tmp_path: Path) -> Path:
    database = tmp_path / "runtime.db"
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    for statement in _BASE_SCHEMA:
        connection.execute(statement)
    _apply_v6_durable_context(connection)
    _apply_v7_background_jobs(connection)
    _apply_v8_tool_registry(connection)
    _apply_v9_subagents_mailbox(connection)
    now = time.time()
    for version, name, checksum in (
        (5, "v5_effect_ledger", V5_CHECKSUM),
        (6, "v6_durable_context", V6_CHECKSUM),
        (7, "v7_background_jobs", V7_CHECKSUM),
        (8, "v8_tool_registry_mcp", V8_CHECKSUM),
        (9, "v9_subagents_mailbox", V9_CHECKSUM),
    ):
        connection.execute(
            "INSERT INTO schema_migrations(version, name, checksum, applied_at, duration_ms, "
            "backup_filename, backup_sha256) VALUES (?, ?, ?, ?, 0.0, NULL, NULL)",
            (version, name, checksum, now),
        )
    connection.commit()
    connection.close()
    return database


def test_public_run_commits_a_verified_subtask_bundle(tmp_path: Path):
    seen: list[VerifierContext] = []

    def verify(context: VerifierContext) -> VerifierResult:
        seen.append(context)
        return VerifierResult(
            status="pass",
            summary="artifact checks passed",
            evidence_manifest=[{"path": "artifact.txt", "sha256": "abc123"}],
        )

    runtime = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="summary\nSUBTASK_COMPLETE\n")]),
        verified_subtask=_config(verify),
    )

    result = runtime.run("Create the artifact")

    assert result.status == "completed"
    assert result.final_text == "summary"
    assert len(seen) == 1
    assert seen[0].repo_root == str(tmp_path.resolve())
    assert seen[0].task_id == result.task_id
    assert seen[0].subtask_id == "s1"
    assert seen[0].completion_summary == "summary"

    item = runtime.store.get_plan_item(result.task_id, "s1")
    assert item is not None
    assert item["status"] == "completed"
    assert runtime.store.list_plans(result.task_id)[0]["status"] == "completed"
    runs = runtime.store.list_verifier_runs(result.task_id)
    assert len(runs) == 1
    assert runs[0]["status"] == "pass"
    assert runs[0]["authoritative"] is True
    assert runs[0]["evidence_manifest"] == [{"path": "artifact.txt", "sha256": "abc123"}]

    checkpoints = runtime.store.list_semantic_checkpoints(result.task_id)
    assert len(checkpoints) == 1
    assert checkpoints[0]["verifier_run_id"] == runs[0]["verifier_run_id"]
    assert checkpoints[0]["execution_checkpoint_id"] == seen[0].execution_checkpoint_id
    assert runtime.store.scan_invariants(result.task_id) == []


def test_bundle_hash_and_evidence_manifest_are_deterministic(tmp_path: Path):
    config_a = _config(lambda _: VerifierResult("pass", "ok", []), ["z.txt", "a.txt"])
    config_b = _config(lambda _: VerifierResult("pass", "ok", []), ["a.txt", "z.txt"])
    assert config_a.verifier_bundle_hash == config_b.verifier_bundle_hash

    runtime = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="done\nSUBTASK_COMPLETE")]),
        verified_subtask=VerifiedSubtaskConfig(
            subtask_id=config_a.subtask_id,
            description=config_a.description,
            completion_criteria=config_a.completion_criteria,
            evidence_paths=config_a.evidence_paths,
            verifier_id=config_a.verifier_id,
            verifier_version=config_a.verifier_version,
            verification_rule=config_a.verification_rule,
            verifier=lambda _: VerifierResult(
                "pass",
                "ok",
                [
                    {"path": "z.txt", "sha256": "z"},
                    {"path": "a.txt", "sha256": "a"},
                ],
            ),
        ),
    )

    result = runtime.run("Create the artifacts")

    assert result.status == "completed"
    assert runtime.store.list_verifier_runs(result.task_id)[0]["evidence_manifest"] == [
        {"path": "a.txt", "sha256": "a"},
        {"path": "z.txt", "sha256": "z"},
    ]


def test_v9_migrates_to_v10_with_verified_subtask_schema(tmp_path: Path):
    database = _v9_database(tmp_path)

    report = SchemaManager(database).migrate()

    assert report.ok is True
    assert report.from_version == 9
    assert report.to_version == 10
    assert report.applied == ("v10_verified_subtask",)
    assert SchemaManager(database).inspect().current_version == 10
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"verifier_runs", "semantic_checkpoints"} <= tables
        assert connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version = 10"
        ).fetchone()[0] == V10_CHECKSUM
        assert "verifier_bundle_hash" in {
            row[1] for row in connection.execute("PRAGMA table_info(plan_items)")
        }


def test_v9_to_v10_migration_rolls_back_before_commit(tmp_path: Path):
    database = _v9_database(tmp_path)

    def inject(point: str, **_: object) -> None:
        if point == "before_migration_commit":
            raise RuntimeError(point)

    with pytest.raises(RuntimeError, match="before_migration_commit"):
        SchemaManager(database, fault_injector=inject).migrate()

    assert SchemaManager(database).inspect().current_version == 9
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "verifier_runs" not in tables
        assert "semantic_checkpoints" not in tables
        assert "verifier_bundle_hash" not in {
            row[1] for row in connection.execute("PRAGMA table_info(plan_items)")
        }


def test_fresh_database_is_v10_and_integrity_checked(tmp_path: Path):
    store = EventStore(tmp_path / "runtime.db")

    assert store.integrity_check() == []
    assert SchemaManager(store.path).inspect().current_version == 10
    with sqlite3.connect(store.path) as connection:
        assert [row[0] for row in connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )] == [10]
        assert connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version = 10"
        ).fetchone()[0] == V10_CHECKSUM


def test_f2_pre_recovery_reverifies_without_leaving_a_partial_bundle(tmp_path: Path):
    verifier_calls: list[int] = []

    def verify(_: VerifierContext) -> VerifierResult:
        verifier_calls.append(1)
        return VerifierResult("pass", "ok", [{"path": "artifact.txt", "sha256": "abc123"}])

    config = _config(verify)

    def crash(point: str, **_: object) -> None:
        if point == "verified_subtask_f2_pre":
            raise InjectedCrash(point)

    first = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="done\nSUBTASK_COMPLETE")]),
        verified_subtask=config,
        fault_injector=crash,
    )
    with pytest.raises(InjectedCrash, match="verified_subtask_f2_pre"):
        first.run("Create the artifact")

    task_id = first.store.list_tasks()[0]["task_id"]
    assert first.store.get_active_plan(task_id)["items"][0]["status"] == "verifying"
    assert first.store.list_verifier_runs(task_id) == []
    assert first.store.list_semantic_checkpoints(task_id) == []

    recovered = Runtime(tmp_path, ScriptedModel([]), verified_subtask=config)
    result = recovered.resume(task_id)

    assert result.status == "completed"
    assert len(verifier_calls) == 2
    assert len(recovered.store.list_verifier_runs(task_id)) == 1
    assert len(recovered.store.list_semantic_checkpoints(task_id)) == 1


def test_f2_mid_recovery_rolls_back_all_authoritative_writes(tmp_path: Path):
    verifier_calls: list[int] = []

    def verify(_: VerifierContext) -> VerifierResult:
        verifier_calls.append(1)
        return VerifierResult("pass", "ok", [{"path": "artifact.txt", "sha256": "abc123"}])

    config = _config(verify)

    def crash(point: str, **_: object) -> None:
        if point == "verified_subtask_f2_mid":
            raise InjectedCrash(point)

    first = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="done\nSUBTASK_COMPLETE")]),
        verified_subtask=config,
        fault_injector=crash,
    )
    with pytest.raises(InjectedCrash, match="verified_subtask_f2_mid"):
        first.run("Create the artifact")

    task_id = first.store.list_tasks()[0]["task_id"]
    assert first.store.get_task(task_id)["status"] != "completed"
    assert first.store.get_active_plan(task_id)["items"][0]["status"] == "verifying"
    assert first.store.list_verifier_runs(task_id) == []
    assert first.store.list_semantic_checkpoints(task_id) == []
    assert not any(
        event["type"] in {"verified_subtask_committed", "semantic_checkpoint_created"}
        for event in first.store.list_events(task_id)
    )

    recovered = Runtime(tmp_path, ScriptedModel([]), verified_subtask=config)
    result = recovered.resume(task_id)

    assert result.status == "completed"
    assert len(verifier_calls) == 2
    assert len(recovered.store.list_verifier_runs(task_id)) == 1
    assert len(recovered.store.list_semantic_checkpoints(task_id)) == 1


def test_f3_recovery_exposes_one_bundle_and_deduplicates_resume(tmp_path: Path):
    verifier_calls: list[int] = []

    def verify(_: VerifierContext) -> VerifierResult:
        verifier_calls.append(1)
        return VerifierResult("pass", "ok", [{"path": "artifact.txt", "sha256": "abc123"}])

    config = _config(verify)

    def crash(point: str, **_: object) -> None:
        if point == "verified_subtask_f3":
            raise InjectedCrash(point)

    first = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="done\nSUBTASK_COMPLETE")]),
        verified_subtask=config,
        fault_injector=crash,
    )
    with pytest.raises(InjectedCrash, match="verified_subtask_f3"):
        first.run("Create the artifact")

    task_id = first.store.list_tasks()[0]["task_id"]
    assert first.store.get_task(task_id)["status"] == "completed"
    assert first.store.get_plan_item(task_id, "s1")["status"] == "completed"
    assert len(first.store.list_verifier_runs(task_id)) == 1
    assert len(first.store.list_semantic_checkpoints(task_id)) == 1

    recovered = Runtime(tmp_path, ScriptedModel([]), verified_subtask=config)
    result = recovered.resume(task_id)
    repeated = recovered.resume(task_id)

    assert result.status == "completed"
    assert repeated.final_text == "done"
    assert verifier_calls == [1]
    assert len(recovered.store.list_verifier_runs(task_id)) == 1
    assert len(recovered.store.list_semantic_checkpoints(task_id)) == 1


def test_trace_exposes_verifier_runs_and_semantic_checkpoints(tmp_path: Path):
    config = _config(
        lambda _: VerifierResult(
            "pass", "ok", [{"path": "artifact.txt", "sha256": "abc123"}]
        )
    )
    runtime = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="done\nSUBTASK_COMPLETE")]),
        verified_subtask=config,
    )
    result = runtime.run("Create the artifact")

    summary = TraceReporter(runtime.store).summary(result.task_id)
    assert summary["verifier_run_count"] == 1
    assert summary["authoritative_verifier_run_count"] == 1
    assert summary["semantic_checkpoint_count"] == 1
    assert summary["verified_subtask_bundle_count"] == 1

    output = tmp_path / "trace.jsonl"
    TraceReporter(runtime.store).export_jsonl(result.task_id, output)
    records = [
        json.loads(line)
        for line in output.read_text(encoding="utf-8").splitlines()
        if "record_type" in json.loads(line)
    ]
    assert {record["record_type"] for record in records} >= {
        "verifier_run",
        "semantic_checkpoint",
    }


def test_verified_resume_fails_closed_when_bundle_is_inconsistent(tmp_path: Path):
    config = _config(
        lambda _: VerifierResult(
            "pass", "ok", [{"path": "artifact.txt", "sha256": "abc123"}]
        )
    )
    runtime = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="done\nSUBTASK_COMPLETE")]),
        verified_subtask=config,
    )
    result = runtime.run("Create the artifact")

    with sqlite3.connect(runtime.store.path) as connection:
        connection.execute("DELETE FROM semantic_checkpoints WHERE task_id = ?", (result.task_id,))
        connection.commit()

    assert runtime.store.scan_invariants(result.task_id)
    with pytest.raises(InvariantViolation, match="invariant"):
        Runtime(tmp_path, ScriptedModel([]), verified_subtask=config).resume(result.task_id)


def test_completion_marker_requires_an_exclusive_line(tmp_path: Path):
    seen: list[str] = []

    def verify(context: VerifierContext) -> VerifierResult:
        seen.append(context.completion_summary)
        return VerifierResult("pass", "ok", [{"path": "artifact.txt", "sha256": "abc123"}])

    runtime = Runtime(
        tmp_path,
        ScriptedModel([
            ModelResponse(text="This text mentions SUBTASK_COMPLETE inline."),
            ModelResponse(text="summary\nSUBTASK_COMPLETE\n"),
        ]),
        verified_subtask=_config(verify),
    )

    result = runtime.run("Create the artifact")

    assert result.status == "completed"
    assert result.final_text == "summary"
    assert seen == ["summary"]


def test_default_exec_full_behavior_remains_unchanged(tmp_path: Path):
    model = ScriptedModel([ModelResponse(text="ordinary completion")])

    result = Runtime(tmp_path, model).run("Finish normally")

    assert result.status == "completed"
    assert result.final_text == "ordinary completion"
    assert model.call_count == 1
    assert Runtime(tmp_path, ScriptedModel([])).store.list_verifier_runs(result.task_id) == []


def test_failed_verification_is_retryable_and_non_authoritative(tmp_path: Path):
    outcomes = iter([
        VerifierResult("fail", "artifact is missing", []),
        VerifierResult("pass", "artifact checks passed", [{"path": "artifact.txt", "sha256": "abc123"}]),
    ])
    model = ScriptedModel([
        ModelResponse(text="first attempt\nSUBTASK_COMPLETE"),
        ModelResponse(text="fixed attempt\nSUBTASK_COMPLETE"),
    ])

    def verify(_: VerifierContext) -> VerifierResult:
        return next(outcomes)

    runtime = Runtime(tmp_path, model, verified_subtask=_config(verify))

    result = runtime.run("Create the artifact")

    assert result.status == "completed"
    runs = runtime.store.list_verifier_runs(result.task_id)
    assert [(run["status"], run["authoritative"]) for run in runs] == [
        ("fail", False),
        ("pass", True),
    ]
    assert len(runtime.store.list_semantic_checkpoints(result.task_id)) == 1
    assert any(
        "Verifier result: fail. artifact is missing" in str(message.get("content"))
        for message in model.calls[1]
    )
    events = runtime.store.list_events(result.task_id)
    assert any(
        event["type"] == "verifier_run_recorded"
        and event["payload"]["status"] == "fail"
        and event["payload"]["authoritative"] is False
        for event in events
    )


def test_verified_resume_is_idempotent_and_rejects_a_mismatched_bundle(tmp_path: Path):
    verifier_calls: list[int] = []

    def verify(_: VerifierContext) -> VerifierResult:
        verifier_calls.append(1)
        return VerifierResult("pass", "ok", [{"path": "artifact.txt", "sha256": "abc123"}])

    config = _config(verify)
    first = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="done\nSUBTASK_COMPLETE")]),
        verified_subtask=config,
    )
    completed = first.run("Create the artifact")

    recovered_model = ScriptedModel([])
    recovered = Runtime(tmp_path, recovered_model, verified_subtask=config)
    resumed = recovered.resume(completed.task_id)

    assert resumed.status == "completed"
    assert resumed.final_text == "done"
    assert recovered_model.call_count == 0
    assert len(verifier_calls) == 1
    assert len(recovered.store.list_verifier_runs(completed.task_id)) == 1
    assert len(recovered.store.list_semantic_checkpoints(completed.task_id)) == 1

    mismatched = VerifiedSubtaskConfig(
        subtask_id=config.subtask_id,
        description=config.description,
        completion_criteria=config.completion_criteria,
        evidence_paths=config.evidence_paths,
        verifier_id=config.verifier_id,
        verifier_version=config.verifier_version,
        verification_rule="different-rule",
        verifier=verify,
    )
    with pytest.raises(RuntimeError, match="bundle hash mismatch"):
        Runtime(tmp_path, ScriptedModel([]), verified_subtask=mismatched).resume(completed.task_id)


def test_uncertain_verification_is_distinct_non_authoritative_retry(tmp_path: Path):
    outcomes = iter([
        VerifierResult("uncertain", "the check could not run", []),
        VerifierResult("pass", "ok", [{"path": "artifact.txt", "sha256": "abc123"}]),
    ])

    def verify(_: VerifierContext) -> VerifierResult:
        return next(outcomes)

    runtime = Runtime(
        tmp_path,
        ScriptedModel([
            ModelResponse(text="uncertain attempt\nSUBTASK_COMPLETE"),
            ModelResponse(text="retry attempt\nSUBTASK_COMPLETE"),
        ]),
        verified_subtask=_config(verify),
    )

    result = runtime.run("Create the artifact")

    assert result.status == "completed"
    runs = runtime.store.list_verifier_runs(result.task_id)
    assert runs[0]["status"] == "uncertain"
    assert runs[0]["authoritative"] is False
    assert runs[1]["status"] == "pass"
    assert runs[1]["authoritative"] is True
    assert runtime.store.get_plan_item(result.task_id, "s1")["status"] == "completed"
