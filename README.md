# sessionlog

[![CI](https://github.com/npow/agenttrace/actions/workflows/ci.yml/badge.svg)](https://github.com/npow/agenttrace/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sessionlog)](https://pypi.org/project/sessionlog/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/) [![Docs](https://img.shields.io/badge/docs-mintlify-18a34a?style=flat-square)](https://mintlify.com/npow/sessionlog)

See exactly what your AI coding agent did — every tool call, error, and decision, ingested in real time.

## The problem

Coding agents generate rich session logs, but those logs are usually raw JSONL blobs in hidden folders. There's no way to query your sessions, spot error patterns, or understand where the agent stumbled. You're flying blind on what your agent actually did.

## Quick start

```bash
pip install sessionlog
sessionlog ingest        # one-shot: parse all sessions into SQLite
sessionlog start         # daemon: watch for new sessions in real time
```

Your sessions are now in `~/.sessionlog/data.sqlite` — query with any SQLite client.

## Current support status

`sessionlog` now supports multiple **source directories**, but parser support is still **format-specific**.

| Agent | Source discovery | Format parsing | Tool-call extraction |
| --- | --- | --- | --- |
| Claude Code | Yes (default) | Yes (validated) | Yes (`tool_use`, `tool_result`, `agent_progress`, `bash_progress`) |
| Codex (CLI / coding agent logs) | Yes (default path) | Yes (validated from local sample logs) | Yes (`response_item:function_call`, `function_call_output`) |
| Cursor | Yes (default path) | Partial (validated transcript format: `agent-transcripts/*.txt`) | No native tool-event extraction from transcript files |
| Antigravity | Yes (default `~/.gemini/antigravity`) | Partial: protobuf storage plus validated `brain/<id>/*.md(.resolved*)` and `code_tracker/active/**` artifacts | Partial: artifact/revision ingestion + bash-snippet heuristics (no protobuf-native tool events yet) |
| Other agents (Copilot, Windsurf, Cline, Roo, Aider, Gemini, Continue, OpenCode) | Yes (default paths + `--sources-dir`) | Not yet validated as native formats | Pending per-agent adapters |

Important: source support and parser support are different. We now have verified adapters for Claude + Codex, partial Cursor transcript ingestion, and partial Antigravity artifact ingestion from `brain/*.md`, `brain/*.resolved*`, and `code_tracker/active/**`. Other agents require schema samples + adapters for accurate tool analytics.

All ingested rows now include `agent_type` in SQLite (`raw_entries.agent_type`, `progress_entries.agent_type`) so cross-agent analytics can disambiguate source reliably.

## Install

```bash
pip install sessionlog
```

From source:

```bash
git clone https://github.com/npow/agenttrace.git
cd agenttrace
make dev-install
source .venv/bin/activate
```

## Usage

### One-shot ingestion

Parse all existing sessions from known sources (Claude, Codex, Cursor):

```bash
sessionlog ingest
# Done. 42/42 files ingested, 18,302 raw entries, 5,841 progress entries
# (0 skipped, 0 failed). DB totals: 18302 entries, 127 sessions, 12 projects.
```

### Real-time daemon

Watch default agent session roots and ingest new entries as sessions run:

```bash
sessionlog start
# Watching claude=/Users/you/.claude/projects, codex=/Users/you/.codex/sessions, cursor=/Users/you/.cursor/projects → /Users/you/.sessionlog/data.sqlite
```

### Re-ingest from scratch

```bash
sessionlog ingest --force
```

### Custom paths

```bash
sessionlog start \
  --db /path/to/my.sqlite \
  --sources-dir codex=/path/to/codex/sessions \
  --sources-dir cursor=/path/to/cursor/sessions \
  --sources-dir claude=/path/to/claude/projects
```

You can also set defaults via `SESSIONLOG_SOURCES`:

```bash
export SESSIONLOG_SOURCES="codex=~/.codex/sessions,cursor=~/.cursor/projects,claude=~/.claude/projects"
```

## How it works

`sessionlog` watches one or more source directories for JSONL session files using watchdog. When a file changes, it runs an incremental parse: only new lines are read, tool calls and errors are classified, and everything is written to SQLite with WAL mode for concurrent access. A 30-second debounce prevents redundant ingestion when many files change at once.

### Tool-call parsing details

Today, tool extraction logic is aligned with Claude-style JSONL:

- Assistant tool calls: `message.content[]` blocks with `type="tool_use"`
- Tool results/errors: `message.content[]` blocks with `type="tool_result"` + `is_error`
- Progress events: top-level `type="progress"` with `data.type in {"agent_progress","bash_progress"}`

If another agent emits different field names/shapes, tool metrics can be incomplete or wrong until a dedicated adapter is implemented.

### Verified on this machine

- Codex: JSONL with `response_item` records (`function_call`, `function_call_output`, `message`)
- Cursor: project data + `agent-transcripts/*.txt` files (plain text transcript sections)
- Antigravity: protobuf files under `~/.gemini/antigravity/conversations/*.pb` and `annotations/*.pbtxt`
- Antigravity: markdown artifacts under `~/.gemini/antigravity/brain/<conversation-id>/*.md` with `*.metadata.json` timestamps
- Antigravity: high-entropy protobuf blobs appear encrypted/compressed-at-rest; `protoc --decode_raw` does not decode directly

## Popular agents to target next

Frequently used agents/editors in current workflows include:

- OpenAI Codex / Codex CLI
- Cursor
- GitHub Copilot Agent
- Windsurf (Cascade)
- Cline / Roo Code
- Aider
- Gemini CLI
- Continue
- Antigravity
- OpenCode

These should be treated as separate parser targets (not just directory aliases), each with fixture-based tests against real sample logs.

## Development

```bash
git clone https://github.com/npow/agenttrace.git
cd agenttrace
make test
```

## License

Apache-2.0 — see [LICENSE](LICENSE).
