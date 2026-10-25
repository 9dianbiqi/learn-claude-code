"""Increment 2: real spawn processes, IPC barriers, no provider or sleep timing.

Expiry is injected by the coordinator only at an acknowledged barrier. It models
TTL expiry, not passage of wall time. All runtime/effect/OS-liveness code is real.
Set PHASE2_MP_EVIDENCE to a NEW directory to retain databases and JSON traces.
"""
from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
from multiprocessing.connection import wait
import os
from pathlib import Path
import sqlite3
import traceback

import pytest

from agent_runtime import Runtime
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, ToolCall
from agent_runtime.runtime import InjectedCrash
from agent_runtime.store import EventStore, LeaseLost


TIMEOUT = 40
TABLES = ('tasks', 'checkpoints', 'model_calls', 'tool_calls', 'operations',
          'operation_outbox', 'effect_reservations', 'leases', 'events', 'file_observations')


def _json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')


def _worker(repo, evidence, label, pipe, gate, config):
    """Spawn target: execute unmodified Runtime, wrap dispatch only to count it."""
    evidence = Path(evidence)
    def emit(kind, **fields):
        item = dict(kind=kind, pid=os.getpid(), worker=label, **fields)
        with (evidence / f'{label}.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(item) + '\n'); f.flush(); os.fsync(f.fileno())
        pipe.send(item)

    try:
        if config.get('external_writer'):
            emit('ready')
            assert gate.wait(TIMEOUT), 'external writer gate timed out'
            (Path(repo) / 'artifact.txt').write_bytes(b'EXTERNAL')
            emit('result', status='external_write', sha256=hashlib.sha256(b'EXTERNAL').hexdigest())
            return
        calls = []
        if config.get('foreign'):
            calls = [ModelResponse(tool_calls=[ToolCall('foreign-shell', 'bash',
                        {'command': 'echo FOREIGN >> foreign.txt'})])]
        model = ScriptedModel(calls + [ModelResponse(text=label + '-done')])
        held_token = None
        def fault(point, **context):
            nonlocal held_token
            if point == config.get('hold'):
                held_token = runtime._lease_token
                emit('held', point=point, token=held_token, context=context)
                assert pipe.poll(TIMEOUT), 'controller did not release barrier'
                command = pipe.recv()
                if command == 'crash':
                    # Pipe notification and per-worker fsynced log precede real exit.
                    emit('hard_exit', code=73)
                    os._exit(73)
                assert command == 'continue', command
        runtime = Runtime(repo, model, owner_id=label, lease_ttl=300,
                          approval_callback=lambda *_: True, fault_injector=fault)
        execute = runtime.tools.execute
        def counted(name, args):
            # Deliberately do not replace any actual tool or effect implementation.
            with (evidence / f'{label}-dispatch.jsonl').open('a', encoding='utf-8') as f:
                f.write(json.dumps({'pid': os.getpid(), 'name': name, 'args': args}) + '\n')
                f.flush(); os.fsync(f.fileno())
            return execute(name, args)
        runtime.tools.execute = counted
        emit('ready')
        assert gate.wait(TIMEOUT), 'start gate timed out'
        try:
            result = runtime.run('foreign task') if config.get('foreign') else runtime.resume(config['task'])
            emit('result', status=result.status, model_calls=model.call_count,
                 final_text=getattr(result, 'final_text', None), error=result.error)
        except Exception as exc:
            probes = {}
            if config.get('probe_stale') and isinstance(exc, LeaseLost):
                runtime.store.bind_lease(str(runtime.repo_root), label, held_token)
                for name, action in {
                    'checkpoint': lambda: runtime.store.save_checkpoint(config['task'], 'input_ready', [], {'turn': 99}),
                    'event': lambda: runtime.store.append_event(config['task'], 'ILLEGAL_STALE'),
                }.items():
                    try:
                        action(); probes[name] = 'UNEXPECTED_SUCCESS'
                    except LeaseLost:
                        probes[name] = 'LeaseLost'
                probes['heartbeat'] = runtime.store.heartbeat_lease(str(runtime.repo_root), label, fencing_token=held_token)
                runtime.store.release_lease(str(runtime.repo_root), label, fencing_token=held_token)
                runtime.store.clear_lease()
            emit('error', exception=type(exc).__name__, message=str(exc),
                 model_calls=model.call_count, probes=probes)
    except BaseException:
        (evidence / f'{label}-fatal.txt').write_text(traceback.format_exc(), encoding='utf-8')
        raise
    finally:
        pipe.close()


class Case:
    def __init__(self, root):
        self.root = root
        self.repo = root / 'repo'
        self.repo.mkdir(parents=True)
        self.db = self.repo / '.agent_runtime/runtime.db'
        self.ctx = mp.get_context('spawn')
        self.children = []
        self.trace = []

    def record(self, **value):
        self.trace.append(value)
        _json(self.root / 'trace.json', self.trace)

    def seed(self, tool=None):
        runtime = Runtime(self.repo, ScriptedModel([]))
        task = 'shared-task'
        messages = [{'role': 'user', 'content': 'deterministic recovery task'}]
        runtime.store.bootstrap_task(task, str(self.repo.resolve()), 'deterministic recovery task',
                                     'scripted-fake', messages, {'turn': 0})
        if tool:
            response = ModelResponse(tool_calls=[tool])
            runtime.store.save_checkpoint(task, 'model_responded', messages +
                [{'role': 'assistant', 'content': response.content}], {'turn': 0})
        self.task = task
        self.snapshot('seed')
        return task

    def start(self, label, gate=None, **config):
        parent, child = self.ctx.Pipe()
        gate = gate or self.ctx.Event()
        process = self.ctx.Process(target=_worker, args=(str(self.repo), str(self.root), label,
                                                       child, gate, {'task': self.task, **config}))
        process.start(); child.close()
        worker = dict(label=label, process=process, pipe=parent, gate=gate)
        self.children.append(worker)
        assert self.receive(worker)['kind'] == 'ready'
        return worker

    def receive(self, worker):
        assert worker['pipe'].poll(TIMEOUT), f"timeout: {worker['label']} exit={worker['process'].exitcode}"
        item = worker['pipe'].recv()
        self.record(ipc=item)
        return item

    def finish(self, worker, expected=0):
        worker['process'].join(TIMEOUT)
        code = worker['process'].exitcode
        self.record(process=worker['label'], pid=worker['process'].pid, exitcode=code)
        assert code == expected

    def snapshot(self, label):
        with sqlite3.connect(f'{self.db.as_uri()}?mode=ro', uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = {table: [dict(r) for r in conn.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                    for table in TABLES}
            assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            with sqlite3.connect(self.root / f'{label}.sqlite') as backup:
                conn.backup(backup)
        files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in self.repo.iterdir() if p.is_file()}
        _json(self.root / f'{label}.json', {'tables': rows, 'file_sha256': files})
        self.record(snapshot=label, file_sha256=files,
                    leases=rows['leases'], operations=rows['operations'])
        return rows

    def expire(self):
        # Injection at a held IPC boundary; token, owner and liveness stay real.
        with sqlite3.connect(self.db) as conn:
            old = conn.execute('SELECT owner_id,fencing_token,expires_at FROM leases').fetchall()
            conn.execute('UPDATE leases SET expires_at=0')
        self.record(injection='lease expiry', previous=old)

    def dispatches(self, name):
        return [json.loads(line) for p in self.root.glob('*-dispatch.jsonl')
                for line in p.read_text(encoding='utf-8').splitlines()
                if json.loads(line)['name'] == name]


@pytest.fixture
def case(tmp_path, request):
    evidence = os.environ.get('PHASE2_MP_EVIDENCE')
    root = Path(evidence) / request.node.name if evidence else tmp_path / 'evidence'
    root.mkdir(parents=True, exist_ok=False)
    value = Case(root)
    try:
        yield value
    finally:
        for worker in value.children:
            process = worker['process']
            if process.is_alive():
                process.terminate(); process.join(TIMEOUT)
                value.record(cleanup_terminated=worker['label'], exitcode=process.exitcode)
            worker['pipe'].close()
        if value.db.exists():
            value.snapshot('final')
        _json(root / 'dispatch-counts.json', {name: len(value.dispatches(name))
                                            for name in ('bash', 'write_file', 'read_file')})


def test_two_workers_resume_one_checkpoint(case):
    case.seed(ToolCall('shell', 'bash', {'command': 'echo ONCE >> marker.txt'}))
    gate = case.ctx.Event()
    workers = [case.start(label, gate, hold='after_tool_started') for label in ('a', 'b')]
    assert workers[0]['process'].pid != workers[1]['process'].pid != os.getpid()
    gate.set()
    ready = wait([w['pipe'] for w in workers], timeout=TIMEOUT)
    assert ready
    first = next(w for w in workers if w['pipe'] == ready[0])
    second = next(w for w in workers if w != first)
    replies = [(first, case.receive(first)), (second, case.receive(second))]
    winner, held = next((w, r) for w, r in replies if r['kind'] == 'held')
    loser, rejected = next((w, r) for w, r in replies if r['kind'] == 'error')
    assert rejected['exception'] == 'RuntimeError' and 'leased' in rejected['message']
    assert rejected['model_calls'] == 0
    case.finish(loser)
    during = case.snapshot('winner-held-loser-rejected')
    assert during['leases'][0]['owner_id'] == winner['label']
    assert during['leases'][0]['fencing_token'] == held['token']
    assert len(during['tool_calls']) == len(during['operations']) == 1
    assert during['operations'][0]['state'] == 'prepared'
    assert case.dispatches('bash') == []
    winner['pipe'].send('continue')
    assert case.receive(winner)['status'] == 'completed'
    case.finish(winner)
    after = case.snapshot('completed')
    assert len(case.dispatches('bash')) == 1
    assert (case.repo / 'marker.txt').read_text().splitlines() == ['ONCE ' ] or (case.repo / 'marker.txt').read_text().splitlines() == ['ONCE']
    assert after['operations'][0]['state'] == 'committed'
    assert after['operations'][0]['attempt_count'] == 1
    assert after['tool_calls'][0]['execution_attempts'] == 1
    EventStore(case.db).assert_invariants(case.task)


def test_old_worker_cannot_commit_after_takeover(case):
    case.seed()
    old = case.start('old', hold='after_model_response_before_persist', probe_stale=True)
    old['gate'].set(); held_old = case.receive(old)
    assert held_old['kind'] == 'held'
    case.expire()
    new = case.start('new', hold='after_model_response_before_persist')
    new['gate'].set(); held_new = case.receive(new)
    assert held_new['kind'] == 'held' and held_new['token'] > held_old['token']
    before = case.snapshot('new-owner-held')
    old['pipe'].send('continue')
    rejected = case.receive(old)
    assert rejected['exception'] == 'LeaseLost'
    assert rejected['probes'] == {'checkpoint': 'LeaseLost', 'event': 'LeaseLost', 'heartbeat': False}
    case.finish(old)
    after = case.snapshot('old-owner-rejected')
    assert before == after, 'old owner mutated durable state or released the new lease'
    new['pipe'].send('continue')
    assert case.receive(new)['status'] == 'completed'
    case.finish(new)
    final = case.snapshot('completed')
    assert not any('old-done' in (row.get('response_json') or '') for row in final['model_calls'])
    assert any('new-done' in (row.get('response_json') or '') for row in final['model_calls'])
    assert not case.dispatches('bash') and not case.dispatches('write_file')
    EventStore(case.db).assert_invariants(case.task)


def _crash_effect(case, tool):
    case.seed(tool)
    writer = case.start('writer', hold='after_tool_effect_before_persist')
    writer['gate'].set(); held = case.receive(writer)
    assert held['kind'] == 'held'
    before = case.snapshot('effect-before-db-commit')
    assert before['operations'][0]['state'] == 'dispatched'
    assert before['effect_reservations'][0]['state'] == 'running'
    assert before['effect_reservations'][0]['owner_pid'] == writer['process'].pid
    assert before['operation_outbox'][0]['state'] == 'claimed'
    return writer, held


def _exit_writer(case, writer):
    writer['pipe'].send('crash')
    assert case.receive(writer)['kind'] == 'hard_exit'
    case.finish(writer, 73)


def test_live_reservation_then_dead_opaque_owner(case):
    writer, held = _crash_effect(case, ToolCall('shell', 'bash', {'command': 'echo ONCE >> marker.txt'}))
    case.expire()
    before = case.snapshot('expired-live-owner')
    contender = case.start('contender')
    contender['gate'].set(); rejected = case.receive(contender)
    assert rejected['exception'] == 'RuntimeError' and 'leased' in rejected['message']
    case.finish(contender)
    assert case.snapshot('live-takeover-refused') == before
    _exit_writer(case, writer)
    resumed = case.start('recovery')
    resumed['gate'].set(); result = case.receive(resumed)
    assert result['status'] == 'needs_review' and result['model_calls'] == 0
    case.finish(resumed)
    after = case.snapshot('dead-owner-recovered')
    assert after['leases'][0]['fencing_token'] > held['token']
    assert after['operations'][0]['state'] == 'unknown'
    assert after['effect_reservations'][0]['state'] == 'unknown'
    assert after['operation_outbox'][0]['state'] == 'blocked'
    foreign = case.start('foreign', foreign=True)
    foreign['gate'].set(); blocked = case.receive(foreign)
    assert blocked['status'] == 'needs_review'
    case.finish(foreign)
    assert not (case.repo / 'foreign.txt').exists()
    assert len(case.dispatches('bash')) == 1
    assert (case.repo / 'marker.txt').read_text().strip() == 'ONCE'
    EventStore(case.db).assert_invariants(case.task)


@pytest.mark.parametrize('external', [False, True], ids=['matching-post-state', 'external-conflict'])
def test_file_effect_crash_recovery(case, external):
    writer, held = _crash_effect(case, ToolCall('write', 'write_file', {'path': 'artifact.txt', 'content': 'WRITTEN'}))
    assert (case.repo / 'artifact.txt').read_bytes() == b'WRITTEN'
    _exit_writer(case, writer)
    case.expire()
    if external:
        other = case.start('external', external_writer=True)
        other['gate'].set(); assert case.receive(other)['status'] == 'external_write'
        case.finish(other)
    pre = case.snapshot('before-recovery')
    resumed = case.start('recovery')
    resumed['gate'].set(); result = case.receive(resumed)
    assert result['status'] == ('needs_review' if external else 'completed')
    assert result['model_calls'] == (0 if external else 1)
    case.finish(resumed)
    after = case.snapshot('after-recovery')
    assert len(case.dispatches('write_file')) == 1
    assert after['tool_calls'][0]['execution_attempts'] == 1
    assert after['operations'][0]['attempt_count'] == 1
    assert after['leases'][0]['fencing_token'] > held['token']
    assert (case.repo / 'artifact.txt').read_bytes() == (b'EXTERNAL' if external else b'WRITTEN')
    assert after['operations'][0]['state'] == ('unknown' if external else 'committed')
    assert after['operation_outbox'][0]['state'] == ('blocked' if external else 'delivered')
    assert after['effect_reservations'][0]['state'] == ('unknown' if external else 'completed')
    assert pre['operations'][0]['state'] == 'dispatched'
    EventStore(case.db).assert_invariants(case.task)
