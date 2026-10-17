"""Three-condition historical-effect pilot; supervised and separately frozen."""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

from agent_runtime.runtime import Runtime
from agent_runtime.trace import TraceReporter
from .bc_pilot import (ALLOWED, CALL_LIMIT, TOKEN_LIMIT, SCOPED_BUDGET, MeasuredModel,
                       check, clone_snapshot, digest, file_hashes, input_tokens,
                       make_dag, requests, save, verify_observations)
from .bc_tasks import TASKS
from .receipt_projection import historical_receipts, render, TIME_NOTE, RECEIPT_NOTE, MAX_RECEIPTS, MAX_RECEIPT_CHARS

CONDITIONS = ('C', 'C-time', 'C-receipt')


class Reads:
    """Parent-process data-file read opens; excludes SQLite and verifier children."""
    def __init__(self, workspace):
        self.prefix = os.path.normcase(os.path.abspath(workspace)) + os.sep
        self.events = []
        self.active = False

    def hook(self, event, args):
        if not self.active or event != 'open' or not isinstance(args[0], (str, bytes)):
            return
        name = os.path.normcase(os.path.abspath(os.fsdecode(args[0])))
        if not name.startswith(self.prefix):
            return
        relative = name[len(self.prefix):]
        if relative.startswith('.agent_runtime' + os.sep):
            return
        mode, flags = args[1], args[2]
        access_mask = os.O_WRONLY | os.O_RDWR
        if (isinstance(mode, str) and ('r' in mode or '+' in mode)) or (mode is None and flags & access_mask != os.O_WRONLY):
            self.events.append(relative)


class ProjectedModel(MeasuredModel):
    def complete(self, messages, tools):
        started = time.perf_counter()
        before = len(self.reads.events)
        receipts, upper = historical_receipts(self.runtime.store.path, self.task_id, self.boundary,
                                             set(TASKS[self.args.task]['steps'][1][2]))
        projected, metadata = render(messages, self.args.condition, receipts)
        metadata.update({'call': self.local_calls+1, 'view_checkpoint_id': upper,
                         'boundary_checkpoint_id': self.boundary,
                         'projection_workspace_read_opens': len(self.reads.events)-before,
                         'projection_seconds': time.perf_counter()-started,
                         'ledger_read_transactions': 1})
        with (self.directory/'projection.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(metadata, ensure_ascii=False)+'\n')
        return super().complete(projected, tools)


def worker(args):
    workspace, directory = Path(args.workspace), Path(args.directory)
    args.mode, args.phase = 'live', 'C'
    model = ProjectedModel(args, directory, requests(directory.parent/'prefix'))
    reads = Reads(workspace)
    sys.addaudithook(reads.hook)
    runtime = Runtime(workspace, model, verified_subtask_dag=make_dag(args.task),
                      scoped_context_budget=SCOPED_BUDGET, tool_scope=ALLOWED,
                      approval_callback=lambda name, *_: name in ALLOWED, lease_ttl=100)
    model.runtime, model.reads = runtime, reads
    task = runtime.store.list_tasks()[0]
    model.task_id, model.boundary = task['task_id'], int(task['checkpoint_id'])
    started = time.perf_counter()
    reads.active = True
    try:
        result = runtime.resume(model.task_id)
        reads.active = False
        elapsed = time.perf_counter()-started
        trace = TraceReporter(runtime.store)
        trace.export_jsonl(model.task_id, directory/'trace.jsonl')
        save(directory/'result.json', {'result':dataclasses.asdict(result), 'trace':trace.summary(model.task_id),
              'seconds':elapsed, 'final_check':check(args.task, workspace),
              'parent_workspace_read_opens':len(reads.events), 'parent_read_paths':reads.events,
              'read_scope':'resume parent process only; excludes verifier child/import reads, setup, final evaluation and DB I/O'})
        return 0
    except Exception as exc:
        reads.active = False
        save(directory/'error.json', {'type':type(exc).__name__, 'message':str(exc)[:1000]})
        return 2


def analyze_cell(case, task, condition, repeat):
    directory = case/condition
    calls = requests(directory)
    projections = [json.loads(x) for x in (directory/'projection.jsonl').read_text(encoding='utf-8').splitlines()] if (directory/'projection.jsonl').exists() else []
    result = json.loads((directory/'result.json').read_text(encoding='utf-8')) if (directory/'result.json').exists() else {}
    trace_file = directory/'trace.jsonl'
    records = [json.loads(line) for line in trace_file.read_text(encoding='utf-8').splitlines()] if trace_file.exists() else []
    committed_tool_ids = {record['tool_use_id'] for record in records
                          if record.get('record_type') == 'operation' and record.get('state') == 'committed'}
    available, exposed = {}, {}
    decisions = [(call, projection) for call, projection in zip(calls, projections) if call.get('response') is not None]
    for call, projection in decisions:
        available.update({r['operation_id']:r for r in projection['eligible_receipts']})
        exposed.update({r['operation_id']:r for r in projection['exposed_receipts']})
    read_events = []
    for call, projection in decisions:
        # Bind reads to the most recent eligible write on that path at the
        # preceding decision point, ending at the next committed write.
        latest = {}
        for receipt in projection['eligible_receipts']:
            latest.setdefault(receipt['path'],receipt)
        visible = {r['operation_id'] for r in projection['exposed_receipts']}
        for block in call.get('response',{}).get('content',[]):
            path = str(block.get('input',{}).get('path','')).replace('\\','/')
            if path.startswith('./'):
                path=path[2:]
            if block.get('name') in ('write_file','edit_file'):
                if block.get('id') in committed_tool_ids:
                    latest.pop(path, None)
                continue
            if block.get('name') != 'read_file':
                continue
            if path in latest:
                receipt=latest[path]
                read_events.append({'call':call['call'],'path':path,'operation_id':receipt['operation_id'],
                                    'receipt_exposed':receipt['operation_id'] in visible})
    # An operation must have at least one subsequent model decision opportunity.
    denominator=len(available)
    numerator=len(read_events)
    observed_ops=len({r['operation_id'] for r in read_events})
    trace=result.get('trace',{})
    return {'repeat':repeat,'task':task,'condition':condition,
        'status':result.get('result',{}).get('status','process_error'),
        'final_pass':result.get('final_check',{}).get('passed',False),
        'input_tokens':sum(input_tokens(c) for c in calls),
        'output_tokens':sum(c.get('usage',{}).get('output_tokens',0) for c in calls),
        'missing_usage_calls':sum(not c.get('usage') for c in calls),
        'model_calls':len(calls),
        'tool_calls':sum(b.get('type')=='tool_use' for c in calls for b in c.get('response',{}).get('content',[])),
        'eligible_write_operations':denominator,'exposed_write_operations':len(exposed),
        'post_write_read_events':read_events,
        'reobservation_intensity':numerator/denominator if denominator else None,
        'reobserved_operation_fraction':observed_ops/denominator if denominator else None,
        'exposure_conditioned_intensity':sum(e['receipt_exposed'] for e in read_events)/len(exposed) if exposed else None,
        'seconds':result.get('seconds'),'parent_workspace_read_opens':result.get('parent_workspace_read_opens'),
        'projection_workspace_read_opens':sum(p['projection_workspace_read_opens'] for p in projections),
        'projection_seconds':sum(p['projection_seconds'] for p in projections),
        'ledger_read_transactions':len(projections),
        'operation_evidence_available':trace_file.exists(),
        'invariant_violations':trace.get('invariant_violation_count'),
        'verifier_runs':trace.get('verifier_run_count'),
        'verifier_non_authoritative':trace.get('non_authoritative_verifier_run_count')}


def supervise(args):
    root, source = Path(args.output).resolve(), Path(args.resume_from).resolve()
    root.mkdir(parents=True, exist_ok=False)
    from dotenv import dotenv_values
    config={**os.environ,**dotenv_values(args.env_file)}
    previous=json.loads((source/'manifest.json').read_text(encoding='utf-8'))
    if previous['model'] != config.get('MODEL_ID') or previous['endpoint'] != config.get('ANTHROPIC_BASE_URL'):
        raise ValueError('Frozen provider configuration mismatch')
    code_root=Path(__file__).resolve().parents[2]
    hashes={str(p.relative_to(code_root)):digest(p) for p in (code_root/'agent_runtime').rglob('*.py') if 'tests' not in p.parts}
    if hashes[str(Path('agent_runtime/experiments/bc_tasks.py'))] != previous['source_hashes'][str(Path('agent_runtime/experiments/bc_tasks.py'))]:
        raise ValueError('Frozen tasks changed')
    schedule=[]
    # Rotate three conditions; offset the second task. Each condition occupies
    # each position 3 or 4 times across the ten task/repetition blocks.
    for repeat in range(1,6):
        for offset,task in enumerate(TASKS):
            start=(repeat-1+offset)%3
            order=list(CONDITIONS[start:]+CONDITIONS[:start])
            schedule.extend({'repeat':repeat,'task':task,'condition':c} for c in order)
    save(root/'protocol.json',{'version':'historical-effect-pilot-v1','schedule':schedule,
         'source_hashes':hashes,'model':previous['model'],'endpoint':previous['endpoint'],
         'runtime_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
         'prefix_source':str(source),'time_note':TIME_NOTE,'receipt_note':RECEIPT_NOTE,
         'max_receipts':MAX_RECEIPTS,'max_receipt_chars':MAX_RECEIPT_CHARS,
         'scope_budget':SCOPED_BUDGET,'call_limit':CALL_LIMIT,'token_stop_threshold':TOKEN_LIMIT,
         'started_at':time.time(),'automatic_retries':0,
         'receipt_scope':'post-boundary related committed file effects with checkpointed tool results',
         'observed_at_definition':'tool finished_at recorded after post-hash check',
         'evidence_checkpoint_definition':'first post-boundary execution checkpoint containing tool result; not atomic commit id',
         'read_metric':'read requests linked to latest available committed write per path at decision time, before next committed write; may exceed one per operation',
         'read_scope':'parent workspace data-file read opens; excludes verifier children, DB and setup; projection reads and DB transactions separate',
         'analysis':'all 30 cells; descriptive per-task differences only; no hidden-test or generalization claim'})
    for relative in hashes:
        target=root/'frozen-source'/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(code_root/relative,target)
    rows=[]
    for slot in schedule:
        repeat,task,condition=slot['repeat'],slot['task'],slot['condition']
        assert all(digest(code_root/p)==h for p,h in hashes.items())
        case=root/f'repeat-{repeat}'/task
        case.mkdir(parents=True,exist_ok=True)
        if not (case/'prefix').exists():
            shutil.copytree(source/task/'prefix',case/'prefix')
        workspace=source/task/'workspace'
        snapshot=source/task/'snapshot'
        original=file_hashes(snapshot)
        db_hash=digest(snapshot/'.agent_runtime/runtime.db')
        clone_snapshot(snapshot,workspace)
        identities=verify_observations(workspace)
        directory=case/condition
        directory.mkdir()
        print(f'{repeat}/5 {task} {condition}',flush=True)
        command=[sys.executable,'-m','agent_runtime.experiments.receipt_pilot','--worker',
                 '--env-file',str(Path(args.env_file).resolve()),'--task',task,'--condition',condition,
                 '--workspace',str(workspace),'--directory',str(directory)]
        with (directory/'process.log').open('w',encoding='utf-8') as log:
            try:
                code=subprocess.run(command,stdout=log,stderr=log,timeout=600,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0).returncode
            except subprocess.TimeoutExpired:
                code=-1
        save(directory/'process.json',{'exit_code':code})
        row=analyze_cell(case,task,condition,repeat)
        row.update({'initial_file_identities':identities,'snapshot_database_sha256':db_hash,
                    'initial_file_hashes':original,'exit_code':code})
        rows.append(row)
        save(root/'results.json',rows)
        workspace.rename(case/f'{condition}-workspace')
        assert original==file_hashes(snapshot) and db_hash==digest(snapshot/'.agent_runtime/runtime.db')
        print(json.dumps({k:row[k] for k in ['status','final_pass','input_tokens','model_calls','reobservation_intensity']}),flush=True)
    save(root/'completed.json',{'finished_at':time.time()})
    return 0


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--worker',action='store_true')
    for name in ['output','resume-from','env-file','task','condition','workspace','directory']:
        p.add_argument('--'+name)
    args=p.parse_args()
    return worker(args) if args.worker else supervise(args)


if __name__=='__main__':
    raise SystemExit(main())
