"""Project SQLite database: connection setup and schema migrations."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from slurmdeck.errors import SchemaVersionError
from slurmdeck.storage.permissions import ensure_private_directory, ensure_private_file, restrict_file_if_present

DB_SCHEMA_VERSION = 1

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


def connect(path: Path) -> sqlite3.Connection:
    """Open (creating/migrating if needed) the project database."""
    ensure_private_directory(path.parent)
    ensure_private_file(path)
    # check_same_thread=False: the TUI calls services from worker threads; WAL
    # journaling plus short transactions keeps cross-thread use safe.
    connection = sqlite3.connect(path, timeout=5.0, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA foreign_keys = ON")
    _migrate(connection)
    for suffix in ("", "-wal", "-shm", "-journal"):
        restrict_file_if_present(Path(f"{path}{suffix}"))
    return connection


def _migrate(connection: sqlite3.Connection) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version == DB_SCHEMA_VERSION:
        return
    if version > DB_SCHEMA_VERSION:
        raise SchemaVersionError("project database", version, DB_SCHEMA_VERSION)
    with connection:
        if version < 1:
            connection.executescript(_SCHEMA_V1)
        connection.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")
