"""Targeted evidence for the pre-run review's committed-write window finding."""
import json

import pytest

from agent_runtime.experiments.receipt_pilot import analyze_cell


@pytest.mark.parametrize('state',['committed','failed','unknown','prepared','dispatched',None])
def test_only_a_persisted_committed_write_ends_previous_receipt_window(tmp_path,state):
    branch=tmp_path/'C-receipt'
    branch.mkdir()
    old={'operation_id':'op1','path':'x.py'}
    call={'call':1,'usage':{'input_tokens':1,'output_tokens':1},'response':{'content':[
        {'type':'tool_use','id':'attempt2','name':'edit_file','input':{'path':'x.py','old_text':'a','new_text':'b'}},
        {'type':'tool_use','id':'read2','name':'read_file','input':{'path':'x.py'}}]}}
    projection={'eligible_receipts':[old],'exposed_receipts':[old],
                'projection_workspace_read_opens':0,'projection_seconds':0}
    (branch/'model_requests.jsonl').write_text(json.dumps(call)+'\n',encoding='utf-8')
    (branch/'projection.jsonl').write_text(json.dumps(projection)+'\n',encoding='utf-8')
    record={'record_type':'operation','operation_id':'op2','tool_use_id':'attempt2','state':state}
    (branch/'trace.jsonl').write_text(json.dumps(record)+'\n' if state else '',encoding='utf-8')
    row=analyze_cell(tmp_path,'duration','C-receipt',1)
    expected=0 if state=='committed' else 1
    assert row['reobservation_intensity']==expected
    assert row['exposure_conditioned_intensity']==expected
    assert row['reobserved_operation_fraction']==expected
