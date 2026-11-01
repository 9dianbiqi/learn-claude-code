"""Owned, bounded SWE workspace lifecycle using a preinstalled immutable image.

No image pull, dependency installation, provider call or official grading occurs.
The controller supplies a trusted base archive and frozen candidate probes.
"""
from contextlib import contextmanager
import io
import os
from pathlib import Path, PurePosixPath
import re
import tarfile
import uuid

from .benchmark_dataset import verify, _fresh
from .docker_workspace import DockerWorkspaceBackend, CommandProbe, load_tmpfs_archive
from .evaluation import _dump, _hash, _read, _json
from .eval_runner import _unsafe_reparse_chain


def checked_archive(path, expected_sha256):
    """Reject links, traversal, duplicate paths and oversized archives before upload."""
    path = Path(path)
    if _unsafe_reparse_chain(path) or not path.is_file():
        raise ValueError("base archive must be a regular file without reparse parents")
    limit = 128 * 1024 * 1024
    if path.stat().st_size > limit:
        raise ValueError("base archive exceeds size bound")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("base archive exceeds size bound")
    if _hash(data) != expected_sha256:
        raise ValueError("base archive digest mismatch")
    seen, total = set(), 0
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for item in archive:
            parts = PurePosixPath(item.name).parts
            name = "/".join(parts)
            if (not parts or item.name.startswith("/") or ".." in parts
                    or ".git" in parts or any(c in item.name for c in "\\\0\r\n")
                    or name in seen or not (item.isfile() or item.isdir())
                    or item.size < 0):
                raise ValueError("unsafe base archive member")
            seen.add(name)
            total += item.size
            if len(seen) > 50000 or total > 128 * 1024 * 1024:
                raise ValueError("base archive content exceeds bound")
    if not seen:
        raise ValueError("empty base archive")
    return data


@contextmanager
def provision(bundle, manifest_sha256, instance_id, *, client, image,
              base_archive, base_archive_sha256, probes, output, expected_engine_id=None):
    """Create/upload/bind one source; remove only this owned source on clean exit.

    On body/setup/cleanup failure retain the source for diagnosis/recovery, with
    its exact identity in lifecycle.json. Never replay an uncertain create.
    """
    bundle, output = Path(bundle).absolute(), Path(output).absolute()
    manifest = verify(bundle, manifest_sha256)
    rows = _json(_read(bundle / "solver/dataset.json"))
    matches = [r for r in rows if r["instance_id"] == instance_id]
    if instance_id not in manifest["instance_ids"] or len(matches) != 1:
        raise ValueError("unique pinned instance required")
    if not isinstance(image, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("immutable local image required")
    probes = tuple(probes)
    if not probes or not all(isinstance(p, CommandProbe) for p in probes):
        raise ValueError("trusted candidate probes required")
    if client.api.timeout is None or not 0 < client.api.timeout <= 5:
        raise ValueError("Docker HTTP timeout must be in (0, 5]")
    data = checked_archive(base_archive, base_archive_sha256)
    _fresh(output, [bundle, Path(base_archive).absolute().parent])
    output.mkdir(parents=True)
    # Bind backend capture to the exact bytes validated above, not a later read
    # of a caller-owned archive that may have changed during Docker startup.
    frozen_base = output / "base.tar"
    frozen_base.write_bytes(data)
    session = uuid.uuid4().hex
    state = {"schema_version": "swe-environment-v1", "instance_id": instance_id,
             "base_commit": matches[0]["base_commit"], "image": image,
             "dataset_manifest_sha256": manifest_sha256, "session": session,
             "source_id": None, "engine_id": None, "stage": "preflight",
             "removed": False, "manual_recovery_required": False,
             "official_resolved": None, "error": None}
    sequence = 0
    def record():
        nonlocal sequence
        sequence += 1
        event = output / f"lifecycle-{sequence:03d}.json"
        _dump(event, state)
        temporary = output / (".lifecycle-" + uuid.uuid4().hex)
        temporary.write_bytes(event.read_bytes())
        os.replace(temporary, output / "lifecycle.json")
    record()
    try:
        info = client.info()
        if info.get("OSType") != "linux" or not info.get("ID"):
            raise ValueError("identified Linux Docker engine required")
        if expected_engine_id is not None and info["ID"] != expected_engine_id:
            raise ValueError("Docker engine differs from the explicit execution pin")
        state["engine_id"] = info["ID"]
        if client.images.get(image).id != image:
            raise ValueError("local image identity mismatch")
        state.update(stage="creating", manual_recovery_required=True)
        record()  # A timed-out create may have succeeded; retain the session label.
        source = client.containers.create(image, command=["sleep", "3600"],
            entrypoint=[], user="65534:65534", network_mode="none", read_only=True,
            cap_drop=["ALL"], security_opt=["no-new-privileges:true"],
            mem_limit="256m", nano_cpus=1000000000, pids_limit=64,
            working_dir="/workspace", labels={"org.lcc.backend-session": session},
            environment={"HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1"},
            tmpfs={"/workspace": "rw,nosuid,nodev,uid=65534,gid=65534,mode=0700,size=128m",
                   "/tmp": "rw,nosuid,nodev,uid=65534,gid=65534,mode=1777,size=16m"})
        state.update(source_id=source.id, stage="starting")
        record()
        source.start()
        backend = DockerWorkspaceBackend(client=client, source_id=source.id, session=session,
            image=image, workspace_path="/workspace", instance_id=instance_id,
            base_commit=matches[0]["base_commit"], base_archive=frozen_base,
            base_archive_sha256=base_archive_sha256, probes=probes, output=output / "backend")
        state["stage"] = "uploading"
        record()
        load_tmpfs_archive(client, source.id, "/workspace", data)
        state["stage"] = "ready"
        record()
        yield backend
        state["stage"] = "cleanup"
        record()
        if client.info()["ID"] != state["engine_id"]:
            raise RuntimeError("Docker engine changed before cleanup")
        owned = backend._assert_source()
        if owned.attrs["State"].get("Paused"):
            raise RuntimeError("paused source requires explicit recovery")
        owned.remove(force=True)
        backend.observe_removed(client, {"engine_id": state["engine_id"], "source_id": source.id})
        state.update(stage="completed", removed=True, manual_recovery_required=False)
        record()
    except BaseException as exc:
        state["error"] = f"{type(exc).__name__}: {exc}"
        record()
        raise
