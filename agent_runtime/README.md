# Agent Runtime MVP

This package is a small, local runtime for durable coding-agent tasks. It keeps task state, model responses, tool calls, file observations, checkpoints, and an append-only event log in `.agent_runtime/runtime.db` inside the target repository.

The deterministic acceptance suite can be run without API credentials:

```text
python -m agent_runtime eval --suite evals/mvp.yaml --runs .agent_runtime/eval-runs
python -m pytest agent_runtime/tests -q
```

The live CLI uses the repository's existing `MODEL_ID`, `ANTHROPIC_BASE_URL`, and compatible credentials:

```text
python -m agent_runtime run --repo . --prompt "Inspect the failing tests and report the cause"
python -m agent_runtime status TASK_ID --repo .
python -m agent_runtime resume TASK_ID --repo .
python -m agent_runtime trace TASK_ID --repo . --output trace.jsonl
```

File writes require approval and an existing file must have been read in the same task before it can be edited. A crash during a verifiable file write is reconciled using the before and expected-after SHA-256 values; ambiguous shell writes stop in `needs_review`.
