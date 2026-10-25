"""Task-local attempt limits, persisted replay semantics, atomicity and fencing."""
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier
from types import SimpleNamespace

import pytest

from agent_runtime import Runtime
from agent_runtime.container_reads import DockerReadBridge
from agent_runtime.docker_workspace import digest
from agent_runtime.experiments.container_read_budget_probe import CASES, CONTENT, READ_PATH, make_runtime, read_response
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, ToolCall
from agent_runtime.read_budget import ControllerReadBudget, ControllerReadBudgetExhausted
from agent_runtime.runtime import InjectedCrash
from agent_runtime.store import EventStore, InvariantViolation, LeaseLost
import agent_runtime.store as store_module


def bridge(tmp_path, name, *, max_calls=1):
    backend = SimpleNamespace(source_id='owned-source', session='same-session', image='sha256:'+'a'*64,
        engine_id='same-engine', path='/workspace', instance_id='fixture', base_commit='base')
    value = DockerReadBridge(backend, tmp_path/name, max_calls=max_calls)
    value.requests = []
    def request(data, record):
        value.requests.append(data)
        if data['args'].get('path') == 'missing.py':
            return {'ok': False, 'error_type': 'PATH_NOT_FOUND', 'path': 'missing.py', 'nonce': data['nonce']}
        return {'ok': True, 'nonce': data['nonce'], 'result': {'sha256': digest(CONTENT), 'text': CONTENT.decode()}}
    value._request = request
    return value


@pytest.mark.parametrize('name', [name for name, case in CASES.items() if case.point in {
    'after_tool_effect_before_persist', 'after_tool_persist', 'after_tool_started'}])
def test_new_owner_cannot_reset_task_attempts_and_saved_results_are_free(tmp_path, name):
    case = CASES[name]
    first = bridge(tmp_path, 'first')
    def crash(point, **context):
        if point == case.point:
            raise InjectedCrash(point)
    runtime = make_runtime(tmp_path/'controller', ScriptedModel([read_response()]), first, case.limit, fault=crash)
    with pytest.raises(InjectedCrash): runtime.run('Read with bounded attempts')
    task = runtime.store.list_tasks()[0]['task_id']
    second = bridge(tmp_path, 'second')
    model = ScriptedModel([ModelResponse(text='Observed')] if case.parent_models else [])
    resumed = make_runtime(tmp_path/'controller', model, second, case.limit)
    result = resumed.resume(task)
    assert result.status == case.status
    assert len(first.requests) == case.child_execs and len(second.requests) == case.parent_execs
    assert model.call_count == case.parent_models
    assert sum(c['execution_attempts'] for c in resumed.store.list_tool_calls(task)) == case.attempts
    if case.status == 'failed':
        assert json.loads(result.error)['code'] == ControllerReadBudgetExhausted.code
        assert resumed.store.get_plan_item(task, 'final')['status'] == 'failed'
        assert resumed.store.list_verified_subtask_checkpoints(task) == []
        failures = [e for e in resumed.store.list_events(task) if e['type'] == 'plan_item_failed']
        assert failures[-1]['payload']['budget_exhausted'] is True
    resumed.store.assert_invariants(task)


def test_default_still_has_only_the_existing_process_local_bridge_cap(tmp_path):
    first = bridge(tmp_path, 'first')
    def crash(point, **context):
        if point == 'after_tool_effect_before_persist': raise InjectedCrash(point)
    original = make_runtime(tmp_path/'controller', ScriptedModel([read_response()]), first, None, fault=crash)
    with pytest.raises(InjectedCrash): original.run('Legacy opt-out')
    task = original.store.list_tasks()[0]['task_id']
    second = bridge(tmp_path, 'second')
    resumed = make_runtime(tmp_path/'controller', ScriptedModel([ModelResponse(text='done')]), second, None)
    assert 'controller_read_budget' not in resumed._runtime_profile()
    assert resumed.resume(task).status == 'completed'
    assert len(first.requests) == len(second.requests) == 1
    assert sum(c['execution_attempts'] for c in resumed.store.list_tool_calls(task)) == 2


@pytest.mark.parametrize('limit', [None, 2])
def test_changed_or_removed_budget_cannot_resume_before_model_or_adapter(tmp_path, limit):
    first = bridge(tmp_path, 'first')
    def crash(point, **context):
        if point == 'after_model_response': raise InjectedCrash(point)
    original = make_runtime(tmp_path/'controller', ScriptedModel([read_response()]), first, 1, fault=crash)
    with pytest.raises(InjectedCrash): original.run('Frozen budget')
    task = original.store.list_tasks()[0]['task_id']
    second = bridge(tmp_path, 'second')
    model = ScriptedModel([])
    resumed = make_runtime(tmp_path/'controller', model, second, limit)
    with pytest.raises(InvariantViolation, match='profile missing or changed'): resumed.resume(task)
    assert not second.requests and model.call_count == 0


def test_error_advice_consumes_one_attempt_and_budget_failure_stops_without_extra_model(tmp_path):
    value = bridge(tmp_path, 'reads')
    model = ScriptedModel([ModelResponse(tool_calls=[ToolCall('missing', 'container_read_file', {'path': 'missing.py'})]),
                           read_response()])
    runtime = make_runtime(tmp_path/'controller', model, value, 1)
    result = runtime.run('Find actual file')
    assert result.status == 'failed' and json.loads(result.error)['code'] == ControllerReadBudgetExhausted.code
    records = runtime.store.list_tool_calls(result.task_id)
    assert records[0]['error'] == 'PATH_NOT_FOUND' and records[0]['execution_attempts'] == 1
    assert records[1]['error'] == ControllerReadBudgetExhausted.code and records[1]['execution_attempts'] == 0
    assert model.call_count == 2 and len(value.requests) == 1
    assert 'PATH_NOT_FOUND' in json.dumps(model.calls[1])
    runtime.store.assert_invariants(result.task_id)


def test_cap_spans_controller_tool_names_and_is_scoped_to_each_task(tmp_path):
    first = bridge(tmp_path, 'first', max_calls=4)
    responses = [ModelResponse(tool_calls=[read_response().tool_calls[0],
        ToolCall('find', 'container_find_files', {'pattern': '**/*.py'}),
        ToolCall('search', 'container_search_symbols', {'symbol': 'wanted'})])]
    runtime = make_runtime(tmp_path/'controller', ScriptedModel(responses), first, 2)
    result = runtime.run('Three calls with cap two')
    assert result.status == 'failed' and len(first.requests) == 2
    assert runtime.model.call_count == 1
    records = runtime.store.list_tool_calls(result.task_id)
    assert [c['execution_attempts'] for c in records] == [1, 1, 0]
    second = bridge(tmp_path, 'second', max_calls=4)
    next_runtime = make_runtime(tmp_path/'controller', ScriptedModel([read_response(), ModelResponse(text='done')]), second, 2)
    another = next_runtime.run('Independent task')
    assert another.status == 'completed' and len(second.requests) == 1
    assert sum(c['execution_attempts'] for c in next_runtime.store.list_tool_calls(another.task_id)) == 1


@pytest.mark.parametrize('limit', [0, 65, True, '2', 2.5])
def test_invalid_limits_are_rejected_without_model_calls(tmp_path, limit):
    value = bridge(tmp_path, 'reads'); model = ScriptedModel([])
    with pytest.raises(ValueError): make_runtime(tmp_path/'controller', model, value, limit)
    assert not value.requests and model.call_count == 0


def test_unbound_chat_cannot_enable_controller_attempt_policy(tmp_path):
    with pytest.raises(ValueError, match='bound tools and a frozen completion contract'):
        Runtime(tmp_path, ScriptedModel([]), controller_read_attempt_limit=1)


def test_persisted_registration_without_live_rebind_cannot_enable_budget(tmp_path):
    value = bridge(tmp_path, 'reads')
    configured = make_runtime(tmp_path/'controller', ScriptedModel([]), value, 1)
    model = ScriptedModel([])
    with pytest.raises(ValueError, match='bound tools'):
        Runtime(tmp_path/'controller', model, tool_scope=value.names,
            completion_contract=configured.completion_contract, verified_subtask_dag=configured.verified_subtask_dag,
            controller_read_attempt_limit=1)
    assert model.call_count == 0 and not value.requests
    assert configured.store.list_tasks() == []


def test_atomic_reservation_allows_only_one_concurrent_start(tmp_path):
    path = tmp_path/'ledger.db'; store = EventStore(path)
    store.create_task('task', str(tmp_path), 'fixture', 'scripted')
    for call in ('one', 'two'):
        store.create_tool_call('task', call, 0, 'container_read_file', {}, call, effect='read_only')
    policy = ControllerReadBudget(1, ('container_read_file',))
    barrier = Barrier(2)
    def start(call):
        actor = EventStore(path)
        barrier.wait(timeout=10)
        try:
            actor.start_tool_call('task', call, 'read_only', read_budget=policy)
            return 'started'
        except ControllerReadBudgetExhausted:
            return 'exhausted'
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(start, ('one', 'two')))
    assert sorted(results) == ['exhausted', 'started']
    assert sum(c['execution_attempts'] for c in store.list_tool_calls('task')) == 1


def test_stale_owner_cannot_reserve_or_charge_budget(tmp_path, monkeypatch):
    now = [1000.0]; monkeypatch.setattr(store_module, '_now', lambda: now[0])
    path = tmp_path/'ledger.db'; old = EventStore(path)
    old.create_task('task', str(tmp_path), 'fixture', 'scripted')
    old.create_tool_call('task', 'read', 0, 'container_read_file', {}, 'args', effect='read_only')
    fence = old.acquire_lease(str(tmp_path), 'task', 'old', ttl=1)
    old.bind_lease(str(tmp_path), 'old', fence)
    now[0] = 1002.0
    new = EventStore(path)
    assert new.acquire_lease(str(tmp_path), 'task', 'new', ttl=10) > fence
    with pytest.raises(LeaseLost):
        old.start_tool_call('task', 'read', 'read_only', read_budget=ControllerReadBudget(1, ('container_read_file',)))
    assert new.get_tool_call('task', 'read')['execution_attempts'] == 0


@pytest.mark.parametrize('point', ['budget_record_saved', 'contract_failure_after_item',
                                 'contract_failure_after_checkpoint', 'contract_failure_before_commit'])
def test_persisted_budget_denial_recovers_atomic_terminal_reason_without_dispatch(tmp_path, monkeypatch, point):
    value = bridge(tmp_path, 'first')
    model = ScriptedModel([ModelResponse(tool_calls=[read_response().tool_calls[0],
        ToolCall('denied', 'container_read_file', {'path': READ_PATH})])])
    def fault(current, **context):
        if current == point: raise InjectedCrash(point)
    runtime = make_runtime(tmp_path/'controller', model, value, 1, fault=fault)
    update = runtime.store.update_tool_call
    def crash_after_record(*args, **fields):
        update(*args, **fields)
        if point == 'budget_record_saved' and fields.get('error') == ControllerReadBudgetExhausted.code:
            raise InjectedCrash(point)
    monkeypatch.setattr(runtime.store, 'update_tool_call', crash_after_record)
    with pytest.raises(InjectedCrash): runtime.run('Two reads under cap one')
    task = runtime.store.list_tasks()[0]['task_id']
    assert runtime.store.get_tool_call(task, 'denied')['error'] == ControllerReadBudgetExhausted.code
    assert runtime.store.get_task(task)['status'] not in {'completed', 'failed'}
    assert runtime.store.get_plan_item(task, 'final')['status'] != 'failed'
    other = bridge(tmp_path, 'second'); next_model = ScriptedModel([])
    resumed = make_runtime(tmp_path/'controller', next_model, other, 1)
    result = resumed.resume(task)
    assert result.status == 'failed' and json.loads(result.error)['code'] == ControllerReadBudgetExhausted.code
    assert resumed.store.get_plan_item(task, 'final')['status'] == 'failed'
    assert not other.requests and next_model.call_count == 0
    assert sum(c['execution_attempts'] for c in resumed.store.list_tool_calls(task)) == 1
    failures = [e for e in resumed.store.list_events(task) if e['type'] == 'plan_item_failed']
    assert len(failures) == 1 and failures[0]['payload']['budget_exhausted'] is True
    assert any(e['type'] == 'controller_read_budget_recovered' for e in resumed.store.list_events(task))
    assert resumed.store.list_verified_subtask_checkpoints(task) == []
    resumed.store.assert_invariants(task)
