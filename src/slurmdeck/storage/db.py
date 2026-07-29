"""Project SQLite database: connection setup and schema migrations."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from slurmdeck.errors import SchemaVersionError
from slurmdeck.storage.permissions import ensure_private_directory, ensure_private_file, restrict_file_if_present

DB_SCHEMA_VERSION = 2

_SCHEMA_V1 = """
CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    project_display_name TEXT NOT NULL,
    name TEXT NOT NULL,
    remote TEXT NOT NULL,
    created_at TEXT NOT NULL,
    state TEXT NOT NULL,
    slurm_job_id TEXT NOT NULL DEFAULT '',
    remote_root TEXT NOT NULL DEFAULT '',
    snapshot_hash TEXT NOT NULL DEFAULT '',
    env_id TEXT NOT NULL DEFAULT '',
    env_generation_id TEXT NOT NULL DEFAULT '',
    env_prefix TEXT NOT NULL DEFAULT '',
    env_attempt_id TEXT NOT NULL DEFAULT '',
    env_build_job_id TEXT NOT NULL DEFAULT '',
    env_wait_policy TEXT NOT NULL DEFAULT '',
    env_dependency_state TEXT NOT NULL DEFAULT '',
    env_dependency_reason TEXT NOT NULL DEFAULT '',
    resources_json TEXT NOT NULL,
    command_json TEXT NOT NULL,
    sweep_file TEXT,
    retry_of TEXT,
    submission_token TEXT NOT NULL DEFAULT '',
    submission_phase TEXT NOT NULL DEFAULT '',
    submission_error_json TEXT NOT NULL DEFAULT '{}',
    status_refreshed_at REAL NOT NULL DEFAULT 0,
    status_refresh_failed_at REAL NOT NULL DEFAULT 0,
    status_refresh_error_json TEXT NOT NULL DEFAULT '{}',
    status_sources_json TEXT NOT NULL DEFAULT '[]',
    scan_watermark REAL NOT NULL DEFAULT 0,
    summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE tasks (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    argv_json TEXT,
    shell TEXT,
    env_json TEXT NOT NULL DEFAULT '{}',
    config_rel TEXT,
    result_rel TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    args_template_json TEXT,
    env_template_json TEXT NOT NULL DEFAULT '{}',
    arg_style TEXT NOT NULL DEFAULT 'posix',
    scheduler_job_id TEXT NOT NULL DEFAULT '',
    scheduler_array_task_id TEXT,
    scheduler_state TEXT NOT NULL DEFAULT '',
    scheduler_exit TEXT NOT NULL DEFAULT '',
    scheduler_reason TEXT NOT NULL DEFAULT '',
    scheduler_observed_at REAL NOT NULL DEFAULT 0,
    scheduler_source TEXT NOT NULL DEFAULT '',
    artifact_state TEXT NOT NULL DEFAULT 'UNKNOWN',
    artifact_exit_code INTEGER,
    artifact_reason TEXT NOT NULL DEFAULT '',
    artifact_observed_at REAL NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL DEFAULT '',
    ended_at TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, idx)
);

CREATE INDEX tasks_by_state ON tasks(run_id, artifact_state, scheduler_state);
"""

_SCHEMA_V2 = """
ALTER TABLE runs ADD COLUMN target TEXT NOT NULL DEFAULT '';
"""


def connect(path: Path) -> sqlite3.Connection:
    """Open (creating/migrating if needed) the project database."""
    ensure_private_directory(path.parent)
    ensure_private_file(path)
    # check_same_thread=False: the TUI calls services from worker threads; WAL
    # journaling plus short transactions keeps cross-thread use safe.
    connection = sqlite3.connect(path, timeout=5.0, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    _migrate(connection)
    for suffix in ("", "-wal", "-shm", "-journal"):
        restrict_file_if_present(Path(f"{path}{suffix}"))
    return connection


def _migrate(connection: sqlite3.Connection) -> None:
    version = _schema_version(connection)
    if version == DB_SCHEMA_VERSION:
        return
    if version > DB_SCHEMA_VERSION:
        raise SchemaVersionError("project database", version, DB_SCHEMA_VERSION)

    # Serialize migration across the per-thread connections used by the TUI.
    # The first version read is only a fast path: another process may finish
    # the migration while this connection waits for the write lock.
    connection.execute("BEGIN IMMEDIATE")
    try:
        version = _schema_version(connection)
        if version > DB_SCHEMA_VERSION:
            raise SchemaVersionError("project database", version, DB_SCHEMA_VERSION)
        if version < 1:
            _execute_schema(connection, _SCHEMA_V1)
        # A process can be killed after an older non-transactional ALTER but
        # before advancing user_version.  Treat the column as the durable fact
        # and finish that migration instead of repeating it.
        if version < 2 and "target" not in _table_columns(connection, "runs"):
            connection.execute(_SCHEMA_V2)
        connection.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def _schema_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _execute_schema(connection: sqlite3.Connection, script: str) -> None:
    """Execute a schema script without sqlite3.executescript's implicit commit."""
    pending: list[str] = []
    for line in script.splitlines(keepends=True):
        pending.append(line)
        statement = "".join(pending)
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            pending.clear()
    if "".join(pending).strip():
        raise sqlite3.OperationalError("incomplete schema statement")
