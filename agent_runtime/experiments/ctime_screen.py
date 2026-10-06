"""32-cell engineering screen using frozen C/C-time recovery and scripted F3 prefixes."""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, ToolCall
from agent_runtime.runtime import Runtime
from agent_runtime.trace import TraceReporter
from .bc_pilot import ALLOWED, FullHistory, MeasuredModel, clone_snapshot, digest, file_hashes, input_tokens, requests, save, verify_observations
from .receipt_pilot import analyze_cell
from .receipt_projection import TIME_NOTE, historical_receipts, render
from .screen_tasks import TASKS, make_dag, prompt
from .screen_oracle import evaluate
from .screen_metrics import TaskPaths, canonical_path, costs, observation_metrics


def model_sequence(task, prefix=False):
    spec=TASKS[task]
    if prefix:
        groups=[spec['prefix_files']]
    else:
        groups=[]
        done=set(spec['prefix_files'])
        if task=='U1': groups.append(spec['prefix_files'])
        for _,_,paths,_ in spec['nodes']:
            group={p:spec['reference'][p] for p in paths if p in spec['reference'] and p not in done}
            groups.append(group)
            done.update(group)
    sequence=[]
    for index,files in enumerate(groups):
        sequence.append(ModelResponse(tool_calls=[ToolCall(f'{prefix}-{index}-r-{i}','read_file',{'path':p}) for i,p in enumerate(files)]))
        sequence.append(ModelResponse(tool_calls=[ToolCall(f'{prefix}-{index}-w-{i}','write_file',{'path':p,'content':v}) for i,(p,v) in enumerate(files.items())]))
        sequence.append(ModelResponse(text='Published stage contract implemented.\nSUBTASK_COMPLETE'))
    return sequence


class PrefixModel(ScriptedModel):
    def __init__(self, task, directory):
        super().__init__(model_sequence(task,True))
        self.directory=directory
    def complete(self,messages,tools):
        response=super().complete(messages,tools)
        with (self.directory/'model_requests.jsonl').open('a',encoding='utf-8') as f:
            f.write(json.dumps({'call':self.call_count,'messages':messages,'response':response.as_dict(),
                                'usage':{},'provider_tokens':False,'source':'scripted fixture prefix'},ensure_ascii=False)+'\n')
        return response


class ScreenModel(MeasuredModel):
    def __init__(self,args,directory,prior):
        if args.offline:
            self.args,self.directory=args,directory
            self.local_calls=0
            self.used_calls=len(prior)
            self.used_tokens=0
            self.provider=ScriptedModel(model_sequence(args.task))
            self.name='offline-reference-only'
            self.timeout=0.1
        else:
            super().__init__(args,directory,prior)
    def complete(self,messages,tools):
        receipts,upper=historical_receipts(self.runtime.store.path,self.task_id,self.boundary,
                                         TaskPaths(self.runtime.repo_root,TASKS[self.args.task]['files']))
        receipts=[{**r,'path':canonical_path(self.runtime.repo_root,r['path'])} for r in receipts]
        projected,metadata=render(messages,self.args.condition,[])
        metadata.update({'call':self.local_calls+1,'eligible_receipts':receipts,'exposed_receipts':[],
                         'view_checkpoint_id':upper,'boundary_checkpoint_id':self.boundary,
                         'projection_workspace_read_opens':0,'projection_seconds':0})
        with (self.directory/'projection.jsonl').open('a',encoding='utf-8') as f:
            f.write(json.dumps(metadata,ensure_ascii=False)+'\n')
        return super().complete(projected,tools)


def worker(args):
    root,directory=Path(args.workspace),Path(args.directory)
    if args.prefix:
        def fault(point,**context):
            if point=='verified_subtask_f3':
                save(directory/'fault.json',{'point':point,'context':context,'pid':os.getpid()})
                os._exit(73)
        runtime=Runtime(root,PrefixModel(args.task,directory),verified_subtask_dag=make_dag(args.task),
                        fault_injector=fault,approval_callback=lambda *_:True,lease_ttl=3)
        runtime.projector=FullHistory()
        result=runtime.run(prompt(args.task))
        save(directory/'unexpected-prefix-result.json',dataclasses.asdict(result))
        return 2
    args.mode,args.phase='live','C'
    model=ScreenModel(args,directory,requests(Path(args.source_prefix)))
    runtime=Runtime(root,model,verified_subtask_dag=make_dag(args.task),scoped_context_budget=16000,
                    tool_scope=ALLOWED,approval_callback=lambda name,*_:name in ALLOWED,lease_ttl=100 if not args.offline else 3)
    model.runtime=runtime
    task=runtime.store.list_tasks()[0]
    model.task_id,model.boundary=task['task_id'],task['checkpoint_id']
    started=time.perf_counter()
    try:
        result=runtime.resume(model.task_id)
        elapsed=time.perf_counter()-started
        reporter=TraceReporter(runtime.store)
        reporter.export_jsonl(model.task_id,directory/'trace.jsonl')
        events=json.loads((directory/'events.json').read_text(encoding='utf-8'))
        oracle=evaluate(args.task,root,directory/'trace.jsonl',events,result.status)
        save(directory/'result.json',{'result':dataclasses.asdict(result),'trace':reporter.summary(model.task_id),
                                     'seconds':elapsed,'final_check':oracle['correctness'],'oracle':oracle})
        return 0
    except Exception as exc:
        save(directory/'error.json',{'type':type(exc).__name__,'message':str(exc)[:1500]})
        return 2


def launch(args,task,workspace,directory,prefix=False,condition=None,source_prefix=None):
    directory.mkdir(exist_ok=True)
    cmd=[sys.executable,'-m','agent_runtime.experiments.ctime_screen','--worker','--task',task,
         '--workspace',str(workspace),'--directory',str(directory)]
    if prefix: cmd.append('--prefix')
    else: cmd+=['--condition',condition,'--source-prefix',str(source_prefix)]
    if args.env_file: cmd+=['--env-file',str(Path(args.env_file).resolve())]
    if args.offline: cmd.append('--offline')
    with (directory/'process.log').open('w',encoding='utf-8') as f:
        try:
            code=subprocess.run(cmd,stdout=f,stderr=f,timeout=600,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0).returncode
        except subprocess.TimeoutExpired: code=-1
    save(directory/'process.json',{'exit_code':code})
    return code


def apply_events(task,workspace):
    events=[]
    for path,content in TASKS[task].get('drift',{}).items():
        destination=workspace/path
        before=digest(destination)
        temporary=workspace/(path+'.external-temp')
        temporary.write_text(content,encoding='utf-8',newline='\n')
        os.replace(temporary,destination)  # Break hard link; never edit source inode.
        events.append({'path':path,'before_sha256':before,'after_sha256':digest(destination),
                       'observed_at':time.time(),'kind':'pre-resume external change'})
    return events


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('output','env-file','task','workspace','directory','condition','source-prefix'):
        parser.add_argument('--'+name)
    parser.add_argument('--worker',action='store_true')
    parser.add_argument('--prefix',action='store_true')
    parser.add_argument('--offline',action='store_true')
    args=parser.parse_args()
    if args.worker: return worker(args)
    if not args.offline and not args.env_file: parser.error('--env-file required for live runs')
    output=Path(args.output).resolve()
    output.mkdir(parents=True,exist_ok=False)
    code_root=Path(__file__).resolve().parents[2]
    source_hashes={str(p.relative_to(code_root)):digest(p) for p in (code_root/'agent_runtime').rglob('*.py') if 'tests' not in p.parts}
    schedule=[]
    repeats=1 if args.offline else 2
    for repeat in range(1,repeats+1):
        for index,task in enumerate(TASKS):
            order=['C','C-time'] if (index+repeat)%2 else ['C-time','C']
            schedule.extend({'task':task,'repeat':repeat,'condition':condition} for condition in order)
    manifest={'version':'ctime-screen-v1','offline':args.offline,'schedule':schedule,'source_hashes':source_hashes,
              'prefix':'scripted fixture history + real os._exit(73); zero model API calls',
              'runtime_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
              'time_note':TIME_NOTE,'call_budget':32,'scope_budget':16000,'token_stop_threshold':150000,
              'retries':0,'started_at':time.time(),'tasks':{k:{'category':v['category'],'nodes':[(n,d,list(p)) for n,d,p,_ in v['nodes']],
                'fixture_hashes':{p:hashlib.sha256(c.encode()).hexdigest() for p,c in v['files'].items()},
                'drift':v.get('drift',{})} for k,v in TASKS.items()}}
    if not args.offline:
        from dotenv import dotenv_values
        config={**os.environ,**dotenv_values(args.env_file)}
        manifest.update({'model':config.get('MODEL_ID'),'endpoint':config.get('ANTHROPIC_BASE_URL'),'temperature':'provider default'})
    save(output/'protocol.json',manifest)
    for relative in source_hashes:
        dst=output/'frozen-source'/relative; dst.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(code_root/relative,dst)
    available={}
    for task,spec in TASKS.items():
        case=output/'sources'/task
        workspace=case/'workspace'; workspace.mkdir(parents=True)
        for path,content in spec['files'].items(): (workspace/path).write_text(content,encoding='utf-8',newline='\n')
        print(f'Constructing F3 fixture {task}',flush=True)
        prefix=case/'prefix'
        code=launch(args,task,workspace,prefix,True)
        if code!=73:
            available[task]=False
            continue
        if json.loads((prefix/'fault.json').read_text())['point']!='verified_subtask_f3': raise ValueError('wrong fault')
        with sqlite3.connect(workspace/'.agent_runtime/runtime.db') as conn:
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            expires=conn.execute('SELECT max(expires_at) FROM leases').fetchone()[0]
        conn.close()
        while time.time()<=expires: time.sleep(0.2)
        workspace.rename(case/'snapshot')
        available[task]=True
    results=[]
    for slot in schedule:
        task,repeat,condition=slot['task'],slot['repeat'],slot['condition']
        assert all(digest(code_root/p)==h for p,h in source_hashes.items())
        if not available[task]:
            results.append({**slot,'status':'prefix_unavailable','task_success':False,'input_tokens':None,'skipped_required_verification':None})
            save(output/'results.json',results); continue
        source=output/'sources'/task; snapshot=source/'snapshot'; workspace=source/'workspace'
        case=output/f'repeat-{repeat}'/task; directory=case/condition
        directory.mkdir(parents=True)
        expected_hashes=file_hashes(snapshot); db_hash=digest(snapshot/'.agent_runtime/runtime.db')
        clone_snapshot(snapshot,workspace)
        assert digest(workspace/'.agent_runtime/runtime.db')==db_hash
        identities=verify_observations(workspace)
        events=apply_events(task,workspace)
        post_event_hashes=file_hashes(workspace)
        save(directory/'events.json',events)
        print(f'{repeat}/{repeats} {task} {condition}',flush=True)
        code=launch(args,task,workspace,directory,False,condition,source/'prefix')
        row=analyze_cell(case,task,condition,repeat)
        outcome=json.loads((directory/'result.json').read_text(encoding='utf-8')) if (directory/'result.json').exists() else {}
        calls=requests(directory)
        projections=[json.loads(x) for x in (directory/'projection.jsonl').read_text(encoding='utf-8').splitlines()] if (directory/'projection.jsonl').exists() else []
        records=[json.loads(x) for x in (directory/'trace.jsonl').read_text(encoding='utf-8').splitlines()] if (directory/'trace.jsonl').exists() else []
        row.update(costs(calls,args.offline))
        row.update(observation_metrics(calls,projections,records,workspace))
        if not outcome:
            row.update({'input_tokens':None,'output_tokens':None,'model_calls_lower_bound':len(calls),
                        'model_calls':None,'cost_complete':False})
        if not records:
            row.update({'reobservation_intensity':None,'reobserved_operation_fraction':None,'observation_metrics_valid':False})
        else: row['observation_metrics_valid']=True
        row.update({'category':TASKS[task]['category'],'initial_file_hashes':expected_hashes,'initial_file_identities':identities,
                    'post_event_file_hashes':post_event_hashes,
                    'snapshot_database_sha256':db_hash,'exit_code':code,
                    'task_success':outcome.get('oracle',{}).get('task_success',False),
                    'skipped_required_verification':outcome.get('oracle',{}).get('skipped_required_verification'),
                    'unresolved_obligations':outcome.get('oracle',{}).get('unresolved'),
                    'error':outcome.get('result',{}).get('error'),
                    'framework_refresh_count':outcome.get('trace',{}).get('verified_subtask_refresh_count'),
                    'offline_reference':args.offline})
        results.append(row); save(output/'results.json',results)
        workspace.rename(case/f'{condition}-workspace')
        assert file_hashes(snapshot)==expected_hashes and digest(snapshot/'.agent_runtime/runtime.db')==db_hash
        print(json.dumps({k:row[k] for k in ['task_success','status','input_tokens','model_calls','skipped_required_verification']}),flush=True)
    save(output/'completed.json',{'finished_at':time.time()})
    return 0


if __name__=='__main__': raise SystemExit(main())
