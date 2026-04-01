"""Tests for parse_entry() and parse_progress_entry() in ingest.py."""

import json


from sessionlog.ingest import parse_entry, parse_progress_entry

PROJECT = "test-project"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def make_line(**kwargs) -> str:
    return json.dumps(kwargs)


BASE_FIELDS = {
    "uuid": "entry-uuid-1",
    "sessionId": "session-abc",
    "timestamp": "2024-01-15T10:00:00Z",
    "parentUuid": None,
    "isSidechain": False,
    "gitBranch": "main",
    "cwd": "/home/user/project",
}


# ---------------------------------------------------------------------------
# parse_entry — filtering
# ---------------------------------------------------------------------------


class TestParseEntryFiltering:
    def test_returns_none_for_progress_type(self):
        line = make_line(
            type="progress", uuid="x", sessionId="s", data={"type": "agent_progress"}
        )
        assert parse_entry(line, PROJECT) is None

    def test_returns_none_for_file_history_snapshot(self):
        line = make_line(type="file-history-snapshot", uuid="x", sessionId="s")
        assert parse_entry(line, PROJECT) is None

    def test_returns_none_for_invalid_json(self):
        assert parse_entry("not-json{{{", PROJECT) is None

    def test_returns_none_when_no_uuid(self):
        line = make_line(type="assistant", sessionId="s")
        assert parse_entry(line, PROJECT) is None


# ---------------------------------------------------------------------------
# parse_entry — assistant with tool_use
# ---------------------------------------------------------------------------


class TestParseEntryAssistantToolUse:
    def test_extracts_tool_names_from_tool_use_blocks(self):
        line = make_line(
            type="assistant",
            **BASE_FIELDS,
            message={
                "model": "claude-opus-4-6",
                "content": [
                    {"type": "tool_use", "id": "tu1", "name": "Read", "input": {}},
                    {"type": "tool_use", "id": "tu2", "name": "Write", "input": {}},
                ],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        )
        result = parse_entry(line, PROJECT)
        assert result is not None
        assert result["entry_type"] == "assistant"
        assert result["tool_names"] == ["Read", "Write"]
        assert result["is_tool_result"] is False
        assert result["model"] == "claude-opus-4-6"
        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 50

    def test_content_types_deduplicated(self):
        line = make_line(
            type="assistant",
            **BASE_FIELDS,
            message={
                "content": [
                    {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {}},
                    {"type": "tool_use", "id": "tu2", "name": "Read", "input": {}},
                    {"type": "text", "text": "Here is my plan."},
                ],
                "usage": {},
            },
        )
        result = parse_entry(line, PROJECT)
        assert result is not None
        # tool_use appears twice but should be deduplicated
        assert result["content_types"].count("tool_use") == 1
        assert "text" in result["content_types"]

    def test_text_content_extracted_for_assistant(self):
        line = make_line(
            type="assistant",
            **BASE_FIELDS,
            message={
                "content": [{"type": "text", "text": "Hello!"}],
                "usage": {},
            },
        )
        result = parse_entry(line, PROJECT)
        assert result is not None
        assert result["text_content"] == "Hello!"
        assert result["text_length"] == len("Hello!")


# ---------------------------------------------------------------------------
# parse_entry — user with text content
# ---------------------------------------------------------------------------


class TestParseEntryUserText:
    def test_parses_user_entry_with_text_content(self):
        line = make_line(
            type="user",
            **BASE_FIELDS,
            message={
                "content": [{"type": "text", "text": "Please fix the bug."}],
            },
        )
        result = parse_entry(line, PROJECT)
        assert result is not None
        assert result["entry_type"] == "user"
        assert result["user_text"] == "Please fix the bug."
        assert result["user_text_length"] == len("Please fix the bug.")
        assert result["is_tool_result"] is False

    def test_project_name_set_correctly(self):
        line = make_line(
            type="user",
            **BASE_FIELDS,
            message={"content": [{"type": "text", "text": "hi"}]},
        )
        result = parse_entry(line, "my-project")
        assert result is not None
        assert result["project_name"] == "my-project"

    def test_session_and_entry_ids(self):
        line = make_line(
            type="user",
            **BASE_FIELDS,
            message={"content": [{"type": "text", "text": "hi"}]},
        )
        result = parse_entry(line, PROJECT)
        assert result is not None
        assert result["entry_id"] == BASE_FIELDS["uuid"]
        assert result["session_id"] == BASE_FIELDS["sessionId"]


# ---------------------------------------------------------------------------
# parse_entry — user with tool_result
# ---------------------------------------------------------------------------


class TestParseEntryUserToolResult:
    def test_is_tool_result_true_for_tool_result_blocks(self):
        line = make_line(
            type="user",
            **BASE_FIELDS,
            message={
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu1",
                        "content": "file contents here",
                        "is_error": False,
                    }
                ]
            },
        )
        result = parse_entry(line, PROJECT)
        assert result is not None
        assert result["is_tool_result"] is True
        assert result["tool_result_error"] is False

    def test_tool_result_error_true_when_is_error_set(self):
        line = make_line(
            type="user",
            **BASE_FIELDS,
            message={
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu1",
                        "content": "command not found",
                        "is_error": True,
                    }
                ]
            },
        )
        result = parse_entry(line, PROJECT)
        assert result is not None
        assert result["is_tool_result"] is True
        assert result["tool_result_error"] is True


# ---------------------------------------------------------------------------
# parse_entry — system turn_duration
# ---------------------------------------------------------------------------


class TestParseEntrySystemTurnDuration:
    def test_parses_system_turn_duration(self):
        line = make_line(
            type="system",
            subtype="turn_duration",
            uuid="system-uuid-1",
            sessionId="session-abc",
            timestamp="2024-01-15T10:01:00Z",
            durationMs=12345,
        )
        result = parse_entry(line, PROJECT)
        assert result is not None
        assert result["entry_type"] == "system"
        assert result["system_subtype"] == "turn_duration"
        assert result["duration_ms"] == 12345

    def test_duration_ms_defaults_to_zero_when_absent(self):
        line = make_line(
            type="system",
            subtype="turn_duration",
            uuid="system-uuid-2",
            sessionId="session-abc",
            timestamp="2024-01-15T10:01:00Z",
        )
        result = parse_entry(line, PROJECT)
        assert result is not None
        assert result["duration_ms"] == 0


# ---------------------------------------------------------------------------
# parse_entry — content as plain string
# ---------------------------------------------------------------------------


class TestParseEntryPlainStringContent:
    def test_handles_assistant_content_as_plain_string(self):
        line = make_line(
            type="assistant",
            **BASE_FIELDS,
            message={
                "content": "Just a plain text response.",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )
        result = parse_entry(line, PROJECT)
        assert result is not None
        assert result["text_content"] == "Just a plain text response."
        assert result["text_length"] == len("Just a plain text response.")
        assert result["content_types"] == ["text"]

    def test_handles_user_content_as_plain_string(self):
        line = make_line(
            type="user",
            **BASE_FIELDS,
            message={"content": "Simple user prompt."},
        )
        result = parse_entry(line, PROJECT)
        assert result is not None
        assert result["user_text"] == "Simple user prompt."
        assert result["user_text_length"] == len("Simple user prompt.")
        assert result["content_types"] == ["text"]


# ---------------------------------------------------------------------------
# parse_entry — Codex response_item format
# ---------------------------------------------------------------------------


class TestParseEntryCodexFormat:
    def test_parses_codex_function_call_as_assistant_tool_use(self):
        line = make_line(
            type="response_item",
            timestamp="2026-02-27T19:10:46.837Z",
            payload={
                "type": "function_call",
                "name": "exec_command",
                "arguments": '{"cmd":"rg -n \\"toLowerCase\\\\(\\" -S src"}',
                "call_id": "call_abc123",
            },
        )
        result = parse_entry(line, PROJECT, fallback_session_id="codex-session-1")
        assert result is not None
        assert result["entry_type"] == "assistant"
        assert result["session_id"] == "codex-session-1"
        assert result["tool_names"] == ["exec_command"]
        assert result["content_types"] == ["tool_use"]
        assert result["tool_input_preview"].startswith("rg -n")

    def test_parses_codex_function_call_output_as_tool_result(self):
        line = make_line(
            type="response_item",
            timestamp="2026-02-27T19:10:46.957Z",
            payload={
                "type": "function_call_output",
                "call_id": "call_abc123",
                "output": "Chunk ID: x\nProcess exited with code 2\nOutput:\nrg: src: No such file",
            },
        )
        result = parse_entry(line, PROJECT, fallback_session_id="codex-session-1")
        assert result is not None
        assert result["entry_type"] == "user"
        assert result["is_tool_result"] is True
        assert result["tool_result_error"] is True
        assert result["tool_result_error_type"] == "file_not_found"


# ---------------------------------------------------------------------------
# parse_progress_entry — filtering
# ---------------------------------------------------------------------------


class TestParseProgressEntryFiltering:
    def test_returns_none_for_non_progress_type(self):
        line = make_line(type="assistant", uuid="x", sessionId="s", message={})
        assert parse_progress_entry(line, PROJECT) is None

    def test_returns_none_for_user_type(self):
        line = make_line(type="user", uuid="x", sessionId="s", message={})
        assert parse_progress_entry(line, PROJECT) is None

    def test_returns_none_for_mcp_progress(self):
        line = make_line(
            type="progress",
            uuid="prog-uuid-1",
            sessionId="session-abc",
            timestamp="2024-01-15T10:00:00Z",
            data={"type": "mcp_progress", "toolUseId": "tu1"},
        )
        assert parse_progress_entry(line, PROJECT) is None

    def test_returns_none_for_invalid_json(self):
        assert parse_progress_entry("{bad json", PROJECT) is None

    def test_returns_none_when_no_uuid(self):
        line = make_line(
            type="progress",
            sessionId="s",
            data={"type": "agent_progress"},
        )
        assert parse_progress_entry(line, PROJECT) is None


# ---------------------------------------------------------------------------
# parse_progress_entry — agent_progress
# ---------------------------------------------------------------------------


class TestParseProgressEntryAgentProgress:
    # Real format: parentUuid (outer) = parent Task's UUID.
    # tool_name extracted from assistant messages; has_result from user tool_result messages.

    def test_parses_agent_progress_assistant_with_tool_use(self):
        """Assistant message from sub-agent containing a tool_use block."""
        line = make_line(
            type="progress",
            uuid="prog-uuid-1",
            sessionId="session-abc",
            parentUuid="parent-tool-id",
            timestamp="2024-01-15T10:00:00Z",
            data={
                "type": "agent_progress",
                "message": {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "sub-tu1",
                                "name": "Read",
                                "input": {"file_path": "/foo.py"},
                            },
                        ],
                    },
                },
            },
        )
        result = parse_progress_entry(line, PROJECT)
        assert result is not None
        assert result["entry_id"] == "prog-uuid-1"
        assert result["session_id"] == "session-abc"
        assert result["progress_type"] == "agent_progress"
        assert result["parent_tool_id"] == "parent-tool-id"
        assert result["tool_name"] == "Read"
        assert result["has_result"] == 0
        assert result["result_error"] == 0

    def test_parses_agent_progress_user_with_tool_result(self):
        """User message from sub-agent containing a tool_result block (success)."""
        line = make_line(
            type="progress",
            uuid="prog-uuid-2",
            sessionId="session-abc",
            parentUuid="parent-tool-id",
            timestamp="2024-01-15T10:00:00Z",
            data={
                "type": "agent_progress",
                "message": {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "sub-tu1",
                                "content": "file content",
                                "is_error": False,
                            },
                        ],
                    },
                },
            },
        )
        result = parse_progress_entry(line, PROJECT)
        assert result is not None
        assert result["has_result"] == 1
        assert result["tool_name"] is None  # tool name comes from assistant messages
        assert result["result_error"] == 0

    def test_sets_result_error_when_tool_result_has_is_error_true(self):
        """User message with is_error=True tool_result."""
        line = make_line(
            type="progress",
            uuid="prog-uuid-3",
            sessionId="session-abc",
            parentUuid="parent-tool-id",
            timestamp="2024-01-15T10:00:00Z",
            data={
                "type": "agent_progress",
                "message": {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "sub-tu2",
                                "content": "bash: not found",
                                "is_error": True,
                            },
                        ],
                    },
                },
            },
        )
        result = parse_progress_entry(line, PROJECT)
        assert result is not None
        assert result["has_result"] == 1
        assert result["result_error"] == 1


# ---------------------------------------------------------------------------
# parse_progress_entry — bash_progress
# ---------------------------------------------------------------------------


class TestParseProgressEntryBashProgress:
    def test_parses_bash_progress_with_no_tool_name_or_has_result(self):
        # bash_progress: parentUuid is outer field; no tool_name or has_result
        line = make_line(
            type="progress",
            uuid="bash-prog-1",
            sessionId="session-abc",
            parentUuid="bash-tool-id",
            timestamp="2024-01-15T10:00:00Z",
            data={
                "type": "bash_progress",
                "output": "still running...",
            },
        )
        result = parse_progress_entry(line, PROJECT)
        assert result is not None
        assert result["progress_type"] == "bash_progress"
        assert result["tool_name"] is None
        assert result["has_result"] == 0
        assert result["result_error"] == 0
        assert result["parent_tool_id"] == "bash-tool-id"
        assert result["entry_id"] == "bash-prog-1"
        assert result["session_id"] == "session-abc"
