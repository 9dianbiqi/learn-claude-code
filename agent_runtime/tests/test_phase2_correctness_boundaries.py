"""Additional boundaries, without re-running the already-passing Phase 1 suites."""
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from dataclasses import replace

import pytest

from agent_runtime import Runtime, PlanPatch, UpdatePlanItemDependencies
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, ToolCall, VerifiedSubtaskConfig, VerifiedSubtaskDAGConfig, VerifierResult
from agent_runtime.runtime import InjectedCrash
from agent_runtime.store import StaleState
from agent_runtime.tools import ToolExecutor, FileConflict


def test_opaque_shell_hard_exit_requires_review_without_second_effect(tmp_path):
    (tmp_path/'note.txt').write_text('note')
    program = '''
import os,sys
from agent_runtime import Runtime
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse,ToolCall
def crash(point,**context):
    if point=='after_tool_effect_before_persist': os._exit(73)
command='type note.txt & echo X >> marker.txt' if os.name=='nt' else 'cat note.txt; echo X >> marker.txt'
Runtime(sys.argv[1],ScriptedModel([ModelResponse(tool_calls=[ToolCall('shell','bash',{'command':command})])]),approval_callback=lambda *_:True,fault_injector=crash,lease_ttl=1).run('Run approved command')
'''
    child=subprocess.run([sys.executable,'-c',program,str(tmp_path)],capture_output=True,text=True,timeout=20,
                         creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    assert child.returncode==73,child.stderr
    db=tmp_path/'.agent_runtime/runtime.db'
    with sqlite3.connect(db) as c:
        expires=c.execute('SELECT max(expires_at) FROM leases').fetchone()[0]
    while time.time()<=expires: time.sleep(.05)
    before=(tmp_path/'marker.txt').read_bytes()
    model=ScriptedModel([])
    runtime=Runtime(tmp_path,model)
    task=runtime.store.list_tasks()[0]['task_id']
    result=runtime.resume(task)
    assert result.status=='needs_review' and model.call_count==0
    assert (tmp_path/'marker.txt').read_bytes()==before
    assert runtime.store.list_operations(task)[0]['state']=='unknown'


@pytest.mark.parametrize('capture',[False,True])
def test_mutation_during_handle_read_is_rejected(tmp_path,monkeypatch,capture):
    path=tmp_path/'note.txt'; path.write_bytes(b'OLD')
    original=Path.open
    class MutatingRead:
        def __init__(self,handle): self.handle=handle; self.changed=False
        def __enter__(self): self.handle.__enter__(); return self
        def __exit__(self,*args): return self.handle.__exit__(*args)
        def fileno(self): return self.handle.fileno()
        def read(self,size):
            chunk=self.handle.read(size)
            if chunk and not self.changed:
                self.changed=True
                path.write_bytes(b'EXTERNAL')
            return chunk
    def open_file(p,mode='r',*args,**kwargs):
        handle=original(p,mode,*args,**kwargs)
        return MutatingRead(handle) if p==path and mode=='rb' else handle
    monkeypatch.setattr(Path,'open',open_file)
    tools=ToolExecutor(tmp_path)
    with pytest.raises(FileConflict,match='changed while reading'):
        tools.run_read_file('note.txt') if capture else tools.file_state('note.txt')


@pytest.mark.parametrize('scoped',[False,True])
def test_new_dependency_cannot_replay_a_now_blocked_pending_tool_batch(tmp_path,scoped):
    def node(name):
        path=tmp_path/f'{name}.txt'; path.write_bytes(b'artifact')
        return VerifiedSubtaskConfig(name,name,name,(f'{name}.txt',),name,'1','exists',
            lambda _:VerifierResult('pass','valid',[{'path':f'{name}.txt','sha256':hashlib.sha256(path.read_bytes()).hexdigest()}]),
            'b'*64,max_turns=4)
    a,b=node('a'),node('b')
    def crash(point,**context):
        if point=='after_model_response': raise InjectedCrash(point)
    first=Runtime(tmp_path,ScriptedModel([ModelResponse(tool_calls=[
        ToolCall('read-a','read_file',{'path':'a.txt'}),
        ToolCall('write-a','write_file',{'path':'a.txt','content':'UNSAFE-BEFORE-B'})])]),
        verified_subtask_dag=VerifiedSubtaskDAGConfig((a,b)),fault_injector=crash,approval_callback=lambda *_:True)
    with pytest.raises(InjectedCrash): first.run('Complete a and b')
    task=first.store.list_tasks()[0]['task_id']; old=first.get_current_plan_revision(task)
    first.apply_plan_patch(task,old.revision_id,PlanPatch('new prerequisite','operator',(UpdatePlanItemDependencies('a',('b',)),)))
    dag=VerifiedSubtaskDAGConfig((replace(a,blocked_by=('b',)),b))
    model=ScriptedModel([ModelResponse(text='b\nSUBTASK_COMPLETE'),ModelResponse(text='a\nSUBTASK_COMPLETE')])
    resumed=Runtime(tmp_path,model,verified_subtask_dag=dag,scoped_context_budget=10000 if scoped else None,approval_callback=lambda *_:True)
    with pytest.raises(StaleState,match='blocked.*pending|pending.*blocked'):
        resumed.resume(task)
    assert (tmp_path/'a.txt').read_bytes()==b'artifact'
    assert model.call_count==0
    assert resumed.store.list_tool_calls(task)==[]
    resumed.store.assert_invariants(task)


@pytest.mark.parametrize('state',['retryable','in_progress','verifying'])
def test_projector_uses_ready_frontier_when_old_active_item_is_blocked(state):
    from agent_runtime.projector import ContextProjector
    plan={'dag_hash':'frozen','items':[
        {'subtask_id':'a','status':state,'blocked_by':['b']},
        {'subtask_id':'b','status':'pending','blocked_by':[]}]}
    assert ContextProjector._select_active_subtask(plan)['subtask_id']=='b'
