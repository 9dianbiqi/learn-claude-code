"""New process-exit coverage for stale-evidence invalidation, not old tests.

Public seam: Runtime.run/resume. Test-only transaction instrumentation delegates
all SQL/commit/rollback to EventStore and exits at the selected durable boundary.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from contextlib import contextmanager

import pytest

from agent_runtime import Runtime
from agent_runtime.fake_model import ScriptedModel
from agent_runtime.models import ModelResponse, ToolCall, VerifiedSubtaskConfig, VerifiedSubtaskDAGConfig, VerifierResult


TABLES = ('tasks', 'plans', 'plan_items', 'checkpoints', 'model_calls', 'verifier_runs',
          'semantic_checkpoints', 'semantic_checkpoint_state_events', 'events', 'leases',
          'operations', 'tool_calls')


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2), encoding='utf-8')


def rows(conn):
    conn.row_factory = sqlite3.Row
    return {t: [dict(r) for r in conn.execute(f'SELECT * FROM {t} ORDER BY rowid')] for t in TABLES}


def snapshot(repo, out, label):
    db = repo / '.agent_runtime/runtime.db'
    with sqlite3.connect(f'{db.as_uri()}?mode=ro', uri=True) as conn:
        data = rows(conn)
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        with sqlite3.connect(out / f'{label}.sqlite') as target:
            conn.backup(target)
    write_json(out / f'{label}.json', data)
    write_json(out / f'{label}-hashes.json', {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in repo.glob('*.txt')})
    return data


def dag(out):
    def verifier(context):
        data = (Path(context.repo_root) / f'{context.subtask_id}.txt').read_bytes()
        status = 'pass' if data == b'GOOD' else 'fail'
        with (out / 'verifiers.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps({'pid': os.getpid(), 'node': context.subtask_id, 'status': status}) + '\n')
            f.flush(); os.fsync(f.fileno())
        return VerifierResult(status, 'content equals GOOD',
            [{'path': f'{context.subtask_id}.txt', 'sha256': hashlib.sha256(data).hexdigest()}] if status == 'pass' else [])
    return VerifiedSubtaskDAGConfig(tuple(VerifiedSubtaskConfig(
        name, name, 'content equals GOOD', (f'{name}.txt',), name, '1', 'content equals GOOD',
        verifier, 'b' * 64, blocked_by=deps, max_turns=6)
        for name, deps in [('a', ()), ('b', ('a',)), ('c', ('b',)), ('independent', ())]))


def crash_child(repo, out, task, point):
    repo, out = Path(repo), Path(out)
    runtime = Runtime(repo, ScriptedModel([]), verified_subtask_dag=dag(out), lease_ttl=300)
    original_transaction = runtime.store.transaction
    original_invalidate = runtime.store.invalidate_verified_subtask_evidence
    active = False

    @contextmanager
    def transaction(guard=True):
        with original_transaction(guard=guard) as conn:
            yield conn
            if active and point == 'before_commit':
                # Full transaction body executed; record uncommitted state before
                # exiting WITHOUT unwinding the original context manager.
                write_json(out / 'child-boundary.json', {'pid': os.getpid(), 'point': point,
                    'in_transaction': conn.in_transaction, 'tables': rows(conn)})
                os._exit(86)
        if active and point == 'after_commit':
            snapshot(repo, out, 'child-committed')
            write_json(out / 'child-boundary.json', {'pid': os.getpid(), 'point': point,
                'in_transaction': False})
            os._exit(86)

    def invalidate(**kwargs):
        nonlocal active
        active = True
        try:
            return original_invalidate(**kwargs)
        finally:
            active = False

    runtime.store.transaction = transaction
    runtime.store.invalidate_verified_subtask_evidence = invalidate
    runtime.resume(task)
    raise AssertionError('invalidation exit boundary was not reached')


def latest_states(data):
    states = {}
    for row in data['semantic_checkpoint_state_events']:
        states[row['semantic_checkpoint_id']] = row['state']
    return states


@pytest.mark.parametrize('point', ['before_commit', 'after_commit'])
def test_invalidation_process_exit_preserves_required_reverification(tmp_path, point):
    configured = os.environ.get('PHASE2_I3_EVIDENCE')
    out = Path(configured) / point if configured else tmp_path / point
    out.mkdir(parents=True, exist_ok=False)
    repo = out / 'repo'; repo.mkdir()
    names = ('a', 'b', 'c', 'independent')
    for name in names:
        (repo / f'{name}.txt').write_bytes(b'GOOD')
    initial = Runtime(repo, ScriptedModel([ModelResponse(text=f'{n}\nSUBTASK_COMPLETE') for n in names]),
                      verified_subtask_dag=dag(out))
    result = initial.run('Verify a -> b -> c and an independent branch')
    assert result.status == 'completed'
    baseline = snapshot(repo, out, 'baseline')
    old_ids = {r['subtask_id']: r['semantic_checkpoint_id'] for r in baseline['semantic_checkpoints']}
    (repo / 'a.txt').write_bytes(b'BAD')
    program = 'import sys; sys.path.insert(0, sys.argv[1]); from test_phase2_evidence_crash import crash_child; crash_child(*sys.argv[2:])'
    child = subprocess.run([sys.executable, '-c', program, str(Path(__file__).parent),
        str(repo), str(out), result.task_id, point], capture_output=True, timeout=40,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    (out / 'child-stdout.log').write_bytes(child.stdout)
    (out / 'child-stderr.log').write_bytes(child.stderr)
    write_json(out / 'process.json', {'returncode': child.returncode, 'parent_pid': os.getpid(), 'point': point})
    assert child.returncode == 86, child.stderr
    boundary = json.loads((out / 'child-boundary.json').read_text())
    assert boundary['pid'] != os.getpid()
    assert boundary['in_transaction'] == (point == 'before_commit')
    persisted = snapshot(repo, out, 'after-crash')
    # Immutable historical evidence must survive both outcomes unchanged.
    assert persisted['semantic_checkpoints'] == baseline['semantic_checkpoints']
    target = boundary['tables'] if point == 'before_commit' else persisted
    assert {r['subtask_id']: r['status'] for r in target['plan_items']} == {
        'a': 'retryable', 'b': 'pending', 'c': 'pending', 'independent': 'completed'}
    assert [latest_states(target)[old_ids[n]] for n in names] == ['stale', 'stale', 'stale', 'valid']
    assert target['tasks'][0]['status'] == 'running'
    assert target['plans'][0]['status'] == 'active'
    if point == 'before_commit':
        for table in ('plans', 'plan_items', 'verifier_runs', 'semantic_checkpoint_state_events'):
            assert persisted[table] == baseline[table], table
        assert persisted['tasks'][0]['status'] == 'completed'
        assert not any(r['type'] == 'verified_subtask_evidence_refresh_fail' for r in persisted['events'])
    else:
        assert len(persisted['verifier_runs']) == len(baseline['verifier_runs']) + 1
        assert sum(r['type'] == 'verified_subtask_evidence_refresh_fail' for r in persisted['events']) == 1
    # Explicit expiry after confirmed process death; no timing-based sleep.
    with sqlite3.connect(initial.store.path) as conn:
        conn.execute('UPDATE leases SET expires_at=0')
    model = ScriptedModel([
        ModelResponse(tool_calls=[ToolCall('read-a', 'read_file', {'path': 'a.txt'}),
                                  ToolCall('repair-a', 'write_file', {'path': 'a.txt', 'content': 'GOOD'})]),
        *[ModelResponse(text=f'{n}\nSUBTASK_COMPLETE') for n in ('a', 'b', 'c')]])
    recovered = Runtime(repo, model, verified_subtask_dag=dag(out), approval_callback=lambda *_: True)
    final_result = recovered.resume(result.task_id)
    final = snapshot(repo, out, 'recovered')
    write_json(out / 'result.json', {'status': final_result.status, 'model_calls': model.call_count,
        'process_exit': child.returncode, 'recovery_pid': os.getpid()})
    assert final_result.status == 'completed' and model.call_count == 4
    assert len(final['semantic_checkpoints']) == 7
    assert final['semantic_checkpoints'][:4] == baseline['semantic_checkpoints']
    assert [latest_states(final)[old_ids[n]] for n in names] == ['stale', 'stale', 'stale', 'valid']
    assert sum(r['type'] == 'verified_subtask_evidence_refresh_fail' for r in final['events']) == 1
    assert {r['subtask_id']: r['consumed_turns'] for r in final['plan_items']} == {'a': 3, 'b': 2, 'c': 2, 'independent': 1}
    assert all(r['status'] == 'completed' for r in final['plan_items'])
    assert final['leases'][0]['fencing_token'] > persisted['leases'][0]['fencing_token']
    assert final['operations'][0]['state'] == 'committed' and final['operations'][0]['attempt_count'] == 1
    assert (repo / 'a.txt').read_bytes() == b'GOOD'
    verification = [json.loads(line) for line in (out / 'verifiers.jsonl').read_text().splitlines()]
    assert [r['node'] for r in verification if r['status'] == 'pass'] == [*names, 'a', 'b', 'c']
    assert sum(r['status'] == 'fail' for r in verification) == (2 if point == 'before_commit' else 1)
    recovered.store.assert_invariants(result.task_id)
