"""Offline artifact mutations: no process recovery, model network or oracle execution."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from agent_runtime.experiments.bc_artifact_audit import audit


COMMIT = 'a'*40
LANE = 'b'*32
NAMES = {'duration': ['duration.py', 'settings.py', 'README.md'],
         'inventory': ['records.py', 'inventory.py', 'README.md']}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def fixture_root(tmp_path):
    root = tmp_path/'artifacts'
    lane = root/LANE
    manifest = {'baseline_commit': COMMIT, 'protocol': 'bc-f3-paired-pilot-v4', 'mode': 'scripted',
                'fault': 'verified_subtask_f3', 'tasks': list(NAMES), 'arms': ['B', 'C'],
                'source_hashes': {'agent_runtime/experiments/bc_pilot.py': 'c'*64}}
    write(lane/'pilot/manifest.json', manifest)
    continued = {**manifest, 'prefix_source_manifest_sha256': digest(lane/'pilot/manifest.json')}
    write(lane/'continued/manifest.json', continued)
    result_rows = []
    for task, names in NAMES.items():
        snapshot = lane/'pilot'/task/'snapshot'
        db = snapshot/'.agent_runtime/runtime.db'
        db.parent.mkdir(parents=True)
        db.write_bytes(b'synthetic hash-only snapshot; not a SQLite validation fixture')
        for name in names:
            (snapshot/name).write_text('frozen '+name, encoding='utf-8')
        task_id = 'fixture-'+task
        for run in ('pilot', 'continued'):
            prefix = lane/run/task/'prefix'
            write(prefix/'fault.json', {'point': 'verified_subtask_f3', 'context': {'task_id': task_id}})
            write(prefix/'process.json', {'exit_code': 73})
            write(prefix/'model_requests.jsonl', {'call': 1, 'provider_tokens': False, 'response': {'content': []}})
            for arm in ('B', 'C'):
                phase = lane/run/task/arm
                write(phase/'process.json', {'exit_code': 0})
                write(phase/'result.json', {'result': {'task_id': task_id, 'status': 'completed', 'error': None},
                    'trace': {'task_id': task_id, 'status': 'completed', 'model_calls': 2, 'invariant_violation_count': 0,
                              'permission_bypass_count': 0, 'stale_lease_execution_attempts': 0,
                              'confirmed_duplicate_side_effects': 0}, 'final_check': {'passed': True}})
                write(phase/'model_requests.jsonl', {'call': 1, 'provider_tokens': False, 'response': {'content': []}})
        for arm in ('B', 'C'):
            result_rows.append({'task': task, 'arm': arm, 'exit_code': 0, 'status': 'completed',
                'final_pass': True, 'completed_parser_unchanged': True, 'invariant_violations': 0,
                'snapshot_database_sha256': digest(db), 'initial_file_hashes': {n: digest(snapshot/n) for n in names},
                'initial_file_identities': {n: {'st_dev': 1, 'st_ino': i+1} for i, n in enumerate(names)},
                'post_restart_calls': 1})
    for run in ('pilot', 'continued'):
        write(lane/run/'results.json', copy.deepcopy(result_rows))
    return root


def test_valid_fixture_is_read_only_and_reports_artifact_scope(fixture_root):
    before = {str(p): digest(p) for p in fixture_root.rglob('*') if p.is_file()}
    report = audit(fixture_root, COMMIT)
    assert report['status'] == 'PASS' and report['result_rows_checked'] == 8
    assert 'not Runtime or SWE-bench certification' in report['scope']
    assert before == {str(p): digest(p) for p in fixture_root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('fault', [
    'missing_manifest', 'bad_json', 'wrong_commit', 'duplicate_row', 'failed_row', 'failed_oracle',
    'failed_process', 'false_zero', 'prefix_not_crashed', 'snapshot_changed', 'paired_mismatch',
    'prefix_changed', 'trace_failure', 'provider_record', 'bad_model_response', 'missing_process',
    'wrong_prefix_link', 'call_count', 'source_changed', 'ambiguous_lane', 'oversized_manifest',
])
def test_incomplete_or_contradictory_evidence_never_passes(fixture_root, fault):
    lane = fixture_root/LANE
    manifest = lane/'continued/manifest.json'
    results = lane/'continued/results.json'
    phase = lane/'continued/duration/C'
    if fault == 'missing_manifest':
        manifest.unlink()
    elif fault == 'bad_json':
        manifest.write_text('bad-json', encoding='utf-8')
    elif fault == 'oversized_manifest':
        manifest.write_bytes(b'x'*(2*1024*1024+1))
    elif fault in {'wrong_commit', 'wrong_prefix_link', 'source_changed'}:
        value = read(manifest)
        if fault == 'wrong_commit': value['baseline_commit'] = 'd'*40
        elif fault == 'wrong_prefix_link': value['prefix_source_manifest_sha256'] = 'd'*64
        else: value['source_hashes'] = {'agent_runtime/runtime.py': 'd'*64}
        write(manifest, value)
    elif fault in {'duplicate_row', 'failed_row', 'false_zero', 'paired_mismatch', 'call_count'}:
        value = read(results)
        if fault == 'duplicate_row': value[1] = value[0]
        elif fault == 'failed_row': value[1]['final_pass'] = False
        elif fault == 'false_zero': value[1]['exit_code'] = False
        elif fault == 'paired_mismatch': value[1]['snapshot_database_sha256'] = 'd'*64
        else: value[1]['post_restart_calls'] = 100
        write(results, value)
    elif fault == 'failed_oracle':
        value = read(phase/'result.json'); value['final_check']['passed'] = False
        write(phase/'result.json', value)
    elif fault == 'failed_process':
        write(phase/'process.json', {'exit_code': 2})
    elif fault == 'missing_process':
        (phase/'process.json').unlink()
    elif fault == 'prefix_not_crashed':
        write(lane/'pilot/duration/prefix/process.json', {'exit_code': 0})
    elif fault == 'snapshot_changed':
        (lane/'pilot/duration/snapshot/settings.py').write_text('changed', encoding='utf-8')
    elif fault == 'prefix_changed':
        write(lane/'continued/duration/prefix/model_requests.jsonl', {'provider_tokens': False, 'response': {}})
    elif fault == 'trace_failure':
        value = read(phase/'result.json'); value['trace']['permission_bypass_count'] = 1
        write(phase/'result.json', value)
    elif fault in {'provider_record', 'bad_model_response'}:
        value = {'provider_tokens': fault == 'provider_record', 'response': None if fault == 'bad_model_response' else {}}
        write(phase/'model_requests.jsonl', value)
    elif fault == 'ambiguous_lane':
        (fixture_root/('d'*32)).mkdir()
    report = audit(fixture_root, COMMIT)
    assert report['status'] in {'FAILED', 'INCOMPLETE'} and report['findings']


def test_symlink_out_of_artifacts_is_not_read(fixture_root, tmp_path):
    target = fixture_root/LANE/'continued/duration/C/result.json'
    outside = tmp_path/'host-sentinel.json'
    outside.write_bytes(target.read_bytes())
    target.unlink()
    try:
        target.symlink_to(outside)
    except OSError:
        pytest.skip('Host does not allow creating symlinks; Linux execution required')
    report = audit(fixture_root, COMMIT)
    assert report['status'] == 'INCOMPLETE' and report['findings'][0]['code'] == 'UNSAFE_EVIDENCE_PATH'


def test_missing_root_and_invalid_expected_commit_are_not_success(tmp_path):
    assert audit(tmp_path/'missing', COMMIT)['status'] == 'INCOMPLETE'
    with pytest.raises(ValueError): audit(tmp_path/'missing', 'short-sha')


@pytest.mark.parametrize('value', [999, False, None])
def test_cumulative_trace_model_count_must_cover_prefix_and_continuation(fixture_root, value):
    path = fixture_root/LANE/'continued/duration/C/result.json'
    report = read(path)
    report['trace']['model_calls'] = value
    write(path, report)
    assert audit(fixture_root, COMMIT)['status'] == 'FAILED'


def test_cli_writes_failure_report_outside_input_and_never_overwrites(fixture_root, tmp_path):
    output = tmp_path/'report.json'
    args = [sys.executable, '-B', '-m', 'agent_runtime.experiments.bc_artifact_audit', '--root', str(fixture_root),
            '--expected-commit', 'd'*40, '--output', str(output)]
    process = subprocess.run(args, capture_output=True, timeout=10)
    assert process.returncode == 1 and read(output)['status'] == 'FAILED'
    before = output.read_bytes()
    assert subprocess.run(args, capture_output=True, timeout=10).returncode != 0
    assert output.read_bytes() == before
    args[-1] = str(fixture_root/'new-report.json')
    assert subprocess.run(args, capture_output=True, timeout=10).returncode == 2
    assert not (fixture_root/'new-report.json').exists()
