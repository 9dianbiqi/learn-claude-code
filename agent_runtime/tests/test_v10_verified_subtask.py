from __future__ import annotations

import sqlite3
import time
import json
from functools import partial
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
    MigrationValidationError,
    _BASE_SCHEMA,
    _apply_v6_durable_context,
    _apply_v7_background_jobs,
    _apply_v8_tool_registry,
    _apply_v9_subagents_mailbox,
)
from agent_runtime.models import (
    ModelResponse,
    ToolCall,
    VerifierContext,
    VerifierResult,
    VerifiedSubtaskConfig,
)
from agent_runtime.runtime import InjectedCrash
from agent_runtime.store import EventStore, InvariantViolation
from agent_runtime.trace import TraceReporter


ARTIFACT_SHA256 = "a" * 64
EVIDENCE_A_SHA256 = "a" * 64
EVIDENCE_Z_SHA256 = "b" * 64


def _config(
    verifier,
    evidence_paths: list[str] | tuple[str, ...] = ("artifact.txt",),
    *,
    description: str = "Create the artifact",
    completion_criteria: str = "The artifact exists with the expected content.",
) -> VerifiedSubtaskConfig:
    return VerifiedSubtaskConfig(
        subtask_id="s1",
        description=description,
        completion_criteria=completion_criteria,
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
            evidence_manifest=[{"path": "artifact.txt", "sha256": ARTIFACT_SHA256}],
        )

    runtime = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="summary\nSUBTASK_COMPLETE\n")]),
        verified_subtask=_config(verify),
    )

    result = runtime.run("Create the artifact")

    assert result.status == "completed"
    assert result.final_text == "summary\n"
    assert len(seen) == 1
    assert seen[0].repo_root == str(tmp_path.resolve())
    assert seen[0].task_id == result.task_id
    assert seen[0].subtask_id == "s1"
    assert seen[0].completion_summary == "summary\n"

    item = runtime.store.get_plan_item(result.task_id, "s1")
    assert item is not None
    assert item["status"] == "completed"
    assert runtime.store.list_plans(result.task_id)[0]["status"] == "completed"
    runs = runtime.store.list_verifier_runs(result.task_id)
    assert len(runs) == 1
    assert runs[0]["status"] == "pass"
    assert runs[0]["authoritative"] is True
    assert runs[0]["evidence_manifest"] == [{"path": "artifact.txt", "sha256": ARTIFACT_SHA256}]

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
                    {"path": "z.txt", "sha256": EVIDENCE_Z_SHA256},
                    {"path": "a.txt", "sha256": EVIDENCE_A_SHA256},
                ],
            ),
        ),
    )

    result = runtime.run("Create the artifacts")

    assert result.status == "completed"
    assert runtime.store.list_verifier_runs(result.task_id)[0]["evidence_manifest"] == [
        {"path": "a.txt", "sha256": EVIDENCE_A_SHA256},
        {"path": "z.txt", "sha256": EVIDENCE_Z_SHA256},
    ]


def test_bundle_hash_covers_the_full_frozen_config_and_verifier_implementation():
    def verify_v1(_: VerifierContext) -> VerifierResult:
        return VerifierResult("pass", "ok", [])

    def verify_v2(_: VerifierContext) -> VerifierResult:
        return VerifierResult("pass", "changed", [])

    baseline = _config(verify_v1)
    changed_description = _config(verify_v1, description="A different artifact")
    changed_criteria = _config(verify_v1, completion_criteria="A different criterion")
    changed_verifier = _config(verify_v2)

    assert baseline.verifier_bundle_hash != changed_description.verifier_bundle_hash
    assert baseline.verifier_bundle_hash != changed_criteria.verifier_bundle_hash
    assert baseline.verifier_bundle_hash != changed_verifier.verifier_bundle_hash
    assert len(baseline.verifier_implementation_hash or "") == 64


def test_bundle_hash_is_stable_for_equivalent_callable_state():
    class Policy:
        def __init__(self, label: str):
            self.label = label

    def build_verifier(policy: Policy):
        def verify(_: VerifierContext) -> VerifierResult:
            return VerifierResult("pass", policy.label, [])

        return verify

    first = _config(build_verifier(Policy("same")))
    second = _config(build_verifier(Policy("same")))
    changed = _config(build_verifier(Policy("changed")))

    assert first.verifier_bundle_hash == second.verifier_bundle_hash
    assert first.verifier_bundle_hash != changed.verifier_bundle_hash


def test_bundle_hash_covers_callable_instance_state():
    class StatefulVerifier:
        def __init__(self, expected: str):
            self.expected = expected

        def __call__(self, _: VerifierContext) -> VerifierResult:
            return VerifierResult("pass", self.expected, [])

    first = _config(StatefulVerifier("same"))
    second = _config(StatefulVerifier("same"))
    changed = _config(StatefulVerifier("changed"))

    assert first.verifier_bundle_hash == second.verifier_bundle_hash
    assert first.verifier_bundle_hash != changed.verifier_bundle_hash


def test_bundle_hash_covers_partial_verifier_arguments():
    def verify_with_label(label: str, _: VerifierContext) -> VerifierResult:
        return VerifierResult("pass", label, [])

    first = _config(partial(verify_with_label, "same"))
    second = _config(partial(verify_with_label, "same"))
    changed = _config(partial(verify_with_label, "changed"))

    assert first.verifier_bundle_hash == second.verifier_bundle_hash
    assert first.verifier_bundle_hash != changed.verifier_bundle_hash


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
        return VerifierResult("pass", "ok", [{"path": "artifact.txt", "sha256": ARTIFACT_SHA256}])

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
        return VerifierResult("pass", "ok", [{"path": "artifact.txt", "sha256": ARTIFACT_SHA256}])

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
        event["type"] in {"verified_subtask_committed", "verified_subtask_checkpoint_created"}
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
        return VerifierResult("pass", "ok", [{"path": "artifact.txt", "sha256": ARTIFACT_SHA256}])

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
    assert repeated.final_text == "done\n"
    assert verifier_calls == [1]
    assert len(recovered.store.list_verifier_runs(task_id)) == 1
    assert len(recovered.store.list_semantic_checkpoints(task_id)) == 1


def test_trace_exposes_verified_subtask_checkpoints(tmp_path: Path):
    config = _config(
        lambda _: VerifierResult(
            "pass", "ok", [{"path": "artifact.txt", "sha256": ARTIFACT_SHA256}]
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
    assert summary["verified_subtask_checkpoint_count"] == 1
    assert "semantic_checkpoint_count" not in summary
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
        "verified_subtask_checkpoint",
    }
    assert "semantic_checkpoint" not in {record["record_type"] for record in records}
    checkpoint_record = next(
        record for record in records
        if record["record_type"] == "verified_subtask_checkpoint"
    )
    assert "verified_subtask_checkpoint_id" in checkpoint_record
    assert "semantic_checkpoint_id" not in checkpoint_record


def test_v10_business_tables_without_migration_history_fail_closed(tmp_path: Path):
    database = tmp_path / "runtime.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE verifier_runs(verifier_run_id TEXT PRIMARY KEY)"
        )

    with pytest.raises(MigrationValidationError):
        SchemaManager(database).inspect()


def test_resume_allows_append_only_verified_history(tmp_path: Path):
    verifier_calls: list[int] = []

    def verify(_: VerifierContext) -> VerifierResult:
        verifier_calls.append(1)
        return VerifierResult("pass", "ok", [{"path": "artifact.txt", "sha256": ARTIFACT_SHA256}])

    config = _config(verify)
    first = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(text="done\nSUBTASK_COMPLETE")]),
        verified_subtask=config,
    )
    completed = first.run("Create the artifact")
    run_id = first.store.list_verifier_runs(completed.task_id)[0]["verifier_run_id"]
    checkpoint_id = first.store.list_semantic_checkpoints(completed.task_id)[0]["semantic_checkpoint_id"]

    with sqlite3.connect(first.store.path) as connection:
        connection.execute(
            """
            INSERT INTO verifier_runs(
                verifier_run_id, task_id, plan_item_id, subtask_id, status, summary,
                completion_summary, verifier_id, verifier_version, verification_rule,
                verifier_bundle_hash, evidence_manifest_json, evidence_hash,
                authoritative, execution_checkpoint_id, created_at
            )
            SELECT ?, task_id, plan_item_id, subtask_id, status, summary,
                   completion_summary, verifier_id, verifier_version, verification_rule,
                   verifier_bundle_hash, evidence_manifest_json, evidence_hash,
                   authoritative, execution_checkpoint_id, created_at
            FROM verifier_runs WHERE verifier_run_id = ?
            """,
            ("verifier_history_2", run_id),
        )
        connection.execute(
            """
            INSERT INTO semantic_checkpoints(
                task_id, plan_item_id, subtask_id, verifier_run_id,
                execution_checkpoint_id, completion_summary, verifier_id,
                verifier_version, verification_rule, verifier_bundle_hash,
                evidence_manifest_json, evidence_hash, created_at
            )
            SELECT task_id, plan_item_id, subtask_id, ?, execution_checkpoint_id,
                   completion_summary, verifier_id, verifier_version, verification_rule,
                   verifier_bundle_hash, evidence_manifest_json, evidence_hash, created_at
            FROM semantic_checkpoints WHERE semantic_checkpoint_id = ?
            """,
            ("verifier_history_2", checkpoint_id),
        )

    recovered = Runtime(tmp_path, ScriptedModel([]), verified_subtask=config)
    result = recovered.resume(completed.task_id)

    assert result.status == "completed"
    assert verifier_calls == [1]
    assert len(recovered.store.list_verifier_runs(completed.task_id)) == 2
    assert len(recovered.store.list_semantic_checkpoints(completed.task_id)) == 2
    assert recovered.store.scan_invariants(completed.task_id) == []


def test_verified_resume_fails_closed_when_bundle_is_inconsistent(tmp_path: Path):
    config = _config(
        lambda _: VerifierResult(
            "pass", "ok", [{"path": "artifact.txt", "sha256": ARTIFACT_SHA256}]
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
        return VerifierResult("pass", "ok", [{"path": "artifact.txt", "sha256": ARTIFACT_SHA256}])

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
    assert result.final_text == "summary\n"
    assert seen == ["summary\n"]


def test_marker_with_tool_calls_fails_closed_without_executing_tools(tmp_path: Path):
    model = ScriptedModel([
        ModelResponse(
            text="done\nSUBTASK_COMPLETE",
            tool_calls=[ToolCall("write-1", "write_file", {
                "path": "should-not-exist.txt",
                "content": "must not be written",
            })],
        ),
    ])
    runtime = Runtime(
        tmp_path,
        model,
        verified_subtask=_config(
            lambda _: VerifierResult("pass", "ok", [{"path": "artifact.txt", "sha256": ARTIFACT_SHA256}])
        ),
    )

    result = runtime.run("Create the artifact")

    assert result.status == "failed"
    assert result.error is not None
    assert "completion marker" in result.error
    assert model.call_count == 1
    assert not (tmp_path / "should-not-exist.txt").exists()
    assert runtime.store.list_tool_calls(result.task_id) == []
    assert runtime.store.list_verifier_runs(result.task_id) == []
    assert runtime.store.list_semantic_checkpoints(result.task_id) == []


def test_recovered_marker_with_tool_calls_fails_closed_without_executing_tools(tmp_path: Path):
    def crash(point: str, **_: object) -> None:
        if point == "after_model_response_before_persist":
            raise InjectedCrash(point)

    model = ScriptedModel([
        ModelResponse(
            text="done\nSUBTASK_COMPLETE",
            tool_calls=[ToolCall("write-1", "write_file", {
                "path": "should-not-exist.txt",
                "content": "must not be written",
            })],
        ),
    ])
    config = _config(
        lambda _: VerifierResult("pass", "ok", [{"path": "artifact.txt", "sha256": ARTIFACT_SHA256}])
    )
    first = Runtime(tmp_path, model, verified_subtask=config, fault_injector=crash)

    with pytest.raises(InjectedCrash, match="after_model_response_before_persist"):
        first.run("Create the artifact")

    task_id = first.store.list_tasks()[0]["task_id"]
    recovered = Runtime(tmp_path, ScriptedModel([]), verified_subtask=config)
    result = recovered.resume(task_id)

    assert result.status == "failed"
    assert not (tmp_path / "should-not-exist.txt").exists()
    assert recovered.store.list_tool_calls(task_id) == []
    assert recovered.store.list_verifier_runs(task_id) == []


def test_duplicate_completion_markers_fail_closed_without_verification(tmp_path: Path):
    seen: list[VerifierContext] = []

    def verify(context: VerifierContext) -> VerifierResult:
        seen.append(context)
        return VerifierResult("pass", "ok", [{"path": "artifact.txt", "sha256": ARTIFACT_SHA256}])

    runtime = Runtime(
        tmp_path,
        ScriptedModel([
            ModelResponse(text="first\nSUBTASK_COMPLETE\nsecond\nSUBTASK_COMPLETE"),
        ]),
        verified_subtask=_config(verify),
    )

    result = runtime.run("Create the artifact")

    assert result.status == "failed"
    assert result.error == "completion marker must appear exactly once"
    assert seen == []
    assert runtime.store.list_verifier_runs(result.task_id) == []
    assert runtime.store.list_semantic_checkpoints(result.task_id) == []


def test_completion_summary_removes_only_the_marker_line():
    text = "  leading  \nSUBTASK_COMPLETE\ntrailing  \n"

    assert Runtime._completion_summary(text) == "  leading  \ntrailing  \n"


@pytest.mark.parametrize("failure", ["raises", "malformed_result"])
def test_verifier_failures_are_audited_as_uncertain_and_retryable(
    tmp_path: Path,
    failure: str,
):
    attempts = 0

    def verify(_: VerifierContext):
        nonlocal attempts
        attempts += 1
        if attempts == 1 and failure == "raises":
            raise RuntimeError("verifier backend unavailable")
        if attempts == 1:
            return {"status": "pass"}
        return VerifierResult("pass", "ok", [{"path": "artifact.txt", "sha256": ARTIFACT_SHA256}])

    model = ScriptedModel([
        ModelResponse(text="first attempt\nSUBTASK_COMPLETE"),
        ModelResponse(text="retry attempt\nSUBTASK_COMPLETE"),
    ])
    runtime = Runtime(tmp_path, model, verified_subtask=_config(verify))

    result = runtime.run("Create the artifact")

    assert result.status == "completed"
    runs = runtime.store.list_verifier_runs(result.task_id)
    assert runs[0]["status"] == "uncertain"
    assert runs[0]["authoritative"] is False
    assert "Verifier invocation failed" in runs[0]["summary"]
    assert runs[1]["status"] == "pass"
    assert runs[1]["authoritative"] is True


@pytest.mark.parametrize("digest", ["abc123", "A" * 64, "g" * 64])
def test_evidence_manifest_requires_lowercase_sha256(digest: str):
    with pytest.raises(ValueError, match="lowercase 64-hex SHA-256"):
        VerifierResult(
            "fail",
            "evidence is unavailable",
            [{"path": r".\evidence.txt", "sha256": digest}],
        )


def test_evidence_manifest_uses_normalized_repository_relative_paths():
    result = VerifierResult(
        "uncertain",
        "partial evidence",
        [{"path": r".\evidence.txt", "sha256": ARTIFACT_SHA256}],
    )

    assert result.evidence_manifest == [{
        "path": "evidence.txt",
        "sha256": ARTIFACT_SHA256,
    }]


def test_pass_requires_complete_configured_evidence_coverage(tmp_path: Path):
    outcomes = iter([
        VerifierResult(
            "pass",
            "reported pass",
            [{"path": "artifact.txt", "sha256": ARTIFACT_SHA256}],
        ),
        VerifierResult(
            "pass",
            "all evidence present",
            [
                {"path": "metadata.json", "sha256": EVIDENCE_Z_SHA256},
                {"path": "artifact.txt", "sha256": ARTIFACT_SHA256},
            ],
        ),
    ])

    def verify(_: VerifierContext) -> VerifierResult:
        return next(outcomes)

    runtime = Runtime(
        tmp_path,
        ScriptedModel([
            ModelResponse(text="first attempt\nSUBTASK_COMPLETE"),
            ModelResponse(text="retry attempt\nSUBTASK_COMPLETE"),
        ]),
        verified_subtask=_config(
            verify,
            ("artifact.txt", "metadata.json"),
        ),
    )

    result = runtime.run("Create the artifact and metadata")

    assert result.status == "completed"
    runs = runtime.store.list_verifier_runs(result.task_id)
    assert [(run["status"], run["authoritative"]) for run in runs] == [
        ("uncertain", False),
        ("pass", True),
    ]
    assert "metadata.json" in runs[0]["summary"]


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
        VerifierResult("pass", "artifact checks passed", [{"path": "artifact.txt", "sha256": ARTIFACT_SHA256}]),
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
        return VerifierResult("pass", "ok", [{"path": "artifact.txt", "sha256": ARTIFACT_SHA256}])

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
    assert resumed.final_text == "done\n"
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
        VerifierResult("pass", "ok", [{"path": "artifact.txt", "sha256": ARTIFACT_SHA256}]),
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
