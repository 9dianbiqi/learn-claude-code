"""New deterministic repros from the independent Phase 2 entry audits."""
import hashlib
import os
from dataclasses import replace

import pytest

from agent_runtime import Runtime, PlanPatch, UpdatePlanItemDependencies
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, ToolCall, VerifiedSubtaskConfig, VerifiedSubtaskDAGConfig, VerifierResult
from agent_runtime.runtime import InjectedCrash


def test_approved_read_prefixed_shell_does_not_replay_append_after_crash(tmp_path):
    (tmp_path/'note.txt').write_text('note',encoding='utf-8')
    command='type note.txt & echo X >> marker.txt' if os.name=='nt' else 'cat note.txt; echo X >> marker.txt'
    def crash(point,**context):
        if point=='after_tool_effect_before_persist': raise InjectedCrash(point)
    first=Runtime(tmp_path,ScriptedModel([ModelResponse(tool_calls=[ToolCall('shell','bash',{'command':command})])]),
                  approval_callback=lambda *_:True,fault_injector=crash)
    with pytest.raises(InjectedCrash): first.run('Run approved command')
    before=(tmp_path/'marker.txt').read_bytes()
    task=first.store.list_tasks()[0]['task_id']
    resumed=Runtime(tmp_path,ScriptedModel([ModelResponse(text='done')]),approval_callback=lambda *_:True)
    result=resumed.resume(task)
    assert result.status=='needs_review'
    assert (tmp_path/'marker.txt').read_bytes()==before
    assert len(resumed.store.list_operations(task))==1


@pytest.mark.parametrize('limit',[None,1])
def test_read_observation_cannot_claim_an_external_change_was_seen(tmp_path,limit):
    path=tmp_path/'note.txt'; path.write_bytes(b'OLD\nTAIL\n')
    def change(point,**context):
        if point=='after_tool_effect_before_persist' and context.get('tool_use_id')=='read':
            path.write_bytes(b'EXTERNAL\nTAIL\n')
    args={'path':'note.txt'}
    if limit is not None: args['limit']=limit
    model=ScriptedModel([
        ModelResponse(tool_calls=[ToolCall('read','read_file',args)]),
        ModelResponse(tool_calls=[ToolCall('write','write_file',{'path':'note.txt','content':'MODEL_EDIT'})]),
        ModelResponse(text='done')])
    runtime=Runtime(tmp_path,model,approval_callback=lambda *_:True,fault_injector=change)
    result=runtime.run('Read then edit')
    assert result.status=='needs_review'
    assert path.read_bytes()==b'EXTERNAL\nTAIL\n'
    observation=runtime.store.get_file_observation(result.task_id,str(path.resolve()))
    assert observation['sha256']==hashlib.sha256(b'OLD\nTAIL\n').hexdigest()


@pytest.mark.parametrize('state',['retryable','in_progress','verifying'])
@pytest.mark.parametrize('scoped',[False,True])
def test_new_dependency_executes_ready_prerequisite_before_old_active_item(tmp_path,state,scoped):
    verified=[]
    def node(name):
        (tmp_path/f'{name}.txt').write_bytes(b'artifact')
        def verify(context):
            verified.append(context.subtask_id)
            return VerifierResult('pass','valid',[{'path':f'{name}.txt','sha256':hashlib.sha256(b'artifact').hexdigest()}])
        return VerifiedSubtaskConfig(name,name,name,(f'{name}.txt',),name,'1','valid',verify,'b'*64,max_turns=4)
    a,b=node('a'),node('b')
    def crash(point,**context):
        if point=='before_model_call': raise InjectedCrash(point)
    first=Runtime(tmp_path,ScriptedModel([]),verified_subtask_dag=VerifiedSubtaskDAGConfig((a,b)),fault_injector=crash)
    with pytest.raises(InjectedCrash): first.run('Complete a and b')
    task=first.store.list_tasks()[0]['task_id']; item=first.store.get_plan_item(task,'a')
    if state=='retryable':
        first.store.fail_plan_item(item['plan_item_id'],'try again')
        first.store.mark_plan_item_retryable(item['plan_item_id'])
    elif state=='verifying':
        first.store.submit_plan_item_for_verification(item['plan_item_id'],completion_summary='a implementation ready')
    old=first.get_current_plan_revision(task)
    first.apply_plan_patch(task,old.revision_id,PlanPatch('prerequisite required','operator',(UpdatePlanItemDependencies('a',('b',)),)))
    dag=VerifiedSubtaskDAGConfig((replace(a,blocked_by=('b',)),b))
    model=ScriptedModel([ModelResponse(text='b done\nSUBTASK_COMPLETE'),ModelResponse(text='a done\nSUBTASK_COMPLETE')])
    resumed=Runtime(tmp_path,model,verified_subtask_dag=dag,scoped_context_budget=10000 if scoped else None)
    result=resumed.resume(task)
    assert result.status=='completed'
    assert verified==['b','a']
    assert model.call_count==2
    assert resumed.store.get_plan_item(task,'a')['consumed_turns']==2
    assert resumed.store.get_plan_item(task,'b')['consumed_turns']==1
