from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from agent_runtime.cli import build_parser, main
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, ToolCall
from agent_runtime.permissions import PermissionEngine
from agent_runtime.runtime import Runtime
from agent_runtime.tools import ToolExecutor


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        ".git/config",
        "keys/deploy.pem",
        "keys/signing.key",
        "credentials.json",
        "config/secrets.yaml",
    ],
)
def test_sensitive_paths_are_hard_denied_even_under_allow_policy(tmp_path: Path, path: str):
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        yaml.safe_dump({"rules": [{"id": "allow-all-files", "effect": "allow", "tools": ["*"]}]}),
        encoding="utf-8",
    )
    engine = PermissionEngine(tmp_path, policy)

    decision = engine.evaluate("read_file", {"path": path})

    assert decision.effect == "deny"
    assert decision.rule_id == "invariant.sensitive-path"


@pytest.mark.parametrize("command", ["type .env", "cat keys/deploy.pem", "type credentials.json"])
def test_shell_cannot_read_sensitive_material(tmp_path: Path, command: str):
    decision = PermissionEngine(tmp_path).evaluate("bash", {"command": command})
    assert decision.effect == "deny"
    assert decision.rule_id == "invariant.sensitive-path"
    with pytest.raises(ValueError, match="sensitive"):
        ToolExecutor(tmp_path).run_bash(command)


def test_broad_glob_filters_sensitive_files_and_targeted_glob_is_denied(tmp_path: Path):
    (tmp_path / "app.py").write_text("print('ok')", encoding="utf-8")
    (tmp_path / ".env").write_text("API_KEY=secret", encoding="utf-8")
    (tmp_path / "deploy.pem").write_text("secret", encoding="utf-8")

    output = ToolExecutor(tmp_path).run_glob("**/*")

    assert "app.py" in output
    assert ".env" not in output
    assert "deploy.pem" not in output
    decision = PermissionEngine(tmp_path).evaluate("glob", {"pattern": "**/*.pem"})
    assert decision.effect == "deny"
    assert decision.rule_id == "invariant.sensitive-path"


def test_sensitive_matching_does_not_block_ordinary_source_names(tmp_path: Path):
    source = tmp_path / "src" / "secrets_manager.py"
    source.parent.mkdir()
    source.write_text("class SecretsManager: pass", encoding="utf-8")
    (source.parent / "credentials_client.py").write_text("class CredentialsClient: pass", encoding="utf-8")
    engine = PermissionEngine(tmp_path)

    assert engine.evaluate("read_file", {"path": "src/secrets_manager.py"}).effect == "allow"
    assert engine.evaluate("read_file", {"path": "src/credentials_client.py"}).effect == "allow"
    shell_decision = engine.evaluate("bash", {"command": "rg credentials src"})
    assert shell_decision.effect != "deny"
    assert shell_decision.risk == "read_only"
    assert "secrets_manager.py" in ToolExecutor(tmp_path).run_glob("src/*.py")


def test_runtime_denies_sensitive_read_without_executing_tool(tmp_path: Path):
    (tmp_path / ".env").write_text("API_KEY=super-secret", encoding="utf-8")
    runtime = Runtime(
        tmp_path,
        ScriptedModel([
            ModelResponse(tool_calls=[ToolCall("secret-read", "read_file", {"path": ".env"})]),
            ModelResponse(text="The protected read was denied."),
        ]),
    )

    result = runtime.run("Read .env")
    call = runtime.store.get_tool_call(result.task_id, "secret-read")

    assert result.status == "completed"
    assert call["status"] == "denied"
    assert call["execution_attempts"] == 0
    assert "super-secret" not in json.dumps(runtime.store.list_events(result.task_id))


def test_cli_subcommands_have_scoped_required_arguments():
    parser = build_parser()
    trace = parser.parse_args(["trace", "task-1"])
    assert trace.task_id == "task-1"
    assert not hasattr(trace, "action")
    with pytest.raises(SystemExit):
        parser.parse_args(["resolve-call", "call-1"])
    with pytest.raises(SystemExit):
        parser.parse_args(["status"])


def _completed_runtime(repo: Path) -> tuple[Runtime, str]:
    runtime = Runtime(repo, ScriptedModel([ModelResponse(text="done")]))
    result = runtime.run("No tools needed")
    return runtime, result.task_id


def test_cli_list_show_events_trace_and_db_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    _, task_id = _completed_runtime(tmp_path)

    assert main(["list", "--repo", str(tmp_path)]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["task_id"] == task_id

    assert main(["show", task_id, "--repo", str(tmp_path)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["task"]["status"] == "completed"
    assert shown["checkpoint"]["phase"] == "completed"

    assert main(["events", task_id, "--repo", str(tmp_path), "--limit", "2"]) == 0
    events = json.loads(capsys.readouterr().out)
    assert len(events) == 2
    assert events[-1]["type"] == "checkpoint_saved"

    assert main(["trace", task_id, "--repo", str(tmp_path)]) == 0
    trace = json.loads(capsys.readouterr().out)
    assert trace["status"] == "completed"

    assert main(["db-check", "--repo", str(tmp_path)]) == 0
    database = json.loads(capsys.readouterr().out)
    assert database["ok"] is True
    assert database["task_count"] == 1


def test_cli_pending_includes_operator_next_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    runtime = Runtime(
        tmp_path,
        ScriptedModel([ModelResponse(tool_calls=[ToolCall(
            "write-pending", "write_file", {"path": "generated.txt", "content": "value"}
        )])]),
    )
    result = runtime.run("Create generated.txt")
    assert result.status == "waiting_approval"

    assert main(["pending", "--repo", str(tmp_path)]) == 0
    pending = json.loads(capsys.readouterr().out)
    assert pending[0]["tool_use_id"] == "write-pending"
    assert pending[0]["status"] == "waiting_approval"
    assert "approve write-pending" in pending[0]["next_commands"][0]


def test_cli_doctor_reports_healthy_environment_without_creating_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MODEL_ID", "test-model")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("AGENT_RUNTIME_MODEL_TIMEOUT_SECONDS", "120")

    assert main(["doctor", "--repo", str(tmp_path), "--minimum-free-mb", "0"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["ok"] is True
    assert report["version"] == "0.1.1"
    assert not (tmp_path / ".agent_runtime" / "runtime.db").exists()
    checks = {item["name"]: item for item in report["checks"]}
    assert checks["repository"]["status"] == "pass"
    assert checks["policy"]["status"] == "pass"
    assert checks["database"]["status"] == "warn"


def test_cli_doctor_rejects_negative_disk_threshold(tmp_path: Path):
    with pytest.raises(SystemExit, match="non-negative"):
        main(["doctor", "--repo", str(tmp_path), "--minimum-free-mb", "-1"])
