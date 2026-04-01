"""Integration tests for ingest_file() — full pipeline from JSONL → SQLite."""

import importlib
import json
import os
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(db_path: Path):
    """Set env, reload modules, return (conn, db_mod, ingest_mod)."""
    os.environ["CLAUDE_RETRO_DB"] = str(db_path)
    import sessionlog.config as cfg
    import sessionlog.db as db_mod
    import sessionlog.ingest as ingest_mod

    importlib.reload(cfg)
    importlib.reload(db_mod)
    importlib.reload(ingest_mod)

    conn = db_mod.get_writer()
    return conn, db_mod, ingest_mod


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with open(path, "w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


# ---------------------------------------------------------------------------
# Sample records
# ---------------------------------------------------------------------------

RAW_ASSISTANT = {
    "type": "assistant",
    "uuid": "raw-asst-1",
    "sessionId": "session-int-1",
    "timestamp": "2024-01-15T10:00:00Z",
    "parentUuid": None,
    "isSidechain": False,
    "gitBranch": "main",
    "cwd": "/project",
    "message": {
        "model": "claude-opus-4-6",
        "content": [
            {
                "type": "tool_use",
                "id": "tu1",
                "name": "Read",
                "input": {"file_path": "/foo.py"},
            },
        ],
        "usage": {"input_tokens": 200, "output_tokens": 30},
    },
}

RAW_USER_TOOL_RESULT = {
    "type": "user",
    "uuid": "raw-user-1",
    "sessionId": "session-int-1",
    "timestamp": "2024-01-15T10:00:01Z",
    "parentUuid": "raw-asst-1",
    "isSidechain": False,
    "message": {
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "tu1",
                "content": "file content here",
                "is_error": False,
            }
        ]
    },
}

PROGRESS_AGENT = {
    "type": "progress",
    "uuid": "prog-1",
    "sessionId": "session-int-1",
    "parentUuid": "parent-task-id",
    "timestamp": "2024-01-15T10:00:02Z",
    "data": {
        "type": "agent_progress",
        "message": {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "sub-bash-1",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    },
                ],
            },
        },
    },
}

PROGRESS_BASH = {
    "type": "progress",
    "uuid": "bash-prog-1",
    "sessionId": "session-int-1",
    "parentUuid": "bash-tool-id",
    "timestamp": "2024-01-15T10:00:03Z",
    "data": {
        "type": "bash_progress",
        "output": "running...",
    },
}

PROGRESS_MCP = {
    "type": "progress",
    "uuid": "mcp-prog-1",
    "sessionId": "session-int-1",
    "timestamp": "2024-01-15T10:00:04Z",
    "data": {
        "type": "mcp_progress",
        "toolUseId": "mcp-tool-id",
    },
}

SYSTEM_TURN = {
    "type": "system",
    "subtype": "turn_duration",
    "uuid": "sys-1",
    "sessionId": "session-int-1",
    "timestamp": "2024-01-15T10:00:05Z",
    "durationMs": 5000,
}

CODEX_SESSION_META = {
    "timestamp": "2026-02-27T19:10:35.817Z",
    "type": "session_meta",
    "payload": {"id": "codex-session-1"},
}

CODEX_TOOL_CALL = {
    "timestamp": "2026-02-27T19:10:46.837Z",
    "type": "response_item",
    "payload": {
        "type": "function_call",
        "name": "exec_command",
        "arguments": '{"cmd":"ls -la"}',
        "call_id": "call_abc123",
    },
}

CODEX_TOOL_RESULT = {
    "timestamp": "2026-02-27T19:10:46.957Z",
    "type": "response_item",
    "payload": {
        "type": "function_call_output",
        "call_id": "call_abc123",
        "output": "Process exited with code 0",
    },
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestIngestFileCounts:
    def test_ingest_file_returns_correct_raw_and_progress_counts(self, tmp_path):
        db_path = tmp_path / "test.sqlite"
        conn, _, ingest_mod = _make_db(db_path)

        jsonl = tmp_path / "session.jsonl"
        _write_jsonl(
            jsonl,
            [
                RAW_ASSISTANT,
                RAW_USER_TOOL_RESULT,
                SYSTEM_TURN,
                PROGRESS_AGENT,
                PROGRESS_BASH,
                PROGRESS_MCP,  # should NOT be counted
            ],
        )

        raw_count, progress_count = ingest_mod.ingest_file(jsonl, "proj", conn)

        # 3 raw records: assistant, user, system
        assert raw_count == 3
        # 2 progress records: agent_progress + bash_progress (mcp_progress excluded)
        assert progress_count == 2


class TestProgressEntriesTable:
    def test_progress_entries_stored_after_ingest(self, tmp_path):
        db_path = tmp_path / "test.sqlite"
        conn, _, ingest_mod = _make_db(db_path)

        jsonl = tmp_path / "session.jsonl"
        _write_jsonl(jsonl, [PROGRESS_AGENT, PROGRESS_BASH])

        ingest_mod.ingest_file(jsonl, "proj", conn)

        rows = conn.execute(
            "SELECT entry_id FROM progress_entries ORDER BY entry_id"
        ).fetchall()
        entry_ids = {r[0] for r in rows}
        assert "prog-1" in entry_ids
        assert "bash-prog-1" in entry_ids

    def test_agent_progress_stored_with_correct_tool_name_and_has_result(
        self, tmp_path
    ):
        db_path = tmp_path / "test.sqlite"
        conn, _, ingest_mod = _make_db(db_path)

        jsonl = tmp_path / "session.jsonl"
        _write_jsonl(jsonl, [PROGRESS_AGENT])

        ingest_mod.ingest_file(jsonl, "proj", conn)

        row = conn.execute(
            "SELECT progress_type, tool_name, has_result, result_error FROM progress_entries WHERE entry_id = ?",
            ["prog-1"],
        ).fetchone()
        assert row is not None
        progress_type, tool_name, has_result, result_error = row
        assert progress_type == "agent_progress"
        assert tool_name == "Bash"
        assert (
            has_result == 0
        )  # assistant message with tool_use; result comes in a separate user message
        assert result_error == 0

    def test_bash_progress_stored_with_correct_progress_type_and_no_tool_name(
        self, tmp_path
    ):
        db_path = tmp_path / "test.sqlite"
        conn, _, ingest_mod = _make_db(db_path)

        jsonl = tmp_path / "session.jsonl"
        _write_jsonl(jsonl, [PROGRESS_BASH])

        ingest_mod.ingest_file(jsonl, "proj", conn)

        row = conn.execute(
            "SELECT progress_type, tool_name, has_result FROM progress_entries WHERE entry_id = ?",
            ["bash-prog-1"],
        ).fetchone()
        assert row is not None
        progress_type, tool_name, has_result = row
        assert progress_type == "bash_progress"
        assert tool_name is None
        assert has_result == 0

    def test_mcp_progress_not_stored(self, tmp_path):
        db_path = tmp_path / "test.sqlite"
        conn, _, ingest_mod = _make_db(db_path)

        jsonl = tmp_path / "session.jsonl"
        _write_jsonl(jsonl, [PROGRESS_MCP])

        raw_count, progress_count = ingest_mod.ingest_file(jsonl, "proj", conn)

        assert progress_count == 0
        row = conn.execute(
            "SELECT entry_id FROM progress_entries WHERE entry_id = ?",
            ["mcp-prog-1"],
        ).fetchone()
        assert row is None


class TestRawEntriesTable:
    def test_raw_entries_stored_correctly(self, tmp_path):
        db_path = tmp_path / "test.sqlite"
        conn, _, ingest_mod = _make_db(db_path)

        jsonl = tmp_path / "session.jsonl"
        _write_jsonl(jsonl, [RAW_ASSISTANT, RAW_USER_TOOL_RESULT])

        ingest_mod.ingest_file(jsonl, "proj", conn)

        rows = conn.execute(
            "SELECT entry_id FROM raw_entries ORDER BY entry_id"
        ).fetchall()
        entry_ids = {r[0] for r in rows}
        assert "raw-asst-1" in entry_ids
        assert "raw-user-1" in entry_ids

    def test_assistant_entry_has_tool_names(self, tmp_path):
        db_path = tmp_path / "test.sqlite"
        conn, _, ingest_mod = _make_db(db_path)

        jsonl = tmp_path / "session.jsonl"
        _write_jsonl(jsonl, [RAW_ASSISTANT])

        ingest_mod.ingest_file(jsonl, "proj", conn)

        row = conn.execute(
            "SELECT tool_names FROM raw_entries WHERE entry_id = ?",
            ["raw-asst-1"],
        ).fetchone()
        assert row is not None
        tool_names = json.loads(row[0])
        assert "Read" in tool_names


class TestCodexIngestion:
    def test_codex_function_calls_and_outputs_ingest(self, tmp_path):
        db_path = tmp_path / "test.sqlite"
        conn, _, ingest_mod = _make_db(db_path)
        jsonl = tmp_path / "codex.jsonl"
        _write_jsonl(jsonl, [CODEX_SESSION_META, CODEX_TOOL_CALL, CODEX_TOOL_RESULT])

        raw_count, progress_count = ingest_mod.ingest_file(jsonl, "codex:demo", conn)
        assert raw_count == 2
        assert progress_count == 0

        tool_row = conn.execute(
            "SELECT entry_type, tool_names, agent_type FROM raw_entries WHERE content_types LIKE '%tool_use%' LIMIT 1"
        ).fetchone()
        assert tool_row is not None
        assert tool_row[0] == "assistant"
        assert "exec_command" in (tool_row[1] or "")
        assert tool_row[2] == "codex"

        result_row = conn.execute(
            "SELECT is_tool_result, tool_result_error FROM raw_entries WHERE is_tool_result = 1 LIMIT 1"
        ).fetchone()
        assert result_row is not None
        assert result_row[0] == 1
        assert result_row[1] == 0


class TestCursorTranscriptIngestion:
    def test_cursor_txt_transcript_ingests_user_and_assistant(self, tmp_path):
        db_path = tmp_path / "test.sqlite"
        conn, _, ingest_mod = _make_db(db_path)
        transcript = tmp_path / "cursor-session.txt"
        transcript.write_text("user:\nhello there\n\nassistant:\nHi, how can I help?\n")

        raw_count, progress_count = ingest_mod.ingest_file(
            transcript, "cursor:demo", conn
        )
        assert raw_count == 2
        assert progress_count == 0

        rows = conn.execute(
            "SELECT entry_type, user_text, text_content FROM raw_entries ORDER BY rowid"
        ).fetchall()
        entry_types = [r[0] for r in rows]
        assert "user" in entry_types
        assert "assistant" in entry_types


class TestAntigravityMarkdownIngestion:
    def test_antigravity_brain_markdown_ingests_as_assistant_artifact(self, tmp_path):
        db_path = tmp_path / "test.sqlite"
        conn, _, ingest_mod = _make_db(db_path)

        session_dir = tmp_path / "brain" / "session-xyz"
        session_dir.mkdir(parents=True)
        md = session_dir / "task.md"
        md.write_text("# Task\n- [x] Do the thing\n")
        (session_dir / "task.md.metadata.json").write_text(
            json.dumps({"updatedAt": "2026-02-07T12:33:51.402918Z"})
        )

        raw_count, progress_count = ingest_mod.ingest_file(
            md, "antigravity:brain", conn
        )
        assert raw_count == 1
        assert progress_count == 0

        row = conn.execute(
            "SELECT session_id, entry_type, system_subtype, timestamp_utc, model, agent_type FROM raw_entries LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row[0] == "session-xyz"
        assert row[1] == "assistant"
        assert row[2] == "antigravity_artifact:task"
        assert row[3] == "2026-02-07T12:33:51.402918Z"
        assert row[4] == "antigravity"
        assert row[5] == "antigravity"

    def test_antigravity_resolved_revision_and_bash_snippet_detected(self, tmp_path):
        db_path = tmp_path / "test.sqlite"
        conn, _, ingest_mod = _make_db(db_path)

        session_dir = tmp_path / "brain" / "session-abc"
        session_dir.mkdir(parents=True)
        rev = session_dir / "implementation_plan.md.resolved.2"
        rev.write_text("# Plan\n```bash\nnpm install\nnpm test\n```\n")

        raw_count, _ = ingest_mod.ingest_file(rev, "antigravity:brain", conn)
        assert raw_count == 1

        row = conn.execute(
            "SELECT system_subtype, tool_names, content_types, tool_input_preview FROM raw_entries LIMIT 1"
        ).fetchone()
        assert row is not None
        assert (
            row[0] == "antigravity_artifact_revision:implementation_plan.md.resolved.2"
        )
        assert "Bash" in (row[1] or "")
        assert "tool_use" in (row[2] or "")
        assert row[3] == "npm install"

    def test_antigravity_code_tracker_file_with_binary_prefix(self, tmp_path):
        db_path = tmp_path / "test.sqlite"
        conn, _, ingest_mod = _make_db(db_path)

        tracker_dir = tmp_path / "code_tracker" / "active" / "proj"
        tracker_dir.mkdir(parents=True)
        tracker = tracker_dir / "deadbeef_README.md"
        tracker.write_bytes(b"\x12\xff\x80\x05# Title\nHello world\n")

        raw_count, _ = ingest_mod.ingest_file(tracker, "antigravity:tracker", conn)
        assert raw_count == 1

        row = conn.execute(
            "SELECT text_content, entry_type, model, agent_type FROM raw_entries LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row[0].startswith("# Title")
        assert row[1] == "assistant"
        assert row[2] == "antigravity"
        assert row[3] == "antigravity"

    def test_user_tool_result_flagged_correctly(self, tmp_path):
        db_path = tmp_path / "test.sqlite"
        conn, _, ingest_mod = _make_db(db_path)

        jsonl = tmp_path / "session.jsonl"
        _write_jsonl(jsonl, [RAW_USER_TOOL_RESULT])

        ingest_mod.ingest_file(jsonl, "proj", conn)

        row = conn.execute(
            "SELECT is_tool_result, tool_result_error FROM raw_entries WHERE entry_id = ?",
            ["raw-user-1"],
        ).fetchone()
        assert row is not None
        is_tool_result, tool_result_error = row
        assert is_tool_result == 1
        assert tool_result_error == 0
