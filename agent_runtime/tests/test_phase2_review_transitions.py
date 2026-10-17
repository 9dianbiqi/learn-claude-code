"""New regressions requested by the first fresh increment reviewer."""
import hashlib
import os
from dataclasses import replace

import pytest

from agent_runtime import Runtime, PlanPatch, UpdatePlanItemDependencies
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, ToolCall, VerifiedSubtaskConfig, VerifiedSubtaskDAGConfig, VerifierResult
from agent_runtime.runtime import InjectedCrash, PendingPlanConflict
from agent_runtime.tests.test_phase2_legacy_shell import old_interrupted_shell


def nodes(root):
    result=[]
    for name in ('a','b'):
        (root/f'{name}.txt').write_bytes(b'artifact')
        def verify(context, path=f'{name}.txt'):
            return VerifierResult('pass','valid',[{'path':path,'sha256':hashlib.sha256((root/path).read_bytes()).hexdigest()}])
        result.append(VerifiedSubtaskConfig(name,name,name,(f'{name}.txt',),name,'1','valid',verify,'b'*64,max_turns=4))
    return result


@pytest.mark.parametrize('scoped',[False,True])
@pytest.mark.parametrize('review',['approval','retry'])
def test_review_does_not_remove_batch_owner_or_dependency_guard(tmp_path,scoped,review):
    a,b=nodes(tmp_path)
    if review=='approval':
        calls=[ToolCall('read','read_file',{'path':'a.txt'}),ToolCall('write','write_file',{'path':'a.txt','content':'changed'})]
    else:
        command='echo X >> marker.txt & exit 1' if os.name=='nt' else 'echo X >> marker.txt; exit 1'
        calls=[ToolCall('shell','bash',{'command':command})]
    first=Runtime(tmp_path,ScriptedModel([ModelResponse(tool_calls=calls)]),verified_subtask_dag=VerifiedSubtaskDAGConfig((a,b)),
                  approval_callback=(lambda *_:True) if review=='retry' else None)
    result=first.run('complete both')
    assert result.status==('waiting_approval' if review=='approval' else 'needs_review')
    checkpoint=first.store.get_checkpoint(first.store.get_task(result.task_id)['checkpoint_id'])
    assert checkpoint['cursor'].get('active_subtask_id')=='a'
    old=first.get_current_plan_revision(result.task_id)
    first.apply_plan_patch(result.task_id,old.revision_id,PlanPatch('new prerequisite','operator',(UpdatePlanItemDependencies('a',('b',)),)))
    model=ScriptedModel([])
    resumed=Runtime(tmp_path,model,verified_subtask_dag=VerifiedSubtaskDAGConfig((replace(a,blocked_by=('b',)),b)),
                    scoped_context_budget=10000 if scoped else None,approval_callback=lambda *_:True)
    assert resumed.resume(result.task_id).status==result.status
    before=(tmp_path/('a.txt' if review=='approval' else 'marker.txt')).read_bytes()
    with pytest.raises(PendingPlanConflict,match='blocked'):
        resumed.approve('write') if review=='approval' else resumed.resolve_call('shell','retry')
    assert (tmp_path/('a.txt' if review=='approval' else 'marker.txt')).read_bytes()==before
    assert model.call_count==0 and resumed._lease_token is None
    resumed.store.assert_invariants(result.task_id)


def test_ownerless_dag_pending_batch_fails_closed(tmp_path):
    a,b=nodes(tmp_path)
    def crash(point,**context):
        if point=='after_model_response': raise InjectedCrash(point)
    first=Runtime(tmp_path,ScriptedModel([ModelResponse(tool_calls=[ToolCall('read','read_file',{'path':'a.txt'})])]),
                  verified_subtask_dag=VerifiedSubtaskDAGConfig((a,b)),fault_injector=crash)
    with pytest.raises(InjectedCrash): first.run('work')
    task=first.store.list_tasks()[0]['task_id']
    first.store.recover_model_checkpoint(task)
    checkpoint=first.store.get_checkpoint(first.store.get_task(task)['checkpoint_id'])
    first.store.save_checkpoint(task,'model_responded',checkpoint['messages'],{'turn':0})
    resumed=Runtime(tmp_path,ScriptedModel([]),verified_subtask_dag=VerifiedSubtaskDAGConfig((a,b)))
    with pytest.raises(PendingPlanConflict,match='owner'):
        resumed.resume(task)
    assert resumed.store.list_tool_calls(task)==[]
    assert resumed._lease_token is None


def test_old_review_checkpoint_and_another_owner_are_quarantined_idempotently(tmp_path):
    first,_=old_interrupted_shell(tmp_path)
    second,before=old_interrupted_shell(tmp_path,tool_id='second-shell',simulate_pre_upgrade_acquire=True)
    runtime=Runtime(tmp_path,ScriptedModel([]))
    checkpoint=runtime.store.get_checkpoint(runtime.store.get_task(first)['checkpoint_id'])
    runtime.store.transition_review(first,'old-shell','needs_review',checkpoint['messages'],
                                    {'turn':0,'pending_tool_use_id':'old-shell'},'legacy review')
    for task,tool in [(first,'old-shell'),(second,'second-shell')]:
        assert runtime.resume(task).status=='needs_review'
        assert runtime.store.get_tool_call(task,tool)['effect']=='unknown_write'
        assert runtime.store.get_effect_reservation(task,tool)['state']=='unknown'
    for task in (first,second): assert runtime.resume(task).status=='needs_review'
    assert len(runtime.store.list_effect_reservations())==2
    assert runtime.model.call_count==0 and (tmp_path/'marker.txt').read_bytes()==before
    runtime.store.assert_invariants()
