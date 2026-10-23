from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from test_ai import parsed

from remindly.ai.contracts import GeneratedResponse
from remindly.ai.parser import SemanticParser
from remindly.reminders.models import ReminderDraft
from remindly.reminders.parser import ReminderParser, participant_from_user
from remindly.reminders.service import (
    Confirmation,
    DraftPrompt,
    EditPrompt,
    RecurrenceRejected,
    ReminderService,
)
from remindly.storage.session_stores import SqliteDraftStore, SqliteEditSessionStore
from remindly.storage.sqlite import ReminderRepository
from remindly.telegram.models import TelegramChat, TelegramMessage, TelegramUser

FIXTURES = Path(__file__).parent / "fixtures"


class TimeBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = ReminderRepository(Path(self.directory.name) / "boundaries.db")
        self.repo.migrate()
        self.service = ReminderService(
            repository=self.repo,
            parser=ReminderParser("Asia/Taipei"),
            draft_store=SqliteDraftStore(10, self.repo),
            edit_store=SqliteEditSessionStore(10, self.repo),
            default_timezone="Asia/Taipei",
        )
        self.message = TelegramMessage(
            id=1,
            chat=TelegramChat(id=100, type="private"),
            from_user=TelegramUser(id=7, first_name="Orange", username="orange"),
            text="",
            entities=(),
        )
        self.now = datetime(2026, 10, 24, 23, 59, tzinfo=ZoneInfo("Asia/Taipei"))

    def answer(self, text, now):
        return self.service.continue_draft(replace(self.message, text=text), now)

    def test_shared_formats_across_followup_edit_and_variable_ai(self):
        ids = {
            "slash-date",
            "single-digit-month-follow-up-format",
            "padded-month-follow-up-format",
            "hyphen-date-clock",
            "year-date-chinese-clock",
            "fullwidth-colon",
            "morning-twelve",
            "clock-extra-minute-digit",
            "clock-extra-hour-digit",
            "clock-with-seconds",
            "clock-missing-minute",
            "evening-twelve-ambiguous",
            "alternative-dates",
            "alternative-clock",
            "dst-nonexistent",
            "dst-ambiguous",
            "dst-before-gap",
            "dst-after-gap",
            "dst-after-fold",
            "fullwidth-clock-invalid",
            "short-date-extra-day-digit",
            "short-date-extra-month-digit",
            "absolute-date-extra-day-digit",
            "absolute-date-leading-zero-day",
            "absolute-date-extra-year-digit",
            "absolute-date-short-year",
            "chinese-date-extra-day-digit",
            "partial-minute-extra-digit",
        }
        cases = [
            json.loads(line)
            for line in (FIXTURES / "time_regressions.jsonl").read_text().splitlines()
        ]
        selected = [case for case in cases if case["id"] in ids]
        self.assertEqual(ids, {case["id"] for case in selected})
        for case in selected:
            now = datetime.fromisoformat(case["now"]).astimezone(ZoneInfo(case["timezone"]))
            expected = datetime.fromisoformat(case["at"]) if case["at"] else None
            self.repo.set_user_timezone(7, case["timezone"])
            with self.subTest(case=case["id"], entry="followup"):
                self.service.begin_create("提醒我買湯", self.message, now)
                result = self.answer(case["text"], now)
                self.assertIsInstance(result, Confirmation if expected else DraftPrompt)
                self.assertEqual(expected, result.draft.remind_at)
                self.assertEqual("買湯", result.draft.title)
            with self.subTest(case=case["id"], entry="edit"):
                initial = self.service.begin_create(
                    f"提醒我 {now:%Y-%m-%d} 23:59 買湯", self.message, now
                )
                original = self.service.confirm(initial.draft.id, 7, now).reminder
                self.service.begin_edit(100, original.short_id, 7, "time", now)
                result = self.service.continue_edit(replace(self.message, text=case["text"]), now)
                saved = self.repo.get_by_short_id(100, original.short_id)
                if expected:
                    self.assertEqual(expected, saved.remind_at)
                else:
                    self.assertIsInstance(result, EditPrompt)
                    self.assertEqual(original, saved)
            context = ReminderDraft(
                id="guard",
                chat_id=100,
                chat_type="private",
                creator_user_id=7,
                timezone=case["timezone"],
                source_text="提醒我買湯",
                title="買湯",
                participants=[participant_from_user(self.message.from_user)],
                missing_fields=["time"],
            )
            for intent, day, clock in (
                ("create", "2030-01-01", "01:00"),
                ("update", "2025-12-31", "23:59"),
                ("not_reminder", None, None),
            ):
                generator = Mock()
                generator.generate.return_value = GeneratedResponse(
                    parsed(intent=intent, title="被模型改掉", date=day, time=clock),
                    "test",
                    "variable",
                )
                parser = SemanticParser(generator)
                for entry, text, prior in (
                    ("ai-followup", case["text"], context),
                    ("ai-edit", case["text"], context),
                    ("ai-leading", f"{case['text']}買湯", None),
                    ("ai-prompt", f"提醒我{case['text']}買湯", None),
                    *([("ai-bare-invalid", case["text"], None)] if expected is None else []),
                ):
                    with self.subTest(case=case["id"], entry=entry, intent=intent):
                        result = parser.parse(
                            text,
                            7,
                            now,
                            context.participants,
                            context=prior,
                            purpose="edit_time" if entry == "ai-edit" else "reminder",
                        )
                        self.assertEqual(expected, result.remind_at)
                        self.assertEqual(expected is None, "time" in result.missing_fields)
                        if prior:
                            self.assertEqual("買湯", result.title)
                            self.assertEqual(tuple(context.participants), result.participants)
                        if entry == "ai-prompt":
                            generator.enabled.return_value = True
                            self.service.ai_parser = parser
                            try:
                                preview = self.service.begin_create(text, self.message, now)
                                self.assertIsInstance(
                                    preview, Confirmation if expected else DraftPrompt
                                )
                                if expected is None:
                                    self.assertIsNone(
                                        self.service.confirm(preview.draft.id, 7, now)
                                    )
                            finally:
                                self.service.ai_parser = None

    def test_date_answer_survives_midnight_and_store_reload(self):
        self.service.begin_create("提醒我買湯", self.message, self.now)
        self.answer("明天", self.now)
        self.service._draft_store = SqliteDraftStore(10, self.repo)
        result = self.answer("23:55", self.now + timedelta(minutes=2))
        self.assertIsInstance(result, Confirmation)
        self.assertEqual(self.now.replace(day=25, hour=23, minute=55), result.draft.remind_at)
        self.assertEqual([], self.repo.list_pending(100))

    def test_ai_guard_does_not_replace_clock_before_date_with_today(self):
        for text in ("23:55 明天買湯", "23:55 買湯 明天"):
            with self.subTest(text=text):
                generator = Mock()
                generator.generate.return_value = GeneratedResponse(
                    parsed(date="2026-10-25", time="23:55"), "test", "variable"
                )
                person = participant_from_user(self.message.from_user)
                result = SemanticParser(generator).parse(text, 7, self.now, [person])
                self.assertEqual(self.now.replace(day=25, hour=23, minute=55), result.remind_at)

    def test_recurrence_cannot_bypass_clock_or_dst_checks(self):
        now = datetime(2027, 3, 13, 12, tzinfo=ZoneInfo("America/New_York"))
        self.repo.set_user_timezone(7, str(now.tzinfo))
        for clock in ("23:555", "23:55:30", "晚上十二點", "02:30"):
            with self.subTest(clock=clock):
                result = self.service.begin_create(f"每天{clock}提醒我買湯", self.message, now)
                self.assertIsInstance(result, RecurrenceRejected)
                generator = Mock()
                generator.generate.return_value = GeneratedResponse(parsed(), "test", "variable")
                for text in (f"每天{clock}提醒我買湯", f"提醒我每天{clock}買湯"):
                    person = participant_from_user(self.message.from_user)
                    guarded = SemanticParser(generator).parse(text, 7, now, [person])
                    self.assertIn("time", guarded.missing_fields)
                    self.assertIsNone(guarded.remind_at)
        result = self.service.begin_create("每天03:30提醒我買湯", self.message, now)
        self.assertIsInstance(result, Confirmation)

    def test_oversized_clock_tokens_require_followup_without_raising(self):
        for text in ("9" * 5000 + ":00", "9" * 5000 + "分"):
            with self.subTest(suffix=text[-3:]):
                result = self.service.begin_create(f"提醒我{text}買湯", self.message, self.now)
                self.assertIsInstance(result, DraftPrompt)
                self.assertIn("time", result.draft.missing_fields)

    def test_ai_preserves_date_after_midnight_despite_changed_model_answers(self):
        now = self.now + timedelta(minutes=2)
        person = participant_from_user(self.message.from_user)
        context = ReminderDraft(
            id="midnight",
            chat_id=100,
            chat_type="private",
            creator_user_id=7,
            timezone="Asia/Taipei",
            source_text="提醒我買湯",
            title="買湯",
            remind_at=self.now.replace(day=25, hour=0, minute=0),
            participants=[person],
            missing_fields=["time"],
        )
        for day in ("2026-10-24", "2026-10-25", "2030-01-01"):
            with self.subTest(day=day):
                generator = Mock()
                generator.generate.return_value = GeneratedResponse(
                    parsed(date=day, time="01:00", title="其他事項"), "test", "variable"
                )
                result = SemanticParser(generator).parse("23:55", 7, now, [person], context=context)
                self.assertEqual(self.now.replace(day=25, hour=23, minute=55), result.remind_at)
                self.assertEqual("買湯", result.title)
                self.assertEqual((), result.missing_fields)

    def test_expired_date_is_not_reinterpreted_after_midnight(self):
        self.service.begin_create("提醒我買湯", self.message, self.now)
        self.answer("今天", self.now)
        result = self.answer("23:55", self.now + timedelta(minutes=2))
        self.assertIsInstance(result, DraftPrompt)
        self.assertIn("time", result.draft.missing_fields)
        self.assertIsNone(self.service.confirm(result.draft.id, 7, self.now + timedelta(minutes=2)))
        corrected = self.answer("10/25 23:55", self.now + timedelta(minutes=3))
        self.assertIsInstance(corrected, Confirmation)
        self.assertEqual(self.now.replace(day=25, hour=23, minute=55), corrected.draft.remind_at)

    def test_confirmation_cannot_create_time_that_passed_across_midnight(self):
        now = self.now.replace(minute=58)
        result = self.service.begin_create("提醒我23:59買湯", self.message, now)
        self.assertIsInstance(result, Confirmation)
        self.assertIsNone(self.service.confirm(result.draft.id, 7, now + timedelta(minutes=3)))
        self.assertEqual([], self.repo.list_pending(100))

    def test_quick_time_cannot_bypass_dst_or_invalid_clock(self):
        now = datetime(2027, 3, 13, 12, tzinfo=ZoneInfo("America/New_York"))
        self.repo.set_user_timezone(7, str(now.tzinfo))
        for clock in ("02:30", "23:555", "23:55:30"):
            with self.subTest(clock=clock):
                draft = self.service.begin_create("提醒我明天買湯", self.message, now).draft
                result = self.service.apply_quick_time(draft.id, clock, 7, now)
                self.assertIsInstance(result, DraftPrompt)
                self.assertIn("time", result.draft.missing_fields)
                self.assertIsNone(self.service.confirm(draft.id, 7, now))

    def test_relative_elapsed_time_remains_valid_in_repeated_hour(self):
        now = datetime(2027, 11, 7, 1, 45, tzinfo=ZoneInfo("America/New_York"))
        self.repo.set_user_timezone(7, str(now.tzinfo))
        result = self.service.begin_create("半小時後提醒我買湯", self.message, now)
        self.assertIsInstance(result, Confirmation)
        self.assertEqual(
            1800, (result.draft.remind_at.astimezone(UTC) - now.astimezone(UTC)).total_seconds()
        )
        self.assertIsNotNone(self.service.confirm(result.draft.id, 7, now))
