"""Offline read replay after actual controller death; no model API or SWE grading."""
import argparse
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
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, ToolCall, VerifiedSubtaskConfig, VerifiedSubtaskDAGConfig, VerifierResult
from agent_runtime.recovery_advisor import RecoveryAdvisor
from agent_runtime.store import EventStore, InvariantViolation
from agent_runtime.trace import TraceReporter


CONTENT = b'def wanted_symbol(): return 7\n'
READ_PATH = 'src/actual.py'
LEASE_TTL = 10
# point, persisted response count at death, child execs, recovery execs, recovery responses
CASES = {
    'response_saved': ('after_model_response', 1, 0, 1, 1),
    'tool_started': ('after_tool_started', 1, 0, 1, 1),
    'read_returned': ('after_tool_effect_before_persist', 1, 1, 1, 1),
    'result_saved': ('after_tool_persist', 1, 1, 0, 1),
    'stop_saved': ('after_model_response', 2, 1, 0, 0),
    'advice_saved': ('recovery_advice_after_result', 1, 1, 1, 2),
    'repair_stop_saved': ('after_model_response', 2, 1, 0, 0),
}


def save(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, default=str)
        stream.write('\n')


def responses(case, *, recovery=False):
    read = ModelResponse(tool_calls=[ToolCall('read', 'container_read_file', {'path': READ_PATH})])
    stop = ModelResponse(text='Read goal observed; no repair was performed')
    if recovery:
        return ([read, stop] if case == 'advice_saved' else [stop]) if CASES[case][4] else []
    prefix = [ModelResponse(tool_calls=[ToolCall('missing', 'container_read_file', {'path': 'src/guess.py'})])]
    return (prefix if case == 'advice_saved' else []) + [read, stop]


class RecordingModel(ScriptedModel):
    """Persist fixture requests separately from Runtime's own request ledger."""
    def __init__(self, values, output):
        super().__init__(values)
        self.output = Path(output)
        self.output.mkdir()

    def complete(self, messages, tools):
        response = super().complete(messages, tools)
        save(self.output/f'{self.call_count:03d}.json',
             {'messages': messages, 'tools': tools, 'response': response.as_dict(),
              'usage_kind': 'scripted; no real token consumption'})
        return response


def configured_runtime(root, model, bridge, case, *, verifier=None, fault=None, scope=None, bind=True):
    root = Path(root)
    policy = root/'policy.yaml'
    if not policy.exists():
        root.mkdir(parents=True, exist_ok=True)
        policy.write_text('rules:\n  - effect: allow\n    tools: [container_read_file, container_find_files, container_search_symbols]\n', encoding='utf-8')
    repair = case == 'repair_stop_saved'
    if verifier is None:
        def verifier(context):
            store = EventStore(Path(context.repo_root)/'.agent_runtime/runtime.db')
            call = store.get_tool_call(context.task_id, 'read')
            valid = bool(call and call['status'] == 'succeeded'
                         and json.loads(call['output']).get('sha256') == digest(CONTENT))
            return VerifierResult('pass' if valid else 'fail', 'Historical fixture read observation', [])
    node = VerifiedSubtaskConfig('final', 'Observe fixture bytes' if not repair else 'Prepare repair',
        'Bound read observation' if not repair else 'Applicable patch with validation',
        (verifier.evidence_path,) if repair else (), 'read-resume-fixture', '1', 'read-resume-fixture',
        verifier, digest(Path(__file__).read_bytes()), max_turns=4)
    return Runtime(root, model, policy_path=policy, lease_ttl=LEASE_TTL,
        verified_subtask_dag=VerifiedSubtaskDAGConfig([node]),
        completion_contract=CompletionContract('candidate_patch' if repair else 'verified_task', 1),
        recovery_advisor=RecoveryAdvisor(), tool_setup=bridge.install if bind else None,
        tool_scope=bridge.names if scope is None else scope, fault_injector=fault)


def controller(config, phase, *, negative=None):
    # Docker is an optional experiment dependency, never needed to collect unit tests.
    from agent_runtime.experiments.docker_backend_probe import IMAGE, PYTHON, local_client
    root = Path(config['directory'])
    client = local_client()
    try:
        backend = DockerWorkspaceBackend(client=client, source_id=config['source'], session=config['session'],
            image=IMAGE, workspace_path='/workspace', instance_id='offline-read-resume-fixture',
            base_commit=config['base_commit'], base_archive=config['base'], base_archive_sha256=config['base_sha256'],
            probes=[CommandProbe((PYTHON, '-I', '-c', "print('UNEXPECTED_VALIDATION')"), digest(b'UNEXPECTED_VALIDATION\n'))],
            output=root/(phase+'-backend'))
        bridge = DockerReadBridge(backend, root/(phase+'-reads'))
        if negative == 'binding':
            bridge.identity['instance'] = 'different-fixture-instance'
        model = RecordingModel(responses(config['case'], recovery=phase != 'child'), root/(phase+'-model'))
        runtime = None
        def fault(point, **context):
            expected_point, count, *_ = CASES[config['case']]
            if point == expected_point and len(runtime.store.list_model_calls(context['task_id'])) == count:
                save(root/'crash-observation.json', {'point': point, 'pid': os.getpid(), 'exit': 73, **context})
                os._exit(73)  # Intentionally bypass finally blocks, lease release and cleanup.
        runtime = configured_runtime(root/'controller', model, bridge, config['case'],
            verifier=backend.verifier() if config['case'] == 'repair_stop_saved' else None,
            fault=fault if phase == 'child' else None, bind=negative != 'unbound',
            scope=bridge.names[:1] if negative == 'scope' else None)
        backend.heartbeat = runtime.heartbeat_verification
        if phase == 'child':
            runtime.run('Offline fixture: observe bound file; read capability is not repair completion')
            raise AssertionError('Requested process-death boundary was not reached')
        task = runtime.store.list_tasks()[0]['task_id']
        if negative:
            try:
                runtime.resume(task)
            except InvariantViolation as exc:
                assert 'profile missing or changed' in str(exc)
                assert model.call_count == 0 and bridge.calls == 0
                save(root/(phase+'-result.json'), {'status': 'REJECTED_BEFORE_MODEL_AND_EXEC', 'error': str(exc)})
                return
            raise AssertionError('Mismatched/unbound profile resumed')
        result = runtime.resume(task)
        runtime.store.assert_invariants(task)
        TraceReporter(runtime.store).export_jsonl(task, root/'trace.jsonl')
        save(root/'runtime-summary.json', TraceReporter(runtime.store).summary(task))
        save(root/'resume-result.json', {'result': result.__dict__, 'model_calls': model.call_count,
            'read_calls': bridge.calls, 'pid': os.getpid()})
    finally:
        client.close()


def lease_row(database):
    with sqlite3.connect(database) as connection:
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        return connection.execute('SELECT owner_id,fencing_token,expires_at FROM leases').fetchone()


def finalize_case(root, state, client, source, session):
    """Seal overall success only after cleanup, retaining any original protocol error."""
    errors = []
    def record(stage, exc):
        errors.append({'stage': stage, 'error_type': type(exc).__name__, 'error': str(exc)})
    try:
        if source is not None:
            source.remove(force=True)
    except Exception as exc:
        record('remove_source', exc)
    try:
        remaining = client.containers.list(all=True, filters={'label': 'org.lcc.backend-session='+session})
        state['owned_sources_remaining'] = len(remaining)
        if remaining:
            record('inventory', RuntimeError('Owned source containers remain'))
    except Exception as exc:
        state['owned_sources_remaining'] = None
        record('inventory', exc)
    try:
        client.close()
    except Exception as exc:
        record('close_client', exc)
    state.update(fixture_owner_cleanup=not errors, cleanup_errors=errors)
    if state.get('protocol_status') == 'PASS':
        state['status'] = 'CLEANUP_FAILED' if errors else 'PASS_OFFLINE_READ_PROCESS_RECOVERY'
    save(root/'result.json', state)


def run_case(output, case, base, commit):
    from agent_runtime.experiments.docker_backend_probe import IMAGE, files_tar, local_client
    root = output/case
    root.mkdir()
    client = local_client()
    source = None
    state = {'case': case, 'status': 'FAILED', 'protocol_status': 'FAILED', 'lease_ttl_seconds': LEASE_TTL,
             'real_model_api_calls': 0, 'real_model_tokens': 0,
             'official_harness_runs': 0, 'agent_patch_generated': False}
    session = uuid.uuid4().hex
    try:
        source = client.containers.run(IMAGE, command=['sleep', '300'], detach=True, user='65534:65534',
            network_mode='none', read_only=True, cap_drop=['ALL'], security_opt=['no-new-privileges:true'],
            mem_limit='256m', nano_cpus=1000000000, pids_limit=64, working_dir='/workspace',
            labels={'org.lcc.backend-session': session}, environment={'HOME': '/tmp'}, tmpfs={
                '/workspace': 'rw,nosuid,nodev,uid=65534,gid=65534,mode=0700,size=32m',
                '/tmp': 'rw,nosuid,nodev,uid=65534,gid=65534,mode=1777,size=16m'})
        state['source_id'] = source.id
        load_tmpfs_archive(client, source.id, '/workspace', files_tar({READ_PATH: CONTENT}))
        host = root/'controller'/READ_PATH
        host.parent.mkdir(parents=True)
        host.write_text('HOST_SENTINEL_DO_NOT_READ', encoding='utf-8')
        config = {'directory': str(root), 'source': source.id, 'session': session, 'case': case,
                  'base': str(base), 'base_commit': commit, 'base_sha256': digest(base.read_bytes())}
        save(root/'config.json', config)
        child = subprocess.run([sys.executable, '-B', '-m', __spec__.name, '--child', str(root/'config.json')],
                               capture_output=True, timeout=30)
        (root/'child-stdout.log').write_bytes(child.stdout)
        (root/'child-stderr.log').write_bytes(child.stderr)
        assert child.returncode == 73, child.stderr.decode(errors='replace')
        db = root/'controller/.agent_runtime/runtime.db'
        old = lease_row(db)
        store = EventStore(db)
        task = store.list_tasks()[0]['task_id']
        assert store.get_task(task)['status'] not in {'completed', 'failed'}
        assert store.list_verified_subtask_checkpoints(task) == []
        save(root/'before-resume.json', {'lease': old, 'task': store.get_task(task),
            'tools': store.list_tool_calls(task), 'model_calls': store.list_model_calls(task)})
        deadline = time.monotonic()+LEASE_TTL+5
        while time.time() <= old[2]:
            if time.monotonic() > deadline:
                raise TimeoutError('Dead controller lease did not expire within fixture bound')
            time.sleep(.03)
        if case == 'response_saved':
            for negative in ('binding', 'unbound', 'scope'):
                controller(config, 'reject-'+negative, negative=negative)
                assert lease_row(db) == old  # Profile rejection precedes acquisition, not merely exec.
        controller(config, 'recovery')
        new = lease_row(db)
        assert new[1] > old[1] and new[0] != old[0]
        resumed = json.loads((root/'resume-result.json').read_text(encoding='utf-8'))
        assert resumed['pid'] != json.loads((root/'crash-observation.json').read_text())['pid']
        _, count, child_execs, recovery_execs, recovery_models = CASES[case]
        for phase, expected in [('child', child_execs), ('recovery', recovery_execs)]:
            records = [json.loads(p.read_text()) for p in sorted((root/(phase+'-reads')).glob('*.json'))]
            assert len(records) == expected
            assert all(r['binding']['source_id'] == source.id and r.get('exec_id') for r in records)
        assert resumed['model_calls'] == recovery_models and resumed['read_calls'] == recovery_execs
        assert len(store.list_model_calls(task)) == count+recovery_models
        assert all(c['status'] == 'succeeded' for c in store.list_model_calls(task))
        calls = store.list_tool_calls(task)
        assert len(calls) == (2 if case == 'advice_saved' else 1)
        assert calls[-1]['status'] == 'succeeded' and json.loads(calls[-1]['output'])['sha256'] == digest(CONTENT)
        assert all(c['effect'] == 'read_only' for c in calls) and store.list_operations(task) == []
        requests = [json.loads(p.read_text()) for phase in ('child', 'recovery')
                    for p in sorted((root/(phase+'-model')).glob('*.json'))]
        assert 'HOST_SENTINEL_DO_NOT_READ' not in json.dumps(requests)
        assert host.read_text(encoding='utf-8') == 'HOST_SENTINEL_DO_NOT_READ'
        repair = case == 'repair_stop_saved'
        assert resumed['result']['status'] == ('failed' if repair else 'completed')
        if repair:
            assert json.loads(resumed['result']['error'])['code'] == 'UNRESOLVED_STOP_LIMIT'
            assert store.list_verified_subtask_checkpoints(task) == []
            assert not [e for e in store.list_events(task) if e['type'] == 'task_completed']
            runs = store.list_verifier_runs(task)
            assert len(runs) == 1 and runs[0]['status'] == 'fail' and not runs[0]['authoritative']
            assert 'Missing or non-applicable candidate patch' in runs[0]['summary']
            assert not list((root/'recovery-backend').glob('*-validation.json'))
        else:
            assert len(store.list_verified_subtask_checkpoints(task)) == 1
        if case == 'advice_saved':
            assert calls[0]['error'] == 'PATH_NOT_FOUND'
            assert 'suggested_actions' in calls[0]['output']
            assert 'PATH_NOT_FOUND' in json.dumps(requests[-2]['messages'])
        store.assert_invariants(task)
        state.update(protocol_status='PASS', child_exit=73,
            child_model_responses=count, recovery_model_responses=recovery_models,
            child_container_exec_reads=child_execs, recovery_container_exec_reads=recovery_execs,
            old_fence=old[1], new_fence=new[1], runtime_result=resumed['result'],
            replay_semantics='Unpersisted reads may replay; persisted results are reused; not exactly-once',
            completion_claim='Historical read goal only' if not repair else 'No-patch repair remains unresolved')
    except Exception as exc:
        state.update(error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        finalize_case(root, state, client, source, session)
    if not state['fixture_owner_cleanup']:
        raise RuntimeError('Fixture cleanup failed: '+json.dumps(state['cleanup_errors']))
    print(json.dumps({'case': case, 'status': state['status']}), flush=True)
    return state


def main(output, selected=None):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    repo = Path(__file__).resolve().parents[2]
    source_index = {str(p.relative_to(repo)): digest(p.read_bytes()) for p in (repo/'agent_runtime').rglob('*.py')}
    save(output/'source-index.json', source_index)
    seed = output/'trusted-base'
    (seed/'src').mkdir(parents=True)
    (seed/READ_PATH).write_bytes(CONTENT)
    for command in [['git', 'init'], ['git', 'add', READ_PATH], ['git', '-c', 'user.name=Fixture',
                    '-c', 'user.email=fixture@local.invalid', 'commit', '-m', 'read resume fixture base']]:
        subprocess.run(command, cwd=seed, check=True, capture_output=True, timeout=10)
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=seed, text=True).strip()
    base = output/'trusted-base.tar'
    base.write_bytes(subprocess.check_output(['git', 'archive', 'HEAD'], cwd=seed, timeout=10))
    cases = [run_case(output, case, base, commit) for case in (selected or CASES)]
    assert all(digest((repo/path).read_bytes()) == value for path, value in source_index.items())
    save(output/'summary.json', {'status': 'PASS_OFFLINE_READ_PROCESS_RECOVERY', 'cases': cases,
        'real_model_api_calls': 0, 'real_model_tokens': 0, 'official_harness_runs': 0, 'agent_patch_generated': False})


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
