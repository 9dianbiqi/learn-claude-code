import os

import pytest

from agent_runtime import Runtime
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, ToolCall
from agent_runtime.runtime import InjectedCrash
from agent_runtime.store import InvariantViolation


def old_interrupted_shell(tmp_path, *, tool_id='old-shell', simulate_pre_upgrade_acquire=False):
    (tmp_path/'note.txt').write_text('note')
    command='type note.txt & echo X >> marker.txt' if os.name=='nt' else 'cat note.txt; echo X >> marker.txt'
    def crash(point,**context):
        if point=='after_tool_effect_before_persist': raise InjectedCrash(point)
    first=Runtime(tmp_path,ScriptedModel([ModelResponse(tool_calls=[ToolCall(tool_id,'bash',{'command':command})])]),
                  approval_callback=lambda *_:True,fault_injector=crash)
    existing_tasks={task['task_id'] for task in first.store.list_tasks()}
    if simulate_pre_upgrade_acquire:
        first.store.list_untracked_shell_calls=lambda _: []
    original=first.permissions.classify_effect
    first.permissions.classify_effect=lambda name,args:'read_only' if name=='bash' else original(name,args)
    with pytest.raises(InjectedCrash): first.run('legacy command')
    task_id=next(task['task_id'] for task in first.store.list_tasks() if task['task_id'] not in existing_tasks)
    return task_id,(tmp_path/'marker.txt').read_bytes()


def test_old_readonly_shell_is_quarantined_on_resume(tmp_path):
    task,before=old_interrupted_shell(tmp_path)
    model=ScriptedModel([ModelResponse(text='done')])
    runtime=Runtime(tmp_path,model,approval_callback=lambda *_:True)
    result=runtime.resume(task)
    assert result.status=='needs_review' and model.call_count==0
    assert (tmp_path/'marker.txt').read_bytes()==before
    assert runtime.store.get_effect_reservation(task,'old-shell')['state']=='unknown'
    assert runtime.store.get_tool_call(task,'old-shell')['effect']=='unknown_write'
    runtime.store.assert_invariants(task)


def test_two_legacy_tasks_can_each_be_quarantined_without_a_guard_deadlock(tmp_path):
    first,_=old_interrupted_shell(tmp_path)
    second,before=old_interrupted_shell(tmp_path,tool_id='second-shell',simulate_pre_upgrade_acquire=True)
    for task,tool_id in [(first,'old-shell'),(second,'second-shell')]:
        model=ScriptedModel([])
        runtime=Runtime(tmp_path,model)
        assert runtime.resume(task).status=='needs_review'
        assert runtime.store.get_effect_reservation(task,tool_id)['state']=='unknown'
        assert model.call_count==0
    assert (tmp_path/'marker.txt').read_bytes()==before
    runtime.store.assert_invariants()


def test_untracked_legacy_shell_blocks_other_task_before_model_call(tmp_path):
    _,before=old_interrupted_shell(tmp_path)
    model=ScriptedModel([ModelResponse(text='must not run')])
    runtime=Runtime(tmp_path,model)
    with pytest.raises(InvariantViolation,match='legacy.*shell|shell.*legacy'):
        runtime.run('new task')
    assert model.call_count==0 and (tmp_path/'marker.txt').read_bytes()==before


def test_quarantine_is_atomic_and_explicit_complete_does_not_replay(tmp_path):
    task,before=old_interrupted_shell(tmp_path)
    model=ScriptedModel([ModelResponse(text='done')])
    runtime=Runtime(tmp_path,model,approval_callback=lambda *_:True)
    emit=runtime.store._emit_conn
    def interrupt(*args,**kwargs):
        result=emit(*args,**kwargs)
        if args[2]=='legacy_shell_quarantined': raise InjectedCrash('quarantine before commit')
        return result
    runtime.store._emit_conn=interrupt
    with pytest.raises(InjectedCrash): runtime.resume(task)
    assert runtime.store.get_effect_reservation(task,'old-shell') is None
    assert runtime.store.get_tool_call(task,'old-shell')['effect']=='read_only'
    runtime.store._emit_conn=emit
    assert runtime.resume(task).status=='needs_review'
    assert runtime.resolve_call('old-shell','complete').status=='completed'
    assert (tmp_path/'marker.txt').read_bytes()==before
    assert model.call_count==1
    runtime.store.assert_invariants(task)


def test_legacy_guard_is_checked_after_acquiring_the_fenced_lease(tmp_path):
    model=ScriptedModel([ModelResponse(text='must not start')])
    runtime=Runtime(tmp_path,model)
    acquire=runtime.store.acquire_lease
    injected=False
    def interleave(*args,**kwargs):
        nonlocal injected
        if not injected:
            injected=True
            old_interrupted_shell(tmp_path)
        return acquire(*args,**kwargs)
    runtime.store.acquire_lease=interleave
    with pytest.raises(InvariantViolation,match='legacy.*shell|shell.*legacy'):
        runtime.run('new task')
    assert model.call_count==0
    assert runtime._lease_token is None
