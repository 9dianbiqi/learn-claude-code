"""Incremental journal/authority fault tests; no daemon or model network."""
import json
import os

import pytest

from agent_runtime.docker_recovery import FreezeJournal, durable_write
from agent_runtime.docker_workspace import digest


def frozen(tmp_path, monkeypatch):
    import agent_runtime.docker_recovery as module
    monkeypatch.setattr(module, "process_identity", lambda pid: {"pid": pid, "boot": "boot", "start": "1", "state": "S"})
    monkeypatch.setattr(module, "same_process_alive", lambda identity: False)
    lease = {"repo_root": "/controller", "task_id": "task", "owner_id": "old", "fencing_token": 1}
    journal = FreezeJournal(tmp_path/"freeze-journal.json", {"source": "source"}, lambda: dict(lease))
    journal.begin()
    journal.snapshot(b"snapshot", digest(b"snapshot"), [{"pid": 3, "start": "8"}], 1)
    journal.update(state="paused")
    lease.update(owner_id="new", fencing_token=2)
    return journal, lease


def test_adoption_persists_new_fenced_owner_before_returning_snapshot(tmp_path, monkeypatch):
    journal, lease = frozen(tmp_path, monkeypatch)
    archive, stopped, files, _ = journal.adopt(paused=True, max_bytes=1024, digest=digest)
    saved = json.loads(journal.path.read_text())
    assert archive == b"snapshot" and files == 1 and stopped[0]["pid"] == 3
    assert saved["state"] == "adopted" and saved["lease"] == lease
    with pytest.raises(RuntimeError, match="new fenced owner"):
        journal.adopt(paused=True, max_bytes=1024, digest=digest)


@pytest.mark.parametrize("change", ["same_owner", "old_fence", "wrong_task", "wrong_repo", "alive",
                                     "resumed", "partial", "wrong_source", "released", "tamper", "oversize"])
def test_unproven_recovery_is_rejected_without_claiming_or_thawing(tmp_path, monkeypatch, change):
    journal, lease = frozen(tmp_path, monkeypatch)
    paused, bound = True, 1024
    if change == "same_owner": lease["owner_id"] = "old"
    if change == "old_fence": lease["fencing_token"] = 1
    if change == "wrong_task": lease["task_id"] = "other"
    if change == "wrong_repo": lease["repo_root"] = "/other"
    if change == "alive": monkeypatch.setattr("agent_runtime.docker_recovery.same_process_alive", lambda _: True)
    if change == "resumed": paused = False
    if change == "partial": journal.update(state="exporting")
    if change == "wrong_source": journal.binding = {"source": "other"}
    if change == "released": journal.update(state="released")
    if change == "tamper": (tmp_path/"frozen-workspace.tar").write_bytes(b"tampered")
    if change == "oversize": bound = 2
    before = journal.path.read_bytes()
    with pytest.raises((RuntimeError, ValueError)):
        journal.adopt(paused=paused, max_bytes=bound, digest=digest)
    assert journal.path.read_bytes() == before


def test_lost_lease_during_claim_does_not_transfer_authority(tmp_path, monkeypatch):
    from agent_runtime.store import LeaseLost
    journal, lease = frozen(tmp_path, monkeypatch)
    calls = []
    def authority():
        calls.append(True)
        if len(calls) == 2: raise LeaseLost("new owner also expired")
        return dict(lease)
    journal.authority = authority
    before = journal.path.read_bytes()
    with pytest.raises(LeaseLost):
        journal.adopt(paused=True, max_bytes=1024, digest=digest)
    assert journal.path.read_bytes() == before


def test_replace_failure_preserves_old_durable_record(tmp_path, monkeypatch):
    path = tmp_path/"record"
    durable_write(path, b"old")
    monkeypatch.setattr(os, "replace", lambda *args: (_ for _ in ()).throw(OSError("injected replace failure")))
    with pytest.raises(OSError): durable_write(path, b"new")
    assert path.read_bytes() == b"old" and list(tmp_path.iterdir()) == [path]


def test_runtime_identity_requires_a_live_bound_lease(tmp_path):
    from agent_runtime import Runtime
    from agent_runtime.fake_model import ScriptedModel
    from agent_runtime.store import LeaseLost
    runtime = Runtime(tmp_path, ScriptedModel([]))
    with pytest.raises(LeaseLost): runtime.verification_lease_identity()
