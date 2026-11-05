from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from remindly.reminders.parser import ReminderParser
from remindly.reminders.service import DraftPrompt, ReminderService
from remindly.storage.session_stores import SqliteDraftStore, SqliteEditSessionStore
from remindly.storage.sqlite import ReminderRepository
from remindly.telegram.models import TelegramChat, TelegramMessage, TelegramUser


class SharedGuardsTest(unittest.TestCase):
    def test_native_partial_time_correction_cannot_reach_confirmation(self):
        now = datetime(2026, 10, 31, 12, tzinfo=ZoneInfo("Asia/Taipei"))
        message = TelegramMessage(
            1,
            TelegramChat(100, "private"),
            TelegramUser(7, "Orange"),
            "提醒我明天20:00寄包裹",
            (),
        )
        with tempfile.TemporaryDirectory() as directory:
            repo = ReminderRepository(Path(directory) / "test.db")
            repo.migrate()
            service = ReminderService(
                repo,
                ReminderParser("Asia/Taipei"),
                SqliteDraftStore(10, repo),
                SqliteEditSessionStore(10, repo),
                "Asia/Taipei",
            )
            service.begin_create(message.text, message, now)
            result = service.continue_draft(
                replace(message, text="改成明天晚上八點過二十分鐘"), now
            )
            self.assertIsInstance(result, DraftPrompt)
            self.assertIn("time", result.draft.missing_fields)
            self.assertIsNone(service.confirm(result.draft.id, 7, now))

    def test_native_time_correction_preserves_existing_condition_and_blocks_confirmation(self):
        now = datetime(2026, 10, 31, 12, tzinfo=ZoneInfo("Asia/Taipei"))
        message = TelegramMessage(
            1,
            TelegramChat(100, "private"),
            TelegramUser(7, "Orange"),
            "提醒我明天15:00只在工作日喝水",
            (),
        )
        with tempfile.TemporaryDirectory() as directory:
            repo = ReminderRepository(Path(directory) / "test.db")
            repo.migrate()
            service = ReminderService(
                repo,
                ReminderParser("Asia/Taipei"),
                SqliteDraftStore(10, repo),
                SqliteEditSessionStore(10, repo),
                "Asia/Taipei",
            )
            initial = service.begin_create(message.text, message, now)
            self.assertIsInstance(initial, DraftPrompt)
            question = initial.draft.parse_result["condition_question"]
            result = service.continue_draft(replace(message, text="改成明天九點"), now)
            self.assertEqual(9, result.draft.remind_at.hour)
            self.assertEqual("只在工作日喝水", result.draft.title)
            self.assertIn("clarification", result.draft.missing_fields)
            self.assertEqual(question, result.draft.parse_result["condition_question"])
            self.assertIsNone(service.confirm(result.draft.id, 7, now))
            self.assertEqual([], repo.list_pending(100))
            result = service.continue_draft(replace(message, text="不是11/17 23:55"), now)
            self.assertIn("time", result.draft.missing_fields)
            self.assertIn("clarification", result.draft.missing_fields)
            self.assertIsNone(result.draft.remind_at)

    def test_native_conditions_and_literal_nouns_use_the_same_guard(self):
        now = datetime(2026, 10, 31, 12, tzinfo=ZoneInfo("Asia/Taipei"))
        message = TelegramMessage(
            1, TelegramChat(100, "private"), TelegramUser(7, "Orange"), "", ()
        )
        parser = ReminderParser("Asia/Taipei")
        for text in (
            "提醒我明天下午三點前交報表",
            "每天九點提醒我喝水，排除國定假日",
        ):
            with self.subTest(text=text):
                result = parser.parse(text, message, now)
                self.assertIn("clarification", result.missing_fields)
        for title in ("前往車站", "之前的會議紀錄", "工作日行事曆"):
            with self.subTest(title=title):
                result = parser.parse("提醒我明天15:00" + title, message, now)
                self.assertNotIn("clarification", result.missing_fields)
                self.assertEqual(title, result.title)
