"""SQLite database with WAL mode for proper concurrency.

Replaces DuckDB which had constant lock contention issues.
SQLite with WAL mode supports:
- Multiple concurrent readers
- Single writer (but writers don't block readers)
- No lock timeout errors
"""

import sqlite3
import threading

from sessionlog.config import DB_PATH

# Thread-local storage for reader connections
_local = threading.local()

# Single writer connection (protected by lock)
_writer_lock = threading.Lock()
_writer_conn = None


def get_writer() -> sqlite3.Connection:
    """Get the serialized writer connection.

    Use this for INSERT, UPDATE, DELETE, or DDL statements.
    """
    global _writer_conn
    if _writer_conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _writer_conn = _connect()
        _init_schema(_writer_conn)
    return _writer_conn


def get_reader() -> sqlite3.Connection:
    """Get a reader connection for this thread.

    Each thread gets its own reader. Uses autocommit so each query sees
    the latest committed WAL data without holding a stale snapshot.
    """
    if not hasattr(_local, "reader"):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _local.reader = _connect(autocommit=True)
    return _local.reader


def get_conn() -> sqlite3.Connection:
    """Legacy API: returns reader by default."""
    return get_reader()


def _connect(autocommit: bool = False) -> sqlite3.Connection:
    """Create a SQLite connection with optimal settings.

    autocommit=True uses isolation_level=None so readers always see the
    latest committed WAL data without holding a stale snapshot.
    """
    conn = sqlite3.connect(
        str(DB_PATH),
        check_same_thread=False,  # Allow use across threads
        timeout=30.0,  # 30s timeout (rarely hit with WAL)
        isolation_level=None if autocommit else "",
    )

    # Enable WAL mode for concurrent access
    conn.execute("PRAGMA journal_mode=WAL")

    # Other optimizations
    conn.execute("PRAGMA synchronous=NORMAL")  # Faster, still safe with WAL
    conn.execute("PRAGMA busy_timeout=30000")  # 30s busy timeout
    conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA temp_store=MEMORY")

    return conn


def _migrate_add_columns(conn: sqlite3.Connection, table: str, columns: list):
    """Add columns to table if they don't exist (safe migration)."""
    existing = {
        row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for col_name, col_type in columns:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")


def _init_schema(conn: sqlite3.Connection):
    """Initialize database schema."""

    # Main tables
    conn.execute("""
        CREATE TABLE IF NOT EXISTS raw_entries (
            entry_id TEXT PRIMARY KEY,
            session_id TEXT,
            project_name TEXT,
            agent_type TEXT DEFAULT 'unknown',
            entry_type TEXT,
            timestamp_utc TIMESTAMP,
            parent_uuid TEXT,
            is_sidechain INTEGER DEFAULT 0,
            user_text TEXT,
            user_text_length INTEGER DEFAULT 0,
            is_tool_result INTEGER DEFAULT 0,
            tool_result_error INTEGER DEFAULT 0,
            tool_result_error_type TEXT,         -- classified error type (command_failed, user_rejected, etc.)
            model TEXT,
            content_types TEXT,  -- JSON array as text
            tool_names TEXT,     -- JSON array as text
            tool_file_paths TEXT, -- JSON array of file paths from file-touching tool_use blocks
            text_content TEXT,
            text_length INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            system_subtype TEXT,
            duration_ms INTEGER DEFAULT 0,
            git_branch TEXT,
            cwd TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            project_name TEXT,
            started_at TIMESTAMP,
            ended_at TIMESTAMP,
            duration_seconds INTEGER DEFAULT 0,
            user_prompt_count INTEGER DEFAULT 0,
            assistant_msg_count INTEGER DEFAULT 0,
            tool_use_count INTEGER DEFAULT 0,
            tool_error_count INTEGER DEFAULT 0,
            turn_count INTEGER DEFAULT 0,
            first_prompt TEXT,
            intent TEXT DEFAULT 'unknown',
            trajectory TEXT DEFAULT 'unknown',
            convergence_score REAL DEFAULT 0.0,
            drift_score REAL DEFAULT 0.0,
            thrash_score REAL DEFAULT 0.0
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_features (
            session_id TEXT PRIMARY KEY,
            avg_prompt_length REAL DEFAULT 0,
            prompt_length_trend REAL DEFAULT 0,
            max_prompt_length INTEGER DEFAULT 0,
            avg_response_length REAL DEFAULT 0,
            response_length_trend REAL DEFAULT 0,
            response_length_cv REAL DEFAULT 0,
            total_input_tokens INTEGER DEFAULT 0,
            total_output_tokens INTEGER DEFAULT 0,
            edit_write_ratio REAL DEFAULT 0,
            read_grep_ratio REAL DEFAULT 0,
            bash_ratio REAL DEFAULT 0,
            task_ratio REAL DEFAULT 0,
            web_ratio REAL DEFAULT 0,
            unique_tools_used INTEGER DEFAULT 0,
            avg_turn_duration_ms REAL DEFAULT 0,
            hour_of_day INTEGER DEFAULT 0,
            day_of_week INTEGER DEFAULT 0,
            correction_count INTEGER DEFAULT 0,
            correction_rate REAL DEFAULT 0,
            rephrasing_count INTEGER DEFAULT 0,
            decision_marker_count INTEGER DEFAULT 0,
            topic_keyword_entropy REAL DEFAULT 0,
            sidechain_count INTEGER DEFAULT 0,
            sidechain_ratio REAL DEFAULT 0,
            abandoned INTEGER DEFAULT 0,
            has_pr_link INTEGER DEFAULT 0,
            branch_switch_count INTEGER DEFAULT 0,
            prompt_length_oscillation REAL DEFAULT 0,
            api_error_count INTEGER DEFAULT 0
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_tool_usage (
            session_id TEXT,
            tool_name TEXT,
            use_count INTEGER DEFAULT 0,
            error_count INTEGER DEFAULT 0,
            PRIMARY KEY (session_id, tool_name)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_languages (
            session_id TEXT,
            extension TEXT,
            file_count INTEGER DEFAULT 0,
            PRIMARY KEY (session_id, extension)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS progress_entries (
            entry_id       TEXT PRIMARY KEY,
            session_id     TEXT,
            agent_type     TEXT DEFAULT 'unknown',
            progress_type  TEXT,              -- 'agent_progress' | 'bash_progress'
            parent_tool_id TEXT,              -- toolUseId of parent Task/Bash call
            tool_name      TEXT,              -- sub-agent tool name (agent_progress only)
            has_result     INTEGER DEFAULT 0, -- 1 if tool_result was included inline
            result_error   INTEGER DEFAULT 0, -- 1 if tool_result had is_error=true
            timestamp_utc  TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS baselines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            window_size INTEGER,
            computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            avg_convergence REAL,
            avg_drift REAL,
            avg_thrash REAL,
            avg_duration REAL,
            avg_turns REAL,
            avg_tool_errors REAL,
            avg_correction_rate REAL,
            session_count INTEGER
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS prescriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT,
            title TEXT,
            description TEXT,
            evidence TEXT,
            confidence REAL,
            dismissed INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_judgments (
            session_id TEXT PRIMARY KEY,
            outcome TEXT,
            outcome_confidence REAL DEFAULT 0.0,
            outcome_reasoning TEXT,
            prompt_clarity REAL DEFAULT 0.0,
            prompt_completeness REAL DEFAULT 0.0,
            prompt_missing TEXT,
            prompt_summary TEXT,
            trajectory_summary TEXT,
            underspecified_parts TEXT,
            misalignment_count INTEGER DEFAULT 0,
            misalignments TEXT,
            correction_count INTEGER DEFAULT 0,
            corrections TEXT,
            productive_turns INTEGER DEFAULT 0,
            waste_turns INTEGER DEFAULT 0,
            productivity_ratio REAL DEFAULT 0.0,
            waste_breakdown TEXT,
            narrative TEXT,
            what_worked TEXT,
            what_failed TEXT,
            user_quote TEXT,
            claude_md_suggestion TEXT,
            claude_md_rationale TEXT,
            raw_analysis_1 TEXT,
            raw_analysis_2 TEXT,
            judged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS ingestion_log (
            file_path TEXT PRIMARY KEY,
            mtime REAL,
            entry_count INTEGER DEFAULT 0,
            ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS skip_cache (
            file_path TEXT PRIMARY KEY,
            mtime REAL,
            error_type TEXT,
            error_message TEXT,
            skip_until TIMESTAMP,
            cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_skills (
            session_id TEXT PRIMARY KEY,
            d1_level INTEGER DEFAULT 0,
            d1_opportunity INTEGER DEFAULT 0,
            d2_level INTEGER DEFAULT 0,
            d2_opportunity INTEGER DEFAULT 0,
            d3_level INTEGER DEFAULT 0,
            d3_opportunity INTEGER DEFAULT 0,
            d4_level INTEGER DEFAULT 0,
            d4_opportunity INTEGER DEFAULT 0,
            d5_level INTEGER DEFAULT 0,
            d5_opportunity INTEGER DEFAULT 0,
            d6_level INTEGER DEFAULT 0,
            d6_opportunity INTEGER DEFAULT 0,
            d7_level INTEGER DEFAULT 0,
            d7_opportunity INTEGER DEFAULT 0,
            d8_level INTEGER DEFAULT 0,
            d8_opportunity INTEGER DEFAULT 0,
            d9_level INTEGER DEFAULT 0,
            d9_opportunity INTEGER DEFAULT 0,
            d10_level INTEGER DEFAULT 0,
            d10_opportunity INTEGER DEFAULT 0,
            detection_confidence REAL DEFAULT 0.0,
            assessed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS skill_profile (
            id INTEGER PRIMARY KEY DEFAULT 1,
            d1_score REAL DEFAULT 0.0,
            d2_score REAL DEFAULT 0.0,
            d3_score REAL DEFAULT 0.0,
            d4_score REAL DEFAULT 0.0,
            d5_score REAL DEFAULT 0.0,
            d6_score REAL DEFAULT 0.0,
            d7_score REAL DEFAULT 0.0,
            d8_score REAL DEFAULT 0.0,
            d9_score REAL DEFAULT 0.0,
            d10_score REAL DEFAULT 0.0,
            gap_1 TEXT,
            gap_2 TEXT,
            gap_3 TEXT,
            session_count INTEGER DEFAULT 0,
            computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS skill_nudges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dimension TEXT,
            current_level INTEGER DEFAULT 0,
            target_level INTEGER DEFAULT 0,
            nudge_text TEXT,
            evidence TEXT,
            frequency INTEGER DEFAULT 1,
            dismissed INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS synthesis (
            id INTEGER PRIMARY KEY DEFAULT 1,
            at_a_glance TEXT,
            usage_narrative TEXT,
            top_wins TEXT,
            top_friction TEXT,
            claude_md_additions TEXT,
            fun_headline TEXT,
            generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Migrate: add new columns to raw_entries if missing
    _migrate_add_columns(
        conn,
        "raw_entries",
        [
            ("agent_type", "TEXT DEFAULT 'unknown'"),
            ("tool_result_error_type", "TEXT"),
            ("tool_file_paths", "TEXT"),
            # Primary tool's key input: Bash command, Task prompt snippet, etc.
            # Lets the live monitor show "what it's doing" beyond just the tool name.
            ("tool_input_preview", "TEXT"),
        ],
    )

    # Migrate: add new columns to session_judgments if missing
    _migrate_add_columns(
        conn,
        "session_judgments",
        [
            ("narrative", "TEXT"),
            ("what_worked", "TEXT"),
            ("what_failed", "TEXT"),
            ("user_quote", "TEXT"),
            ("claude_md_suggestion", "TEXT"),
            ("claude_md_rationale", "TEXT"),
        ],
    )

    # FTS5 virtual table for full-text search across messages
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            content,
            session_id UNINDEXED,
            entry_type UNINDEXED,
            tokenize='porter unicode61'
        )
    """)

    # Migrate: add subagent feature columns to session_features if missing
    _migrate_add_columns(
        conn,
        "session_features",
        [
            ("subagent_spawn_count", "INTEGER DEFAULT 0"),
            ("subagent_tool_diversity", "INTEGER DEFAULT 0"),
            ("subagent_error_rate", "REAL DEFAULT 0"),
            ("bash_heartbeat_count", "INTEGER DEFAULT 0"),
        ],
    )
    _migrate_add_columns(
        conn,
        "progress_entries",
        [
            ("agent_type", "TEXT DEFAULT 'unknown'"),
        ],
    )

    # Create indexes for common queries
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_raw_entries_session ON raw_entries(session_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_raw_entries_timestamp ON raw_entries(timestamp_utc)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_progress_session ON progress_entries(session_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_progress_type ON progress_entries(progress_type)"
    )

    conn.commit()


def execute_write(sql: str, params=None):
    """Execute a write query with proper locking."""
    with _writer_lock:
        writer = get_writer()
        if params:
            result = writer.execute(sql, params)
        else:
            result = writer.execute(sql)
        writer.commit()
        return result


def execute_read(sql: str, params=None):
    """Execute a read query using a reader connection."""
    reader = get_reader()
    if params:
        return reader.execute(sql, params)
    return reader.execute(sql)


def rebuild_fts_index():
    """Rebuild the FTS5 index from raw_entries."""
    writer = get_writer()
    writer.execute("DELETE FROM messages_fts")
    writer.execute("""
        INSERT INTO messages_fts(content, session_id, entry_type)
        SELECT
            COALESCE(user_text, '') || ' ' || COALESCE(text_content, ''),
            session_id,
            entry_type
        FROM raw_entries
        WHERE (user_text IS NOT NULL AND user_text != '')
           OR (text_content IS NOT NULL AND text_content != '')
    """)
    writer.commit()
