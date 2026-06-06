from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from remindly.storage.migrations import (
    INITIAL_SCHEMA_SQL,
    latest_schema_version,
    migrate_sqlite_database,
)


class MigrationTest(unittest.TestCase):
    def test_migrate_records_latest_version_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            with connect(database_path) as connection:
                migrate_sqlite_database(connection)
                migrate_sqlite_database(connection)

                rows = migration_rows(connection)
                tables = table_names(connection)

        self.assertEqual([(latest_schema_version(), "initial_schema")], rows)
        self.assertIn("reminders", tables)
        self.assertIn("reminder_drafts", tables)
        self.assertIn("edit_sessions", tables)

    def test_migrate_adopts_unversioned_existing_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            with connect(database_path) as connection:
                connection.executescript(INITIAL_SCHEMA_SQL)
                migrate_sqlite_database(connection)

                rows = migration_rows(connection)

        self.assertEqual([(latest_schema_version(), "initial_schema")], rows)


def connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    return connection


def migration_rows(connection: sqlite3.Connection) -> list[tuple[int, str]]:
    rows = connection.execute(
        "select version, name from schema_migrations order by version"
    ).fetchall()
    return [(int(row["version"]), str(row["name"])) for row in rows]


def table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "select name from sqlite_master where type = 'table'"
    ).fetchall()
    return {str(row["name"]) for row in rows}
