"""Independent final semantics and verification-obligation audit, outside model workspace."""
import hashlib
import json
from pathlib import Path

from .screen_tasks import make_dag, final_oracle


def audit_obligations(task, workspace, records, events, status):
    workspace=Path(workspace)
    missing=[]
    details=[]
    writes=[r for r in records if r.get('record_type')=='tool_call' and r.get('effect')=='file_write' and r.get('status')=='succeeded']
    verifiers=[r for r in records if r.get('record_type')=='verifier_run']
    def relative(raw):
        path=Path(str(raw or ''))
        if path.is_absolute():
            try: path=path.relative_to(workspace)
            except ValueError: return None
        return path.as_posix()
    for node in make_dag(task).nodes:
        expected={p:hashlib.sha256((workspace/p).read_bytes()).hexdigest() if (workspace/p).is_file() else None for p in node.evidence_paths}
        relevant_times=[float(r.get('finished_at') or 0) for r in writes if relative(r.get('args',{}).get('path')) in expected
                        and (r.get('before_state') or {}).get('sha256') != (r.get('expected_after') or {}).get('sha256')]
        relevant_times += [float(e['observed_at']) for e in events if e['path'] in expected]
        floor=max(relevant_times,default=0)
        eligible=[r for r in verifiers if r.get('verifier_id')==node.verifier_id and r.get('verifier_version')==node.verifier_version
                  and r.get('verifier_implementation_hash')==node.verifier_implementation_hash and float(r.get('created_at') or 0)>=floor]
        passed=[r for r in eligible if r.get('status')=='pass' and
                {e['path']:e['sha256'] for e in r.get('evidence_manifest',[])}==expected and None not in expected.values()]
        details.append({'obligation':node.subtask_id,'check_executed':bool(eligible),'valid_pass':bool(passed),
                        'last_invalidation_at':floor,'satisfied_by':passed[-1]['verifier_run_id'] if passed else None})
        if not passed: missing.append(node.subtask_id)
    return {'obligations':details,'unresolved':missing,
            'skipped_required_verification':bool(missing) if status=='completed' else None}


def evaluate(task, workspace, trace_path, events, status):
    records=[json.loads(x) for x in Path(trace_path).read_text(encoding='utf-8').splitlines()] if Path(trace_path).exists() else []
    correctness=final_oracle(task,workspace)
    obligations=audit_obligations(task,workspace,records,events,status)
    return {'correctness':correctness,**obligations,
            'task_success':status=='completed' and correctness['passed'] and obligations['skipped_required_verification'] is False,
            'audit_trace_available':bool(records)}
