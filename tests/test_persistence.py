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

    def test_reminder_persists_recurrence_roundtrip(self) -> None:
        from remindly.reminders.models import (
            RecurrencePeriod,
            RecurrenceRule,
            Reminder,
            ReminderStatus,
        )

        rule = RecurrenceRule(
            period=RecurrencePeriod.MONTHLY,
            hour=9,
            minute=0,
            month_days=(1, 18, 25),
        )
        reminder = Reminder(
            id="rmd_r1",
            short_id="R-REC1",
            chat_id=100,
            chat_type="private",
            creator_user_id=7,
            title="繳信用卡",
            remind_at=self.now,
            timezone="Asia/Taipei",
            status=ReminderStatus.PENDING,
            source_text="每月 1, 18, 25 提醒我繳信用卡",
            parse_result={},
            created_at=self.now,
            updated_at=self.now,
            recurrence=rule,
        )
        self.repository.create_reminder(reminder, [])

        got = self.repository.get_by_short_id(100, "R-REC1")
        self.assertIsNotNone(got)
        self.assertEqual(rule, got.recurrence)

    def test_reminder_without_recurrence_reads_none(self) -> None:
        from remindly.reminders.models import Reminder, ReminderStatus

        reminder = Reminder(
            id="rmd_r2",
            short_id="R-REC2",
            chat_id=100,
            chat_type="private",
            creator_user_id=7,
            title="一次性",
            remind_at=self.now,
            timezone="Asia/Taipei",
            status=ReminderStatus.PENDING,
            source_text="",
            parse_result={},
            created_at=self.now,
            updated_at=self.now,
        )
        self.repository.create_reminder(reminder, [])

        got = self.repository.get_by_short_id(100, "R-REC2")
        self.assertIsNone(got.recurrence)

    def test_reschedule_transitions_firing_to_pending(self) -> None:
        from remindly.reminders.models import Reminder, ReminderStatus

        reminder = Reminder(
            id="rmd_r3",
            short_id="R-REC3",
            chat_id=100,
            chat_type="private",
            creator_user_id=7,
            title="每天洗澡",
            remind_at=self.now,
            timezone="Asia/Taipei",
            status=ReminderStatus.PENDING,
            source_text="",
            parse_result={},
            created_at=self.now,
            updated_at=self.now,
        )
        self.repository.create_reminder(reminder, [])
        # 手動把 status 推到 FIRING（模擬 claim_due 的效果）
        with self.repository.connect() as connection:
            connection.execute(
                "update reminders set status = ? where id = ?",
                ("firing", "rmd_r3"),
            )

        next_at = self.now + timedelta(days=1)
        self.repository.reschedule("rmd_r3", next_at, self.now)

        got = self.repository.get_by_short_id(100, "R-REC3")
        from remindly.reminders.models import ReminderStatus as RS
        self.assertEqual(RS.PENDING, got.status)
        self.assertEqual(next_at, got.remind_at)

    def test_reschedule_ignores_non_firing_reminder(self) -> None:
        """對已 FIRED / CANCELLED 的提醒呼叫 reschedule 不會誤改。"""
        from remindly.reminders.models import Reminder, ReminderStatus

        reminder = Reminder(
            id="rmd_r4",
            short_id="R-REC4",
            chat_id=100,
            chat_type="private",
            creator_user_id=7,
            title="已 fired",
            remind_at=self.now,
            timezone="Asia/Taipei",
            status=ReminderStatus.PENDING,
            source_text="",
            parse_result={},
            created_at=self.now,
            updated_at=self.now,
        )
        self.repository.create_reminder(reminder, [])
        with self.repository.connect() as connection:
            connection.execute(
                "update reminders set status = ? where id = ?",
                ("fired", "rmd_r4"),
            )

        self.repository.reschedule("rmd_r4", self.now + timedelta(days=1), self.now)
        got = self.repository.get_by_short_id(100, "R-REC4")
        self.assertEqual(ReminderStatus.FIRED, got.status)
        self.assertEqual(self.now, got.remind_at)

    def test_upsert_user_and_chat(self) -> None:
        self.repository.upsert_user(7, "orange", "Orange", self.now)
        self.repository.upsert_chat(100, "group", "Test Group", None, self.now)

        self.assertEqual("@orange", self.repository.get_user_display_name(7))

    def test_draft_persists_prompt_message_id(self) -> None:
        store = SqliteDraftStore(ttl_minutes=10, repository=self.repository)
        draft = ReminderDraft(
            id="draft_prompt",
            chat_id=100,
            chat_type="private",
            creator_user_id=7,
            timezone="Asia/Taipei",
            source_text="提醒我明天倒垃圾",
            title="倒垃圾",
            remind_at=self.now + timedelta(days=1),
            participants=[],
            missing_fields=["time"],
            parse_result={},
        )

        store.save(draft, self.now)
        store.set_prompt_message_id("draft_prompt", 4242)
        reloaded_store = SqliteDraftStore(ttl_minutes=10, repository=self.repository)
        reloaded = reloaded_store.get_by_id("draft_prompt", self.now)

        self.assertIsNotNone(reloaded)
        self.assertEqual(4242, reloaded.prompt_message_id)

    def test_edit_session_persists_prompt_message_id(self) -> None:
        store = SqliteEditSessionStore(ttl_minutes=10, repository=self.repository)
        store.save(EditSession(chat_id=100, user_id=7, short_id="R-1", field="time"), self.now)
        store.set_prompt_message_id(100, 7, 999)

        reloaded_store = SqliteEditSessionStore(ttl_minutes=10, repository=self.repository)
        session = reloaded_store.get_for_context(100, 7, self.now)

        self.assertIsNotNone(session)
        self.assertEqual(999, session.prompt_message_id)

    def test_draft_save_refreshes_ttl_on_every_call(self) -> None:
        store = SqliteDraftStore(ttl_minutes=10, repository=self.repository)
        draft = ReminderDraft(
            id="draft_refresh",
            chat_id=100,
            chat_type="private",
            creator_user_id=7,
            timezone="Asia/Taipei",
            source_text="提醒我",
            title=None,
            remind_at=None,
            participants=[],
            missing_fields=["title", "time"],
            parse_result={},
        )
        store.save(draft, self.now)
        first_expiry = draft.expires_at

        later = self.now + timedelta(minutes=3)
        store.save(draft, later)

        self.assertEqual(later + timedelta(minutes=10), draft.expires_at)
        self.assertNotEqual(first_expiry, draft.expires_at)

    def test_draft_save_upgrades_ttl_when_transitioning_to_confirming(self) -> None:
        store = SqliteDraftStore(
            ttl_minutes=5,
            repository=self.repository,
            confirming_ttl_minutes=30,
        )
        draft = ReminderDraft(
            id="draft_upgrade",
            chat_id=100,
            chat_type="private",
            creator_user_id=7,
            timezone="Asia/Taipei",
            source_text="提醒我",
            title=None,
            remind_at=None,
            participants=[],
            missing_fields=["title", "time"],
            parse_result={},
        )
        store.save(draft, self.now)
        self.assertEqual(self.now + timedelta(minutes=5), draft.expires_at)

        # 使用者回完追問，draft 進入 confirming 狀態
        draft.title = "倒垃圾"
        draft.remind_at = self.now + timedelta(days=1)
        draft.participants = [
            Participant(
                user_id=7,
                username="orange",
                display_name="@orange",
                mention_kind=MentionKind.USERNAME,
            )
        ]
        draft.missing_fields = []
        store.save(draft, self.now)

        self.assertEqual(self.now + timedelta(minutes=30), draft.expires_at)

    def test_edit_session_save_refreshes_ttl_on_every_call(self) -> None:
        store = SqliteEditSessionStore(ttl_minutes=10, repository=self.repository)
        session = EditSession(chat_id=100, user_id=7, short_id="R-1", field="time")
        store.save(session, self.now)
        first_expiry = session.expires_at

        later = self.now + timedelta(minutes=4)
        store.save(session, later)

        self.assertEqual(later + timedelta(minutes=10), session.expires_at)
        self.assertNotEqual(first_expiry, session.expires_at)

    def test_draft_confirming_state_uses_longer_ttl(self) -> None:
        store = SqliteDraftStore(
            ttl_minutes=5,
            repository=self.repository,
            confirming_ttl_minutes=30,
        )
        asking_draft = ReminderDraft(
            id="draft_asking",
            chat_id=100,
            chat_type="private",
            creator_user_id=7,
            timezone="Asia/Taipei",
            source_text="提醒我",
            title=None,
            remind_at=None,
            participants=[],
            missing_fields=["title", "time"],
            parse_result={},
        )
        confirming_draft = ReminderDraft(
            id="draft_confirming",
            chat_id=200,
            chat_type="private",
            creator_user_id=8,
            timezone="Asia/Taipei",
            source_text="提醒我明天下午三點倒垃圾",
            title="倒垃圾",
            remind_at=self.now + timedelta(days=1),
            participants=[
                Participant(
                    user_id=8,
                    username="orange2",
                    display_name="@orange2",
                    mention_kind=MentionKind.USERNAME,
                )
            ],
            missing_fields=[],
            parse_result={},
        )

        store.save(asking_draft, self.now)
        store.save(confirming_draft, self.now)

        self.assertEqual(self.now + timedelta(minutes=5), asking_draft.expires_at)
        self.assertEqual(self.now + timedelta(minutes=30), confirming_draft.expires_at)

    def test_list_expired_draft_and_session(self) -> None:
        draft_store = SqliteDraftStore(ttl_minutes=10, repository=self.repository)
        edit_store = SqliteEditSessionStore(ttl_minutes=10, repository=self.repository)
        draft = ReminderDraft(
            id="draft_expired",
            chat_id=100,
            chat_type="private",
            creator_user_id=7,
            timezone="Asia/Taipei",
            source_text="提醒我",
            title=None,
            remind_at=None,
            participants=[],
            missing_fields=["title", "time"],
            parse_result={},
        )
        draft_store.save(draft, self.now)
        edit_store.save(
            EditSession(chat_id=100, user_id=7, short_id="R-EXP", field="time"),
            self.now,
        )

        # 快轉超過 TTL；save() 一律以當下 now 計算 expires_at。
        later = self.now + timedelta(minutes=11)
        expired_drafts = draft_store.list_expired(later)
        expired_sessions = edit_store.list_expired(later)

        self.assertEqual(1, len(expired_drafts))
        self.assertEqual("draft_expired", expired_drafts[0].id)
        self.assertEqual(1, len(expired_sessions))
        self.assertEqual("R-EXP", expired_sessions[0].short_id)

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
