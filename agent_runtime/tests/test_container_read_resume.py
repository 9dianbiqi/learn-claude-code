"""Controller-tool replay at Runtime persistence boundaries; Docker evidence is separate."""
from contextlib import nullcontext
import json
from types import SimpleNamespace

import pytest

from agent_runtime.container_reads import DockerReadBridge
from agent_runtime.docker_workspace import digest
from agent_runtime.experiments.container_read_resume_probe import (
    CASES, CONTENT, READ_PATH, configured_runtime, responses, finalize_case,
)
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.runtime import InjectedCrash
from agent_runtime.workspace_verifier import WorkspaceSnapshot, WorkspaceVerifier


def bound_bridge(tmp_path, phase):
    backend = SimpleNamespace(source_id='owned-source', session='fixed-session', image='sha256:'+'a'*64,
        engine_id='fixed-engine', path='/workspace', instance_id='fixture', base_commit='fixed-base')
    bridge = DockerReadBridge(backend, tmp_path/phase)
    dispatches = []
    def request(value, record):
        dispatches.append(value)
        if value['args']['path'] == 'src/guess.py':
            return {'ok': False, 'error_type': 'PATH_NOT_FOUND', 'path': 'src/guess.py', 'nonce': value['nonce']}
        return {'ok': True, 'nonce': value['nonce'],
                'result': {'path': READ_PATH, 'text': CONTENT.decode(), 'sha256': digest(CONTENT)}}
    bridge._request = request
    return bridge, dispatches


def no_patch_verifier():
    def unexpected_validation(snapshot):
        raise AssertionError('Empty patch must fail before validation')
    return WorkspaceVerifier(goal='candidate_patch', workspace_id='owned-source', instance_id='fixture',
        base_commit='fixed-base', validator_id='unused', freeze=nullcontext,
        capture=lambda: WorkspaceSnapshot('owned-source', 'fixture', 'fixed-base', digest(CONTENT), '', True),
        validate=unexpected_validation)


@pytest.mark.parametrize('case', CASES)
def test_bound_read_replay_and_completion_use_existing_durable_ledger(tmp_path, case):
    root = tmp_path/'controller'
    host = root/READ_PATH
    host.parent.mkdir(parents=True)
    host.write_text('HOST_SENTINEL_DO_NOT_READ')
    first, first_dispatches = bound_bridge(tmp_path, 'first')
    point, count, before_reads, after_reads, after_responses = CASES[case]
    runtime = None
    def fault(current, **context):
        if current == point and len(runtime.store.list_model_calls(context['task_id'])) == count:
            raise InjectedCrash('Simulated controller death at '+current)
    verifier = no_patch_verifier() if case == 'repair_stop_saved' else None
    runtime = configured_runtime(root, ScriptedModel(responses(case)), first, case, verifier=verifier, fault=fault)
    with pytest.raises(InjectedCrash):
        runtime.run('Bound read goal; source modification is not authorized')
    task = runtime.store.list_tasks()[0]['task_id']
    assert len(first_dispatches) == before_reads
    assert runtime.store.list_verified_subtask_checkpoints(task) == []
    second, second_dispatches = bound_bridge(tmp_path, 'second')
    resumed = configured_runtime(root, ScriptedModel(responses(case, recovery=True)), second, case, verifier=verifier)
    result = resumed.resume(task)
    assert len(second_dispatches) == after_reads and resumed.model.call_count == after_responses
    assert len(resumed.store.list_model_calls(task)) == count+after_responses
    records = resumed.store.list_tool_calls(task)
    assert len(records) == (2 if case == 'advice_saved' else 1)
    assert records[-1]['status'] == 'succeeded'
    assert json.loads(records[-1]['output'])['sha256'] == digest(CONTENT)
    assert resumed.store.list_operations(task) == []
    assert host.read_text() == 'HOST_SENTINEL_DO_NOT_READ'
    assert 'HOST_SENTINEL_DO_NOT_READ' not in json.dumps(resumed.model.calls)
    if case == 'repair_stop_saved':
        assert result.status == 'failed'
        assert json.loads(result.error)['code'] == 'UNRESOLVED_STOP_LIMIT'
        assert resumed.store.list_verified_subtask_checkpoints(task) == []
        assert not [e for e in resumed.store.list_events(task) if e['type'] == 'task_completed']
    else:
        assert result.status == 'completed'
        assert len(resumed.store.list_verified_subtask_checkpoints(task)) == 1
    if case == 'advice_saved':
        assert records[0]['status'] == 'failed' and records[0]['error'] == 'PATH_NOT_FOUND'
        feedback = json.loads(records[0]['output'])['recovery']
        assert feedback['task_recoverable'] and feedback['suggested_actions']
        assert not feedback['same_arguments_retryable']
        assert 'PATH_NOT_FOUND' in json.dumps(resumed.model.calls[0])
    resumed.store.assert_invariants(task)


@pytest.mark.parametrize('failure', ['remove', 'inventory', 'remaining', 'close', None])
@pytest.mark.parametrize('protocol_passed', [True, False])
def test_cleanup_cannot_seal_false_pass_or_hide_protocol_failure(tmp_path, failure, protocol_passed):
    def remove(**kwargs):
        assert kwargs == {'force': True}
        if failure == 'remove':
            raise RuntimeError('injected remove failure')
    def inventory(**kwargs):
        assert kwargs == {'all': True, 'filters': {'label': 'org.lcc.backend-session=owned-session'}}
        if failure == 'inventory':
            raise RuntimeError('injected inventory failure')
        return ['owned-source'] if failure == 'remaining' else []
    def close():
        if failure == 'close':
            raise RuntimeError('injected close failure')
    source = SimpleNamespace(remove=remove)
    client = SimpleNamespace(containers=SimpleNamespace(list=inventory), close=close)
    state = {'status': 'FAILED', 'protocol_status': 'PASS' if protocol_passed else 'FAILED'}
    if not protocol_passed:
        state.update(error_type='OriginalProtocolError', error='original failure')
    finalize_case(tmp_path, state, client, source, 'owned-session')
    saved = json.loads((tmp_path/'result.json').read_text())
    assert saved['fixture_owner_cleanup'] == (failure is None)
    expected = ('PASS_OFFLINE_READ_PROCESS_RECOVERY' if failure is None else 'CLEANUP_FAILED')
    assert saved['status'] == (expected if protocol_passed else 'FAILED')
    if failure:
        assert saved['cleanup_errors'] and saved['cleanup_errors'][0]['stage']
    if not protocol_passed:
        assert saved['error_type'] == 'OriginalProtocolError' and saved['error'] == 'original failure'
