"""New real-process fail-stop tests for opt-in termination and advice recovery."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from agent_runtime import Runtime
from agent_runtime.completion import CompletionContract
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, VerifiedSubtaskConfig, VerifiedSubtaskDAGConfig, VerifierResult
from agent_runtime.recovery_advisor import RecoveryAdvisor


PROGRAM = r'''
import os,sys
from agent_runtime import Runtime
from agent_runtime.completion import CompletionContract
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse,ToolCall,VerifiedSubtaskConfig,VerifiedSubtaskDAGConfig,VerifierResult
from agent_runtime.recovery_advisor import RecoveryAdvisor
root,point,mode=sys.argv[1:]
def verifier(context):
    return VerifierResult('pass' if mode=='pass' else 'fail','fixture verification',[])
def crash(name,**context):
    if name==point: os._exit(73)
if mode=='advice':
    model=ScriptedModel([ModelResponse(tool_calls=[ToolCall('read','read_file',{'path':'missing.py'})])])
    Runtime(root,model,recovery_advisor=RecoveryAdvisor(),lease_ttl=1,fault_injector=crash).run('Read')
else:
    node=VerifiedSubtaskConfig('final','final','criteria',(),'verifier','1','rule',verifier,'b'*64,max_turns=3)
    Runtime(root,ScriptedModel([ModelResponse(text='model stopped without marker')]),
        verified_subtask_dag=VerifiedSubtaskDAGConfig([node]),completion_contract=CompletionContract('verified_task',1),
        lease_ttl=1,fault_injector=crash).run('Verify')
'''


def child(root, point, mode):
    process = subprocess.run([sys.executable, "-c", PROGRAM, str(root), point, mode],
        capture_output=True, text=True, timeout=25,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert process.returncode == 73, process.stderr
    database = root/".agent_runtime/runtime.db"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        expires = connection.execute("SELECT MAX(expires_at) FROM leases").fetchone()[0]
    while time.time() <= expires:
        time.sleep(0.03)
    return database


def resume_runtime(root, status):
    calls = []
    def verifier(context):
        calls.append(context.execution_checkpoint_id)
        return VerifierResult(status, "fixture verification", [])
    node = VerifiedSubtaskConfig("final", "final", "criteria", (), "verifier", "1", "rule",
                                  verifier, "b"*64, max_turns=3)
    runtime = Runtime(root, ScriptedModel([]), verified_subtask_dag=VerifiedSubtaskDAGConfig([node]),
                      completion_contract=CompletionContract("verified_task", 1))
    return runtime, calls


@pytest.mark.parametrize("point", ["verified_subtask_f2_pre", "verified_subtask_f2_mid", "verified_subtask_f3"])
def test_contract_success_authority_is_all_or_none_and_resume_never_bypasses_gate(tmp_path, point):
    database = child(tmp_path, point, "pass")
    expected = 1 if point == "verified_subtask_f3" else 0
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM semantic_checkpoints").fetchone()[0] == expected
        assert connection.execute("SELECT COUNT(*) FROM verifier_runs WHERE authoritative=1").fetchone()[0] == expected
        assert connection.execute("SELECT status FROM plan_items").fetchone()[0] == ("completed" if expected else "verifying")
    runtime, verifier_calls = resume_runtime(tmp_path, "pass")
    task = runtime.store.list_tasks()[0]["task_id"]
    assert runtime.resume(task).status == "completed"
    assert runtime.model.call_count == 0
    assert len(verifier_calls) == (0 if expected else 1)
    assert len(runtime.store.list_verified_subtask_checkpoints(task)) == 1
    runtime.store.assert_invariants(task)


@pytest.mark.parametrize("point", ["contract_failure_after_item", "contract_failure_after_checkpoint",
                                   "contract_failure_before_commit"])
def test_contract_failure_bundle_rolls_back_and_budget_survives_restart(tmp_path, point):
    database = child(tmp_path, point, "fail")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT status FROM tasks").fetchone()[0] == "running"
        assert connection.execute("SELECT status FROM plan_items").fetchone()[0] == "in_progress"
        assert connection.execute("SELECT COUNT(*) FROM checkpoints WHERE phase='failed'").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM verifier_runs WHERE authoritative=1").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM verifier_runs").fetchone()[0] == 1
    runtime, verifier_calls = resume_runtime(tmp_path, "fail")
    task = runtime.store.list_tasks()[0]["task_id"]
    stopped = runtime.resume(task)
    assert stopped.status == "failed" and json.loads(stopped.error)["code"] == "UNRESOLVED_STOP_LIMIT"
    assert not verifier_calls and runtime.model.call_count == 0
    assert runtime.store.get_plan_item(task, "final")["status"] == "failed"
    runtime.store.assert_invariants(task)


def test_advice_result_is_replayed_after_crash_without_reexecuting_read(tmp_path):
    child(tmp_path, "recovery_advice_after_result", "advice")
    model = ScriptedModel([ModelResponse(text="acknowledged")])
    runtime = Runtime(tmp_path, model, recovery_advisor=RecoveryAdvisor())
    runtime.tools.run_read_file = lambda **kwargs: (_ for _ in ()).throw(AssertionError("read replayed"))
    task = runtime.store.list_tasks()[0]["task_id"]
    assert runtime.resume(task).status == "completed"
    feedback = json.loads(model.calls[0][-1]["content"][0]["content"])
    assert feedback["recovery"]["error_type"] == "PATH_NOT_FOUND"
    assert len(runtime.store.list_tool_calls(task)) == 1
    runtime.store.assert_invariants(task)
