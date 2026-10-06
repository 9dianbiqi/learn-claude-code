import copy
import hashlib
import json

import pytest

from agent_runtime.experiments.screen_tasks import TASKS, make_dag, final_oracle
from agent_runtime.experiments.screen_oracle import audit_obligations


@pytest.mark.parametrize('task',list(TASKS))
def test_private_final_oracle_rejects_stub_and_accepts_reference(tmp_path,task):
    spec=TASKS[task]
    for path,content in spec['files'].items(): (tmp_path/path).write_text(content,encoding='utf-8',newline='\n')
    assert final_oracle(task,tmp_path)['passed'] is False
    for path,content in {**spec['prefix_files'],**spec['reference']}.items(): (tmp_path/path).write_text(content,encoding='utf-8',newline='\n')
    # U2's approved environment event changes config; U1's harmful code is repaired.
    if task=='U2': (tmp_path/'config.json').write_text(spec['drift']['config.json'],encoding='utf-8',newline='\n')
    assert final_oracle(task,tmp_path)['passed'] is True


def test_obligation_auditor_rejects_missing_failed_stale_or_wrong_version_evidence(tmp_path):
    task='S1'
    for path,content in {**TASKS[task]['files'],**TASKS[task]['prefix_files'],**TASKS[task]['reference']}.items():
        (tmp_path/path).write_text(content,encoding='utf-8',newline='\n')
    records=[]
    for i,node in enumerate(make_dag(task).nodes):
        records.append({'record_type':'verifier_run','verifier_id':node.verifier_id,'verifier_version':node.verifier_version,
                        'verifier_implementation_hash':node.verifier_implementation_hash,'created_at':10,'status':'pass',
                        'verifier_run_id':str(i),'evidence_manifest':[{'path':p,'sha256':hashlib.sha256((tmp_path/p).read_bytes()).hexdigest()} for p in node.evidence_paths]})
    assert audit_obligations(task,tmp_path,records,[],'completed')['skipped_required_verification'] is False
    assert audit_obligations(task,tmp_path,records[:-1],[],'completed')['skipped_required_verification'] is True
    for key,value in [('status','fail'),('status','uncertain'),('verifier_version','wrong')]:
        mutated=copy.deepcopy(records); mutated[-1][key]=value
        assert audit_obligations(task,tmp_path,mutated,[],'completed')['skipped_required_verification'] is True
    events=[{'path':'ranges.py','observed_at':20}]
    assert audit_obligations(task,tmp_path,records,events,'completed')['skipped_required_verification'] is True
    assert audit_obligations(task,tmp_path,records,events,'needs_review')['skipped_required_verification'] is None


def test_absolute_write_paths_cannot_hide_a_later_invalidation(tmp_path):
    task='S1'
    for path,content in {**TASKS[task]['files'],**TASKS[task]['prefix_files'],**TASKS[task]['reference']}.items():
        (tmp_path/path).write_text(content,encoding='utf-8',newline='\n')
    records=[]
    for i,node in enumerate(make_dag(task).nodes):
        records.append({'record_type':'verifier_run','verifier_id':node.verifier_id,'verifier_version':'1',
                        'verifier_implementation_hash':node.verifier_implementation_hash,'created_at':10,'status':'pass',
                        'verifier_run_id':str(i),'evidence_manifest':[{'path':p,'sha256':hashlib.sha256((tmp_path/p).read_bytes()).hexdigest()} for p in node.evidence_paths]})
    records.append({'record_type':'tool_call','effect':'file_write','status':'succeeded','finished_at':20,
                    'args':{'path':str(tmp_path/'ranges.py')},'before_state':{'sha256':'0'*64},
                    'expected_after':{'sha256':hashlib.sha256((tmp_path/'ranges.py').read_bytes()).hexdigest()}})
    assert audit_obligations(task,tmp_path,records,[],'completed')['skipped_required_verification'] is True
