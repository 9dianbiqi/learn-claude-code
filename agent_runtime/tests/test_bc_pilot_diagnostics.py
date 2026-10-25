"""Failure evidence is exercised at actual worker and subprocess boundaries."""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent_runtime.experiments import bc_pilot


@pytest.mark.parametrize('failure_stage', ['runtime_init', 'resume'])
def test_worker_preserves_scripted_failure_stage_and_traceback(tmp_path, monkeypatch, failure_stage):
    directory = tmp_path/'C'
    directory.mkdir()
    args = SimpleNamespace(workspace=str(tmp_path/'workspace'), directory=str(directory),
                           task='duration', phase='C', mode='scripted')
    class BrokenRuntime:
        def __init__(self, *args, **kwargs):
            if failure_stage == 'runtime_init':
                raise RuntimeError('injected construction failure')
            self.store = SimpleNamespace(list_tasks=lambda: [{'task_id': 'fixture-task'}])
        def resume(self, task):
            raise RuntimeError('injected resume failure')
    monkeypatch.setattr(bc_pilot, 'Runtime', BrokenRuntime)
    assert bc_pilot.worker(args) == 2
    error = json.loads((directory/'error.json').read_text(encoding='utf-8'))
    assert error['stage'] == failure_stage
    assert error['task'] == 'duration' and error['phase'] == 'C'
    assert error['type'] == 'RuntimeError' and 'injected' in error['message']
    assert 'Traceback' in error['traceback'] and 'RuntimeError' in error['traceback']
    assert not (directory/'result.json').exists()


@pytest.mark.parametrize('artifact', ['valid', 'malformed', 'oversized', 'missing'])
def test_parent_keeps_bounded_evidence_for_unexpected_exit(tmp_path, artifact):
    error = {'type': 'InjectedError', 'stage': 'resume', 'message': 'fixture failure'}
    if artifact != 'missing':
        payload = json.dumps(error) if artifact == 'valid' else ('bad-json' if artifact == 'malformed' else 'x'*65537)
        (tmp_path/'error.json').write_text(payload, encoding='utf-8')
    (tmp_path/'process.log').write_bytes(b'x'*10000+b'FINAL_FAILURE_MARKER')
    value = bc_pilot.process_diagnostic(tmp_path, task='duration', phase='C', mode='scripted', exit_code=2)
    assert value['exit_code'] == 2 and value['phase'] == 'C'
    assert value['process_log_tail'].endswith('FINAL_FAILURE_MARKER')
    assert len(value['process_log_tail'].encode()) <= 8192
    if artifact == 'valid':
        assert value['worker_error'] == error
    elif artifact != 'missing':
        assert value['error_artifact_failure']
    assert 'worker_error' not in bc_pilot.process_diagnostic(tmp_path, task='duration', phase='C', mode='live', exit_code=2)
    assert 'process_log_tail' not in bc_pilot.process_diagnostic(tmp_path, task='duration', phase='C', mode='live', exit_code=2)


def test_launch_retains_real_child_traceback_when_no_error_json(tmp_path, monkeypatch):
    original = subprocess.run
    def failing_child(command, **kwargs):
        # Exercise the real subprocess -> process.log -> diagnostic.json seam.
        return original([sys.executable, '-c', "raise RuntimeError('CHILD_FAILURE_EVIDENCE')"], **kwargs)
    monkeypatch.setattr(bc_pilot.subprocess, 'run', failing_child)
    args = SimpleNamespace(mode='scripted', env_file=None)
    directory = tmp_path/'C'
    code = bc_pilot.launch(args, 'duration', 'C', tmp_path/'workspace', directory)
    assert code != 0
    value = json.loads((directory/'diagnostic.json').read_text(encoding='utf-8'))
    assert value['exit_code'] == code and 'Traceback' in value['process_log_tail']
    assert 'CHILD_FAILURE_EVIDENCE' in value['process_log_tail']
    assert json.loads((directory/'process.json').read_text())['exit_code'] == code


def test_log_read_failure_cannot_replace_child_exit_code(tmp_path, monkeypatch):
    log = tmp_path/'process.log'
    log.write_text('fixture log', encoding='utf-8')
    original = Path.open
    def locked(path, *args, **kwargs):
        if path == log:
            raise PermissionError('injected log sharing violation')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', locked)
    value = bc_pilot.process_diagnostic(tmp_path, task='duration', phase='C', mode='scripted', exit_code=2)
    assert value['exit_code'] == 2 and value['log_artifact_failure'] == 'PermissionError'


def test_prefix_failure_reaches_parent_results_and_console(tmp_path, monkeypatch, capsys):
    def broken_launch(args, task, phase, workspace, directory):
        assert phase == 'prefix'
        directory.mkdir()
        (directory/'error.json').write_text(json.dumps({'type': 'InjectedError', 'stage': 'runtime_init',
            'message': 'PARENT_FAILURE_EVIDENCE'}), encoding='utf-8')
        return 2
    monkeypatch.setattr(bc_pilot, 'launch', broken_launch)
    output = tmp_path/'parent'
    args = SimpleNamespace(output=str(output), mode='scripted', resume_from=None, env_file=None)
    assert bc_pilot.supervise(args) == 1
    rows = json.loads((output/'results.json').read_text(encoding='utf-8'))
    assert len(rows) == len(bc_pilot.TASKS)
    assert all(r['exit_code'] == 2 and r['status'] == 'prefix_failed_or_fault_not_reached' for r in rows)
    assert all(r['worker_diagnostic']['worker_error']['stage'] == 'runtime_init' for r in rows)
    assert 'PARENT_FAILURE_EVIDENCE' in capsys.readouterr().out
