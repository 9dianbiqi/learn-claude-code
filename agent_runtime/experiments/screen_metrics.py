"""Screen-only path and missing-data adaptation; no model-visible changes."""
import os
from pathlib import Path

from .bc_pilot import input_tokens


def canonical_path(workspace, raw):
    path=Path(str(raw or '').replace('\\','/'))
    if path.is_absolute():
        try: path=path.relative_to(Path(workspace))
        except ValueError: return None
    if '..' in path.parts: return None
    result=path.as_posix()
    return result.casefold() if os.name=='nt' else result


class TaskPaths:
    def __init__(self,workspace,paths):
        self.workspace=workspace
        self.paths={canonical_path(workspace,p) for p in paths}
    def __contains__(self,path):
        return canonical_path(self.workspace,path) in self.paths


def costs(calls,offline=False):
    known=[]
    missing=0
    for call in calls:
        usage=call.get('provider_usage') or call.get('usage',{})
        if not all(type(usage.get(k)) is int and usage[k]>=0 for k in ('input_tokens','output_tokens')):
            missing+=1
        else: known.append(call)
    known_input=sum(input_tokens(c) for c in known)
    known_output=sum((c.get('provider_usage') or c['usage'])['output_tokens'] for c in known)
    return {'input_tokens':None if offline or missing else known_input,
            'output_tokens':None if offline or missing else known_output,
            'known_input_tokens':known_input,'known_output_tokens':known_output,
            'missing_usage_calls':missing,'cost_complete':not offline and not missing}


def observation_metrics(calls,projections,records,workspace):
    committed={r['tool_use_id'] for r in records if r.get('record_type')=='operation' and r.get('state')=='committed'}
    available={}
    events=[]
    for call,projection in zip(calls,projections):
        if call.get('response') is None: continue
        latest={}
        for receipt in projection['eligible_receipts']:
            available[receipt['operation_id']]=receipt
            latest.setdefault(canonical_path(workspace,receipt['path']),receipt)
        for block in call['response']['content']:
            path=canonical_path(workspace,block.get('input',{}).get('path'))
            if block.get('name') in ('write_file','edit_file'):
                if block.get('id') in committed: latest.pop(path,None)
            elif block.get('name')=='read_file' and path in latest:
                events.append({'call':call['call'],'path':path,'operation_id':latest[path]['operation_id'],'receipt_exposed':False})
    count=len(available)
    return {'eligible_write_operations':count,'exposed_write_operations':0,
            'post_write_read_events':events,'reobservation_intensity':len(events)/count if count else None,
            'reobserved_operation_fraction':len({e['operation_id'] for e in events})/count if count else None,
            'exposure_conditioned_intensity':None}
