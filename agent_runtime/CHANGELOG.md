# Changelog

## v0.1.1 — operator safety and usability

- Added hard default protection for `.env*`, `.git/**`, private-key formats,
  `.netrc`, and credential/secret file families across file, glob, and Shell
  access paths.
- Added `list`, `show`, `pending`, `events`, `doctor`, and `db-check` commands.
- Scoped CLI arguments to their actual subcommands and made task/tool IDs and
  reconciliation actions explicit required arguments.
- Added SQLite integrity checks and pending-call queries to `EventStore`.
- Added a deterministic three-scenario demo with SQLite and JSONL evidence.
- Added architecture/demo documentation and a Windows/Linux acceptance CI
  workflow with Runtime tests, MVP Eval, Hardening Eval, and demo smoke.

## v0.1.0 — frozen durable Runtime baseline

- Added SQLite WAL checkpoints, append-only events, durable model/tool state,
  file observations, leases, fencing, and effect reservations.
- Added allow/ask/deny permissions, repository containment, read-before-edit,
  SHA-256 conflict detection, atomic replacement, and explicit reconciliation.
- Added trace metrics, deterministic fault injection, MVP Eval, Hardening Eval,
  and controlled real-model read/write/Shell acceptance evidence.
