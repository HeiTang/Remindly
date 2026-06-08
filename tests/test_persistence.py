from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from remindly.reminders.drafts import EditSession
from remindly.reminders.models import MentionKind, Participant, ReminderDraft
from remindly.storage.migrations import INITIAL_SCHEMA_SQL
from remindly.storage.session_stores import SqliteDraftStore, SqliteEditSessionStore
from remindly.storage.sqlite import ReminderRepository


class PersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repository = ReminderRepository(Path(self.temp_dir.name) / "test.db")
        self.repository.migrate()
        self.zone = ZoneInfo("Asia/Taipei")
        self.now = datetime(2026, 6, 4, 12, 0, tzinfo=self.zone)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_draft_survives_store_recreation(self) -> None:
        store = SqliteDraftStore(ttl_minutes=10, repository=self.repository)
        draft = ReminderDraft(
            id="draft_1",
            chat_id=100,
            chat_type="private",
            creator_user_id=7,
            timezone="Asia/Taipei",
            source_text="提醒我明天倒垃圾",
            title="倒垃圾",
            remind_at=self.now + timedelta(days=1),
            participants=[
                Participant(
                    user_id=7,
                    username="orange",
                    display_name="@orange",
                    mention_kind=MentionKind.USERNAME,
                )
            ],
            missing_fields=["time"],
            parse_result={"grain": "day"},
        )

        store.save(draft, self.now)
        reloaded_store = SqliteDraftStore(ttl_minutes=10, repository=self.repository)
        reloaded = reloaded_store.get_by_id("draft_1", self.now)

        self.assertIsNotNone(reloaded)
        self.assertEqual("倒垃圾", reloaded.title)
        self.assertEqual(["time"], reloaded.missing_fields)
        self.assertEqual("@orange", reloaded.participants[0].display_name)

    def test_edit_session_survives_store_recreation(self) -> None:
        store = SqliteEditSessionStore(ttl_minutes=10, repository=self.repository)
        store.save(EditSession(chat_id=100, user_id=7, short_id="R-0001", field="time"), self.now)

        reloaded_store = SqliteEditSessionStore(ttl_minutes=10, repository=self.repository)
        session = reloaded_store.get_for_context(100, 7, self.now)

        self.assertIsNotNone(session)
        self.assertEqual("R-0001", session.short_id)
        self.assertEqual("time", session.field)

    def test_upsert_user_and_chat(self) -> None:
        self.repository.upsert_user(7, "orange", "Orange", self.now)
        self.repository.upsert_chat(100, "group", "Test Group", None, self.now)

        self.assertEqual("@orange", self.repository.get_user_display_name(7))

    def test_existing_v1_database_migrates_chat_settings(self) -> None:
        database_path = Path(self.temp_dir.name) / "v1.db"
        with sqlite3.connect(database_path) as connection:
            connection.executescript(INITIAL_SCHEMA_SQL)
            connection.execute(
                """
                create table schema_migrations (
                    version integer primary key,
                    name text not null,
                    applied_at text not null
                )
                """
            )
            connection.execute(
                "insert into schema_migrations (version, name, applied_at) values (?, ?, ?)",
                (1, "initial_schema", self.now.isoformat()),
            )

        repository = ReminderRepository(database_path)
        repository.migrate()

        self.assertFalse(repository.is_chat_natural_language_enabled(100))
        repository.set_chat_natural_language_enabled(100, True, 7, self.now)
        self.assertTrue(repository.is_chat_natural_language_enabled(100))


if __name__ == "__main__":
    unittest.main()
