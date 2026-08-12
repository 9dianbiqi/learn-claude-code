# Deterministic demo walkthrough

The demo uses `ScriptedModel`, the same Runtime entry point, real repository
tools, and real SQLite files. It does not call an external model.

## Run all scenarios

```powershell
$root = "F:\CodexTemp\agent-runtime-demo"
python -m agent_runtime.demos.run_demo --output-root $root
```

The command refuses to overwrite a non-empty output directory. Pick a new path
for each run or remove an old disposable demo directory yourself.

## Scenario 1: normal edit

The model reads `note.txt`, receives an allowed read result, requests an edit,
and receives deterministic approval. Expected evidence:

- task status `completed`;
- final content `NEW_VALUE`;
- one read and one file-write tool call;
- one confirmed effect reservation;
- permission bypasses and invariant violations both zero.

## Scenario 2: crash after effect

A fault is injected after atomic replacement but before effect completion is
persisted. A new Runtime owner resumes the task.

Expected evidence:

- the file already contains `RECOVERED_VALUE` after the injected crash;
- resume compares the file to expected-after SHA-256;
- the original reservation becomes completed;
- `effect_attempts` remains one;
- an event records `tool_recovered_succeeded`;
- final task status is `completed`.

## Scenario 3: external hash conflict

After the task reads `note.txt`, the demo simulates an external editor changing
the file before `edit_file` executes.

Expected evidence:

- task status `needs_review`;
- external content `VERSION_EXTERNAL` remains unchanged;
- the attempted edit does not silently overwrite it;
- no confirmed file effect is recorded.

## Inspect the output

```powershell
Get-ChildItem -Recurse F:\CodexTemp\agent-runtime-demo
Get-Content F:\CodexTemp\agent-runtime-demo\normal-edit\trace.jsonl
```

Each scenario directory contains:

```text
note.txt
.agent_runtime/runtime.db
trace.jsonl
summary.json
```

The SQLite database can be inspected using Python's built-in `sqlite3` module;
the standalone `sqlite3` command is not required.
