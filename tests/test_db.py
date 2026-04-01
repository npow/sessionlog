"""Tests for database schema creation and migration in db.py."""

import sqlite3
from pathlib import Path


def make_conn(db_path: Path) -> sqlite3.Connection:
    """Create a fresh SQLite connection pointing at a temp path."""
    import os

    os.environ["CLAUDE_RETRO_DB"] = str(db_path)

    # Force db module to re-initialize with the new path
    import importlib
    import sessionlog.config as cfg
    import sessionlog.db as db_mod

    # Reload config so DB_PATH picks up the env var
    importlib.reload(cfg)
    importlib.reload(db_mod)

    conn = db_mod.get_writer()
    return conn, db_mod


class TestSchemaCreation:
    def test_get_writer_creates_all_tables(self, tmp_path):
        db_path = tmp_path / "test.sqlite"
        conn = make_conn(db_path)[0]

        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        expected = {
            "raw_entries",
            "sessions",
            "session_features",
            "session_tool_usage",
            "progress_entries",
            "baselines",
            "prescriptions",
            "session_judgments",
            "ingestion_log",
            "skip_cache",
        }
        assert expected.issubset(tables)

    def test_progress_entries_has_correct_columns(self, tmp_path):
        db_path = tmp_path / "test2.sqlite"
        conn, _ = make_conn(db_path)

        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(progress_entries)").fetchall()
        }
        expected_cols = {
            "entry_id",
            "session_id",
            "agent_type",
            "progress_type",
            "parent_tool_id",
            "tool_name",
            "has_result",
            "result_error",
            "timestamp_utc",
        }
        assert expected_cols == cols

    def test_session_features_has_subagent_and_heartbeat_columns(self, tmp_path):
        db_path = tmp_path / "test3.sqlite"
        conn, _ = make_conn(db_path)

        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(session_features)").fetchall()
        }
        assert "subagent_spawn_count" in cols
        assert "subagent_tool_diversity" in cols
        assert "subagent_error_rate" in cols
        assert "bash_heartbeat_count" in cols

    def test_ingestion_log_table_exists(self, tmp_path):
        db_path = tmp_path / "test4.sqlite"
        conn, _ = make_conn(db_path)

        result = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ingestion_log'"
        ).fetchone()
        assert result is not None
        assert result[0] == "ingestion_log"

    def test_migrate_add_columns_is_idempotent(self, tmp_path):
        db_path = tmp_path / "test5.sqlite"
        import os

        os.environ["CLAUDE_RETRO_DB"] = str(db_path)

        import importlib
        import sessionlog.config as cfg
        import sessionlog.db as db_mod

        importlib.reload(cfg)
        importlib.reload(db_mod)

        conn = db_mod.get_writer()

        # Call _migrate_add_columns twice with the same columns — must not raise
        db_mod._migrate_add_columns(
            conn,
            "session_features",
            [
                ("subagent_spawn_count", "INTEGER DEFAULT 0"),
                ("bash_heartbeat_count", "INTEGER DEFAULT 0"),
            ],
        )
        db_mod._migrate_add_columns(
            conn,
            "session_features",
            [
                ("subagent_spawn_count", "INTEGER DEFAULT 0"),
                ("bash_heartbeat_count", "INTEGER DEFAULT 0"),
            ],
        )

        # Columns should still exist
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(session_features)").fetchall()
        }
        assert "subagent_spawn_count" in cols
        assert "bash_heartbeat_count" in cols
