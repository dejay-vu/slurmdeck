from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from slurmdeck.storage import db as db_module
from slurmdeck.storage.db import DB_SCHEMA_VERSION, connect


def _create_v1_database(path) -> None:
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA journal_mode = WAL")
    raw.executescript(db_module._SCHEMA_V1)
    raw.execute("PRAGMA user_version = 1")
    raw.execute(
        "INSERT INTO runs "
        "(id, project_id, project_display_name, name, remote, created_at, state, "
        "resources_json, command_json) "
        "VALUES ('legacy', 'p1', 'project', 'run', 'cluster', '2026-01-01', "
        "'planned', '{}', '{\"argv\":[\"x\"]}')"
    )
    raw.commit()
    raw.close()


def test_v1_migration_recovers_an_already_added_target_column(tmp_path):
    path = tmp_path / "half-migrated.db"
    _create_v1_database(path)
    raw = sqlite3.connect(path)
    raw.execute("ALTER TABLE runs ADD COLUMN target TEXT NOT NULL DEFAULT ''")
    raw.commit()
    raw.close()

    migrated = connect(path)
    try:
        row = migrated.execute("SELECT id, target FROM runs").fetchone()
        assert tuple(row) == ("legacy", "")
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == DB_SCHEMA_VERSION
    finally:
        migrated.close()


def test_v1_migration_rolls_back_ddl_when_version_advance_fails(tmp_path):
    path = tmp_path / "rollback.db"
    _create_v1_database(path)
    connection = sqlite3.connect(path)

    def deny_version_advance(action, argument1, argument2, _database, _trigger):
        if action == sqlite3.SQLITE_PRAGMA and argument1 == "user_version" and argument2 == str(DB_SCHEMA_VERSION):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(deny_version_advance)
    with pytest.raises(sqlite3.DatabaseError):
        db_module._migrate(connection)
    connection.set_authorizer(None)

    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    columns = [str(row[1]) for row in connection.execute("PRAGMA table_info(runs)")]
    assert "target" not in columns

    db_module._migrate(connection)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == DB_SCHEMA_VERSION
    connection.close()


def test_concurrent_v1_migration_rechecks_version_after_write_lock(tmp_path, monkeypatch):
    path = tmp_path / "concurrent.db"
    _create_v1_database(path)
    barrier = threading.Barrier(2)
    calls = threading.local()
    real_schema_version = db_module._schema_version

    def synchronized_first_read(connection):
        version = real_schema_version(connection)
        count = getattr(calls, "count", 0)
        calls.count = count + 1
        if count == 0:
            barrier.wait(timeout=5)
        return version

    monkeypatch.setattr(db_module, "_schema_version", synchronized_first_read)

    def migrate() -> tuple[int, list[str]]:
        connection = connect(path)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            columns = [str(row[1]) for row in connection.execute("PRAGMA table_info(runs)")]
            return version, columns
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: migrate(), range(2)))

    assert all(version == DB_SCHEMA_VERSION for version, _columns in results)
    assert all(columns.count("target") == 1 for _version, columns in results)
