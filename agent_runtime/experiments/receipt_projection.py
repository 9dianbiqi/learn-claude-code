"""Experimental, read-only historical receipts. Never probes workspace files."""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from pathlib import Path

from agent_runtime.projector import estimate_tokens

TIME_NOTE = (
    "\nTemporal scope: relevant_path_state and remaining_turn_budget in the above "
    "base bundle describe the initial resume snapshot. They are not assertions "
    "about the current filesystem or the current remaining budget."
)
RECEIPT_NOTE = (
    "\nHistorical committed-effect evidence: each receipt records a past write "
    "whose post-write hash matched. observed_at is the recorded post-check "
    "timestamp; evidence_checkpoint_id is the first execution checkpoint "
    "containing that tool result, not the atomic effect-commit transaction. "
    "This is not a current filesystem assertion or semantic verifier pass.\n"
)
MAX_RECEIPTS = 1
MAX_RECEIPT_CHARS = 2000


def historical_receipts(database: Path, task_id: str, boundary_id: int, paths: set[str]):
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        task = connection.execute("SELECT checkpoint_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        upper = int(task['checkpoint_id'])
        checkpoints = connection.execute(
            "SELECT checkpoint_id,messages_json,created_at FROM checkpoints WHERE task_id=? AND checkpoint_id>? AND checkpoint_id<=? ORDER BY checkpoint_id",
            (task_id, boundary_id, upper)).fetchall()
        first_result = {}
        for checkpoint in checkpoints:
            for message in json.loads(checkpoint['messages_json']):
                if message.get('role') != 'user' or not isinstance(message.get('content'), list):
                    continue
                for block in message['content']:
                    if block.get('type') == 'tool_result':
                        first_result.setdefault(block['tool_use_id'], (checkpoint['checkpoint_id'], checkpoint['created_at']))
        # Exclude pre-boundary tool results repeated in full-history checkpoints.
        boundary = connection.execute("SELECT messages_json FROM checkpoints WHERE task_id=? AND checkpoint_id=?", (task_id, boundary_id)).fetchone()
        if boundary is None:
            raise ValueError('Missing frozen resume boundary')
        old_ids = {b['tool_use_id'] for m in json.loads(boundary[0]) if isinstance(m.get('content'), list)
                   for b in m['content'] if b.get('type') == 'tool_result'}
        rows = connection.execute(
            "SELECT o.operation_id,o.state,o.completed_at,o.probe_evidence_json,t.tool_use_id,t.name,t.args_json,t.effect,t.effect_confirmed,t.status,t.finished_at "
            "FROM operations o JOIN tool_calls t ON t.operation_id=o.operation_id WHERE o.task_id=? AND t.task_id=? ORDER BY o.completed_at DESC,o.operation_id",
            (task_id, task_id)).fetchall()
        receipts = []
        for row in rows:
            if row['state'] != 'committed' or row['status'] != 'succeeded' or row['effect'] != 'file_write' or row['effect_confirmed'] != 1:
                continue
            tool_id = row['tool_use_id']
            if tool_id in old_ids or tool_id not in first_result:
                continue
            evidence = json.loads(row['probe_evidence_json'] or '{}')
            if evidence.get('outcome') != 'post_hash_match':
                continue
            args = json.loads(row['args_json'])
            path = str(args.get('path', '')).replace('\\', '/')
            if path.startswith('./'):
                path = path[2:]
            if path not in paths:
                continue
            sha = evidence.get('sha256')
            if not isinstance(sha, str) or len(sha) != 64 or any(ch not in '0123456789abcdef' for ch in sha):
                continue
            if first_result[tool_id][1] < row['completed_at']:
                continue
            written = None
            if row['name'] == 'write_file':
                content = args['content'].encode('utf-8')
                if hashlib.sha256(content).hexdigest() != sha:
                    continue
                written = len(content)
            receipts.append({
                'operation_id': row['operation_id'], 'tool_call_id': tool_id, 'path': path,
                'effect_type': 'file_write', 'committed_after_sha256': sha, 'bytes_written': written,
                'commit_check': 'post_hash_match', 'evidence_checkpoint_id': first_result[tool_id][0],
                'observed_at': row['finished_at'], 'freshness': 'historical_committed_effect'})
        return receipts, upper
    finally:
        connection.close()


def render(messages, condition, receipts, *, max_receipts=MAX_RECEIPTS,
           max_chars=MAX_RECEIPT_CHARS, budget=16000):
    view = copy.deepcopy(messages)
    selected = []
    eligible = len(receipts)
    if condition != 'C':
        if not view or view[0].get('role') != 'system' or not isinstance(view[0].get('content'), str):
            raise ValueError('Expected a scoped system bundle')
        view[0]['content'] += TIME_NOTE
    if condition == 'C-receipt' and receipts:
        for receipt in receipts[:max_receipts]:
            candidate = selected + [receipt]
            if len(json.dumps(candidate, ensure_ascii=False, sort_keys=True)) > max_chars:
                break
            selected = candidate
        payload = {'receipts': selected, 'omitted_count': eligible-len(selected)}
        view[0]['content'] += RECEIPT_NOTE + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    if estimate_tokens(json.dumps(view, ensure_ascii=False, sort_keys=True, separators=(',', ':'))) > budget:
        raise ValueError('Experimental projection exceeds unchanged scoped budget')
    return view, {'eligible_receipts': receipts, 'exposed_receipts': selected,
                  'omitted_receipts': eligible-len(selected) if condition == 'C-receipt' else 0}
