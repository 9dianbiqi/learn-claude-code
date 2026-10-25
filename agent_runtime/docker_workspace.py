"""Bounded, opt-in Docker backend for candidate verification (not SWE grading).

The controller supplies a trusted base archive and command probes. Solver Git
configuration and model-written reports are never used as verification authority.
Only explicitly owned, isolated containers and pre-existing image digests are used.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid

from .store import LeaseLost
from .docker_recovery import FreezeJournal
from .workspace_cleanup import workspace_cleanup_effect
from .workspace_verifier import WorkspaceSnapshot, ValidationEvidence, WorkspaceVerifier, WorkspaceCleanupError


QUIESCE_EXPORT = r'''
import io,json,os,pathlib,signal,stat,sys,tarfile,time
signal.alarm(8)
root=pathlib.Path(sys.argv[1]); limit=int(sys.argv[2]); own=os.getpid()
print(json.dumps({'export_pid':own}),flush=True)
stopped={}; deadline=time.monotonic()+5; stable=0
while stable<2:
    changed=False
    for entry in pathlib.Path('/proc').iterdir():
        # PID 1 is a controller-validated immutable sleep command, not a writer.
        # Namespace init ignores same-namespace SIGSTOP; freeze it via cgroup next.
        if not entry.name.isdigit() or int(entry.name) in (1,own): continue
        pid=int(entry.name)
        try:
            text=(entry/'stat').read_text(); fields=text[text.rfind(')')+2:].split()
            state,start=fields[0],fields[19]
            if state=='Z': continue
            if pid not in stopped:
                if state in ('T','t'): raise RuntimeError('Pre-stopped process requires explicit recovery')
                os.kill(pid,signal.SIGSTOP); stopped[pid]=start; changed=True
            elif stopped[pid]!=start: raise RuntimeError('Process identity changed')
            elif state not in ('T','t'): changed=True
        except ProcessLookupError: pass
        except FileNotFoundError: pass
    stable=0 if changed else stable+1
    if time.monotonic()>deadline: raise RuntimeError('Cannot quiesce workspace writers')
    time.sleep(.02)
paths=[]; total=0
for path in sorted(root.rglob('*')):
    relative=path.relative_to(root)
    if relative.parts[0]=='.git': continue
    info=path.lstat()
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)) or info.st_nlink>1 and path.is_file():
        raise RuntimeError('Unsupported link or special file')
    total+=info.st_size if path.is_file() else 0
    if total>limit or len(paths)>50000: raise RuntimeError('Export size/member bound')
    paths.append(path)
print(json.dumps({'stopped':[{'pid':p,'start':s} for p,s in stopped.items()],
                  'files':sum(p.is_file() for p in paths)}),flush=True)
with tarfile.open(fileobj=sys.stdout.buffer,mode='w|') as archive:
    for path in paths:
        archive.add(str(path),arcname='workspace/'+str(path.relative_to(root)),recursive=False)
'''


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load_tmpfs_archive(client, container_id, destination, archive, check=lambda: None):
    """Bounded stdin transfer through an owned container exec, without host Shell.

    Docker put_archive rejects readonly-root containers even at tmpfs mounts.
    This receiver remains UID 65534 and can write only the writable tmpfs paths.
    Callers pass controller-built, traversal/link-checked archives, not solver tar.
    """
    if destination not in {"/workspace", "/tmp"} or len(archive) > 128*1024*1024:
        raise ValueError("Invalid archive destination/size")
    check()
    exec_id = client.api.exec_create(container_id, cmd=["/bin/tar", "--no-same-owner",
        "--no-same-permissions", "-xf", "-", "-C", destination], stdin=True,
        stdout=True, stderr=True, user="65534:65534")["Id"]
    stream = client.api.exec_start(exec_id, socket=True)
    connection = getattr(stream, "_sock", stream)
    connection.settimeout(2)
    deadline = time.monotonic()+10
    received = bytearray()
    try:
        for offset in range(0, len(archive), 65536):
            check()
            if time.monotonic() > deadline:
                raise TimeoutError("Archive upload deadline")
            connection.sendall(archive[offset:offset+65536])
        connection.shutdown(socket.SHUT_WR)
        while True:
            check()
            if time.monotonic() > deadline:
                raise TimeoutError("Archive receiver deadline")
            data = connection.recv(8192)
            if not data:
                break
            received.extend(data)
            if len(received) > 65536:
                raise RuntimeError("Archive receiver output limit")
    finally:
        stream.close()
    while True:
        check()
        state = client.api.exec_inspect(exec_id)
        if not state["Running"]:
            if state["ExitCode"] != 0:
                raise RuntimeError("Archive receiver exited nonzero: "+received[-512:].decode(errors="replace"))
            return exec_id
        if time.monotonic() > deadline:
            raise TimeoutError("Archive receiver did not stop")
        time.sleep(.03)


@dataclass(frozen=True)
class CommandProbe:
    argv: tuple[str, ...]
    stdout_sha256: str
    timeout_seconds: float = 5

    def __post_init__(self):
        if (not self.argv or not all(isinstance(v, str) and v and "\0" not in v for v in self.argv)
                or len(self.stdout_sha256) != 64
                or any(c not in "0123456789abcdef" for c in self.stdout_sha256)
                or not 0 < self.timeout_seconds <= 60):
            raise ValueError("A frozen argv, output oracle and bounded timeout are required")


class DockerWorkspaceBackend:
    def __init__(self, *, client, source_id, session, image, workspace_path, instance_id,
                 base_commit, base_archive, base_archive_sha256, probes, output,
                 heartbeat=None, heartbeat_interval=0.2, max_bytes=128*1024*1024, scope_timeout_seconds=30,
                 lease_identity=None, recovery_journal=None, fault_injector=None):
        if not image.startswith("sha256:") or len(image) != 71:
            raise ValueError("Immutable local image digest required")
        if (not workspace_path.startswith("/") or ".." in PurePosixPath(workspace_path).parts
                or workspace_path == "/" or not 0 < heartbeat_interval <= 1
                or not 1024 <= max_bytes <= 512*1024*1024 or not 0 < scope_timeout_seconds <= 300):
            raise ValueError("Invalid workspace or bounds")
        if client.api.timeout is None or client.api.timeout > 5:
            raise ValueError("Use a dedicated Docker client with HTTP timeout <=5 seconds")
        self.client, self.source_id, self.session, self.image = client, source_id, session, image
        self.engine_id = client.info()["ID"] if hasattr(client, "info") else None
        self.path, self.instance_id, self.base_commit = workspace_path, instance_id, base_commit
        self.probes = tuple(probes)
        if not self.probes or not all(isinstance(p, CommandProbe) for p in self.probes):
            raise ValueError("At least one trusted command probe required")
        self.base_archive = Path(base_archive).read_bytes()
        if digest(self.base_archive) != base_archive_sha256:
            raise ValueError("Trusted base archive hash mismatch")
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=False)
        self.heartbeat, self.interval, self.max_bytes = heartbeat, heartbeat_interval, max_bytes
        self.scope_timeout = scope_timeout_seconds
        self._held = False
        self._heartbeat_error = None
        self._cache = {}
        self._sequence = 0
        self._validation_containers = set()
        self._scope_archive = None
        self._export_expected_files = None
        self._stopped = []
        self._lock = threading.Lock()
        self.lease_identity = lease_identity
        self.recovery_journal = Path(recovery_journal) if recovery_journal else None
        self.fault_injector = fault_injector or (lambda point: None)
        self._journal = None
        if self.recovery_journal and not lease_identity:
            raise ValueError("Explicit recovery requires a fenced Runtime authority callback")
        rule = {"image": image, "base_commit": base_commit, "base_archive_sha256": base_archive_sha256,
                "probes": [asdict(p) for p in self.probes], "backend_version": "2",
                "export_program_sha256": digest(QUIESCE_EXPORT.encode()),
                "scope_timeout_seconds": scope_timeout_seconds, "max_archive_bytes": max_bytes,
                "docker_http_timeout": client.api.timeout, "heartbeat_interval": heartbeat_interval}
        self.validator_id = "docker-command-probes-v1:"+digest(json.dumps(rule, sort_keys=True).encode())
        self._record("configuration", {**rule, "source_id": source_id, "session": session,
                                      "workspace_path": workspace_path})
        self._assert_source()

    def _new_journal(self, path):
        return FreezeJournal(path, {"source_id": self.source_id, "session": self.session,
            "validator_id": self.validator_id, "workspace_path": self.path,
            "instance_id": self.instance_id, "engine_id": self.client.info()["ID"]}, self.lease_identity)

    def _cleanup_orphan_validators(self):
        # List by two controller-owned labels as a crash can occur after Docker
        # create but before its ID is journaled. Never adopt or delete the source.
        labels = ["org.lcc.validation-session="+self.session,
                  "org.lcc.validation-source="+self.source_id]
        for box in self.client.containers.list(all=True, filters={"label": labels}):
            self._check()
            self.lease_identity()
            box.reload()
            config = box.attrs["Config"]
            if (box.attrs["Image"] != self.image
                    or config.get("Labels", {}).get("org.lcc.validation-source") != self.source_id
                    or config.get("Labels", {}).get("org.lcc.validation-session") != self.session):
                raise RuntimeError("Orphan validator identity mismatch")
            box.remove(force=True)
            self._record("orphan-validator-cleanup", {"container_id": box.id, "removed": True})

    def _record(self, kind, value):
        with self._lock:
            self._sequence += 1
            path = self.output/f"{self._sequence:03d}-{kind}.json"
            path.write_text(json.dumps(value, indent=2, default=str)+"\n", encoding="utf-8")

    def _assert_source(self):
        source = self.client.containers.get(self.source_id)
        source.reload()
        item = source.attrs
        host, config = item["HostConfig"], item["Config"]
        command = config.get("Cmd") or []
        idle = (not config.get("Entrypoint") and len(command) == 2 and command[0] == "sleep"
                and command[1].isdigit() and 1 <= int(command[1]) <= 3600)
        if not (item["Id"] == self.source_id and item["Image"] == self.image
                and idle
                and item["State"]["Running"] and config["User"] == "65534:65534"
                and config.get("Labels", {}).get("org.lcc.backend-session") == self.session
                and host["NetworkMode"] == "none" and host["ReadonlyRootfs"]
                and host.get("PidMode", "") in {"", "private"}
                and not host["Privileged"] and not host.get("Binds") and not item["Mounts"]
                and host.get("CapDrop") == ["ALL"]
                and "no-new-privileges:true" in host.get("SecurityOpt", [])
                and not any("API_KEY=" in e or "AUTH_TOKEN=" in e for e in config.get("Env", []))):
            raise RuntimeError("Source container identity/isolation mismatch")
        return source

    def _check(self):
        if self._heartbeat_error is not None:
            raise LeaseLost("Verification heartbeat failed; frozen source retained")
        if not self._held:
            raise RuntimeError("Workspace is not frozen")
        if time.monotonic() > self._deadline:
            raise TimeoutError("Workspace verification scope deadline exceeded")

    @contextmanager
    def freeze(self):
        source = self._assert_source()
        if source.attrs["State"]["Paused"] and not self.recovery_journal:
            raise RuntimeError("Already paused: explicit ownership recovery is required")
        if self.heartbeat:
            self.heartbeat()
        self._held, self._heartbeat_error = True, None
        self._deadline = time.monotonic()+self.scope_timeout
        stop = threading.Event()
        def keep_alive():
            while not stop.wait(self.interval):
                try:
                    if self.heartbeat:
                        self.heartbeat()
                except Exception as exc:
                    self._heartbeat_error = type(exc).__name__
                    return
        worker = threading.Thread(target=keep_alive, daemon=True)
        worker.start()
        paused = False
        self._stopped = []
        try:
            if self.recovery_journal:
                self._journal = self._new_journal(self.recovery_journal)
                self._scope_archive, self._stopped, self._export_expected_files, _ = self._journal.adopt(
                    paused=source.attrs["State"]["Paused"], max_bytes=self.max_bytes, digest=digest)
                paused = True
                self.recovery_journal = None
                self._record("recovery-adopted", self._journal.value)
                self._cleanup_orphan_validators()
            else:
                if self.lease_identity:
                    self._journal = self._new_journal(self.output/"freeze-journal.json")
                    self._journal.begin()
                self.fault_injector("export_started")
                self._scope_archive = self._quiesce_export(source)
                if self._journal:
                    self._journal.snapshot(self._scope_archive, digest(self._scope_archive),
                                           self._stopped, self._export_expected_files)
                self.fault_injector("export_durable")
                self._check()
                source.pause()
                paused = True
                if self._journal:
                    self._journal.update(state="paused")
                self.fault_injector("source_paused")
            self._record("freeze", {"source_id": self.source_id, "paused": True,
                         "owner_pid": os.getpid(), "stopped": self._stopped})
            yield
            if self._heartbeat_error is not None:
                raise LeaseLost("Verification lease lost")
        finally:
            primary_error = sys.exc_info()[1]
            try:
                if paused:
                    resource = {"kind": "docker_workspace", "source_id": self.source_id,
                                "image": self.image, "session": self.session, "engine_id": self.engine_id}
                    if self._journal and self._journal.value:
                        resource["engine_id"] = self._journal.binding["engine_id"]
                    with workspace_cleanup_effect(resource):
                        self._release_source(source, paused)
                else:
                    self._release_source(source, paused)
            except Exception as exc:
                if primary_error is None:
                    raise
                # Preserve e.g. LeaseLost/verification failure as the primary
                # exception. The independent cleanup report is still durable.
                if hasattr(primary_error, "add_note"):
                    primary_error.add_note(str(exc))
            finally:
                # Lease renewal covers unpause/SIGCONT too, not only validation.
                stop.set()
                worker.join(timeout=5)
                self._held = False

    @staticmethod
    def observe_removed(client, resource):
        """Trusted read-only probe; never treats daemon errors as source absence."""
        if not resource.get("engine_id") or client.info()["ID"] != resource["engine_id"]:
            raise ValueError("Exact Docker engine identity is required")
        try:
            client.containers.get(resource["source_id"])
        except Exception as exc:
            # Docker's typed NotFound is a 404 APIError. Other daemon/network
            # failures are uncertainty, not evidence that a resource was removed.
            if getattr(getattr(exc, "response", None), "status_code", None) != 404:
                raise
        else:
            raise RuntimeError("Workspace source still exists; cleanup remains blocked")
        return {"outcome": "removed", "resource": resource, "method": "docker-get-404",
                "engine_id": resource["engine_id"]}

    def _release_source(self, source, paused):
        report = {"source_id": self.source_id, "cleanup_status": "requires_review",
                  "stage": "ownership_check", "unpaused": False, "processes_resumed": False,
                  "observed_paused": None, "retained_paused_for_recovery": False,
                  "manual_recovery_required": True}
        error = None
        try:
            if self._heartbeat_error is not None:
                raise LeaseLost("Verification heartbeat failed before release")
            if self.heartbeat:
                self.heartbeat()
            if not paused:
                report["stage"] = "freeze_incomplete"
                return  # Never guess which processes a partial exporter stopped.
            report["stage"] = "before_unpause"
            if self._journal:
                self._journal.update(state="releasing", release_stage=report["stage"])
            self.fault_injector("release_before_unpause")
            if self.heartbeat:
                self.heartbeat()
            self._assert_source().unpause()
            report.update(unpaused=True, stage="after_unpause")
            if self._journal:
                self._journal.update(release_stage=report["stage"])
            self.fault_injector("release_after_unpause")
            if self._heartbeat_error is not None:
                raise LeaseLost("Verification heartbeat failed before SIGCONT")
            if self.heartbeat:
                self.heartbeat()
            report["stage"] = "before_thaw"
            self.fault_injector("release_before_thaw")
            self._thaw(source)
            report.update(processes_resumed=True, stage="after_thaw")
            if self._journal:
                self._journal.update(release_stage=report["stage"])
            self.fault_injector("release_after_thaw")
            if self.heartbeat:
                self.heartbeat()
            if self._journal:
                self._journal.update(state="released")
            report.update(cleanup_status="released", manual_recovery_required=False)
        except Exception as exc:
            error = exc
            report.update(error_type=type(exc).__name__, reason=str(exc)[:256])
            if self._journal and self._journal.value is not None:
                try:
                    self._journal.update(release_error=report)
                except Exception as journal_error:
                    report["journal_error_type"] = type(journal_error).__name__
        finally:
            try:
                source.reload()
                report["observed_paused"] = source.attrs["State"]["Paused"]
            except Exception as observation_error:
                report["observation_error_type"] = type(observation_error).__name__
            report["retained_paused_for_recovery"] = bool(
                report["observed_paused"] and self._heartbeat_error is not None)
            self._record("freeze-release", report)
        if error is not None:
            raise WorkspaceCleanupError(report) from error

    def _quiesce_export(self, source):
        self._record("export-start", {"source_id": self.source_id, "program_sha256": digest(QUIESCE_EXPORT.encode())})
        exec_id = self.client.api.exec_create(source.id, cmd=["/opt/miniconda3/envs/testbed/bin/python",
            "-I", "-c", QUIESCE_EXPORT, self.path, str(self.max_bytes)], user="65534:65534",
            stdout=True, stderr=True)["Id"]
        stream = self.client.api.exec_start(exec_id, socket=True)
        connection = getattr(stream, "_sock", stream)
        connection.settimeout(2)
        stdout, stderr, pending = bytearray(), bytearray(), bytearray()
        deadline = time.monotonic()+10
        try:
            while True:
                self._check()
                if time.monotonic()>deadline:
                    raise TimeoutError("Workspace export deadline")
                data = connection.recv(65536)
                if not data:
                    break
                pending.extend(data)
                while len(pending)>=8:
                    channel, length = pending[0], struct.unpack('>I',pending[4:8])[0]
                    if length>self.max_bytes+65536:
                        raise ValueError("Export frame exceeds bound")
                    if len(pending)<8+length:
                        break
                    target = stdout if channel==1 else stderr
                    target.extend(pending[8:8+length]); del pending[:8+length]
                    if len(stdout)+len(stderr)>self.max_bytes+65536:
                        raise ValueError("Export exceeds bound")
        except Exception as exc:
            self._record("export-error", {"source_id": source.id, "exec_id": exec_id,
                         "error_type": type(exc).__name__, "stdout_prefix": bytes(stdout[:512]).decode(errors="replace"),
                         "stderr": bytes(stderr[-512:]).decode(errors="replace")})
            raise
        finally:
            stream.close()
        state = self.client.api.exec_inspect(exec_id)
        if pending or state["Running"] or state["ExitCode"]!=0:
            raise RuntimeError("Quiescence/export failed: "+stderr[-512:].decode(errors="replace"))
        header, metadata, body = bytes(stdout).split(b'\n',2)
        info = json.loads(metadata)
        self._stopped, self._export_expected_files = info["stopped"], info["files"]
        self._record("export-complete", {"source_id": self.source_id, "exec_id": exec_id,
                     "header": json.loads(header), "files": self._export_expected_files,
                     "archive_sha256": digest(body), "archive_bytes": len(body)})
        return body

    def _thaw(self, source):
        code = ("import json,pathlib,os,signal; entries=json.loads("+repr(json.dumps(self._stopped))+"); "
                "\nfor item in entries:\n"
                " p=pathlib.Path('/proc')/str(item['pid'])/'stat'\n"
                " try:\n"
                "  text=p.read_text(); parts=text[text.rfind(')')+2:].split()\n"
                "  if parts[19]==item['start']: os.kill(item['pid'],signal.SIGCONT)\n"
                " except (FileNotFoundError,ProcessLookupError): pass\n")
        result = source.exec_run(["/opt/miniconda3/envs/testbed/bin/python", "-I", "-c", code], user="65534:65534")
        if result.exit_code:
            raise RuntimeError("Owned process thaw failed; explicit recovery required")

    def _extract(self, archive, destination, strip_root=False):
        total, count = 0, 0
        with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
            for entry in stream:
                self._check()
                count += 1
                parts = PurePosixPath(entry.name).parts
                if (entry.name.startswith("/") or ".." in parts or count > 50000
                        or any(c in entry.name for c in ("\\", "\0", "\r", "\n"))):
                    raise ValueError("Unsafe archive path or member count")
                parts = parts[1:] if strip_root else parts
                if not parts or parts[0] == ".git":
                    continue  # Never use solver Git objects/configuration on the host.
                if not (entry.isdir() or entry.isfile()):
                    raise ValueError("Symlinks, hardlinks and special files are unsupported")
                target = destination.joinpath(*parts)
                target.resolve().relative_to(destination.resolve())
                if entry.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                total += entry.size
                if total > self.max_bytes or entry.size < 0:
                    raise ValueError("Archive size limit")
                target.parent.mkdir(parents=True, exist_ok=True)
                handle = stream.extractfile(entry)
                with target.open("wb") as output:
                    shutil.copyfileobj(handle, output)
                target.chmod(0o755 if entry.mode & 0o111 else 0o644)

    def _git(self, args, cwd, data=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_CEILING_DIRECTORIES=str(cwd.parent))
        return subprocess.run(["git", *args], cwd=cwd, env=env, input=data,
                              capture_output=True, timeout=max(.1, min(15, self._deadline-time.monotonic())))

    def capture(self):
        self._check()
        source = self._assert_source()
        if not source.attrs["State"]["Paused"]:
            raise RuntimeError("Source unpaused during verification")
        body = self._scope_archive
        if body is None or len(body)>self.max_bytes:
            raise ValueError("Missing or oversized frozen workspace export")
        with tempfile.TemporaryDirectory(prefix="lcc-capture-") as temporary:
            root = Path(temporary)
            base, candidate = root/"a", root/"b"
            base.mkdir(); candidate.mkdir()
            self._extract(self.base_archive, base)
            self._extract(bytes(body), candidate, strip_root=True)
            manifest = [{"path": str(p.relative_to(candidate)), "sha256": digest(p.read_bytes()),
                         "executable": bool(p.stat().st_mode & 0o111)}
                        for p in sorted(candidate.rglob("*")) if p.is_file()]
            if self._export_expected_files is not None and len(manifest)!=self._export_expected_files:
                raise RuntimeError("Export member count does not match trusted capture")
            revision = digest(json.dumps(manifest, sort_keys=True).encode())
            if revision in self._cache:
                return self._cache[revision][0]
            diff = self._git(["diff", "--no-index", "--no-prefix", "--binary", "--no-renames",
                              "--no-ext-diff", "--no-textconv", "--", "a", "b"], root)
            if diff.returncode not in {0, 1} or len(diff.stdout) > 100000:
                raise RuntimeError("Patch generation failed or exceeded bound")
            patch = diff.stdout.decode("utf-8", errors="strict")
            checked = self._git(["apply", "--check", "--binary", "-"], base, patch.encode()) if patch else None
            applies = checked is not None and checked.returncode == 0
            clean = root/"clean"
            shutil.copytree(base, clean)
            if applies and self._git(["apply", "--binary", "-"], clean, patch.encode()).returncode:
                raise RuntimeError("Candidate reconstruction failed")
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w") as tar:
                for path in sorted(clean.rglob("*")):
                    member = tar.gettarinfo(str(path), str(path.relative_to(clean)))
                    member.uid = member.gid = 65534
                    member.uname = member.gname = ""
                    if path.is_file():
                        with path.open("rb") as handle:
                            tar.addfile(member, handle)
                    else:
                        tar.addfile(member)
            snapshot = WorkspaceSnapshot(self.source_id, self.instance_id, self.base_commit, revision, patch, applies)
            self._cache[revision] = (snapshot, archive.getvalue())
            (self.output/f"candidate-{revision}.patch").write_bytes(patch.encode())
            self._record("capture", {"source_id": self.source_id, "revision": revision,
                         "manifest": manifest, "patch_sha256": snapshot.patch_sha256, "patch_applies": applies})
            return snapshot

    def validate(self, snapshot):
        self._check()
        if snapshot.revision not in self._cache or self._cache[snapshot.revision][0] != snapshot:
            raise ValueError("Snapshot is not owned by this backend")
        completed = 0
        for probe in self.probes:
            self._check()
            record = self._run_probe(probe, self._cache[snapshot.revision][1])
            completed += 1
            if record["exit_code"] != 0 or record["stdout_sha256"] != probe.stdout_sha256:
                return ValidationEvidence(snapshot.revision, snapshot.patch_sha256, self.validator_id,
                                           record["exit_code"] or 1, completed)
        return ValidationEvidence(snapshot.revision, snapshot.patch_sha256, self.validator_id, 0, completed)

    def _run_probe(self, probe, archive):
        box = None
        stdout, stderr = bytearray(), bytearray()
        too_large = threading.Event()
        workers = []
        log_lock = threading.Lock()
        overall_started = time.monotonic()
        try:
            # Docker needs a running mount namespace for the tmpfs archive copy.
            # A separate fixed setup container command waits for a controller
            # marker, then execs the frozen argv without invoking a Shell.
            startup = ("import os,time,json; p='/tmp/validation-ready'; "
                       "\nwhile not os.path.exists(p): time.sleep(.02)\n"
                       "os.execvp("+repr(probe.argv[0])+","+repr(list(probe.argv))+")")
            box = self.client.containers.create(self.image,
                command=["/opt/miniconda3/envs/testbed/bin/python", "-I", "-c", startup],
                user="65534:65534", network_mode="none", read_only=True, cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"], mem_limit="256m", nano_cpus=1000000000,
                pids_limit=64, working_dir="/workspace", labels={"org.lcc.validation-session": self.session,
                    "org.lcc.validation-source": self.source_id},
                environment={"HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1"},
                tmpfs={"/workspace": "rw,nosuid,nodev,uid=65534,gid=65534,mode=0700,size=128m",
                       "/tmp": "rw,nosuid,nodev,uid=65534,gid=65534,mode=1777,size=16m"})
            self._validation_containers.add(box.id)
            if self._journal:
                self._journal.update(validation_ids=sorted(self._validation_containers))
            box.start()
            load_tmpfs_archive(self.client, box.id, "/workspace", archive, self._check)
            def read_logs(target, is_stdout):
                try:
                    for line in box.logs(stream=True, follow=True, stdout=is_stdout, stderr=not is_stdout):
                        with log_lock:
                            if len(stdout)+len(stderr)+len(line) > 65536:
                                too_large.set()
                                return
                            target.extend(line)
                except Exception:
                    too_large.set()
            for target, is_stdout in ((stdout, True), (stderr, False)):
                worker = threading.Thread(target=read_logs, args=(target, is_stdout), daemon=True)
                workers.append(worker)
                worker.start()
            ready = io.BytesIO()
            with tarfile.open(fileobj=ready, mode="w") as tar:
                member = tarfile.TarInfo("validation-ready"); member.size = 1; member.mode = 0o644
                tar.addfile(member, io.BytesIO(b"1"))
            started = time.monotonic()
            load_tmpfs_archive(self.client, box.id, "/tmp", ready.getvalue(), self._check)
            self.fault_injector("validation_started")
            while True:
                self._check()
                box.reload()
                if too_large.is_set() or time.monotonic()-started > probe.timeout_seconds:
                    raise TimeoutError("Validation time/output bound exceeded")
                if not box.attrs["State"]["Running"]:
                    break
                time.sleep(.05)
            for worker in workers:
                worker.join(timeout=2)
            if any(worker.is_alive() for worker in workers) or too_large.is_set():
                raise RuntimeError("Validation log capture incomplete")
            record = {"container_id": box.id, "argv": probe.argv, "exit_code": box.attrs["State"]["ExitCode"],
                      "stdout_sha256": digest(bytes(stdout)), "output": stdout.decode("utf-8", errors="replace"),
                      "stderr": stderr.decode("utf-8", errors="replace"),
                      "seconds": time.monotonic()-started, "inspection": box.attrs}
            self._record("validation", record)
            return record
        except Exception as exc:
            self._record("validation-error", {"container_id": box.id if box else None,
                         "error_type": type(exc).__name__, "reason": str(exc)[:256],
                         "seconds": time.monotonic()-overall_started})
            raise
        finally:
            primary_error = sys.exc_info()[1]
            try:
                self._cleanup_validator(box, workers)
            except Exception as exc:
                if primary_error is None:
                    raise
                if hasattr(primary_error, "add_note"):
                    primary_error.add_note(str(exc))

    def _cleanup_validator(self, box, workers):
        report = {"container_id": box.id if box else None, "removed": False,
                  "log_readers_finished": False, "manual_recovery_required": box is not None}
        recorded = False
        def finish_readers():
            nonlocal recorded
            for worker in workers:
                worker.join(timeout=2)
            report["log_readers_finished"] = not any(worker.is_alive() for worker in workers)
            report["manual_recovery_required"] = not (report["removed"] and report["log_readers_finished"])
            self._record("validation-cleanup", report)
            recorded = True
        if box is None:
            finish_readers()
            return
        resource = {"kind": "docker_validator", "source_id": box.id, "image": self.image,
                    "session": self.session, "engine_id": self.engine_id, "parent_source_id": self.source_id}
        try:
            with workspace_cleanup_effect(resource):
                try:
                    box.remove(force=True)
                    self._validation_containers.discard(box.id)
                    report["removed"] = True
                except Exception as exc:
                    report.update(error_type=type(exc).__name__, reason=str(exc)[:256])
                    raise
                finally:
                    finish_readers()
                if not report["log_readers_finished"]:
                    raise RuntimeError("Validation log readers did not stop")
        except Exception as exc:
            if not recorded:
                report.update(error_type=type(exc).__name__, reason=str(exc)[:256])
                finish_readers()
            raise WorkspaceCleanupError(report) from exc

    def verifier(self):
        return WorkspaceVerifier(goal="candidate_patch", workspace_id=self.source_id,
            instance_id=self.instance_id, base_commit=self.base_commit, validator_id=self.validator_id,
            freeze=self.freeze, capture=self.capture, validate=self.validate)
