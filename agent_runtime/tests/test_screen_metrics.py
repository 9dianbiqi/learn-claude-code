import hashlib
import sqlite3

import pytest

from agent_runtime.experiments.screen_metrics import TaskPaths, canonical_path, costs, observation_metrics
from agent_runtime.experiments.receipt_projection import historical_receipts
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, ToolCall
from agent_runtime.runtime import Runtime


def test_absolute_write_is_an_eligible_task_receipt(tmp_path):
    path=tmp_path/'x.py'; path.write_text('old')
    runtime=Runtime(tmp_path,ScriptedModel([
        ModelResponse(tool_calls=[ToolCall('r','read_file',{'path':str(path)})]),
        ModelResponse(tool_calls=[ToolCall('w','write_file',{'path':str(path),'content':'new'})]),
        ModelResponse(text='done')]),approval_callback=lambda *_:True)
    result=runtime.run('write')
    boundary=runtime.store.list_checkpoints(result.task_id)[0]['checkpoint_id']
    receipts,_=historical_receipts(runtime.store.path,result.task_id,boundary,TaskPaths(tmp_path,{'x.py'}))
    assert len(receipts)==1 and canonical_path(tmp_path,receipts[0]['path'])=='x.py'


@pytest.mark.parametrize('state',['committed','failed','unknown'])
def test_mixed_absolute_relative_read_and_overwrite_windows(tmp_path,state):
    receipt={'path':str(tmp_path/'x.py'),'operation_id':'old'}
    projection={'eligible_receipts':[receipt]}
    blocks=[{'type':'tool_use','id':'r1','name':'read_file','input':{'path':'./x.py'}},
            {'type':'tool_use','id':'w2','name':'write_file','input':{'path':str(tmp_path/'x.py')}},
            {'type':'tool_use','id':'r2','name':'read_file','input':{'path':str(tmp_path/'x.py')}}]
    result=observation_metrics([{'call':1,'response':{'content':blocks}}],[projection],
                               [{'record_type':'operation','state':state,'tool_use_id':'w2'}],tmp_path)
    assert result['eligible_write_operations']==1
    assert len(result['post_write_read_events'])==(1 if state=='committed' else 2)


@pytest.mark.parametrize('calls,expected,missing',[
    ([],0,0),
    ([{'usage':{}}],None,1),
    ([{'usage':{'input_tokens':3,'output_tokens':2}},{'usage':{}}],None,1),
    ([{'usage':{'input_tokens':3,'output_tokens':2},'provider_usage':{'input_tokens':3,'cache_read_input_tokens':5,'output_tokens':2}}],8,0),
])
def test_missing_usage_is_unknown_not_partial_total(calls,expected,missing):
    result=costs(calls)
    assert result['input_tokens']==expected and result['missing_usage_calls']==missing
    if expected is None: assert result['cost_complete'] is False
