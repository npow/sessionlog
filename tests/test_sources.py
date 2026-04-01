"""Tests for multi-source session discovery."""

from sessionlog.config import get_source_specs, parse_source_specs
from sessionlog.ingest import find_jsonl_files


def test_parse_source_specs_supports_named_and_unnamed_paths(tmp_path):
    a = tmp_path / "alpha"
    b = tmp_path / "beta"
    specs = parse_source_specs([f"codex={a}", str(b)])

    assert specs[0][0] == "codex"
    assert specs[0][1] == a
    assert specs[1][0] == "beta"
    assert specs[1][1] == b


def test_get_source_specs_prefers_cli_values(tmp_path):
    source = tmp_path / "sessions"
    specs = get_source_specs((f"cursor={source}",))
    assert specs == [("cursor", source)]


def test_find_jsonl_files_reads_all_sources_and_prefixes_project(tmp_path):
    claude_dir = tmp_path / "claude_projects"
    codex_dir = tmp_path / "codex_sessions"

    c_file = claude_dir / "project-a" / "one.jsonl"
    x_file = codex_dir / "workspace-b" / "nested" / "two.jsonl"
    c_file.parent.mkdir(parents=True)
    x_file.parent.mkdir(parents=True)
    c_file.write_text("{}\n")
    x_file.write_text("{}\n")

    files = find_jsonl_files(
        source_specs=[("claude", claude_dir), ("codex", codex_dir)]
    )

    by_path = {p: project for p, project in files}
    assert by_path[c_file] == "claude:project-a"
    assert by_path[x_file] == "codex:workspace-b"
