from __future__ import annotations

import sqlite3
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from remindly.storage.migrations import (
    INITIAL_SCHEMA_SQL,
    MIGRATIONS,
    Migration,
    apply_migration,
    ensure_migration_table,
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

        self.assertEqual(expected_migration_rows(), rows)
        self.assertEqual(8, latest_schema_version())
        self.assertIn("reminders", tables)
        self.assertIn("reminder_drafts", tables)
        self.assertIn("edit_sessions", tables)
        self.assertIn("chat_settings", tables)
        self.assertIn("ai_keys", tables)
        self.assertIn("ai_settings", tables)

    def test_migrate_adopts_unversioned_existing_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "test.db"
            with connect(database_path) as connection:
                connection.executescript(INITIAL_SCHEMA_SQL)
                migrate_sqlite_database(connection)

                rows = migration_rows(connection)

        self.assertEqual(expected_migration_rows(), rows)

    def test_provider_migration_preserves_existing_encrypted_keys_and_state(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            connect(Path(directory) / "test.db") as connection,
        ):
            ensure_migration_table(connection)
            for migration in MIGRATIONS[:6]:
                apply_migration(connection, migration)
            connection.execute(
                """insert into ai_keys
                (id, user_id, encrypted_key, fingerprint, position, status, cooldown_until)
                values ('key1', 7, ?, 'fingerprint', 2, 'cooldown',
                        '2027-01-01T00:00:00+00:00')""",
                (b"encrypted-credential",),
            )
            before = dict(connection.execute("select * from ai_keys").fetchone())
            migrate_sqlite_database(connection)
            migrate_sqlite_database(connection)
            after = dict(connection.execute("select * from ai_keys").fetchone())
            self.assertEqual("gemini", after.pop("provider"))
            self.assertEqual(before, after)

    def test_ledger_failure_rolls_back_schema_and_allows_retry(self) -> None:
        with connect(Path(":memory:")) as connection:
            ensure_migration_table(connection)
            for migration in MIGRATIONS[:6]:
                apply_migration(connection, migration)
            connection.execute(
                """create trigger reject_version before insert on schema_migrations
                when new.version = 7
                begin select raise(abort, 'ledger unavailable'); end"""
            )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "ledger unavailable"):
                migrate_sqlite_database(connection)
            columns = {row["name"] for row in connection.execute("pragma table_info(ai_keys)")}
            self.assertNotIn("provider", columns)
            self.assertEqual(6, migration_rows(connection)[-1][0])
            connection.execute("drop trigger reject_version")
            migrate_sqlite_database(connection)
            self.assertEqual(expected_migration_rows(), migration_rows(connection))

    def test_partial_ddl_failure_and_caller_transaction_are_atomic(self) -> None:
        with connect(Path(":memory:")) as connection:
            ensure_migration_table(connection)
            with self.assertRaises(sqlite3.OperationalError):
                apply_migration(connection, Migration(99, "broken", "create table probe (x); bad;"))
            self.assertNotIn("probe", table_names(connection))
            connection.execute("begin")
            apply_migration(connection, Migration(99, "probe", "create table probe (x);"))
            self.assertTrue(connection.in_transaction)
            connection.rollback()
            self.assertNotIn("probe", table_names(connection))
            self.assertEqual([], migration_rows(connection))


def expected_migration_rows() -> list[tuple[int, str]]:
    return [
        (1, "initial_schema"),
        (2, "chat_settings"),
        (3, "prompt_message_id"),
        (4, "recurrence"),
        (5, "draft_recurrence"),
        (6, "personal_ai_keys"),
        (7, "ai_key_providers"),
        (8, "ai_edit_context"),
    ]


@contextmanager
def connect(database_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            yield connection


def migration_rows(connection: sqlite3.Connection) -> list[tuple[int, str]]:
    rows = connection.execute(
        "select version, name from schema_migrations order by version"
    ).fetchall()
    return [(int(row["version"]), str(row["name"])) for row in rows]


def table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("select name from sqlite_master where type = 'table'").fetchall()
    return {str(row["name"]) for row in rows}
