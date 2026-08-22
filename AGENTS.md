# learn-claude-code — A teaching repository for agent harness engineering

This repo is a 0→1 progressive course: 12 self-contained Python scripts (s01–s12) + capstone. Each script layers one harness mechanism on the `while stop_reason == "tool_use"` agent loop.

## Quick navigation

| Session | Topic | What it adds |
|---------|-------|-------------|
| s01 | Agent Loop | `while` + `stop_reason` |
| s02 | Tool Use | dispatch map: name → handler |
| s03 | TodoWrite | step-first planning |
| s04 | Subagents | independent messages[] per child |
| s05 | Skills | on-demand knowledge via YAML frontmatter |
| s06 | Context Compact | 3-layer compression |
| s07 | Task System | file-based CRUD + dependency graph |
| s08 | Background Tasks | daemon threads + notification queue |
| s09 | Agent Teams | persistent teammates + JSONL mailboxes |
| s10 | Team Protocols | shutdown + plan approval FSM |
| s11 | Autonomous Agents | idle cycle + auto-claim |
| s12 | Worktree Isolation | directory-level execution lanes |

## Setup & run

```sh
pip install -r requirements.txt        # anthropic, python-dotenv, pyyaml
cp .env.example .env                    # fill in ANTHROPIC_API_KEY + MODEL_ID
python agents/s01_agent_loop.py         # start at session 1
python agents/s_full.py                 # capstone: all mechanisms combined
```

The existing `.env` has Chinese-localized defaults (`MODEL_ID=glm-5`, `ANTHROPIC_BASE_URL=https://open.bigmodel.cn/api/anthropic`). `.env` is gitignored; `.env.example` documents alternative providers (MiniMax, GLM, Kimi, DeepSeek) with SWE-bench scores and base URLs.

## Architecture

Each agent in `agents/` is standalone and self-contained. They are **not** a library — there is no shared module. The pattern is identical across all scripts:

```python
def agent_loop(messages):
    while True:
        response = client.messages.create(model=MODEL, ...)
        if response.stop_reason != "tool_use": return
        for block in response.content:
            if block.type == "tool_use":
                execute_tool(block.name, block.input)  # one handler per tool
```

- Uses the Anthropic Python SDK with `ANTHROPIC_BASE_URL` support for compatible providers
- Reads `MODEL_ID` from env (NOT a hardcoded model)
- Strips `ANTHROPIC_AUTH_TOKEN` when `ANTHROPIC_BASE_URL` is set
- `load_dotenv(override=True)` — env takes precedence over existing values

## Directory layout

| Path | What |
|------|------|
| `agents/sNN_*.py` | 12 progressive harness implementations + `s_full.py` |
| `docs/{en,zh,ja}/` | 12 docs each, mental-model-first format |
| `skills/{name}/SKILL.md` | YAML frontmatter skill files (agent-builder, code-review, mcp-builder, pdf) |
| `tests/test_agents_smoke.py` | Compile smoke tests (py_compile, parametrized over all agent files) |
| `tests/test_s_full_background.py` | Unit test for BackgroundManager (mocks anthropic+dotenv, runs in tempdir) |
| `web/` | Next.js 16 / React 19 / Tailwind v4 / TypeScript learning platform |

## Testing

```sh
python -m pytest tests/ -q                           # all tests
python -m pytest tests/test_agents_smoke.py -q       # compile-only smoke test
```

- Smoke test parametrized over all agents/*.py (excluding `__init__.py`)
- CI runs on PRs/commits to `main` only

## CI (`.github/workflows/`)

Two workflows on `ubuntu-latest`:

| File | Job | Command |
|------|-----|---------|
| `test.yml` | Python smoke | `pip install anthropic python-dotenv pytest && pytest tests/test_agents_smoke.py -q` |
| `test.yml` | Web build | `npm ci && npm run build` (Node 20, in `web/`) |
| `ci.yml` | Web typecheck + build | `npm ci && npx tsc --noEmit && npm run build` (Node 20, in `web/`) |

## Web platform

```sh
cd web && npm install && npm run dev     # http://localhost:3000
```

- **Pre-build step**: `npm run extract` (runs `tsx scripts/extract-content.ts`) — auto-runs via `predev`/`prebuild`
- Static export: `output: "export"` in `next.config.ts`
- i18n: locale-based routing via `[locale]/` directory
- Highlighting: `rehype-highlight` for code blocks
- Animation: `framer-motion` (v12)

## Non-obvious facts

- **Each script is fully self-contained.** There is no shared module. Editing one never breaks another.
- **Env precedence**: `load_dotenv(override=True)` — .env overrides existing env vars.
- **Auth quirk**: When `ANTHROPIC_BASE_URL` is set, the code explicitly pops `ANTHROPIC_AUTH_TOKEN` to avoid auth header conflicts.
- **naming convention**: `sNN_sessionname.py` — NN is zero-padded so `ls agents/` sorts correctly.
- **Skills format**: YAML frontmatter in `skills/{name}/SKILL.md` with `name` and `description` fields; rest is markdown body.
- **Runtime artifacts** (gitignored): `.tasks/`, `.team/`, `.transcripts/`, `.task_outputs/` — all created at runtime by agents s07 and later.
- **s12** (worktree isolation) requires `git` and detects the repo root.
- **The phrase "Bash is all you need"** is the repo's tagline — s01 uses only a `bash` tool.

## User preferences

- **Notes directory**: `D:\obisidian\笔记库` (also available as `$env:NOTES_DIR`)
- **Project notes folder**: `D:\obisidian\笔记库\learn-claude-code\` — all learn-claude-code study notes go here
  - Legacy old-track notes (s01-s07) archived in `legacy/`
  - Learning plans saved in `plan/`
  - New 20-chapter notes saved as `sNN-name.md` directly in root
  - `lcc-design.md` for personal lcc.py architecture notes
- **Standard command**: "保存当前对话到笔记库" or "将当前内容以 md 格式保存到笔记库"
  - File naming format: `{topic}-{date}.md`
  - Always use UTF-8 encoding without BOM

## Agent skills

### Issue tracker

GitHub Issues in `9dianbiqi/learn-claude-code`, using the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Domain docs

Single-context: `CONTEXT.md` at the repo root plus `docs/adr/`. See `docs/agents/domain.md`.

## Projects

The repository hosts two independent projects:

- **Teaching track**: the progressive course in `agents/`, `docs/{en,zh,ja}/`,
  `skills/`, `tests/` and the `sNN_*` example directories. It remains a
  self-contained learning repo.
- **agent_runtime**: an independent runtime project under `agent_runtime/`,
  with its own README, CLI, schema migrations, tests and evidence pipeline.
  Treat it as a separate codebase even though it lives in the same git repo.

Reuse rule: teaching scripts are self-contained demos, not an importable
library. When a runtime phase needs a teaching mechanism, port the relevant
module into `agent_runtime/` with tests and record the source in the phase doc
or an ADR instead of importing from `agents/` or `sNN_*/`.
