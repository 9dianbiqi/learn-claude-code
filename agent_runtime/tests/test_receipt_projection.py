import copy
import json
import sqlite3
from pathlib import Path

import pytest

from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, ToolCall
from agent_runtime.runtime import Runtime
from agent_runtime.experiments.receipt_projection import historical_receipts, render, TIME_NOTE


@pytest.fixture
def committed(tmp_path):
    (tmp_path/'x.py').write_text('old', encoding='utf-8')
    runtime=Runtime(tmp_path,ScriptedModel([
        ModelResponse(tool_calls=[ToolCall('read','read_file',{'path':'x.py'})]),
        ModelResponse(tool_calls=[ToolCall('write','write_file',{'path':'x.py','content':'new'})]),
        ModelResponse(text='done')]),approval_callback=lambda *_:True)
    result=runtime.run('write x')
    boundary=runtime.store.list_checkpoints(result.task_id)[0]['checkpoint_id']
    return tmp_path,runtime,result.task_id,boundary


def test_committed_receipt_and_external_drift_remain_historical(committed,monkeypatch):
    path,runtime,task,boundary=committed
    before,upper=historical_receipts(runtime.store.path,task,boundary,{'x.py'})
    assert len(before)==1
    assert before[0]['bytes_written']==3
    assert before[0]['freshness']=='historical_committed_effect'
    assert before[0]['evidence_checkpoint_id']<=upper
    (path/'x.py').write_text('external drift',encoding='utf-8')
    # Receipt projection must not obtain a new hash by opening a source file.
    monkeypatch.setattr(Path,'read_bytes',lambda *_: (_ for _ in ()).throw(AssertionError('workspace read')))
    after,_=historical_receipts(runtime.store.path,task,boundary,{'x.py'})
    assert before==after
    view,_=render([{'role':'system','content':'base'}],'C-receipt',after)
    assert 'not a current filesystem assertion or semantic verifier pass' in view[0]['content']
    assert 'current_hash' not in view[0]['content']


@pytest.mark.parametrize('state',['prepared','dispatched','unknown','failed','cancelled'])
def test_uncommitted_effects_are_not_projected(committed,state):
    _,runtime,task,boundary=committed
    with sqlite3.connect(runtime.store.path) as c:
        c.execute('UPDATE operations SET state=?',(state,))
    assert historical_receipts(runtime.store.path,task,boundary,{'x.py'})[0]==[]


def test_preboundary_and_unrelated_effects_are_not_projected(committed):
    _,runtime,task,boundary=committed
    assert historical_receipts(runtime.store.path,task,boundary,{'other.py'})[0]==[]
    final=runtime.store.get_task(task)['checkpoint_id']
    assert historical_receipts(runtime.store.path,task,final,{'x.py'})[0]==[]


def test_condition_differences_and_bounded_truncation(committed):
    _,runtime,task,boundary=committed
    receipts,_=historical_receipts(runtime.store.path,task,boundary,{'x.py'})
    messages=[{'role':'system','content':'base'},{'role':'user','content':'continue'}]
    original=copy.deepcopy(messages)
    base,meta=render(messages,'C',receipts)
    timed,_=render(messages,'C-time',receipts)
    assert base==original and meta['exposed_receipts']==[]
    assert timed[0]['content']=='base'+TIME_NOTE and timed[1:]==messages[1:]
    assert render(messages,'C-receipt',[])[0]==timed
    view,meta=render(messages,'C-receipt',receipts*3)
    assert len(meta['exposed_receipts'])==1 and meta['omitted_receipts']==2
    _,meta=render(messages,'C-receipt',receipts,max_chars=1)
    assert meta['exposed_receipts']==[] and meta['omitted_receipts']==1
    assert messages==original
    with pytest.raises(ValueError,match='budget'):
        render(messages,'C-receipt',receipts,budget=1)
