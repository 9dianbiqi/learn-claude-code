"""Trusted-controller journal for explicit same-daemon, paused-source recovery.

This is crash recovery, not protection against an administrator editing the journal
or Docker. A partial export or a source that has already resumed is quarantined.
"""
import json
import os
from pathlib import Path
import uuid


def durable_write(path, data):
    path = Path(path)
    temporary = path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def process_identity(pid):
    # Recovery is deliberately Linux-only. Boot ID + process start ticks avoid
    # confusing a reused PID with the controller that originally froze the source.
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    try:
        value = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return None
    fields = value[value.rfind(")")+2:].split()
    return {"pid": pid, "boot": boot, "start": fields[19], "state": fields[0]}


def same_process_alive(identity):
    current = process_identity(identity["pid"])
    return (current is not None and current["state"] != "Z"
            and all(current[k] == identity[k] for k in ("pid", "boot", "start")))


class FreezeJournal:
    def __init__(self, path, binding, lease_identity):
        self.path, self.binding, self.authority = Path(path), binding, lease_identity
        self.value = None

    def begin(self):
        self.value = {"version": 1, "binding": self.binding,
                      "lease": self.authority(), "controller": process_identity(os.getpid()),
                      "state": "exporting", "validation_ids": []}
        self.update()

    def update(self, **values):
        self.value.update(values)
        durable_write(self.path, (json.dumps(self.value, sort_keys=True, indent=2)+"\n").encode())

    def snapshot(self, archive, sha256, stopped, files):
        durable_write(self.path.with_name("frozen-workspace.tar"), archive)
        self.update(state="exported", archive_sha256=sha256, archive_bytes=len(archive),
                    stopped=stopped, files=files)

    def adopt(self, *, paused, max_bytes, digest):
        lease = self.authority()  # Checks/renews the new Runtime lease, never a caller token string.
        value = json.loads(self.path.read_text())
        if (value.get("version") != 1 or value.get("binding") != self.binding
                or value.get("state") not in {"exported", "paused", "adopted"} or not paused):
            raise RuntimeError("Recovery journal/source is incomplete or mismatched; quarantine required")
        old = value["lease"]
        if (any(lease[k] != old[k] for k in ("repo_root", "task_id"))
                or lease["owner_id"] == old["owner_id"]
                or lease["fencing_token"] <= old["fencing_token"]):
            raise RuntimeError("Recovery requires a new fenced owner of the same task")
        if same_process_alive(value["controller"]):
            raise RuntimeError("Previous controller is still alive; quarantine required")
        path = self.path.with_name("frozen-workspace.tar")
        if path.stat().st_size > max_bytes:
            raise ValueError("Recovery snapshot exceeds bound")
        archive = path.read_bytes()
        if len(archive) != value["archive_bytes"] or digest(archive) != value["archive_sha256"]:
            raise ValueError("Recovery snapshot hash/length mismatch")
        self.authority()  # Refuse lost authority before claiming the persistent handoff.
        self.value = value
        self.update(state="adopted", lease=lease, controller=process_identity(os.getpid()))
        return archive, value["stopped"], value["files"], value["validation_ids"]
