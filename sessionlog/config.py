"""Configuration and source discovery for session ingestion."""

import os
from pathlib import Path

# Backward-compatible DB env var name (legacy) + new canonical name.
DB_PATH = Path(
    os.environ.get(
        "SESSIONLOG_DB",
        os.environ.get("CLAUDE_RETRO_DB", Path.home() / ".sessionlog" / "data.sqlite"),
    )
)

# Legacy constant kept for compatibility with older imports/tests.
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Curated default session roots for common coding agents.
# Format: (agent_name, directory)
DEFAULT_SOURCE_SPECS: list[tuple[str, Path]] = [
    ("claude", CLAUDE_PROJECTS_DIR),
    ("codex", Path.home() / ".codex" / "sessions"),
    ("cursor", Path.home() / ".cursor" / "projects"),
    ("copilot", Path.home() / ".copilot" / "sessions"),
    ("windsurf", Path.home() / ".windsurf" / "sessions"),
    ("cline", Path.home() / ".cline" / "sessions"),
    ("roo", Path.home() / ".roo" / "sessions"),
    ("aider", Path.home() / ".aider" / "sessions"),
    ("gemini", Path.home() / ".gemini" / "sessions"),
    ("continue", Path.home() / ".continue" / "sessions"),
    ("antigravity", Path.home() / ".gemini" / "antigravity"),
    ("opencode", Path.home() / ".opencode" / "sessions"),
]


def parse_source_specs(raw_values: list[str]) -> list[tuple[str, Path]]:
    """Parse source specs from CLI/env.

    Accepted formats:
    - `/path/to/sessions` (agent name inferred from directory name)
    - `name=/path/to/sessions` (explicit agent name)
    """
    specs: list[tuple[str, Path]] = []
    seen: set[tuple[str, str]] = set()

    for raw in raw_values:
        val = (raw or "").strip()
        if not val:
            continue

        if "=" in val:
            name_part, path_part = val.split("=", 1)
            agent = (name_part or "").strip().lower() or "unknown"
            source_dir = Path(path_part.strip()).expanduser()
        else:
            source_dir = Path(val).expanduser()
            agent = source_dir.name.lower().replace(" ", "-") or "unknown"

        key = (agent, str(source_dir))
        if key in seen:
            continue
        seen.add(key)
        specs.append((agent, source_dir))

    return specs


def get_source_specs(
    cli_values: tuple[str, ...] | list[str] | None = None,
) -> list[tuple[str, Path]]:
    """Resolve source specs from CLI, env, then defaults."""
    if cli_values:
        parsed = parse_source_specs(list(cli_values))
        if parsed:
            return parsed

    env_val = os.environ.get("SESSIONLOG_SOURCES", "").strip()
    if env_val:
        parsed = parse_source_specs([v for v in env_val.split(",") if v.strip()])
        if parsed:
            return parsed

    return DEFAULT_SOURCE_SPECS.copy()
