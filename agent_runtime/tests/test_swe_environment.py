"""New lifecycle checks with simulated Docker; not real Docker acceptance."""
import io
import json
import tarfile
from unittest.mock import Mock

import pytest

from agent_runtime import swe_environment as env
from agent_runtime.benchmark_dataset import prepare
from agent_runtime.tests.test_benchmark_dataset import fixture


def archive(path, names):
    with tarfile.open(path, "w") as tar:
        for name, kind in names:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = 1 if kind == tarfile.REGTYPE else 0
            tar.addfile(member, io.BytesIO(b"x") if member.size else None)
    return env._hash(path.read_bytes())


def setup(tmp_path, monkeypatch):
    config, dataset = fixture(tmp_path)
    prepare(config, dataset)
    inputs = tmp_path / "base"
    inputs.mkdir()
    base = inputs / "base.tar"
    sha = archive(base, [("app.py", tarfile.REGTYPE)])
    client = Mock()
    client.api.timeout = 3
    client.info.return_value = {"ID": "engine", "OSType": "linux"}
    image = "sha256:" + "a" * 64
    client.images.get.return_value.id = image
    source = client.containers.create.return_value
    source.id = "source"
    source.attrs = {"State": {"Paused": False}}
    backend = Mock()
    backend._assert_source.return_value = source
    monkeypatch.setattr(env, "DockerWorkspaceBackend", Mock(return_value=backend))
    monkeypatch.setattr(env, "load_tmpfs_archive", Mock())
    kwargs = dict(client=client, image=image, base_archive=base, base_archive_sha256=sha,
                  probes=[env.CommandProbe(("python", "-V"), "0" * 64)], output=tmp_path / "out")
    args = (dataset, env._hash((dataset / "manifest.json").read_bytes()), "repo__issue-1")
    return args, kwargs, source, backend


def test_lifecycle_uploads_and_removes_exact_owned_source(tmp_path, monkeypatch):
    args, kwargs, source, backend = setup(tmp_path, monkeypatch)
    with env.provision(*args, **kwargs) as actual:
        assert actual is backend
        assert json.loads((kwargs["output"] / "lifecycle.json").read_text())["stage"] == "ready"
    source.remove.assert_called_once_with(force=True)
    backend.observe_removed.assert_called_once()
    env.load_tmpfs_archive.assert_called_once()
    assert env.DockerWorkspaceBackend.call_args.kwargs["scope_timeout_seconds"] == 180
    options = kwargs["client"].containers.create.call_args.kwargs
    assert options["network_mode"] == "none" and options["read_only"]
    assert options["entrypoint"] == [] and "volumes" not in options
    result = json.loads((kwargs["output"] / "lifecycle.json").read_text())
    assert result["removed"] and not result["manual_recovery_required"]
    assert result["official_resolved"] is None


@pytest.mark.parametrize("stage", ["create", "start", "upload", "body", "remove", "observe", "paused"])
def test_failures_preserve_identity_without_retry(tmp_path, monkeypatch, stage):
    args, kwargs, source, backend = setup(tmp_path, monkeypatch)
    operation = {"create": kwargs["client"].containers.create, "start": source.start,
                 "upload": env.load_tmpfs_archive, "remove": source.remove,
                 "observe": backend.observe_removed}.get(stage)
    if operation is not None:
        operation.side_effect = RuntimeError("injected failure")
    if stage == "paused":
        source.attrs["State"]["Paused"] = True
    with pytest.raises(RuntimeError):
        with env.provision(*args, **kwargs):
            if stage == "body":
                raise RuntimeError("body failure")
    result = json.loads((kwargs["output"] / "lifecycle.json").read_text())
    assert result["manual_recovery_required"] and not result["removed"]
    assert result["error"] and result["session"]
    assert kwargs["client"].containers.create.call_count == 1
    if stage not in {"remove", "observe"}:
        source.remove.assert_not_called()


@pytest.mark.parametrize("name,kind", [("../escape", tarfile.REGTYPE),
    ("/absolute", tarfile.REGTYPE), ("link", tarfile.SYMTYPE),
    ("hard", tarfile.LNKTYPE), (".git/config", tarfile.REGTYPE)])
def test_unsafe_archive_rejected_before_docker(tmp_path, name, kind):
    path = tmp_path / "bad.tar"
    sha = archive(path, [(name, kind)])
    with pytest.raises(ValueError, match="unsafe"):
        env.checked_archive(path, sha)


def test_unavailable_engine_does_not_create_container(tmp_path, monkeypatch):
    args, kwargs, source, backend = setup(tmp_path, monkeypatch)
    kwargs["client"].info.side_effect = RuntimeError("daemon unavailable")
    with pytest.raises(RuntimeError):
        with env.provision(*args, **kwargs):
            pytest.fail("must not yield")
    kwargs["client"].containers.create.assert_not_called()
    result = json.loads((kwargs["output"] / "lifecycle.json").read_text())
    assert not result["manual_recovery_required"]


def test_foreign_engine_blocks_cleanup(tmp_path, monkeypatch):
    args, kwargs, source, backend = setup(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="engine changed"):
        with env.provision(*args, **kwargs):
            kwargs["client"].info.return_value = {"ID": "other"}
    source.remove.assert_not_called()


def test_backend_uses_frozen_archive_when_input_changes(tmp_path, monkeypatch):
    args, kwargs, source, backend = setup(tmp_path, monkeypatch)
    original = kwargs["base_archive"].read_bytes()
    source.start.side_effect = lambda: kwargs["base_archive"].write_bytes(b"changed")
    with env.provision(*args, **kwargs):
        frozen = env.DockerWorkspaceBackend.call_args.kwargs["base_archive"]
        assert frozen.read_bytes() == original


def test_archive_above_generic_json_limit_is_supported(tmp_path):
    path = tmp_path / "large.tar"
    data = b"x" * (17 * 1024 * 1024)
    with tarfile.open(path, "w") as tar:
        member = tarfile.TarInfo("source.bin")
        member.size = len(data)
        tar.addfile(member, io.BytesIO(data))
    raw = path.read_bytes()
    assert env.checked_archive(path, env._hash(raw)) == raw


def test_oversized_archive_rejected_before_read(tmp_path):
    path = tmp_path / "oversized.tar"
    with path.open("wb") as stream:
        stream.truncate(128 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="size bound"):
        env.checked_archive(path, "0" * 64)


def test_execution_engine_pin_is_checked_before_image_or_create(tmp_path, monkeypatch):
    args, kwargs, source, backend = setup(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="execution pin"):
        with env.provision(*args, **kwargs, expected_engine_id="different"):
            pytest.fail("must not yield")
    kwargs["client"].images.get.assert_not_called()
    kwargs["client"].containers.create.assert_not_called()
