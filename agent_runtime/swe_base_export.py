"""Export a pinned Git base from an existing image, without project tests.

Default preflight. Explicit execution creates one isolated metadata-only source;
any uncertain outcome retains its identity for recovery, never automatic retry.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import tarfile
import time
import uuid

from .benchmark_dataset import verify, _fresh
from .docker_workspace import DockerWorkspaceBackend
from .evaluation import _dump, _json, _read
from .swe_environment import checked_archive
from .docker_transport import recv as transport_recv


EXPORT_PROGRAM = """import subprocess,sys
subprocess.run(['git','-c','safe.directory=/testbed','-C','/testbed',
                'archive','--format=tar',sys.argv[1]], check=True, timeout=25)
"""
LIMIT = 128 * 1024 * 1024


def receive(stream, destination):
    connection = getattr(stream, "_sock", stream)
    pending, stderr, count = bytearray(), bytearray(), 0
    digest = hashlib.sha256()
    deadline = time.monotonic() + 35
    try:
        connection.settimeout(2)
        with destination.open("xb") as output:
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError("base archive transfer deadline")
                chunk = transport_recv(connection, 65536)
                if not chunk:
                    break
                pending.extend(chunk)
                while len(pending) >= 8:
                    channel, length = pending[0], struct.unpack(">I", pending[4:8])[0]
                    if channel not in {1, 2} or pending[1:4] != b"\0\0\0" or length > 1024 * 1024:
                        raise ValueError("invalid archive transport frame")
                    if len(pending) < 8 + length:
                        break
                    data = bytes(pending[8:8 + length])
                    del pending[:8 + length]
                    if channel == 1:
                        count += len(data)
                        if count > LIMIT:
                            raise ValueError("base archive transfer exceeds size bound")
                        digest.update(data)
                        output.write(data)
                    else:
                        stderr.extend(data)
                        if len(stderr) > 8192:
                            raise ValueError("archive stderr exceeds bound")
        if pending or stderr or not count:
            raise ValueError("archive transfer incomplete or command reported stderr")
        return digest.hexdigest()
    finally:
        stream.close()


def export(bundle, manifest_sha256, instance_id, image, output, *, client=None,
           expected_engine_id=None, execute=False):
    bundle, output = Path(bundle).absolute(), Path(output).absolute()
    manifest = verify(bundle, manifest_sha256)
    rows = _json(_read(bundle / "solver/dataset.json"))
    selected = [r for r in rows if r["instance_id"] == instance_id]
    if manifest["instance_ids"] != [instance_id] or len(selected) != 1:
        raise ValueError("single pinned dataset instance required")
    if not isinstance(image, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("immutable local image required")
    _fresh(output, [bundle])
    base = selected[0]["base_commit"]
    command = ["/opt/miniconda3/envs/testbed/bin/python", "-I", "-c", EXPORT_PROGRAM, base]
    result = {"schema_version": "swe-base-export-v1", "status": "PREFLIGHT_ONLY",
              "instance_id": instance_id, "base_commit": base, "image": image,
              "dataset_manifest_sha256": manifest_sha256, "engine_id": None, "source_id": None,
              "session": uuid.uuid4().hex, "archive_path": None, "archive_sha256": None,
              "manual_recovery_required": False, "new_provider_requests": 0,
              "new_official_evaluations": 0, "project_tests_executed": 0, "error": None}
    if not execute:
        return result
    if (client is None or not isinstance(expected_engine_id, str)
            or not re.fullmatch(r"[A-Za-z0-9:_.-]{1,128}", expected_engine_id)):
        raise ValueError("explicit client and engine pin required")
    if client.api.timeout is None or not 0 < client.api.timeout <= 5:
        raise ValueError("bounded Docker HTTP timeout required")
    output.mkdir(parents=True)
    _dump(output / "invocation.json", {**result, "argv": command, "expected_engine_id": expected_engine_id})
    sequence = 0
    def record(stage):
        nonlocal sequence
        sequence += 1
        _dump(output / f"event-{sequence:03d}.json", {**result, "stage": stage})
    try:
        info = client.info()
        if info.get("ID") != expected_engine_id or info.get("OSType") != "linux":
            raise ValueError("pinned Linux Docker engine required")
        result["engine_id"] = expected_engine_id
        if client.images.get(image).id != image:
            raise ValueError("local image identity mismatch")
        result["manual_recovery_required"] = True
        record("creating")
        source = client.containers.create(image, command=command, entrypoint=[], user="65534:65534",
            network_mode="none", read_only=True, cap_drop=["ALL"], security_opt=["no-new-privileges:true"],
            mem_limit="256m", nano_cpus=1000000000, pids_limit=64, working_dir="/testbed",
            labels={"org.lcc.base-export-session": result["session"]},
            environment={"HOME": "/tmp", "GIT_CONFIG_NOSYSTEM": "1", "PYTHONDONTWRITEBYTECODE": "1"},
            tmpfs={"/tmp": "rw,nosuid,nodev,uid=65534,gid=65534,mode=1777,size=16m"})
        result["source_id"] = source.id
        record("starting")
        source.start()
        stream = source.attach_socket(params={"stdout": True, "stderr": True, "stream": True, "logs": True})
        part = output / "base.part"
        archive_sha = receive(stream, part)
        status = source.wait(timeout=5)["StatusCode"]
        if type(status) is not int or status != 0:
            raise ValueError("base archive command failed")
        checked_archive(part, archive_sha)
        with tarfile.open(part) as archive:
            if archive.pax_headers.get("comment") != base:
                raise ValueError("Git archive commit marker differs from pinned base")
        archive = output / "base.tar"
        part.rename(archive)
        result.update(archive_path=str(archive), archive_sha256=archive_sha)
        record("exported")
        if client.info().get("ID") != expected_engine_id:
            raise ValueError("engine changed before export cleanup")
        source.reload()
        attrs = source.attrs
        if (attrs.get("Image") != image or attrs.get("Config", {}).get("Labels", {}).get("org.lcc.base-export-session") != result["session"]
                or attrs.get("State", {}).get("Running") is not False or attrs["State"].get("Paused")):
            raise ValueError("owned exited export source identity required")
        source.remove(force=False)
        DockerWorkspaceBackend.observe_removed(client, {"engine_id": expected_engine_id, "source_id": source.id})
        result.update(status="EXPORTED_REMOVED", manual_recovery_required=False)
        record("completed")
    except BaseException as exc:
        result.update(status="BLOCKED", error=f"{type(exc).__name__}: {exc}")
        record("blocked")
        if not isinstance(exc, Exception):
            # Preserve the exact source/session before propagating operator cancellation.
            # Hard process termination may still leave only the earlier event journal.
            _dump(output / "result.json", result)
            raise
    _dump(output / "result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("bundle", "manifest_sha256", "instance_id", "image", "output"):
        parser.add_argument(name)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--docker-host")
    parser.add_argument("--expected-engine-id")
    args = parser.parse_args()
    if args.execute and (not args.docker_host or not args.expected_engine_id):
        parser.error("execute requires explicit endpoint and engine pin")
    client = None
    params = {k: getattr(args, k) for k in ("bundle", "manifest_sha256", "instance_id", "image", "output")}
    try:
        result = export(**params)
        if args.execute:
            import docker
            client = docker.DockerClient(base_url=args.docker_host, timeout=3)
            result = export(**params, execute=True, client=client, expected_engine_id=args.expected_engine_id)
        print(json.dumps(result, indent=2))
        return 2 if result["status"] == "BLOCKED" else 0
    except Exception as exc:
        parser.exit(2, f"base export input/startup error: {type(exc).__name__}: {exc}\n")
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
