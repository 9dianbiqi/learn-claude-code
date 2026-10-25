from agent_runtime.experiments.bc_pilot import clone_snapshot
from agent_runtime.tools import ToolExecutor


def test_snapshot_preserves_observed_file_identity_but_isolates_atomic_writes(tmp_path):
    source = tmp_path / "snapshot"
    source.mkdir()
    (source/"target.py").write_text("before", encoding="utf-8")
    (source/".agent_runtime").mkdir()
    (source/".agent_runtime/runtime.db").write_bytes(b"private database")
    workspace = tmp_path/"workspace"
    original = (source/"target.py").stat()
    clone_snapshot(source, workspace)
    cloned = (workspace/"target.py").stat()
    assert (original.st_dev, original.st_ino) == (cloned.st_dev, cloned.st_ino)
    assert (source/".agent_runtime/runtime.db").stat().st_ino != (workspace/".agent_runtime/runtime.db").stat().st_ino
    ToolExecutor(workspace).run_write_file("target.py", "after")
    assert (source/"target.py").read_text() == "before"
    assert (workspace/"target.py").read_text() == "after"
