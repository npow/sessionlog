"""JSONL parsing → raw_entries table with incremental ingestion."""

import json
import os
import hashlib
import re
from pathlib import Path

from sessionlog.config import get_source_specs


def _json_serialize(obj):
    """Convert list/array types to JSON strings for SQLite."""
    if isinstance(obj, list):
        return json.dumps(obj)
    return obj


def _derive_project_name(source_name: str, source_dir: Path, jsonl_file: Path) -> str:
    """Derive a stable project label from source + relative path."""
    try:
        rel = jsonl_file.relative_to(source_dir)
    except ValueError:
        # Should never happen, but keep ingestion resilient.
        return source_name

    if len(rel.parts) > 1:
        return f"{source_name}:{rel.parts[0]}"
    return source_name


def _infer_agent_type(project_name: str) -> str:
    """Infer canonical agent type from project label."""
    if not project_name:
        return "unknown"
    if ":" in project_name:
        head = project_name.split(":", 1)[0].strip().lower()
        return head or "unknown"
    p = project_name.strip().lower()
    known = {
        "claude",
        "codex",
        "cursor",
        "antigravity",
        "opencode",
        "copilot",
        "windsurf",
        "cline",
        "roo",
        "aider",
        "gemini",
        "continue",
    }
    return p if p in known else "unknown"


def find_jsonl_files(
    source_specs: list[tuple[str, Path]] | None = None,
) -> list[tuple[Path, str]]:
    """Find supported session files across all configured agent sources."""
    results = []
    specs = source_specs if source_specs is not None else get_source_specs()
    for source_name, source_dir in sorted(specs, key=lambda s: (s[0], str(s[1]))):
        if not source_dir.exists() or not source_dir.is_dir():
            continue
        for jsonl_file in sorted(source_dir.rglob("*.jsonl")):
            project_name = _derive_project_name(source_name, source_dir, jsonl_file)
            results.append((jsonl_file, project_name))
        # Cursor commonly stores chat transcripts as text files.
        if source_name == "cursor":
            for txt_file in sorted(source_dir.rglob("agent-transcripts/*.txt")):
                project_name = _derive_project_name(source_name, source_dir, txt_file)
                results.append((txt_file, project_name))
        # Antigravity stores rich artifacts in Markdown under brain/<session-id>/.
        if source_name == "antigravity":
            for md_file in sorted(source_dir.rglob("brain/*/*.md")):
                project_name = _derive_project_name(source_name, source_dir, md_file)
                results.append((md_file, project_name))
            for rev_file in sorted(source_dir.rglob("brain/*/*.resolved*")):
                project_name = _derive_project_name(source_name, source_dir, rev_file)
                results.append((rev_file, project_name))
            for tracker_file in sorted(source_dir.rglob("code_tracker/active/**/*")):
                if tracker_file.is_file():
                    project_name = _derive_project_name(
                        source_name, source_dir, tracker_file
                    )
                    results.append((tracker_file, project_name))
    return results


def needs_ingestion(file_path: Path, conn) -> bool:
    """Check if file needs (re-)ingestion based on mtime and skip cache."""
    mtime = os.path.getmtime(file_path)

    # Check skip cache first (files that failed parsing)
    skip_result = conn.execute(
        "SELECT mtime FROM skip_cache WHERE file_path = ?", [str(file_path)]
    ).fetchone()
    if skip_result is not None:
        # Skip if mtime hasn't changed since last failure
        if mtime <= skip_result[0]:
            return False

    # Check ingestion log
    result = conn.execute(
        "SELECT mtime FROM ingestion_log WHERE file_path = ?", [str(file_path)]
    ).fetchone()
    if result is None:
        return True
    return mtime > result[0]


def mark_skip(file_path: Path, error_type: str, error_message: str, conn):
    """Mark a file to be skipped until its mtime changes."""
    mtime = os.path.getmtime(file_path)
    conn.execute(
        """
        INSERT OR REPLACE INTO skip_cache (file_path, mtime, error_type, error_message, skip_until)
        VALUES (?, ?, ?, ?, datetime('now', '+1 day'))
        """,
        [str(file_path), mtime, error_type, error_message[:500]],
    )


def clear_skip(file_path: Path, conn):
    """Clear a file from the skip cache after successful ingestion."""
    conn.execute("DELETE FROM skip_cache WHERE file_path = ?", [str(file_path)])


def _classify_tool_error(text: str) -> str:
    """Classify a tool error message into a canonical type.

    Types ordered by observed frequency (sample of 103 errors):
      command_failed (43), sibling_error (15), file_not_read (15),
      edit_conflict (13), file_not_found (8), user_rejected (3),
      file_changed (3), network_error (3), permission_denied (2),
      file_too_large (1), validation_error, timeout, other.
    """
    t = text.lower()
    # Sibling cascade — most common non-bash error
    if "sibling tool call errored" in t:
        return "sibling_error"
    # File-not-read constraint (Write/Edit without prior Read)
    if "file has not been read yet" in t or "read it first before writing" in t:
        return "file_not_read"
    # Edit conflicts — string not found or multiple matches
    if (
        "string to replace not found" in t
        or "matches of the string to replace" in t
        or "replace_all is false" in t
    ):
        return "edit_conflict"
    # File does not exist / path not found
    if (
        "file does not exist" in t
        or "no such file" in t
        or "file not found" in t
        or "cannot find" in t
        or "path does not exist" in t
        or "eisdir" in t  # directory where file expected
    ):
        return "file_not_found"
    # File changed between read and write
    if "file has changed" in t or "file was modified" in t or "has been modified" in t:
        return "file_changed"
    # File too large for context window
    if (
        "too large" in t
        or "exceeds maximum" in t
        or ("file content" in t and "tokens" in t)
    ):
        return "file_too_large"
    # System permission denial (Claude Code's permission system)
    if ("permission to use" in t and "denied" in t) or (
        "requested permissions" in t and "but you" in t
    ):
        return "permission_denied"
    # Explicit user rejection
    if (
        "doesn't want to proceed" in t
        or "tool use was rejected" in t
        or "user rejected" in t
        or "user cancelled" in t
        or "user denied" in t
    ):
        return "user_rejected"
    # Bash command failed (exit codes)
    if "exit code" in t or "returned non-zero" in t or "non-zero exit" in t:
        return "command_failed"
    # Network / HTTP errors
    if "request failed" in t or "status code" in t or "network error" in t:
        return "network_error"
    # Tool input validation
    if "inputvalidationerror" in t or "validation error" in t:
        return "validation_error"
    # Timeouts
    if "timed out" in t or "timeout" in t:
        return "timeout"
    # Task tool errors (TaskOutput on missing/completed task)
    if "task not found" in t or "is not running" in t or "tool_use_error" in t:
        return "task_error"
    return "other"


def _stable_entry_id(line: str, project_name: str) -> str:
    digest = hashlib.sha1(f"{project_name}|{line}".encode("utf-8")).hexdigest()[:24]
    return f"entry-{digest}"


def _parse_tool_args(raw: str) -> dict:
    if not raw or not isinstance(raw, str):
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except json.JSONDecodeError:
        return {}


def parse_entry(
    line: str, project_name: str, fallback_session_id: str | None = None
) -> dict | None:
    """Parse a single JSONL line into a raw_entry dict."""
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return None

    agent_type = _infer_agent_type(project_name)

    entry_type = d.get("type")
    if entry_type == "progress":
        return None
    if entry_type == "file-history-snapshot":
        return None

    # Codex-style logs are wrapped as response_item with payload records.
    if entry_type == "response_item":
        payload = d.get("payload", {})
        ptype = payload.get("type")
        timestamp = d.get("timestamp")
        session_id = fallback_session_id
        parent_uuid = None
        is_sidechain = False
        git_branch = None
        cwd = None
        model = None
        input_tokens = 0
        output_tokens = 0
        system_subtype = None
        duration_ms = 0
        user_text = ""
        user_text_length = 0
        is_tool_result = False
        tool_result_error = False
        tool_result_error_type = None
        content_types = []
        tool_names = []
        tool_file_paths = []
        text_content = ""
        text_length = 0
        tool_input_preview = ""

        # ptype=message carries user/assistant/system text blocks.
        if ptype == "message":
            role = payload.get("role")
            if role not in ("user", "assistant", "system", "developer"):
                return None
            parsed_entry_type = "system" if role == "developer" else role
            content = payload.get("content", [])
            text_parts = []
            for block in content if isinstance(content, list) else []:
                btype = block.get("type", "")
                content_types.append(btype or "text")
                t = block.get("text", "")
                if isinstance(t, str) and t:
                    text_parts.append(t)

            merged = "\n".join(text_parts).strip()
            if parsed_entry_type == "user":
                user_text = merged
                user_text_length = len(user_text)
            elif parsed_entry_type == "assistant":
                text_content = merged
                text_length = len(text_content)
            elif parsed_entry_type == "system":
                text_content = merged
                text_length = len(text_content)

            return {
                "entry_id": _stable_entry_id(line, project_name),
                "session_id": session_id,
                "project_name": project_name,
                "agent_type": agent_type,
                "entry_type": parsed_entry_type,
                "timestamp_utc": timestamp,
                "parent_uuid": parent_uuid,
                "is_sidechain": is_sidechain,
                "user_text": user_text,
                "user_text_length": user_text_length,
                "is_tool_result": is_tool_result,
                "tool_result_error": tool_result_error,
                "tool_result_error_type": tool_result_error_type,
                "model": model,
                "content_types": list(dict.fromkeys(content_types)) or ["text"],
                "tool_names": tool_names,
                "tool_file_paths": tool_file_paths,
                "text_content": text_content,
                "text_length": text_length,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "system_subtype": system_subtype,
                "duration_ms": duration_ms,
                "git_branch": git_branch,
                "cwd": cwd,
                "tool_input_preview": tool_input_preview,
            }

        # ptype=function_call contains the agent tool invocation.
        if ptype == "function_call":
            tool_name = payload.get("name", "")
            args = _parse_tool_args(payload.get("arguments", ""))
            tool_names = [tool_name] if tool_name else []
            content_types = ["tool_use"]
            tool_input_preview = (
                str(
                    args.get("cmd")
                    or args.get("command")
                    or args.get("query")
                    or args.get("url")
                    or args.get("pattern")
                    or ""
                )
                .strip()
                .split("\n")[0][:200]
            )
            path_value = args.get("file_path") or args.get("path")
            if isinstance(path_value, str) and path_value:
                tool_file_paths = [path_value]

            return {
                "entry_id": _stable_entry_id(line, project_name),
                "session_id": session_id,
                "project_name": project_name,
                "agent_type": agent_type,
                "entry_type": "assistant",
                "timestamp_utc": timestamp,
                "parent_uuid": payload.get("call_id"),
                "is_sidechain": is_sidechain,
                "user_text": "",
                "user_text_length": 0,
                "is_tool_result": False,
                "tool_result_error": False,
                "tool_result_error_type": None,
                "model": None,
                "content_types": content_types,
                "tool_names": tool_names,
                "tool_file_paths": tool_file_paths,
                "text_content": "",
                "text_length": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "system_subtype": None,
                "duration_ms": 0,
                "git_branch": None,
                "cwd": None,
                "tool_input_preview": tool_input_preview,
            }

        # ptype=function_call_output carries tool result payload.
        if ptype == "function_call_output":
            out = payload.get("output", "")
            out_text = out if isinstance(out, str) else json.dumps(out)
            out_short = out_text[:1500]
            err = "Process exited with code" in out_text and "code 0" not in out_text
            return {
                "entry_id": _stable_entry_id(line, project_name),
                "session_id": session_id,
                "project_name": project_name,
                "agent_type": agent_type,
                "entry_type": "user",
                "timestamp_utc": timestamp,
                "parent_uuid": payload.get("call_id"),
                "is_sidechain": False,
                "user_text": out_short,
                "user_text_length": len(out_short),
                "is_tool_result": True,
                "tool_result_error": err,
                "tool_result_error_type": _classify_tool_error(out_text)
                if err
                else None,
                "model": None,
                "content_types": ["tool_result"],
                "tool_names": [],
                "tool_file_paths": [],
                "text_content": "",
                "text_length": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "system_subtype": None,
                "duration_ms": 0,
                "git_branch": None,
                "cwd": None,
                "tool_input_preview": "",
            }

        return None

    # Legacy Claude-style records:
    entry_id = d.get("uuid") or d.get("id")
    if not entry_id:
        return None

    session_id = d.get("sessionId") or fallback_session_id
    timestamp = d.get("timestamp")
    parent_uuid = d.get("parentUuid")
    is_sidechain = d.get("isSidechain", False)
    git_branch = d.get("gitBranch")
    cwd = d.get("cwd")

    msg = d.get("message", {})
    model = msg.get("model")
    usage = msg.get("usage", {})
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)

    # System entries
    system_subtype = d.get("subtype")
    duration_ms = d.get("durationMs", 0)

    # Parse content
    content = msg.get("content", "")
    user_text = ""
    user_text_length = 0
    is_tool_result = False
    tool_result_error = False
    tool_result_error_type = None
    content_types = []
    tool_names = []
    tool_file_paths = []
    text_content = ""
    text_length = 0

    # Tools that take a file path as input
    _FILE_PATH_TOOLS = {"Edit", "Write", "Read", "NotebookEdit", "NotebookRead"}
    _FILE_PATH_INPUT_KEYS = ("file_path", "notebook_path", "path")

    # Tools whose primary input is a free-text string worth storing for live display
    _TEXT_INPUT_TOOLS: dict[str, str] = {
        "Bash": "command",
        "Task": "prompt",
        "WebSearch": "query",
        "WebFetch": "url",
        "Grep": "pattern",
        "Skill": "skill",
        "Agent": "description",
    }
    tool_input_preview = ""

    if isinstance(content, str):
        if entry_type == "user":
            user_text = content
            user_text_length = len(content)
        elif entry_type == "assistant":
            text_content = content
            text_length = len(content)
        content_types = ["text"]
    elif isinstance(content, list):
        text_parts = []
        user_text_parts = []
        for block in content:
            btype = block.get("type", "")
            content_types.append(btype)
            if btype == "text":
                t = block.get("text", "")
                if entry_type == "user":
                    user_text_parts.append(t)
                else:
                    text_parts.append(t)
            elif btype == "tool_use":
                name = block.get("name", "")
                tool_names.append(name)
                inp = block.get("input", {})
                if name in _FILE_PATH_TOOLS:
                    for key in _FILE_PATH_INPUT_KEYS:
                        fp = inp.get(key)
                        if fp and isinstance(fp, str):
                            tool_file_paths.append(fp)
                            break
                # Capture the primary input of the first text-input tool call
                if not tool_input_preview and name in _TEXT_INPUT_TOOLS:
                    input_key = _TEXT_INPUT_TOOLS[name]
                    raw = inp.get(input_key, "")
                    if raw and isinstance(raw, str):
                        # Store first line, truncated to 200 chars
                        tool_input_preview = raw.strip().split("\n")[0][:200]
            elif btype == "tool_result":
                is_tool_result = True
                tr_content = block.get("content", "")
                # Extract the tool result output text (cap at 1500 chars)
                if isinstance(tr_content, list):
                    tr_texts = [
                        c.get("text", "")
                        for c in tr_content
                        if isinstance(c, dict) and c.get("type") == "text"
                    ]
                    tr_text = "\n".join(tr_texts)
                elif isinstance(tr_content, str):
                    tr_text = tr_content
                else:
                    tr_text = ""
                if tr_text:
                    user_text_parts.append(tr_text[:1500])
                if block.get("is_error"):
                    tool_result_error = True
                    # Extract error text to classify error type
                    err_text = tr_text
                    tool_result_error_type = _classify_tool_error(err_text)
            elif btype == "thinking":
                pass  # skip thinking content to save space

        if entry_type == "user":
            user_text = "\n".join(user_text_parts)
            user_text_length = len(user_text)
        text_content = "\n".join(text_parts)
        text_length = len(text_content)

    # Deduplicate content_types
    content_types = list(dict.fromkeys(content_types))

    return {
        "entry_id": entry_id,
        "session_id": session_id,
        "project_name": project_name,
        "agent_type": agent_type,
        "entry_type": entry_type,
        "timestamp_utc": timestamp,
        "parent_uuid": parent_uuid,
        "is_sidechain": is_sidechain,
        "user_text": user_text,
        "user_text_length": user_text_length,
        "is_tool_result": is_tool_result,
        "tool_result_error": tool_result_error,
        "tool_result_error_type": tool_result_error_type,
        "model": model,
        "content_types": content_types,
        "tool_names": tool_names,
        "tool_file_paths": tool_file_paths,
        "text_content": text_content,
        "text_length": text_length,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "system_subtype": system_subtype,
        "duration_ms": duration_ms or 0,
        "git_branch": git_branch,
        "cwd": cwd,
        "tool_input_preview": tool_input_preview,
    }


def parse_progress_entry(line: str, _project_name: str) -> dict | None:
    """Parse a progress record (type=progress) into a progress_entry dict.

    Only handles agent_progress and bash_progress — mcp_progress is skipped.
    agent_progress: sub-agent tool calls (individually stored, analytically valuable).
    bash_progress:  heartbeat signals for long-running Bash (stored for count aggregation).
    """
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return None

    if d.get("type") != "progress":
        return None
    agent_type = _infer_agent_type(_project_name)

    entry_id = d.get("uuid")
    if not entry_id:
        return None

    data = d.get("data", {})
    progress_type = data.get("type")

    if progress_type not in ("agent_progress", "bash_progress"):
        return None

    # parent_tool_id: the outer record's parentUuid links this progress record
    # to the Task tool_use call in the parent agent that spawned the sub-agent.
    # All agent_progress records from the same sub-agent invocation share a parentUuid.
    parent_tool_id = d.get("parentUuid")
    tool_name = None
    has_result = 0
    result_error = 0

    if progress_type == "agent_progress":
        # Sub-agent messages are in data.message. Extract tool names from
        # assistant messages (data.message.type == "assistant") whose content
        # contains tool_use blocks.
        msg = data.get("message", {})
        msg_type = msg.get("type")
        if msg_type == "assistant":
            inner = msg.get("message", {})
            for block in inner.get("content", []):
                if block.get("type") == "tool_use":
                    tool_name = block.get("name")
                    break
        elif msg_type == "user":
            # tool_result block = sub-agent received a tool result
            inner = msg.get("message", {})
            for block in inner.get("content", []):
                if block.get("type") == "tool_result":
                    has_result = 1
                    if block.get("is_error"):
                        result_error = 1
                    break

    return {
        "entry_id": entry_id,
        "session_id": d.get("sessionId"),
        "agent_type": agent_type,
        "progress_type": progress_type,
        "parent_tool_id": parent_tool_id,
        "tool_name": tool_name,
        "has_result": has_result,
        "result_error": result_error,
        "timestamp_utc": d.get("timestamp"),
    }


def _ingest_cursor_transcript(file_path: Path, project_name: str, conn) -> int:
    """Ingest Cursor agent transcript text files with user:/assistant: sections."""
    with open(file_path, "r", errors="replace") as f:
        lines = f.read().splitlines()

    role = None
    buf: list[str] = []
    count = 0
    session_id = file_path.stem
    agent_type = _infer_agent_type(project_name)

    def flush_block(block_role: str | None, block_lines: list[str], index: int):
        if block_role not in ("user", "assistant", "system"):
            return
        text = "\n".join(block_lines).strip()
        if not text:
            return
        entry_id = _stable_entry_id(
            f"{file_path}:{index}:{block_role}:{text}", project_name
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO raw_entries (
                entry_id, session_id, project_name, agent_type, entry_type, timestamp_utc,
                parent_uuid, is_sidechain, user_text, user_text_length,
                is_tool_result, tool_result_error, tool_result_error_type,
                model, content_types, tool_names, tool_file_paths,
                text_content, text_length, input_tokens, output_tokens,
                system_subtype, duration_ms, git_branch, cwd, tool_input_preview
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            [
                entry_id,
                session_id,
                project_name,
                agent_type,
                block_role,
                None,
                None,
                0,
                text if block_role == "user" else "",
                len(text) if block_role == "user" else 0,
                0,
                0,
                None,
                None,
                '["text"]',
                "[]",
                "[]",
                text if block_role != "user" else "",
                len(text) if block_role != "user" else 0,
                0,
                0,
                None,
                0,
                None,
                None,
                "",
            ],
        )

    for i, line in enumerate(lines):
        marker = line.strip().lower()
        if marker in ("user:", "assistant:", "system:"):
            flush_block(role, buf, i)
            if role in ("user", "assistant", "system") and buf:
                count += 1
            role = marker[:-1]
            buf = []
        else:
            buf.append(line)

    flush_block(role, buf, len(lines))
    if role in ("user", "assistant", "system") and buf:
        count += 1

    mtime = os.path.getmtime(file_path)
    conn.execute(
        "INSERT OR REPLACE INTO ingestion_log VALUES (?, ?, ?, current_timestamp)",
        [str(file_path), mtime, count],
    )
    conn.commit()
    return count


def _ingest_antigravity_markdown(file_path: Path, project_name: str, conn) -> int:
    """Ingest Antigravity brain markdown artifacts as assistant entries."""

    def _normalize_text_payload(raw: bytes) -> str:
        text0 = raw.decode("utf-8", errors="replace")
        # Drop binary-ish prefix if present before first obvious markdown/text marker.
        candidates = [
            p
            for p in (
                text0.find("#"),
                text0.find("##"),
                text0.find("- ["),
                text0.find("Task"),
            )
            if p >= 0
        ]
        if candidates:
            start = min(candidates)
            text0 = text0[start:]
        return text0.strip()

    def _extract_shell_commands(text_payload: str) -> list[str]:
        cmds: list[str] = []
        for m in re.finditer(
            r"```(?:bash|sh|zsh)?\n(.*?)```", text_payload, flags=re.S
        ):
            block = m.group(1)
            for line in block.splitlines():
                c = line.strip()
                if not c or c.startswith("#"):
                    continue
                cmds.append(c[:200])
                if len(cmds) >= 5:
                    return cmds
        return cmds

    raw = file_path.read_bytes()
    text = _normalize_text_payload(raw)

    if not text:
        return 0

    session_id = file_path.parent.name
    agent_type = _infer_agent_type(project_name)
    metadata_file = file_path.with_suffix(file_path.suffix + ".metadata.json")
    timestamp_utc = None
    if metadata_file.exists():
        try:
            meta = json.loads(metadata_file.read_text())
            if isinstance(meta, dict):
                ts = meta.get("updatedAt")
                if isinstance(ts, str):
                    timestamp_utc = ts
        except Exception:
            pass

    tool_cmds = _extract_shell_commands(text)
    tool_names = ["Bash"] if tool_cmds else []
    content_types = ["text"] + (["tool_use"] if tool_cmds else [])
    subtype = f"antigravity_artifact:{file_path.stem}"
    if ".resolved" in file_path.name:
        subtype = f"antigravity_artifact_revision:{file_path.name}"

    entry_id = _stable_entry_id(f"antigravity:{file_path}", project_name)
    conn.execute(
        """
        INSERT OR REPLACE INTO raw_entries (
            entry_id, session_id, project_name, agent_type, entry_type, timestamp_utc,
            parent_uuid, is_sidechain, user_text, user_text_length,
            is_tool_result, tool_result_error, tool_result_error_type,
            model, content_types, tool_names, tool_file_paths,
            text_content, text_length, input_tokens, output_tokens,
            system_subtype, duration_ms, git_branch, cwd, tool_input_preview
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        [
            entry_id,
            session_id,
            project_name,
            agent_type,
            "assistant",
            timestamp_utc,
            None,
            0,
            "",
            0,
            0,
            0,
            None,
            "antigravity",
            _json_serialize(content_types),
            _json_serialize(tool_names),
            "[]",
            text[:20000],  # keep entries bounded
            len(text[:20000]),
            0,
            0,
            subtype,
            0,
            None,
            None,
            tool_cmds[0] if tool_cmds else "",
        ],
    )

    mtime = os.path.getmtime(file_path)
    conn.execute(
        "INSERT OR REPLACE INTO ingestion_log VALUES (?, ?, ?, current_timestamp)",
        [str(file_path), mtime, 1],
    )
    conn.commit()
    return 1


def ingest_file(file_path: Path, project_name: str, conn) -> tuple[int, int]:
    """Ingest a single JSONL file in one pass.

    Returns (raw_entry_count, progress_entry_count).
    parse_entry skips progress lines; parse_progress_entry skips everything else,
    so exactly one parser handles each line.
    """
    if file_path.suffix.lower() == ".txt":
        return _ingest_cursor_transcript(file_path, project_name, conn), 0
    if "/brain/" in str(file_path) or "/code_tracker/active/" in str(file_path):
        return _ingest_antigravity_markdown(file_path, project_name, conn), 0

    entries = []
    progress_entries = []
    fallback_session_id: str | None = None

    with open(file_path, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            # Extract session hint for Codex-style logs.
            try:
                raw = json.loads(line)
                if raw.get("type") == "session_meta":
                    payload = raw.get("payload", {})
                    if isinstance(payload, dict):
                        sid = payload.get("id")
                        if isinstance(sid, str) and sid:
                            fallback_session_id = sid
            except json.JSONDecodeError:
                pass

            entry = parse_entry(
                line, project_name, fallback_session_id=fallback_session_id
            )
            if entry:
                entries.append(entry)
            else:
                progress = parse_progress_entry(line, project_name)
                if progress:
                    progress_entries.append(progress)

    # Accumulate language counts per session while inserting
    from collections import Counter

    session_lang_counts: dict[str, Counter] = {}

    for entry in entries:
        conn.execute(
            """
            INSERT OR REPLACE INTO raw_entries (
                entry_id, session_id, project_name, agent_type, entry_type, timestamp_utc,
                parent_uuid, is_sidechain, user_text, user_text_length,
                is_tool_result, tool_result_error, tool_result_error_type,
                model, content_types, tool_names, tool_file_paths,
                text_content, text_length, input_tokens, output_tokens,
                system_subtype, duration_ms, git_branch, cwd, tool_input_preview
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            [
                entry["entry_id"],
                entry["session_id"],
                entry["project_name"],
                entry.get(
                    "agent_type", _infer_agent_type(entry.get("project_name", ""))
                ),
                entry["entry_type"],
                entry["timestamp_utc"],
                entry["parent_uuid"],
                entry["is_sidechain"],
                entry["user_text"],
                entry["user_text_length"],
                entry["is_tool_result"],
                entry["tool_result_error"],
                entry["tool_result_error_type"],
                entry["model"],
                _json_serialize(entry["content_types"]),
                _json_serialize(entry["tool_names"]),
                _json_serialize(entry["tool_file_paths"]),
                entry["text_content"],
                entry["text_length"],
                entry["input_tokens"],
                entry["output_tokens"],
                entry["system_subtype"],
                entry["duration_ms"],
                entry["git_branch"],
                entry["cwd"],
                entry.get("tool_input_preview", ""),
            ],
        )

        # Accumulate file extensions for session_languages
        sid = entry.get("session_id")
        fps = entry.get("tool_file_paths") or []
        if sid and fps:
            if sid not in session_lang_counts:
                session_lang_counts[sid] = Counter()
            for fp in fps:
                ext = Path(fp).suffix.lstrip(".").lower()
                if ext:
                    session_lang_counts[sid][ext] += 1

    # Write session_languages
    for sid, counter in session_lang_counts.items():
        for ext, count in counter.items():
            conn.execute(
                """
                INSERT INTO session_languages (session_id, extension, file_count)
                VALUES (?, ?, ?)
                ON CONFLICT (session_id, extension)
                DO UPDATE SET file_count = file_count + excluded.file_count
                """,
                [sid, ext, count],
            )

    for p in progress_entries:
        conn.execute(
            """
            INSERT OR REPLACE INTO progress_entries (
                entry_id, session_id, agent_type, progress_type, parent_tool_id,
                tool_name, has_result, result_error, timestamp_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                p["entry_id"],
                p["session_id"],
                p.get("agent_type", _infer_agent_type(project_name)),
                p["progress_type"],
                p["parent_tool_id"],
                p["tool_name"],
                p["has_result"],
                p["result_error"],
                p["timestamp_utc"],
            ],
        )

    mtime = os.path.getmtime(file_path)
    conn.execute(
        "INSERT OR REPLACE INTO ingestion_log VALUES (?, ?, ?, current_timestamp)",
        [str(file_path), mtime, len(entries)],
    )
    conn.commit()

    return len(entries), len(progress_entries)


def run_ingest(source_specs: list[tuple[str, Path]] | None = None) -> dict:
    """Run full incremental ingestion. Returns stats."""
    from sessionlog.db import get_writer

    conn = get_writer()
    files = find_jsonl_files(source_specs=source_specs)

    stats = {
        "total_files": len(files),
        "ingested_files": 0,
        "total_entries": 0,
        "total_progress_entries": 0,
        "skipped_files": 0,
        "failed_files": 0,
    }

    for file_path, project_name in files:
        if not needs_ingestion(file_path, conn):
            stats["skipped_files"] += 1
            continue

        try:
            count, progress_count = ingest_file(file_path, project_name, conn)
            stats["ingested_files"] += 1
            stats["total_entries"] += count
            stats["total_progress_entries"] += progress_count
            clear_skip(file_path, conn)
        except Exception as e:
            error_type = type(e).__name__
            error_message = str(e)
            mark_skip(file_path, error_type, error_message, conn)
            stats["failed_files"] += 1
            print(
                f"[ingest] Failed to ingest {file_path}: {error_type}: {error_message}"
            )

    stats["total_entries_in_db"] = conn.execute(
        "SELECT COUNT(*) FROM raw_entries"
    ).fetchone()[0]
    stats["total_progress_entries_in_db"] = conn.execute(
        "SELECT COUNT(*) FROM progress_entries"
    ).fetchone()[0]
    stats["total_sessions_found"] = conn.execute(
        "SELECT COUNT(DISTINCT session_id) FROM raw_entries WHERE session_id IS NOT NULL"
    ).fetchone()[0]
    stats["total_projects"] = conn.execute(
        "SELECT COUNT(DISTINCT project_name) FROM raw_entries"
    ).fetchone()[0]

    return stats
