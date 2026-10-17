"""New real-process tests for the verified bundle's transaction boundary."""
import hashlib
import os
import sqlite3
import subprocess
import sys
import time

import pytest

from agent_runtime import Runtime
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import VerifiedSubtaskConfig, VerifierResult


@pytest.mark.parametrize('point',['verified_subtask_f2_pre','verified_subtask_f2_mid','verified_subtask_f3'])
def test_verified_bundle_is_all_or_none_after_hard_process_exit(tmp_path,point):
    (tmp_path/'artifact.txt').write_bytes(b'artifact')
    program='''
import hashlib,os,sys
from agent_runtime import Runtime
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse,VerifiedSubtaskConfig,VerifierResult
def verifier(context):
    return VerifierResult('pass','valid',[{'path':'artifact.txt','sha256':hashlib.sha256(b'artifact').hexdigest()}])
config=VerifiedSubtaskConfig('one','one','one',('artifact.txt',),'one','1','valid',verifier,'b'*64,max_turns=1)
def crash(point,**context):
    if point==sys.argv[2]: os._exit(73)
Runtime(sys.argv[1],ScriptedModel([ModelResponse(text='done\\nSUBTASK_COMPLETE')]),verified_subtask=config,fault_injector=crash,lease_ttl=1).run('Complete one')
'''
    child=subprocess.run([sys.executable,'-c',program,str(tmp_path),point],capture_output=True,text=True,timeout=20,
                         creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    assert child.returncode==73,child.stderr
    with sqlite3.connect(tmp_path/'.agent_runtime/runtime.db') as c:
        assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        expected=1 if point=='verified_subtask_f3' else 0
        assert c.execute('SELECT count(*) FROM semantic_checkpoints').fetchone()[0]==expected
        assert c.execute('SELECT count(*) FROM verifier_runs WHERE authoritative=1').fetchone()[0]==expected
        assert c.execute('SELECT status FROM plan_items').fetchone()[0]==('completed' if expected else 'verifying')
        expires=c.execute('SELECT max(expires_at) FROM leases').fetchone()[0]
    while time.time()<=expires: time.sleep(.05)
    observed=[]
    def verify(context):
        observed.append(context.subtask_id)
        return VerifierResult('pass','valid',[{'path':'artifact.txt','sha256':hashlib.sha256(b'artifact').hexdigest()}])
    config=VerifiedSubtaskConfig('one','one','one',('artifact.txt',),'one','1','valid',verify,'b'*64,max_turns=1)
    model=ScriptedModel([])
    runtime=Runtime(tmp_path,model,verified_subtask=config)
    task=runtime.store.list_tasks()[0]['task_id']
    result=runtime.resume(task)
    assert result.status=='completed' and model.call_count==0
    assert observed==([] if expected else ['one'])
    assert len(runtime.store.list_verified_subtask_checkpoints(task))==1
    runtime.store.assert_invariants(task)
