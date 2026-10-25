"""Offline task read-attempt budgeting across actual controller process death."""
import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

from agent_runtime import Runtime
from agent_runtime.completion import CompletionContract
from agent_runtime.container_reads import DockerReadBridge
from agent_runtime.docker_workspace import CommandProbe, DockerWorkspaceBackend, digest, load_tmpfs_archive
from agent_runtime.experiments.container_read_resume_probe import CONTENT, READ_PATH, RecordingModel
from agent_runtime.models import ModelResponse, ToolCall, VerifiedSubtaskConfig, VerifiedSubtaskDAGConfig, VerifierResult
from agent_runtime.read_budget import ControllerReadBudgetExhausted
from agent_runtime.recovery_advisor import RecoveryAdvisor
from agent_runtime.store import EventStore, InvariantViolation
from agent_runtime.trace import TraceReporter


@dataclass(frozen=True)
class Case:
    point: str
    limit: int
    attempts: int
    status: str
    child_execs: int
    parent_execs: int
    parent_models: int


CASES = {
    'unpersisted_limit1': Case('after_tool_effect_before_persist', 1, 1, 'failed', 1, 0, 0),
    'unpersisted_limit2': Case('after_tool_effect_before_persist', 2, 2, 'completed', 1, 1, 1),
    'persisted_limit1': Case('after_tool_persist', 1, 1, 'completed', 1, 0, 1),
    'started_limit1': Case('after_tool_started', 1, 1, 'failed', 0, 0, 0),
    'denial_saved_limit1': Case('budget_record_saved', 1, 1, 'failed', 1, 0, 0),
    'failure_mid_limit1': Case('contract_failure_after_checkpoint', 1, 1, 'failed', 1, 0, 0),
}


def save(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, default=str)
        stream.write('\n')


def make_runtime(root, model, bridge, limit, *, fault=None):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    policy = root/'policy.yaml'
    if not policy.exists():
        policy.write_text('rules:\n  - effect: allow\n    tools: [container_read_file, container_find_files, container_search_symbols]\n', encoding='utf-8')
    def verify(context):
        store = EventStore(Path(context.repo_root)/'.agent_runtime/runtime.db')
        call = store.get_tool_call(context.task_id, 'read')
        valid = bool(call and call['status'] == 'succeeded' and json.loads(call['output']).get('sha256') == digest(CONTENT))
        return VerifierResult('pass' if valid else 'fail', 'Historical fixture read goal', [])
    node = VerifiedSubtaskConfig('final', 'Observe fixture', 'Durable bound read observation', (),
        'read-budget-fixture', '1', 'read-budget-fixture', verify, digest(Path(__file__).read_bytes()), max_turns=6)
    return Runtime(root, model, policy_path=policy, verified_subtask_dag=VerifiedSubtaskDAGConfig([node]),
        completion_contract=CompletionContract('verified_task', 1), recovery_advisor=RecoveryAdvisor(),
        tool_setup=bridge.install, tool_scope=bridge.names, fault_injector=fault, lease_ttl=10,
        controller_read_attempt_limit=limit)


def read_response():
    return ModelResponse(tool_calls=[ToolCall('read', 'container_read_file', {'path': READ_PATH})])


def controller(config, phase, *, changed_limit=False):
    from agent_runtime.experiments.docker_backend_probe import IMAGE, PYTHON, local_client
    root = Path(config['directory'])
    case = CASES[config['case']]
    client = local_client()
    try:
        backend = DockerWorkspaceBackend(client=client, source_id=config['source'], session=config['session'],
            image=IMAGE, workspace_path='/workspace', instance_id='offline-read-budget-fixture',
            base_commit=config['base_commit'], base_archive=config['base'], base_archive_sha256=config['base_sha256'],
            probes=[CommandProbe((PYTHON, '-I', '-c', "print('unused')"), digest(b'unused\n'))],
            output=root/(phase+'-backend'))
        bridge = DockerReadBridge(backend, root/(phase+'-reads'), max_calls=1)
        values = [read_response(), ModelResponse(text='Observed fixture; no repair')] if phase == 'child' else (
            [ModelResponse(text='Observed fixture; no repair')] if case.parent_models else [])
        if phase == 'child' and config['case'] in {'denial_saved_limit1', 'failure_mid_limit1'}:
            values = [ModelResponse(tool_calls=[read_response().tool_calls[0],
                ToolCall('denied', 'container_read_file', {'path': READ_PATH})])]
        model = RecordingModel(values, root/(phase+'-model'))
        def fault(point, **context):
            if point == case.point:
                save(root/'crash.json', {'point': point, 'pid': os.getpid(), 'exit_code': 73, **context})
                os._exit(73)
        runtime = make_runtime(root/'controller', model, bridge, case.limit+1 if changed_limit else case.limit,
                               fault=fault if phase == 'child' else None)
        if phase == 'child' and case.point == 'budget_record_saved':
            update = runtime.store.update_tool_call
            def save_then_exit(*args, **fields):
                update(*args, **fields)
                if fields.get('error') == ControllerReadBudgetExhausted.code:
                    fault('budget_record_saved', task_id=args[0])
            runtime.store.update_tool_call = save_then_exit
        backend.heartbeat = runtime.heartbeat_verification
        if phase == 'child':
            runtime.run('Offline read goal under a frozen task-local attempt cap')
            raise AssertionError('Process-death point not reached')
        task = runtime.store.list_tasks()[0]['task_id']
        if changed_limit:
            try:
                runtime.resume(task)
            except InvariantViolation:
                assert model.call_count == bridge.calls == 0
                save(root/'changed-limit.json', {'rejected_before_model_and_adapter': True})
                return
            raise AssertionError('Budget expansion survived frozen-profile validation')
        result = runtime.resume(task)
        runtime.store.assert_invariants(task)
        calls = runtime.store.list_tool_calls(task)
        assert result.status == case.status and sum(c['execution_attempts'] for c in calls) == case.attempts
        assert runtime.model.call_count == case.parent_models and bridge.calls == case.parent_execs
        if result.status == 'failed':
            assert json.loads(result.error)['code'] == ControllerReadBudgetExhausted.code
            assert runtime.store.list_verified_subtask_checkpoints(task) == []
            assert not [e for e in runtime.store.list_events(task) if e['type'] == 'task_completed']
            item = runtime.store.get_plan_item(task, 'final')
            assert item['status'] == 'failed'
        else:
            assert len(runtime.store.list_verified_subtask_checkpoints(task)) == 1
        assert runtime.store.list_operations(task) == []
        TraceReporter(runtime.store).export_jsonl(task, root/'trace.jsonl')
        save(root/'runtime-summary.json', TraceReporter(runtime.store).summary(task))
        save(root/'resume.json', {'result': result.__dict__, 'model_calls': model.call_count,
            'bridge_calls': bridge.calls, 'attempts': sum(c['execution_attempts'] for c in calls), 'pid': os.getpid()})
    finally:
        client.close()


def run_case(output, name, base, commit):
    from agent_runtime.experiments.docker_backend_probe import IMAGE, files_tar, local_client
    root = output/name
    root.mkdir()
    client = local_client()
    source, session = None, uuid.uuid4().hex
    state = {'case': name, 'status': 'FAILED', 'protocol_passed': False, 'model_api_calls': 0,
             'official_harness_runs': 0, 'agent_patch_generated': False}
    try:
        source = client.containers.run(IMAGE, command=['sleep', '300'], detach=True, user='65534:65534',
            network_mode='none', read_only=True, cap_drop=['ALL'], security_opt=['no-new-privileges:true'],
            mem_limit='256m', nano_cpus=1000000000, pids_limit=64, working_dir='/workspace',
            labels={'org.lcc.backend-session': session}, environment={'HOME': '/tmp'}, tmpfs={
                '/workspace': 'rw,nosuid,nodev,uid=65534,gid=65534,mode=0700,size=32m',
                '/tmp': 'rw,nosuid,nodev,uid=65534,gid=65534,mode=1777,size=16m'})
        state['source_id'] = source.id
        load_tmpfs_archive(client, source.id, '/workspace', files_tar({READ_PATH: CONTENT}))
        sentinel = root/'controller'/READ_PATH
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text('HOST_BUDGET_SENTINEL', encoding='utf-8')
        config = {'case': name, 'directory': str(root), 'source': source.id, 'session': session,
                  'base': str(base), 'base_commit': commit, 'base_sha256': digest(base.read_bytes())}
        save(root/'config.json', config)
        child = subprocess.run([sys.executable, '-B', '-m', __spec__.name, '--child', str(root/'config.json')],
                               capture_output=True, timeout=30)
        (root/'child-stdout.log').write_bytes(child.stdout)
        (root/'child-stderr.log').write_bytes(child.stderr)
        assert child.returncode == 73, child.stderr.decode(errors='replace')
        db = root/'controller/.agent_runtime/runtime.db'
        with sqlite3.connect(db) as connection:
            assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            old = connection.execute('SELECT owner_id,fencing_token,expires_at FROM leases').fetchone()
            assert connection.execute('SELECT SUM(execution_attempts) FROM tool_calls').fetchone()[0] == 1
        if name == 'unpersisted_limit1':
            controller(config, 'changed-limit', changed_limit=True)
        deadline = time.monotonic()+15
        while time.time() <= old[2]:
            if time.monotonic() > deadline:
                raise TimeoutError('Natural lease expiry exceeded fixture bound')
            time.sleep(.03)
        controller(config, 'parent')
        case = CASES[name]
        for phase, expected in [('child', case.child_execs), ('parent', case.parent_execs)]:
            records = [json.loads(p.read_text()) for p in (root/(phase+'-reads')).glob('*.json')]
            assert len(records) == expected and all(r.get('exec_id') and r['binding']['source_id'] == source.id for r in records)
        with sqlite3.connect(db) as connection:
            new = connection.execute('SELECT owner_id,fencing_token,expires_at FROM leases').fetchone()
        assert new[1] > old[1] and new[0] != old[0]
        requests = [json.loads(p.read_text()) for phase in ('child', 'parent')
                    for p in (root/(phase+'-model')).glob('*.json')]
        assert len(requests) == 1+case.parent_models and 'HOST_BUDGET_SENTINEL' not in json.dumps(requests)
        assert sentinel.read_text(encoding='utf-8') == 'HOST_BUDGET_SENTINEL'
        resumed = json.loads((root/'resume.json').read_text())
        assert resumed['pid'] != json.loads((root/'crash.json').read_text())['pid']
        state.update(protocol_passed=True, limit=case.limit, attempts=case.attempts,
            child_execs=case.child_execs, parent_execs=case.parent_execs, parent_models=case.parent_models,
            old_fence=old[1], new_fence=new[1], runtime_status=resumed['result']['status'])
    except Exception as exc:
        state.update(error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        cleanup_errors = []
        try:
            if source is not None:
                source.remove(force=True)
        except Exception as exc:
            cleanup_errors.append({'stage': 'remove', 'error': str(exc)})
        try:
            remaining = client.containers.list(all=True, filters={'label': 'org.lcc.backend-session='+session})
            state['owned_sources_remaining'] = len(remaining)
            if remaining:
                cleanup_errors.append({'stage': 'inventory', 'error': 'Owned containers remain'})
        except Exception as exc:
            cleanup_errors.append({'stage': 'inventory', 'error': str(exc)})
        try:
            client.close()
        except Exception as exc:
            cleanup_errors.append({'stage': 'client_close', 'error': str(exc)})
        state['cleanup_errors'] = cleanup_errors
        if state['protocol_passed'] and not cleanup_errors:
            state['status'] = 'PASS_DURABLE_TASK_READ_BUDGET'
        save(root/'result.json', state)
    assert state['status'] == 'PASS_DURABLE_TASK_READ_BUDGET'
    print(json.dumps(state), flush=True)
    return state


def main(output, selected=None):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    repo = Path(__file__).resolve().parents[2]
    index = {p.relative_to(repo).as_posix(): digest(p.read_bytes()) for p in (repo/'agent_runtime').rglob('*.py')}
    save(output/'source-index.json', index)
    seed = output/'trusted-base'
    (seed/'src').mkdir(parents=True)
    (seed/READ_PATH).write_bytes(CONTENT)
    for args in [['git', 'init'], ['git', 'add', READ_PATH], ['git', '-c', 'user.name=Fixture',
                 '-c', 'user.email=fixture@local.invalid', 'commit', '-m', 'budget fixture']]:
        subprocess.run(args, cwd=seed, check=True, capture_output=True, timeout=10)
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=seed, text=True).strip()
    base = output/'trusted-base.tar'
    base.write_bytes(subprocess.check_output(['git', 'archive', 'HEAD'], cwd=seed, timeout=10))
    cases = [run_case(output, name, base, commit) for name in (selected or CASES)]
    assert all(digest((repo/path).read_bytes()) == value for path, value in index.items())
    save(output/'summary.json', {'status': 'PASS_DURABLE_TASK_READ_BUDGET', 'cases': cases,
                                'model_api_calls': 0, 'official_harness_runs': 0})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--child', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--case', choices=CASES, action='append')
    args = parser.parse_args()
    if args.child:
        controller(json.loads(args.child.read_text(encoding='utf-8')), 'child')
    elif args.output:
        main(args.output, args.case)
    else:
        parser.error('--output is required without --child')
