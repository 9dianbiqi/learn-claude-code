import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_runtime import swe_acceptance as acceptance
from agent_runtime import swe_prepare as preparation
from agent_runtime.tests.test_swe_acceptance import prepare


def config_file(tmp_path, monkeypatch):
    args, kwargs, calls, _ = prepare(tmp_path, monkeypatch)
    config = {k: v for k, v in kwargs.items() if k != "client"}
    config.update(bundle=str(args[0]), manifest_sha256=args[1], instance_id=args[2])
    config["base_archive"] = str(config["base_archive"])
    config["probes"] = [asdict(p) for p in config["probes"]]
    inputs = tmp_path / "config"
    inputs.mkdir()
    path = inputs / "acceptance.json"
    path.write_text(json.dumps(config))
    return path, args[-1], calls


def client():
    value = Mock()
    value.api.timeout = 3
    value.info.return_value = {"ID": "engine", "OSType": "linux"}
    value.images.get.return_value.id = "sha256:" + "a" * 64
    return value


def test_offline_plan_never_constructs_environment_or_output(tmp_path, monkeypatch):
    path, output, calls = config_file(tmp_path, monkeypatch)
    result = preparation.plan(path, output)
    assert result["status"] == "OFFLINE_READY_HOST_PENDING" and result["host"] is None
    assert result["containers_created"] == 0 and not output.exists() and not calls


def test_invalid_assets_do_not_even_inspect_host(tmp_path, monkeypatch):
    path, output, calls = config_file(tmp_path, monkeypatch)
    config = json.loads(path.read_text())
    config["base_archive_sha256"] = "0" * 64
    path.write_text(json.dumps(config))
    host = client()
    result = preparation.plan(path, output, client=host)
    assert result["status"] == "BLOCKED" and "digest mismatch" in result["checks"][0]["error"]
    host.info.assert_not_called()
    assert not output.exists() and not calls


@pytest.mark.parametrize("pin", [None, "engine"])
def test_host_check_only_inspects_engine_and_existing_image(pin):
    host = client()
    result = preparation.inspect_host(host, "sha256:" + "a" * 64, pin)
    assert result["engine_id"] == "engine"
    assert result["status"] == ("PINNED_IMAGE_PRESENT" if pin else "ENGINE_PIN_REQUIRED")
    host.containers.create.assert_not_called()
    host.images.pull.assert_not_called()
    assert not result["probes_executed"]


@pytest.mark.parametrize("failure", ["engine", "os", "image"])
def test_host_mismatch_cannot_be_ready(failure):
    host = client()
    if failure == "engine": host.info.return_value["ID"] = "other"
    if failure == "os": host.info.return_value["OSType"] = "windows"
    if failure == "image": host.images.get.return_value.id = "other"
    with pytest.raises(ValueError):
        preparation.inspect_host(host, "sha256:" + "a" * 64, "engine")
    host.containers.create.assert_not_called()
    if failure != "image": host.images.get.assert_not_called()


def test_pinned_host_plan_still_leaves_probe_and_official_gates_open(tmp_path, monkeypatch):
    path, output, calls = config_file(tmp_path, monkeypatch)
    result = preparation.plan(path, output, client=client(), expected_engine_id="engine")
    assert result["status"] == "PREPARED_ASSETS_AND_HOST"
    assert result["remaining"] and not result["host"]["probes_executed"]
    assert not calls and not output.exists()


def test_config_cannot_inject_execution_flags(tmp_path, monkeypatch):
    path, _, _ = config_file(tmp_path, monkeypatch)
    config = json.loads(path.read_text())
    config["execute"] = True
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="fields"):
        acceptance.load_config(path)


def test_execute_cli_requires_endpoint_and_engine_before_reading_config(monkeypatch):
    monkeypatch.setattr(acceptance.sys, "argv", ["swe_acceptance", "missing.json", "out", "--execute"])
    loader = Mock(side_effect=AssertionError("must not read"))
    monkeypatch.setattr(acceptance, "load_config", loader)
    with pytest.raises(SystemExit) as exc:
        acceptance.main()
    assert exc.value.code == 2
    loader.assert_not_called()


def test_cli_uses_explicit_sdk_endpoint_and_review_exit_status(monkeypatch):
    import sys
    factory = Mock(return_value=Mock())
    monkeypatch.setitem(sys.modules, "docker", SimpleNamespace(DockerClient=factory))
    monkeypatch.setattr(acceptance, "load_config", lambda *a: {})
    monkeypatch.setenv("DOCKER_HOST", "inherited://wrong-engine")
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    monkeypatch.setenv("DOCKER_CERT_PATH", "/wrong-certificates")
    responses = iter([{"status": "PREFLIGHT_ONLY"}, {"status": "REPORTED", "manual_recovery_required": True}])
    def response(*a, **kw):
        if kw.get("execute"):
            assert os.environ["DOCKER_HOST"] == "unix:///var/run/docker.sock"
            assert "DOCKER_TLS_VERIFY" not in os.environ and "DOCKER_CERT_PATH" not in os.environ
        return next(responses)
    run = Mock(side_effect=response)
    monkeypatch.setattr(acceptance, "run", run)
    monkeypatch.setattr(acceptance.sys, "argv", ["swe_acceptance", "config", "out", "--execute",
        "--docker-host", "unix:///var/run/docker.sock", "--expected-engine-id", "engine"])
    assert acceptance.main() == 2
    factory.assert_called_once_with(base_url="unix:///var/run/docker.sock", timeout=3)
    assert run.call_args.kwargs["expected_engine_id"] == "engine"
    factory.return_value.close.assert_called_once()
    assert os.environ["DOCKER_HOST"] == "inherited://wrong-engine"
    assert os.environ["DOCKER_TLS_VERIFY"] == "1"
    assert os.environ["DOCKER_CERT_PATH"] == "/wrong-certificates"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ordinary-user wrapper")
@pytest.mark.parametrize("mode,expected_exit", [("Preflight", 0), ("Execute", 2)])
def test_powershell_entry_preflights_from_another_directory_without_docker(tmp_path, monkeypatch, mode, expected_exit):
    path, output, calls = config_file(tmp_path, monkeypatch)
    shell = shutil.which("pwsh")
    assert shell, "PowerShell 7 is required for this Windows entry-point check"
    script = Path(__file__).parents[1] / "scripts/Invoke-SweAcceptance.ps1"
    process = subprocess.run([shell, "-NoProfile", "-File", str(script), "-Config", str(path),
                              "-Output", str(output), "-Python", sys.executable, "-Mode", mode], cwd=tmp_path,
        env={**os.environ, "DOCKER_HOST": "invalid://must-not-connect"},
        capture_output=True, text=True, timeout=60)
    assert process.returncode == expected_exit, process.stdout + process.stderr
    assert "OFFLINE_READY_HOST_PENDING" in process.stdout
    assert not output.exists() and not calls
