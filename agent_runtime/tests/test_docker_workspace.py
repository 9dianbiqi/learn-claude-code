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
