"""New controller-side archive and patch tests; Docker integration is separate."""
import io
from pathlib import Path
import tarfile
from types import SimpleNamespace

import pytest

from agent_runtime.docker_workspace import CommandProbe, DockerWorkspaceBackend, digest, load_tmpfs_archive


def archive(files, root=""):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as tar:
        for name, value in files.items():
            member = tarfile.TarInfo(root+name)
            member.size = len(value)
            member.mode = 0o644
            tar.addfile(member, io.BytesIO(value))
    return output.getvalue()


class FakeSource:
    id = "owned-source"
    def __init__(self, body):
        self.body = body
        self.attrs = {"Id": self.id, "Image": "sha256:"+"a"*64, "State": {"Running": True, "Paused": False},
            "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True, "Privileged": False,
                "Binds": None, "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"]},
            "Config": {"User": "65534:65534", "Labels": {"org.lcc.backend-session": "owned"}, "Env": [],
                       "Cmd": ["sleep", "180"], "Entrypoint": None}, "Mounts": []}
    def reload(self):
        pass
    def pause(self):
        self.attrs["State"]["Paused"] = True
    def unpause(self):
        self.attrs["State"]["Paused"] = False
    def get_archive(self, path):
        assert self.attrs["State"]["Paused"]
        return iter([self.body]), {}


def backend(tmp_path, files):
    base = archive({"math.py": b"answer = 3\n"})
    base_path = tmp_path/"base.tar"
    base_path.write_bytes(base)
    source = FakeSource(archive(files, "workspace/"))
    client = SimpleNamespace(api=SimpleNamespace(timeout=1), containers=SimpleNamespace(get=lambda _: source))
    worker = DockerWorkspaceBackend(client=client, source_id=source.id, session="owned",
        image=source.attrs["Image"], workspace_path="/workspace", instance_id="fixture",
        base_commit="base", base_archive=base_path, base_archive_sha256=digest(base),
        probes=[CommandProbe(("fixed-check",), digest(b"ok\n"))], output=tmp_path/"evidence")
    worker._quiesce_export = lambda source: source.body  # No Docker daemon in unit fixture.
    worker._thaw = lambda source: None
    return worker, source


def test_capture_patch_contains_new_files_and_reconstructs_clean_base(tmp_path):
    worker, source = backend(tmp_path, {"math.py": b"answer = 4\n", "new.py": b"added = True\n"})
    with worker.freeze():
        snapshot = worker.capture()
        assert snapshot.patch_applies and "+answer = 4" in snapshot.patch and "new.py" in snapshot.patch
        contents = worker._cache[snapshot.revision][1]
        with tarfile.open(fileobj=io.BytesIO(contents)) as tar:
            assert tar.extractfile("math.py").read() == b"answer = 4\n"
            assert tar.extractfile("new.py").read() == b"added = True\n"
        assert worker.capture() == snapshot
    assert not source.attrs["State"]["Paused"]


def test_capture_diff_skips_unchanged_files_but_reconstructs_them(tmp_path):
    worker, _ = backend(tmp_path, {"math.py": b"answer = 3\n", "new.py": b"new\n"})
    original = worker._git
    def git(args, cwd, data=None):
        if args[0] == 'diff':
            assert not (cwd/'a/math.py').exists()
            assert not (cwd/'b/math.py').exists()
            assert (cwd/'b/new.py').read_bytes() == b'new\n'
        return original(args, cwd, data)
    worker._git = git
    with worker.freeze():
        snapshot = worker.capture()
        assert snapshot.patch_applies
        with tarfile.open(fileobj=io.BytesIO(worker._cache[snapshot.revision][1])) as tar:
            assert tar.extractfile('math.py').read() == b'answer = 3\n'
            assert tar.extractfile('new.py').read() == b'new\n'


@pytest.mark.parametrize("files", [
    {"blob.bin": b"\x00\xff\x01"},
    {"math.py/child.py": b"nested = True\n"},
])
def test_sparse_diff_preserves_deletion_and_binary_or_type_change(tmp_path, files):
    worker, _ = backend(tmp_path, files)
    with worker.freeze():
        snapshot = worker.capture()
        assert snapshot.patch_applies
        with tarfile.open(fileobj=io.BytesIO(worker._cache[snapshot.revision][1])) as tar:
            actual = {item.name: tar.extractfile(item).read() for item in tar if item.isfile()}
            assert actual == files


def test_sparse_diff_preserves_directory_replaced_by_file(tmp_path):
    worker, _ = backend(tmp_path, {"package": b"now a file\n"})
    worker.base_archive = archive({"package/module.py": b"old\n"})
    with worker.freeze():
        snapshot = worker.capture()
        assert snapshot.patch_applies
        with tarfile.open(fileobj=io.BytesIO(worker._cache[snapshot.revision][1])) as tar:
            assert {m.name: tar.extractfile(m).read() for m in tar if m.isfile()} == {"package": b"now a file\n"}


@pytest.mark.skipif(__import__('os').name == 'nt', reason='POSIX executable mode semantics')
def test_sparse_diff_preserves_executable_only_change(tmp_path):
    worker, source = backend(tmp_path, {"math.py": b"answer = 3\n"})
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as tar:
        member = tarfile.TarInfo('workspace/math.py')
        member.size = len(b'answer = 3\n')
        member.mode = 0o755
        tar.addfile(member, io.BytesIO(b'answer = 3\n'))
    source.body = output.getvalue()
    with worker.freeze():
        snapshot = worker.capture()
        assert snapshot.patch_applies and 'new mode 100755' in snapshot.patch
        with tarfile.open(fileobj=io.BytesIO(worker._cache[snapshot.revision][1])) as tar:
            assert tar.getmember('math.py').mode & 0o111


def test_solver_git_metadata_is_ignored_not_executed(tmp_path):
    worker, _ = backend(tmp_path, {"math.py": b"answer = 4\n", ".git/config": b"malicious invalid config"})
    with worker.freeze():
        assert worker.capture().patch_applies


@pytest.mark.parametrize("name", ["workspace/../../escape", "/absolute/escape", "workspace/bad\\name"])
def test_unsafe_archive_paths_fail_closed_and_release_owned_pause(tmp_path, name):
    worker, source = backend(tmp_path, {})
    source.body = archive({name: b"bad"})
    with pytest.raises(ValueError):
        with worker.freeze():
            worker.capture()
    assert not source.attrs["State"]["Paused"]


def test_prepaused_source_is_not_silently_adopted(tmp_path):
    worker, source = backend(tmp_path, {})
    source.attrs["State"]["Paused"] = True
    with pytest.raises(RuntimeError, match="ownership recovery"):
        with worker.freeze():
            pass
    assert source.attrs["State"]["Paused"]


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_links_are_not_exported_as_regular_candidate_files(tmp_path, kind):
    worker, source = backend(tmp_path, {})
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as tar:
        member = tarfile.TarInfo("workspace/link")
        member.type, member.linkname = kind, "../../outside"
        tar.addfile(member)
    source.body = output.getvalue()
    with pytest.raises(ValueError, match="Symlinks"):
        with worker.freeze():
            worker.capture()


@pytest.mark.parametrize("change", ["pid_namespace", "pid1_writer", "credential"])
def test_wrong_source_process_or_credential_configuration_is_rejected(tmp_path, change):
    worker, source = backend(tmp_path, {})
    if change == "pid_namespace":
        source.attrs["HostConfig"]["PidMode"] = "host"
    elif change == "pid1_writer":
        source.attrs["Config"]["Cmd"] = ["python", "writer.py"]
    else:
        source.attrs["Config"]["Env"] = ["BIGMODEL_API_KEY=must-not-enter-source"]
    with pytest.raises(RuntimeError, match="identity/isolation"):
        with worker.freeze():
            pass


def test_archive_receiver_never_accepts_arbitrary_destination():
    with pytest.raises(ValueError):
        load_tmpfs_archive(None, "unknown", "/root", b"data")


def test_expired_scope_does_not_launch_git(tmp_path, monkeypatch):
    from unittest.mock import Mock
    worker, _ = backend(tmp_path, {})
    with worker.freeze():
        worker._deadline = 0
        run = Mock()
        monkeypatch.setattr('agent_runtime.docker_workspace.subprocess.run', run)
        with pytest.raises(TimeoutError):
            worker._git(['diff'], tmp_path)
        run.assert_not_called()


@pytest.mark.parametrize("exit_code", [0, 2, None])
def test_archive_upload_named_pipe_full_close_checks_exec_exit(exit_code):
    from unittest.mock import Mock
    class NamedPipe:
        _closed = False
        def settimeout(self, value): pass
        def sendall(self, data): assert not self._closed
        def shutdown(self, how): self._closed = True
        def recv(self, size):
            raise AssertionError("named pipe was fully closed by shutdown")
        def close(self): self._closed = True
    pipe = NamedPipe()
    api = Mock()
    api.exec_create.return_value = {"Id": "upload"}
    api.exec_start.return_value = pipe
    api.exec_inspect.return_value = {"Running": False, "ExitCode": exit_code}
    if exit_code == 0:
        assert load_tmpfs_archive(SimpleNamespace(api=api), "owned", "/workspace", b"tar") == "upload"
    else:
        with pytest.raises(RuntimeError, match="nonzero"):
            load_tmpfs_archive(SimpleNamespace(api=api), "owned", "/workspace", b"tar")
    api.exec_inspect.assert_called_once_with("upload")
    assert pipe._closed


def test_archive_upload_half_close_still_reads_output():
    from unittest.mock import Mock
    stream = Mock()
    stream._sock = stream
    stream._closed = False
    stream.recv.side_effect = [b"receiver failed", b""]
    api = Mock()
    api.exec_create.return_value = {"Id": "upload"}
    api.exec_start.return_value = stream
    api.exec_inspect.return_value = {"Running": False, "ExitCode": 2}
    with pytest.raises(RuntimeError, match="receiver failed"):
        load_tmpfs_archive(SimpleNamespace(api=api), "owned", "/workspace", b"tar")
    assert stream.recv.call_count == 2
    stream.close.assert_called_once()
