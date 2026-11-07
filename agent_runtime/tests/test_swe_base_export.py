import io
from pathlib import Path
import struct
import tarfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_runtime import swe_base_export as exporter
from agent_runtime.benchmark_dataset import prepare
from agent_runtime.evaluation import _hash
from agent_runtime.tests.test_benchmark_dataset import fixture


def tar_bytes(commit):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w", format=tarfile.PAX_FORMAT, pax_headers={"comment": commit}) as tar:
        entry = tarfile.TarInfo("app.py")
        entry.size = 1
        tar.addfile(entry, io.BytesIO(b"x"))
    return data.getvalue()


class Socket:
    def __init__(self, data):
        self.data = [data[:5], data[5:32], data[32:], b""]
        self.closed = False
    def settimeout(self, timeout): pass
    def recv(self, maximum): return self.data.pop(0)
    def close(self): self.closed = True


def frame(data, channel=1):
    return bytes([channel, 0, 0, 0]) + struct.pack(">I", len(data)) + data


@pytest.mark.parametrize("truncated", [False, True])
def test_named_pipe_eof_preserves_frame_completeness(tmp_path, truncated):
    class PipeEnded(Exception):
        winerror = 109
    class Pipe(Socket):
        def recv(self, maximum):
            if len(self.data) == 1:
                raise PipeEnded("pipe ended")
            return super().recv(maximum)
    Pipe.__module__ = "docker.transport.npipesocket"
    content = tar_bytes("a" * 40)
    payload = frame(content)
    stream = Pipe(payload[:-1] if truncated else payload)
    if truncated:
        with pytest.raises(ValueError, match="incomplete"):
            exporter.receive(stream, tmp_path / "archive")
    else:
        assert exporter.receive(stream, tmp_path / "archive") == _hash(content)
    assert stream.closed


def setup(tmp_path):
    config, dataset = fixture(tmp_path)
    prepare(config, dataset)
    image = "sha256:" + "b" * 64
    client = Mock()
    client.api.timeout = 3
    client.info.return_value = {"ID": "engine", "OSType": "linux"}
    client.images.get.return_value.id = image
    source = Mock(id="source")
    def create(*args, **kwargs):
        source.attrs = {"Image": image, "Config": {"Labels": kwargs["labels"]},
                        "State": {"Running": False, "Paused": False}}
        return source
    client.containers.create.side_effect = create
    source.attach_socket.return_value = Socket(frame(tar_bytes("a" * 40)))
    source.wait.return_value = {"StatusCode": 0}
    class Missing(Exception):
        response = SimpleNamespace(status_code=404)
    client.containers.get.side_effect = Missing()
    args = (dataset, _hash((dataset / "manifest.json").read_bytes()), "repo__issue-1", image, tmp_path / "out")
    return args, client, source


def test_preflight_creates_no_client_source_or_output(tmp_path):
    args, client, source = setup(tmp_path)
    assert exporter.export(*args)["status"] == "PREFLIGHT_ONLY"
    client.info.assert_not_called()
    assert not args[-1].exists()


def test_export_binds_commit_and_removes_only_owned_exited_source(tmp_path):
    args, client, source = setup(tmp_path)
    result = exporter.export(*args, client=client, expected_engine_id="engine", execute=True)
    assert result["status"] == "EXPORTED_REMOVED", result
    assert result["archive_sha256"] == _hash(Path(result["archive_path"]).read_bytes())
    assert result["project_tests_executed"] == 0 and not result["manual_recovery_required"]
    source.remove.assert_called_once_with(force=False)
    assert client.containers.create.call_args.kwargs["network_mode"] == "none"
    assert client.containers.create.call_args.kwargs["entrypoint"] == []


@pytest.mark.parametrize("failure", ["engine", "create", "commit", "partial", "identity", "cleanup"])
def test_export_failures_do_not_retry_or_claim_ready(tmp_path, failure):
    args, client, source = setup(tmp_path)
    if failure == "engine": client.info.return_value["ID"] = "other"
    if failure == "create": client.containers.create.side_effect = TimeoutError("create ambiguous")
    if failure == "commit": source.attach_socket.return_value = Socket(frame(tar_bytes("c" * 40)))
    if failure == "partial": source.attach_socket.return_value = Socket(frame(tar_bytes("a" * 40))[:-1])
    if failure == "identity": source.reload.side_effect = lambda: source.attrs.update(Image="foreign")
    if failure == "cleanup": source.remove.side_effect = RuntimeError("remove ambiguous")
    result = exporter.export(*args, client=client, expected_engine_id="engine", execute=True)
    assert result["status"] == "BLOCKED" and result["error"]
    assert result["manual_recovery_required"] == (failure != "engine")
    assert client.containers.create.call_count <= 1
    if failure not in {"cleanup"}: source.remove.assert_not_called()


def test_transfer_is_bounded_and_stream_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(exporter, "LIMIT", 4)
    stream = Socket(frame(b"12345"))
    with pytest.raises(ValueError, match="size bound"):
        exporter.receive(stream, tmp_path / "part")
    assert stream.closed


@pytest.mark.parametrize("failure_stage", ["start", "transfer"])
@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_interrupted_export_preserves_receipt_and_propagates(tmp_path, failure_stage, interrupt):
    args, client, source = setup(tmp_path)
    if failure_stage == "start":
        source.start.side_effect = interrupt("operator interrupted")
    else:
        source.attach_socket.return_value.recv = Mock(side_effect=interrupt("operator interrupted"))
    with pytest.raises(interrupt):
        exporter.export(*args, client=client, expected_engine_id="engine", execute=True)
    import json
    receipt = json.loads((args[-1] / "result.json").read_text())
    assert receipt["status"] == "BLOCKED" and receipt["manual_recovery_required"]
    assert receipt["source_id"] == "source" and receipt["engine_id"] == "engine"
    assert interrupt.__name__ in receipt["error"]
    source.remove.assert_not_called()
    assert client.containers.create.call_count == 1
    if failure_stage == "transfer":
        assert source.attach_socket.return_value.closed


def test_socket_configuration_failure_still_closes_transport(tmp_path):
    stream = Socket(b"")
    stream.settimeout = Mock(side_effect=OSError("socket configuration failed"))
    with pytest.raises(OSError, match="configuration"):
        exporter.receive(stream, tmp_path / "part")
    assert stream.closed
    assert not (tmp_path / "part").exists()
