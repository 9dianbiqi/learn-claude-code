import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_runtime.experiments.bc_pilot import FullHistory, SOLUTIONS
from agent_runtime.experiments.bc_tasks import TASKS, make_dag, prompt
from agent_runtime.experiments.receipt_pilot import worker, analyze_cell
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, ToolCall
from agent_runtime.runtime import Runtime, InjectedCrash


@pytest.mark.parametrize('condition',['C','C-time','C-receipt'])
def test_worker_resumes_real_runtime_with_instrumented_projection(tmp_path,monkeypatch,condition):
    workspace=tmp_path/'workspace'
    workspace.mkdir()
    for name,content in TASKS['duration']['files'].items():
        (workspace/name).write_text(content,encoding='utf-8')
    def crash(point,**context):
        if point=='verified_subtask_f3': raise InjectedCrash(point)
    prefix=Runtime(workspace,ScriptedModel([
        ModelResponse(tool_calls=[ToolCall('r0','read_file',{'path':'duration.py'})]),
        ModelResponse(tool_calls=[ToolCall('w0','write_file',{'path':'duration.py','content':SOLUTIONS['duration'][0]})]),
        ModelResponse(text='Ready\nSUBTASK_COMPLETE')]),verified_subtask_dag=make_dag('duration'),
        fault_injector=crash,approval_callback=lambda *_:True)
    prefix.projector=FullHistory()
    with pytest.raises(InjectedCrash): prefix.run(prompt('duration'))
    class FakeProvider(ScriptedModel):
        name='offline-provider'
        def __init__(self,**kwargs):
            super().__init__([
                ModelResponse(tool_calls=[ToolCall('r1','read_file',{'path':'settings.py'})]),
                ModelResponse(tool_calls=[ToolCall('w1','write_file',{'path':'settings.py','content':SOLUTIONS['duration'][1]})]),
                ModelResponse(text='Done\nSUBTASK_COMPLETE')])
            self.client=self
        def with_options(self,**kwargs): return self
    monkeypatch.setattr('agent_runtime.providers.AnthropicModel',FakeProvider)
    directory=tmp_path/condition
    directory.mkdir()
    env=tmp_path/'empty.env'
    env.write_text('',encoding='utf-8')
    args=SimpleNamespace(workspace=str(workspace),directory=str(directory),env_file=str(env),task='duration',condition=condition)
    assert worker(args)==0
    result=json.loads((directory/'result.json').read_text())
    assert result['result']['status']=='completed' and result['final_check']['passed']
    projections=[json.loads(x) for x in (directory/'projection.jsonl').read_text().splitlines()]
    assert all(p['projection_workspace_read_opens']==0 for p in projections)
    assert projections[0]['eligible_receipts']==[]
    assert len(projections[-1]['eligible_receipts'])==1
    assert len(projections[-1]['exposed_receipts'])==(1 if condition=='C-receipt' else 0)


def test_reobservation_intensity_can_exceed_one_and_exposure_is_separate(tmp_path):
    directory=tmp_path/'C'
    directory.mkdir()
    receipt={'operation_id':'op1','path':'x.py'}
    calls=[{'call':i,'usage':{'input_tokens':1,'output_tokens':1},'response':{'content':[{'type':'tool_use','name':'read_file','input':{'path':'x.py'}}]}} for i in [1,2]]
    projections=[{'eligible_receipts':[receipt],'exposed_receipts':[],'projection_workspace_read_opens':0,'projection_seconds':0} for _ in calls]
    for filename,rows in [('model_requests.jsonl',calls),('projection.jsonl',projections)]:
        (directory/filename).write_text('\n'.join(json.dumps(r) for r in rows),encoding='utf-8')
    row=analyze_cell(tmp_path,'duration','C',1)
    assert row['reobservation_intensity']==2
    assert row['reobserved_operation_fraction']==1
    assert row['exposure_conditioned_intensity'] is None
    calls[0]['response']['content'].insert(0,{'type':'tool_use','id':'next-write','name':'write_file','input':{'path':'x.py','content':'next'}})
    (directory/'trace.jsonl').write_text(json.dumps({'record_type':'operation','tool_use_id':'next-write','state':'committed'})+'\n',encoding='utf-8')
    calls[1].pop('response')  # No provider decision: not an observation opportunity.
    (directory/'model_requests.jsonl').write_text('\n'.join(json.dumps(r) for r in calls),encoding='utf-8')
    row=analyze_cell(tmp_path,'duration','C',1)
    assert row['reobservation_intensity']==0
