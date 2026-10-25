"""Read-only consistency audit of one scripted B/C CI artifact lane; not SWE grading."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import stat


FILES = {'duration': {'duration.py', 'settings.py', 'README.md'},
         'inventory': {'records.py', 'inventory.py', 'README.md'}}
PAIRS = {(task, arm) for task in FILES for arm in ('B', 'C')}


class EvidenceIssue(Exception):
    def __init__(self, status, code, path):
        self.status, self.code, self.path = status, code, path
        super().__init__(code)


def require(condition, code, path):
    if not condition:
        raise EvidenceIssue('FAILED', code, path)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def zero(value):
    return type(value) is int and value == 0


def digest_string(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


class Evidence:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def path(self, relative):
        path = self.root
        try:
            for part in relative.split('/'):
                if not part or part in {'.', '..'}:
                    raise ValueError('Noncanonical evidence path')
                path = path/part
                if path.is_symlink():
                    raise ValueError('Symlink in evidence path')
            path.resolve().relative_to(self.root)
            return path
        except (OSError, ValueError):
            raise EvidenceIssue('INCOMPLETE', 'UNSAFE_EVIDENCE_PATH', relative) from None

    def data(self, relative, limit=2*1024*1024):
        try:
            path = self.path(relative)
            if not stat.S_ISREG(path.stat().st_mode):
                raise ValueError('Not a regular artifact')
            with path.open('rb') as stream:
                data = stream.read(limit+1)
            if len(data) > limit:
                raise ValueError('Evidence size bound')
            return data
        except (OSError, ValueError):
            raise EvidenceIssue('INCOMPLETE', 'MISSING_OR_UNREADABLE_ARTIFACT', relative) from None

    def json(self, relative):
        try:
            return json.loads(self.data(relative).decode('utf-8-sig'))
        except (ValueError, UnicodeError):
            raise EvidenceIssue('INCOMPLETE', 'INVALID_JSON_ARTIFACT', relative) from None


def model_records(evidence, path):
    try:
        records = [json.loads(line) for line in evidence.data(path, 4*1024*1024).splitlines() if line.strip()]
    except ValueError:
        raise EvidenceIssue('INCOMPLETE', 'INVALID_MODEL_RECORDS', path) from None
    require(0 < len(records) <= 32, 'MODEL_RECORD_COUNT', path)
    require(all(isinstance(r, dict) and r.get('provider_tokens') is False and isinstance(r.get('response'), dict)
                for r in records), 'NOT_SCRIPTED_MODEL_RECORDS', path)
    return records


def manifest(evidence, base, commit):
    path = base+'/manifest.json'
    value = evidence.json(path)
    require(isinstance(value, dict), 'MANIFEST_SHAPE', path)
    require(value.get('baseline_commit') == commit, 'COMMIT_MISMATCH', path)
    require(value.get('protocol') == 'bc-f3-paired-pilot-v4' and value.get('mode') == 'scripted'
            and value.get('fault') == 'verified_subtask_f3', 'UNSUPPORTED_PROTOCOL', path)
    require(value.get('tasks') == list(FILES) and value.get('arms') == ['B', 'C'], 'PROTOCOL_MEMBERS', path)
    hashes = value.get('source_hashes')
    require(isinstance(hashes, dict) and bool(hashes) and all(isinstance(k, str) and digest_string(v)
            for k, v in hashes.items()), 'SOURCE_HASH_MANIFEST', path)
    return value


def rows(evidence, base):
    path = base+'/results.json'
    values = evidence.json(path)
    require(isinstance(values, list) and len(values) == 4 and all(isinstance(v, dict) for v in values),
            'RESULT_MEMBERS', path)
    require(all(isinstance(v.get('task'), str) and isinstance(v.get('arm'), str) for v in values),
            'RESULT_IDENTITY', path)
    result = {(v['task'], v['arm']): v for v in values}
    require(set(result) == PAIRS, 'RESULT_MEMBERS', path)
    return result


def inspect_lane(evidence, lane, commit):
    pilot, continued = lane+'/pilot', lane+'/continued'
    first = manifest(evidence, pilot, commit)
    second = manifest(evidence, continued, commit)
    require(first['source_hashes'] == second['source_hashes'], 'SOURCE_CHANGED_BETWEEN_RUNS', continued)
    require(second.get('prefix_source_manifest_sha256') == sha(evidence.data(pilot+'/manifest.json')),
            'PREFIX_MANIFEST_LINK', continued)
    original, replay = rows(evidence, pilot), rows(evidence, continued)
    prefix_calls, continuation_calls = 0, 0
    for task, names in FILES.items():
        prefix = pilot+'/'+task+'/prefix'
        fault = evidence.json(prefix+'/fault.json')
        process = evidence.json(prefix+'/process.json')
        require(isinstance(fault, dict) and fault.get('point') == 'verified_subtask_f3'
                and isinstance(process, dict) and type(process.get('exit_code')) is int
                and process['exit_code'] == 73, 'MISSING_REAL_F3_EXIT', prefix)
        task_id = fault.get('context', {}).get('task_id') if isinstance(fault.get('context'), dict) else None
        require(isinstance(task_id, str) and bool(task_id), 'PREFIX_TASK_ID', prefix)
        prefix_records = model_records(evidence, prefix+'/model_requests.jsonl')
        prefix_calls += len(prefix_records)
        require(evidence.data(prefix+'/model_requests.jsonl') == evidence.data(continued+'/'+task+'/prefix/model_requests.jsonl'),
                'PREFIX_REQUESTS_CHANGED', prefix)
        require(evidence.json(continued+'/'+task+'/prefix/fault.json') == fault
                and evidence.json(continued+'/'+task+'/prefix/process.json') == process,
                'PREFIX_EXIT_CHANGED', prefix)
        reference = original[(task, 'B')]
        snapshot = pilot+'/'+task+'/snapshot'
        require(digest_string(reference.get('snapshot_database_sha256')), 'SNAPSHOT_DIGEST_SHAPE', snapshot)
        require(sha(evidence.data(snapshot+'/.agent_runtime/runtime.db', 32*1024*1024))
                == reference['snapshot_database_sha256'], 'SNAPSHOT_DATABASE_HASH', snapshot)
        hashes, identities = reference.get('initial_file_hashes'), reference.get('initial_file_identities')
        require(isinstance(hashes, dict) and set(hashes) == names and all(digest_string(v) for v in hashes.values()),
                'FIXTURE_FILE_MANIFEST', snapshot)
        require(isinstance(identities, dict) and set(identities) == names
                and all(isinstance(v, dict) and set(v) == {'st_dev', 'st_ino'}
                        and all(type(n) is int for n in v.values()) for v in identities.values()),
                'HISTORICAL_IDENTITY_MANIFEST', snapshot)
        for name in names:
            require(sha(evidence.data(snapshot+'/'+name)) == hashes[name], 'SNAPSHOT_FILE_HASH', snapshot+'/'+name)
        for run, values in ((pilot, original), (continued, replay)):
            for arm in ('B', 'C'):
                row = values[(task, arm)]
                phase = run+'/'+task+'/'+arm
                require(zero(row.get('exit_code')) and row.get('status') == 'completed'
                        and row.get('final_pass') is True and row.get('completed_parser_unchanged') is True
                        and zero(row.get('invariant_violations')), 'FAILED_RESULT_ROW', phase)
                require(all(row.get(k) == reference.get(k) for k in
                        ('snapshot_database_sha256', 'initial_file_hashes', 'initial_file_identities')),
                        'PAIRED_SNAPSHOT_MISMATCH', phase)
                result, process = evidence.json(phase+'/result.json'), evidence.json(phase+'/process.json')
                require(isinstance(result, dict) and isinstance(process, dict) and zero(process.get('exit_code')),
                        'PROCESS_RESULT_CONTRADICTION', phase)
                runtime_result, trace, oracle = result.get('result'), result.get('trace'), result.get('final_check')
                require(all(isinstance(v, dict) for v in (runtime_result, trace, oracle)), 'RESULT_SHAPE', phase)
                require(runtime_result.get('status') == trace.get('status') == 'completed'
                        and runtime_result.get('task_id') == trace.get('task_id') == task_id
                        and runtime_result.get('error') is None and oracle.get('passed') is True,
                        'RESULT_AUTHORITY_CONTRADICTION', phase)
                require(all(zero(trace.get(k)) for k in ('invariant_violation_count', 'permission_bypass_count',
                            'stale_lease_execution_attempts', 'confirmed_duplicate_side_effects')),
                        'TRACE_INVARIANT_FAILURE', phase)
                calls = model_records(evidence, phase+'/model_requests.jsonl')
                require(type(row.get('post_restart_calls')) is int and row['post_restart_calls'] == len(calls),
                        'MODEL_CALL_COUNT_MISMATCH', phase)
                require(type(trace.get('model_calls')) is int
                        and trace['model_calls'] == len(prefix_records)+len(calls),
                        'TRACE_MODEL_COUNT_MISMATCH', phase)
                continuation_calls += len(calls)
    return {'result_rows_checked': 8, 'scripted_prefix_calls': prefix_calls,
            'scripted_continuation_calls': continuation_calls}


def audit(root, expected_commit):
    if not isinstance(expected_commit, str) or not re.fullmatch('[0-9a-f]{40}', expected_commit):
        raise ValueError('A full expected Git SHA from the CI run is required')
    report = {'schema_version': 1, 'expected_commit': expected_commit,
              'scope': 'Scripted B/C artifact consistency only; not Runtime or SWE-bench certification',
              'findings': []}
    evidence = Evidence(root)
    try:
        try:
            lanes = list(evidence.root.iterdir())
        except OSError:
            raise EvidenceIssue('INCOMPLETE', 'MISSING_ARTIFACT_ROOT', '.') from None
        require(len(lanes) == 1 and re.fullmatch('[0-9a-f]{32}', lanes[0].name) is not None,
                'AMBIGUOUS_ARTIFACT_LANES', '.')
        lane = evidence.path(lanes[0].name)
        require(lane.is_dir(), 'ARTIFACT_LANE_NOT_DIRECTORY', lane.name)
        report.update(inspect_lane(evidence, lane.name, expected_commit))
        report['status'] = 'PASS'
    except EvidenceIssue as exc:
        report['status'] = exc.status
        report['findings'].append({'code': exc.code, 'path': exc.path})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--expected-commit', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(args.root.resolve()):
        parser.error('Report output must be outside input artifacts')
    report = audit(args.root, args.expected_commit)
    # Never overwrite evidence or a prior report; the caller owns this output.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2)
        stream.write('\n')
    print(json.dumps(report))
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
