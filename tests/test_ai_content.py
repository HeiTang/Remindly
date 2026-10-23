from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from test_ai import NOW, PERSON, parsed, rule

from remindly.ai.contracts import GeneratedResponse
from remindly.ai.parser import SemanticParser
from remindly.reminders.models import MentionKind, Participant, ReminderDraft
from remindly.reminders.parser import ReminderParser
from remindly.reminders.service import Confirmation, DraftPrompt, ReminderService
from remindly.storage.session_stores import SqliteDraftStore, SqliteEditSessionStore
from remindly.storage.sqlite import ReminderRepository
from remindly.telegram.models import TelegramChat, TelegramMessage, TelegramUser


class GeminiContentRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = Path(__file__).parent / "fixtures" / "gemini_content_regressions.jsonl"
        cls.rows = [json.loads(line) for line in fixture.read_text().splitlines()]

    def parse(self, text, response, *, context=None, participants=None, purpose="reminder"):
        generator = Mock()
        generator.generate.return_value = GeneratedResponse(response, "test", "variable")
        return SemanticParser(generator).parse(
            text, 7, NOW, participants or [PERSON], context=context, purpose=purpose
        )

    def test_literal_content_is_independent_of_model_title_and_question(self):
        for text, title in (
            ("提醒我明天下午三點寄出年度報表", "寄出年度報表"),
            ("明天下午三點提醒我拿包裹和明天要用的資料", "拿包裹和明天要用的資料"),
            ("11/31 23:55提醒我領藥", "領藥"),
            ("提醒我2026/11/31 23:55領藥", "領藥"),
            ("11月170日 23:55領藥", "領藥"),
            ("明天下午三點寄出年度報表", "寄出年度報表"),
        ):
            for model_title in (None, "轉帳", "年度報表"):
                with self.subTest(text=text, model_title=model_title):
                    result = self.parse(text, parsed(title=model_title, question="換個時間嗎？"))
                    self.assertEqual(title, result.title)
                    if result.remind_at and "time" not in result.missing_fields:
                        self.assertIsNone(result.raw["question"])

    def test_missing_source_title_cannot_be_invented_by_model(self):
        result = self.parse("提醒我明天下午三點", parsed(title="轉帳", question="明天幾點？"))
        self.assertIsNone(result.title)
        self.assertIn("title", result.missing_fields)
        self.assertIsNone(result.raw["question"])

    def test_semantic_conditions_and_multiple_reminders_keep_their_question(self):
        for source in (
            "提醒我明天下午三點只在工作日喝水",
            "提醒我明天下午三點排除國定假日喝水",
            "提醒我明天下午三點如果下雨就收衣服",
            "提醒我明天下午三點之前交報表",
            "提醒我明天下午三點前交報表",
            "提醒我明天下午三點拿包裹，後天九點提醒我領藥",
            "提醒我明天下午三點去市場，後天去醫院",
            "提醒我明天下午三點週一至週五喝水",
            "提醒我明天下午三點喝水但只在工作日",
        ):
            with self.subTest(source=source):
                result = self.parse(source, parsed(question="請確認額外條件。"))
                self.assertEqual("請確認額外條件。", result.raw["question"])
                self.assertIn("clarification", result.missing_fields)

    def test_unrequested_recurrence_and_missing_participants_are_not_finalized(self):
        result = self.parse("提醒我明天下午三點拿包裹", parsed(recurrence=rule()))
        self.assertIn("clarification", result.missing_fields)
        other = Participant("Alice", MentionKind.TEXT_MENTION, user_id=8)
        result = self.parse(
            "提醒我明天下午三點和 @alice 拿包裹",
            parsed(participant_indices=[], question="要提醒誰？"),
            participants=[PERSON, other],
        )
        self.assertIn("participants", result.missing_fields)
        self.assertEqual("要提醒誰？", result.raw["question"])

    def test_model_cannot_omit_explicit_condition_or_multiple_reminder_question(self):
        for source in (
            "提醒我明天下午三點只在工作日喝水",
            "提醒我明天下午三點排除國定假日喝水",
            "提醒我明天下午三點如果下雨就收衣服",
            "提醒我明天下午三點拿包裹，後天九點提醒我領藥",
            "提醒我明天下午三點喝水但只在工作日",
        ):
            with self.subTest(source=source):
                result = self.parse(source, parsed(question=None))
                self.assertIn("clarification", result.missing_fields)
                self.assertIsNotNone(result.raw["question"])

    def test_literal_names_and_item_attributes_do_not_invent_conditions(self):
        for title in ("寄若晴的包裹", "買只要五十元的湯", "買假日要用的湯匙"):
            with self.subTest(title=title):
                for question in (None, "要改成明天嗎？"):
                    result = self.parse(f"提醒我明天下午三點{title}", parsed(question=question))
                    self.assertEqual(title, result.title)
                    self.assertEqual((), result.missing_fields)

    def test_existing_condition_survives_explicit_update_and_edit_keeps_title(self):
        context = ReminderDraft(
            id="condition",
            chat_id=100,
            chat_type="private",
            creator_user_id=7,
            timezone="Asia/Taipei",
            source_text="工作日提醒我領藥",
            title="領藥",
            participants=[PERSON],
            missing_fields=["time", "clarification"],
            parse_result={"question": "是否只限週一至週五？"},
        )
        result = self.parse(
            "提醒我明天下午三點領藥", parsed(intent="update", question=None), context=context
        )
        self.assertIn("clarification", result.missing_fields)
        self.assertEqual("是否只限週一至週五？", result.raw["question"])
        result = self.parse(
            "提醒我明天下午三點寄報表",
            parsed(intent="update", title="寄報表"),
            context=context,
            purpose="edit_time",
        )
        self.assertEqual("領藥", result.title)
        result = self.parse(
            "提醒我明天下午三點", parsed(intent="update", title="領藥"), context=context
        )
        self.assertEqual("領藥", result.title)
        self.assertIn("clarification", result.missing_fields)

    def test_source_condition_survives_invalid_time_and_clock_followup(self):
        source = "提醒我明天25:00只在工作日喝水"
        first = self.parse(source, parsed(question="是否只限週一至週五？"))
        self.assertIn("time", first.missing_fields)
        self.assertIn("clarification", first.missing_fields)
        self.assertEqual("是否只限週一至週五？", first.raw["condition_question"])
        context = ReminderDraft(
            id="invalid-condition",
            chat_id=100,
            chat_type="private",
            creator_user_id=7,
            timezone="Asia/Taipei",
            source_text=source,
            title=first.title,
            participants=[PERSON],
            missing_fields=list(first.missing_fields),
            parse_result=first.raw,
        )
        corrected = self.parse("23:55", parsed(intent="update", question=None), context=context)
        self.assertIn("clarification", corrected.missing_fields)
        self.assertEqual("是否只限週一至週五？", corrected.raw["question"])

    def test_original_gemini_responses_keep_source_content_and_required_questions(self):
        self.assertGreaterEqual(len(self.rows), 7)
        person = Participant("HeiTang", MentionKind.TEXT_MENTION, user_id=7)
        for row in self.rows:
            with self.subTest(case=row["id"]):
                generator = Mock()
                generator.generate.return_value = GeneratedResponse(
                    row["response"], "gemini", "recorded"
                )
                now = datetime.fromisoformat(row["now"]).astimezone(ZoneInfo(row["timezone"]))
                result = SemanticParser(generator).parse(row["text"], 7, now, [person])
                expected = row["expected"]
                self.assertEqual(expected["title"], result.title)
                at = datetime.fromisoformat(expected["at"]) if expected["at"] else None
                self.assertEqual(
                    at.astimezone(UTC) if at else None,
                    result.remind_at.astimezone(UTC) if result.remind_at else None,
                )
                if expected["complete"]:
                    self.assertEqual((), result.missing_fields)
                    self.assertIsNone(result.raw["question"])
                else:
                    self.assertIn("time", result.missing_fields)
                    self.assertIsNotNone(result.raw["question"])

    def test_recorded_responses_obey_confirmation_and_saved_title(self):
        for row in self.rows:
            with self.subTest(case=row["id"]), tempfile.TemporaryDirectory() as directory:
                repository = ReminderRepository(Path(directory) / "recorded.db")
                repository.migrate()
                repository.set_user_timezone(7, row["timezone"])
                generator = Mock()
                generator.enabled.return_value = True
                generator.generate.return_value = GeneratedResponse(
                    row["response"], "gemini", "recorded"
                )
                service = ReminderService(
                    repository=repository,
                    parser=ReminderParser("Asia/Taipei"),
                    draft_store=SqliteDraftStore(10, repository),
                    edit_store=SqliteEditSessionStore(10, repository),
                    default_timezone="Asia/Taipei",
                    ai_parser=SemanticParser(generator),
                )
                message = TelegramMessage(
                    id=1,
                    chat=TelegramChat(id=100, type="private"),
                    from_user=TelegramUser(id=7, first_name="HeiTang", username="HeiTang"),
                    text=row["text"],
                    entities=(),
                )
                now = datetime.fromisoformat(row["now"]).astimezone(ZoneInfo(row["timezone"]))
                preview = service.begin_create(message.text, message, now)
                self.assertEqual([], repository.list_pending(100))
                expected = row["expected"]
                self.assertIsInstance(
                    preview, Confirmation if expected["complete"] else DraftPrompt
                )
                confirmed = service.confirm(preview.draft.id, 7, now)
                if expected["complete"]:
                    self.assertIsNotNone(confirmed)
                    self.assertEqual(expected["title"], repository.list_pending(100)[0].title)
                else:
                    self.assertIsNone(confirmed)
                    self.assertEqual([], repository.list_pending(100))
