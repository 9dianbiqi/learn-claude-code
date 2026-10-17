"""Real process exits around benign evidence refresh, through Runtime.resume."""
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
from agent_runtime.models import ModelResponse, VerifiedSubtaskConfig, VerifiedSubtaskDAGConfig, VerifierResult
from test_phase2_evidence_crash import write_json, rows, snapshot, latest_states


def refresh_dag(out):
    def verifier(context):
        data = (Path(context.repo_root) / f'{context.subtask_id}.txt').read_bytes()
        status = 'pass' if data.strip() == b'GOOD' else 'fail'
        with (out / 'verifiers.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps({'pid': os.getpid(), 'node': context.subtask_id, 'status': status}) + '\n')
            f.flush(); os.fsync(f.fileno())
        return VerifierResult(status, 'trimmed content equals GOOD',
            [{'path': f'{context.subtask_id}.txt', 'sha256': hashlib.sha256(data).hexdigest()}] if status == 'pass' else [])
    return VerifiedSubtaskDAGConfig(tuple(VerifiedSubtaskConfig(
        name, name, 'trimmed content equals GOOD', (f'{name}.txt',), name, '1',
        'trimmed content equals GOOD', verifier, 'c' * 64, blocked_by=deps, max_turns=4)
        for name, deps in [('a', ()), ('b', ('a',)), ('independent', ())]))


def refresh_child(repo, out, task, point):
    repo, out = Path(repo), Path(out)
    runtime = Runtime(repo, ScriptedModel([]), verified_subtask_dag=refresh_dag(out), lease_ttl=300)
    original_transaction = runtime.store.transaction
    original_refresh = runtime.store.refresh_verified_subtask
    active = False

    @contextmanager
    def transaction(guard=True):
        with original_transaction(guard=guard) as conn:
            yield conn
            if active and point == 'before_commit':
                write_json(out / 'child-boundary.json', {'pid': os.getpid(), 'point': point,
                    'in_transaction': conn.in_transaction, 'tables': rows(conn)})
                os._exit(87)
        if active and point == 'after_commit':
            snapshot(repo, out, 'child-committed')
            write_json(out / 'child-boundary.json', {'pid': os.getpid(), 'point': point, 'in_transaction': False})
            os._exit(87)

    def refresh(**kwargs):
        nonlocal active
        active = True
        try:
            return original_refresh(**kwargs)
        finally:
            active = False
    runtime.store.transaction = transaction
    runtime.store.refresh_verified_subtask = refresh
    runtime.resume(task)
    raise AssertionError('refresh exit boundary not reached')


@pytest.mark.parametrize('point', ['before_commit', 'after_commit'])
def test_refresh_process_exit_preserves_single_current_authority(tmp_path, point):
    configured = os.environ.get('PHASE2_I4_EVIDENCE')
    out = Path(configured) / point if configured else tmp_path / point
    out.mkdir(parents=True, exist_ok=False)
    repo = out / 'repo'; repo.mkdir()
    names = ('a', 'b', 'independent')
    for name in names:
        (repo / f'{name}.txt').write_bytes(b'GOOD')
    initial = Runtime(repo, ScriptedModel([ModelResponse(text=f'{n}\nSUBTASK_COMPLETE') for n in names]),
                      verified_subtask_dag=refresh_dag(out))
    completed = initial.run('Verify trimmed GOOD content')
    assert completed.status == 'completed'
    baseline = snapshot(repo, out, 'baseline')
    old_ids = {r['subtask_id']: r['semantic_checkpoint_id'] for r in baseline['semantic_checkpoints']}
    (repo / 'a.txt').write_bytes(b'GOOD\n')
    program = 'import sys; sys.path.insert(0, sys.argv[1]); from test_phase2_refresh_crash import refresh_child; refresh_child(*sys.argv[2:])'
    child = subprocess.run([sys.executable, '-c', program, str(Path(__file__).parent),
        str(repo), str(out), completed.task_id, point], capture_output=True, timeout=40,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    (out / 'child-stdout.log').write_bytes(child.stdout)
    (out / 'child-stderr.log').write_bytes(child.stderr)
    write_json(out / 'process.json', {'returncode': child.returncode, 'parent_pid': os.getpid(), 'point': point})
    assert child.returncode == 87, child.stderr
    boundary = json.loads((out / 'child-boundary.json').read_text())
    assert boundary['pid'] != os.getpid()
    assert boundary['in_transaction'] == (point == 'before_commit')
    persisted = snapshot(repo, out, 'after-crash')
    target = boundary['tables'] if point == 'before_commit' else persisted
    assert target['semantic_checkpoints'][:3] == baseline['semantic_checkpoints']
    assert len(target['semantic_checkpoints']) == len(target['verifier_runs']) == 4
    assert latest_states(target)[old_ids['a']] == 'superseded'
    assert sum(s == 'valid' for s in latest_states(target).values()) == 3
    assert all(r['status'] == 'completed' and r['consumed_turns'] == 1 for r in target['plan_items'])
    if point == 'before_commit':
        for table in ('plans', 'plan_items', 'semantic_checkpoints', 'verifier_runs', 'semantic_checkpoint_state_events'):
            assert persisted[table] == baseline[table], table
        assert not any(r['type'] == 'verified_subtask_evidence_refreshed' for r in persisted['events'])
    else:
        assert sum(r['type'] == 'verified_subtask_evidence_refreshed' for r in persisted['events']) == 1
    with sqlite3.connect(initial.store.path) as conn:
        conn.execute('UPDATE leases SET expires_at=0')
    model = ScriptedModel([])
    recovered = Runtime(repo, model, verified_subtask_dag=refresh_dag(out))
    result = recovered.resume(completed.task_id)
    final = snapshot(repo, out, 'recovered')
    write_json(out / 'result.json', {'status': result.status, 'model_calls': model.call_count, 'process_exit': child.returncode})
    assert result.status == 'completed' and model.call_count == 0
    assert final['semantic_checkpoints'][:3] == baseline['semantic_checkpoints']
    assert len(final['semantic_checkpoints']) == len(final['verifier_runs']) == 4
    states = latest_states(final)
    assert [states[old_ids[n]] for n in names] == ['superseded', 'valid', 'valid']
    new = final['semantic_checkpoints'][-1]
    assert new['subtask_id'] == 'a' and states[new['semantic_checkpoint_id']] == 'valid'
    assert json.loads(new['evidence_manifest_json']) == [{'path': 'a.txt', 'sha256': hashlib.sha256(b'GOOD\n').hexdigest()}]
    links = [r for r in final['semantic_checkpoint_state_events'] if r['state'] == 'superseded']
    assert len(links) == 1 and links[0]['replacement_checkpoint_id'] == new['semantic_checkpoint_id']
    assert sum(r['type'] == 'verified_subtask_evidence_refreshed' for r in final['events']) == 1
    assert all(r['status'] == 'completed' and r['consumed_turns'] == 1 for r in final['plan_items'])
    assert final['tool_calls'] == final['operations'] == []
    assert final['model_calls'] == baseline['model_calls']
    log = [json.loads(line) for line in (out / 'verifiers.jsonl').read_text().splitlines()]
    assert [r['node'] for r in log] == [*names, 'a'] + (['a'] if point == 'before_commit' else [])
    assert all(r['status'] == 'pass' for r in log)
    recovered.store.assert_invariants(completed.task_id)
