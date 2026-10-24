from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from test_ai import NOW, PERSON, parsed, rule

from remindly.ai.contracts import AiUnavailable, GeneratedResponse
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
        for title in (
            "寄若晴的包裹",
            "買只要五十元的湯",
            "買假日要用的湯匙",
            "前往車站",
            "到家樂福買湯",
            "內湖寄包裹",
            "起司蛋糕",
            "工作日行事曆",
            "假日用的湯匙",
            "週末報表",
            "若晴的包裹",
            "若晴的時鐘",
            "等待回覆的信件",
            "排除異常資料",
            "不含麩質的麵包",
            "之前的會議紀錄",
        ):
            with self.subTest(title=title):
                for question in (None, "要改成明天嗎？"):
                    result = self.parse(f"提醒我明天下午三點{title}", parsed(question=question))
                    self.assertEqual(title, result.title)
                    self.assertEqual((), result.missing_fields)

    def test_unknown_and_conditional_questions_are_not_cleared(self):
        for title, question in (
            ("有空才交報告", "有空是什麼時間？"),
            ("等收到帳單再繳費", "收到帳單的具體時間？"),
            ("收衣服（如果下雨）", "是否只在下雨時提醒？"),
            ("寄出報表", "請確認尚未釐清的限制。"),
            ("前交報表", "這是截止時間嗎？"),
        ):
            with self.subTest(title=title):
                result = self.parse(f"提醒我明天下午三點{title}", parsed(question=question))
                self.assertIn("clarification", result.missing_fields)
                self.assertEqual(question, result.raw["question"])

    def test_calendar_definition_questions_require_source_condition(self):
        for calendar in ("工作日", "平日", "假日", "週末"):
            question = f"「{calendar}」是指週一至週五嗎？"
            with self.subTest(calendar=calendar):
                result = self.parse(
                    f"提醒我明天下午三點買{calendar}用的筆記本", parsed(question=question)
                )
                self.assertEqual((), result.missing_fields)
                result = self.parse(
                    f"提醒我明天下午三點只在{calendar}喝水", parsed(question=question)
                )
                self.assertIn("clarification", result.missing_fields)
                self.assertEqual(question, result.raw["question"])

    def test_calendar_definition_cannot_clear_deadline_question(self):
        question = "「前」是指週一之前嗎？這是截止時間嗎？"
        result = self.parse("提醒我明天下午三點前交報表", parsed(question=question))
        self.assertIn("clarification", result.missing_fields)
        self.assertEqual(question, result.raw["question"])

    def test_pending_title_does_not_consume_time_corrections(self):
        context = ReminderDraft(
            id="correction",
            chat_id=100,
            chat_type="private",
            creator_user_id=7,
            timezone="Asia/Taipei",
            source_text="提醒我明天下午三點",
            title=None,
            remind_at=NOW.replace(day=23, hour=15),
            participants=[PERSON],
            missing_fields=["title"],
            parse_result={},
        )
        for text, expected in (
            ("改成明天九點", NOW.replace(day=23, hour=9)),
            ("時間改到11/17 23:55", NOW.replace(month=11, day=17, hour=23, minute=55)),
            ("請把時間調整為後天18:30", NOW.replace(day=24, hour=18, minute=30)),
            ("延後到後天18:30", NOW.replace(day=24, hour=18, minute=30)),
        ):
            with self.subTest(text=text):
                result = self.parse(text, parsed(title="模型亂寫"), context=context)
                self.assertIsNone(result.title)
                self.assertIn("title", result.missing_fields)
                self.assertEqual(expected, result.remind_at)
        for title in (
            "修改年度報表",
            "更換印表機墨水",
            "下班後要用的報表",
            "月底前要交的資料",
            "工作日行事曆",
            "提早寄出報表",
            "延後寄包裹",
            "調整年度預算",
            "不對稱花瓶",
        ):
            with self.subTest(title=title):
                result = self.parse(title, parsed(title="模型亂寫"), context=context)
                self.assertEqual(title, result.title)
                self.assertEqual(context.remind_at, result.remind_at)
                self.assertEqual((), result.missing_fields)
        context.missing_fields.append("clarification")
        context.parse_result = {"question": "是否只限工作日？"}
        for text in (
            "不是明天，是後天，時間一樣",
            "改成下班後",
            "時間改成有空時",
            "改成每天下午三點",
            "下班後",
            "月底前",
            "有空時",
            "週末",
            "延後半小時",
            "提早30分鐘",
            "不對，是明天",
        ):
            with self.subTest(text=text):
                result = self.parse(text, parsed(title="模型亂寫"), context=context)
                self.assertIsNone(result.title)
                self.assertIn("time", result.missing_fields)
                self.assertIn("title", result.missing_fields)
                self.assertIn("clarification", result.missing_fields)
                self.assertEqual("是否只限工作日？", result.raw["condition_question"])
        result = self.parse("改成明天九點", parsed(title="模型亂寫"), context=context)
        self.assertIsNone(result.title)
        self.assertIn("clarification", result.missing_fields)
        self.assertEqual("是否只限工作日？", result.raw["question"])

    def test_explicit_waiting_and_nested_conditions_cannot_be_omitted(self):
        for title in ("有空才交報告", "等收到帳單再繳費", "收衣服（如果下雨）"):
            with self.subTest(title=title):
                result = self.parse(f"提醒我明天下午三點{title}", parsed(question=None))
                self.assertIn("clarification", result.missing_fields)

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

    def test_unknown_question_survives_invalid_time_and_followup(self):
        source = "提醒我明天25:00寄出報表"
        question = "請確認尚未釐清的限制。"
        first = self.parse(source, parsed(question=question))
        self.assertIn("time", first.missing_fields)
        self.assertIn("clarification", first.missing_fields)
        context = ReminderDraft(
            id="unknown",
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
        result = self.parse("23:55", parsed(intent="update", question=None), context=context)
        self.assertIn("clarification", result.missing_fields)
        self.assertEqual(question, result.raw["question"])

    def test_resolved_title_question_does_not_become_pending_condition(self):
        for question in ("請問要提醒您什麼事？", "這是什麼事項？"):
            with self.subTest(question=question):
                result = self.parse("提醒我明天下午三點寄出報表", parsed(question=question))
                self.assertEqual((), result.missing_fields)
                self.assertIsNone(result.raw["question"])

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

    def test_title_only_followup_preserves_literal_answer_and_context(self):
        fixture = Path(__file__).parent / "fixtures" / "gemini_followup_regressions.jsonl"
        for line in fixture.read_text().splitlines():
            row = json.loads(line)
            now = datetime.fromisoformat(row["now"])
            gateway = Mock()
            parser = SemanticParser(gateway)
            gateway.generate.return_value = GeneratedResponse(
                row["context"]["response"], "test", "recorded"
            )
            first = parser.parse(row["context"]["text"], 7, now, [PERSON])
            context = ReminderDraft(
                id="missing-title",
                chat_id=100,
                chat_type="private",
                creator_user_id=7,
                timezone=row["timezone"],
                source_text=row["context"]["text"],
                title=first.title,
                remind_at=first.remind_at,
                participants=[PERSON],
                missing_fields=list(first.missing_fields),
                parse_result=first.raw,
            )
            for response in (
                row["response"],
                {
                    **row["response"],
                    "intent": "create",
                    "title": None,
                    "date": "2027-01-01",
                    "time": "09:00",
                    "participant_indices": [],
                },
            ):
                with self.subTest(response=response):
                    gateway.generate.return_value = GeneratedResponse(response, "test", "recorded")
                    result = parser.parse(row["text"], 7, now, [PERSON], context=context)
                    self.assertEqual(row["expected"]["title"], result.title)
                    self.assertEqual(first.remind_at, result.remind_at)
                    self.assertEqual((PERSON,), result.participants)
                    self.assertEqual((), result.missing_fields)
                    self.assertEqual("update", result.raw["intent"])
            gateway.generate.return_value = GeneratedResponse(row["response"], "test", "recorded")
            result = parser.parse("有空才交報告", 7, now, [PERSON], context=context)
            self.assertEqual("有空才交報告", result.title)
            self.assertIn("clarification", result.missing_fields)
            result = parser.parse("11/17 23:55", 7, now, [PERSON], context=context)
            self.assertIsNone(result.title)
            self.assertIn("title", result.missing_fields)
            self.assertEqual(
                (11, 17, 23, 55),
                (
                    result.remind_at.month,
                    result.remind_at.day,
                    result.remind_at.hour,
                    result.remind_at.minute,
                ),
            )
            context.missing_fields = ["time", "title"]
            context.remind_at = first.remind_at.replace(hour=0, minute=0)
            result = parser.parse(row["text"], 7, now, [PERSON], context=context)
            self.assertEqual(row["expected"]["title"], result.title)
            self.assertEqual(context.remind_at, result.remind_at)
            self.assertIn("time", result.missing_fields)
            context.remind_at = now.replace(hour=0, minute=0, second=0, microsecond=0)
            result = parser.parse(row["text"], 7, now, [PERSON], context=context)
            self.assertEqual(context.remind_at, result.remind_at)
            self.assertIn("time", result.missing_fields)
            self.assertNotIn("過去", result.raw["question"])
            context.remind_at = None
            result = parser.parse(row["text"], 7, now, [PERSON], context=context)
            self.assertEqual(row["expected"]["title"], result.title)
            self.assertIsNone(result.remind_at)
            self.assertIn("time", result.missing_fields)

    def test_negated_time_cannot_be_used_as_replacement(self):
        for title in (None, "寄報表"):
            for condition in (False, True):
                context = ReminderDraft(
                    id="negated-time",
                    chat_id=100,
                    chat_type="private",
                    creator_user_id=7,
                    timezone="Asia/Taipei",
                    source_text="提醒我明天15:00",
                    title=title,
                    remind_at=NOW.replace(day=23, hour=15),
                    participants=[PERSON],
                    missing_fields=(["title"] if title is None else [])
                    + (["clarification"] if condition else []),
                    parse_result={"question": "是否只限工作日？"} if condition else {},
                )
                for text in ("不是明天", "不是11/17 23:55", "請把時間不是明天九點"):
                    for intent in ("create", "update", "not_reminder"):
                        with self.subTest(
                            title=title, condition=condition, text=text, intent=intent
                        ):
                            result = self.parse(
                                text, parsed(intent=intent, title="模型改寫"), context=context
                            )
                            self.assertEqual(title, result.title)
                            self.assertIsNone(result.remind_at)
                            self.assertIn("time", result.missing_fields)
                            self.assertEqual("update", result.raw["intent"])
                            if condition:
                                self.assertIn("clarification", result.missing_fields)
                                self.assertEqual(
                                    "是否只限工作日？", result.raw["condition_question"]
                                )

    def test_recorded_corrections_preserve_fields_questions_and_confirmation(self):
        fixture = Path(__file__).parent / "fixtures" / "gemini_correction_regressions.jsonl"
        for line in fixture.read_text().splitlines():
            row = json.loads(line)
            with self.subTest(case=row["id"]), tempfile.TemporaryDirectory() as directory:
                repo = ReminderRepository(Path(directory) / "recorded.db")
                repo.migrate()
                generator = Mock()
                generator.enabled.return_value = True
                generator.generate.return_value = GeneratedResponse(
                    row["response"], "gemini", "recorded"
                )
                service = ReminderService(
                    repository=repo,
                    parser=ReminderParser("Asia/Taipei"),
                    draft_store=SqliteDraftStore(10, repo),
                    edit_store=SqliteEditSessionStore(10, repo),
                    default_timezone="Asia/Taipei",
                    ai_parser=SemanticParser(generator),
                )
                now = datetime.fromisoformat(row["now"]).astimezone(ZoneInfo(row["timezone"]))
                seed = row["seed"]
                context = None
                if seed:
                    context = ReminderDraft(
                        id="seed",
                        chat_id=100,
                        chat_type="private",
                        creator_user_id=7,
                        timezone=row["timezone"],
                        source_text=seed["source"],
                        title=seed["title"],
                        remind_at=datetime.fromisoformat(seed["at"]),
                        participants=[PERSON],
                        missing_fields=seed["missing"]
                        + (["clarification"] if seed["condition"] else []),
                        parse_result={
                            "grain": "ai",
                            "question": seed["question"],
                            "condition_question": seed["question"],
                        },
                    )
                    service._draft_store.save(context, now)
                message = TelegramMessage(
                    id=1,
                    chat=TelegramChat(id=100, type="private"),
                    from_user=TelegramUser(id=7, first_name="HeiTang"),
                    text=row["text"],
                    entities=(),
                )
                if row.get("rejected"):
                    with self.assertRaises(AiUnavailable):
                        service.parse_ai(message.text, message, now, context=context)
                    self.assertEqual(context, service.peek_draft(100, 7, now))
                    self.assertEqual([], repo.list_pending(100))
                    continue
                result = service.parse_ai(message.text, message, now, context=context)
                expected = row["expected"]
                self.assertEqual(expected["title"], result.title)
                self.assertEqual(
                    datetime.fromisoformat(expected["at"]) if expected["at"] else None,
                    result.remind_at,
                )
                self.assertTrue(set(expected["missing"]).issubset(result.missing_fields))
                if seed and seed["condition"]:
                    self.assertEqual(seed["question"], result.raw["condition_question"])
                preview = service.begin_create(
                    context.source_text if context else message.text,
                    message,
                    now,
                    parse_result=result,
                )
                self.assertEqual([], repo.list_pending(100))
                self.assertIsInstance(
                    preview, Confirmation if expected["complete"] else DraftPrompt
                )
                confirmed = service.confirm(preview.draft.id, 7, now)
                if expected["complete"]:
                    self.assertIsNotNone(confirmed)
                    self.assertEqual(expected["title"], confirmed.reminder.title)
                else:
                    self.assertIsNone(confirmed)
                    self.assertEqual([], repo.list_pending(100))

    def test_boundary_actions_and_compound_titles_are_distinct(self):
        for title in ("前交報表", "前完成繳費", "後再領藥", "後寄包裹"):
            with self.subTest(title=title):
                result = self.parse(f"提醒我明天下午三點{title}", parsed(question=None))
                self.assertIn("clarification", result.missing_fields)
        for title in ("前往車站", "前進營地", "前置作業", "後續報表", "後製影片"):
            with self.subTest(title=title):
                result = self.parse(f"提醒我明天下午三點{title}", parsed(question=None))
                self.assertEqual(title, result.title)
                self.assertEqual((), result.missing_fields)

    def test_known_title_correction_uses_native_time_and_retains_question(self):
        context = ReminderDraft(
            id="known-title",
            chat_id=100,
            chat_type="private",
            creator_user_id=7,
            timezone="Asia/Taipei",
            source_text="只在工作日提醒我寄報表",
            title="寄報表",
            remind_at=NOW.replace(day=23, hour=15),
            participants=[PERSON],
            missing_fields=["clarification"],
            parse_result={"question": "是否只限週一至週五？"},
        )
        result = self.parse(
            "改成明天九點",
            parsed(intent="update", title="模型誤改", date="2027-01-01", time="18:00"),
            context=context,
        )
        self.assertEqual("寄報表", result.title)
        self.assertEqual(NOW.replace(day=23, hour=9), result.remind_at)
        self.assertEqual("是否只限週一至週五？", result.raw["question"])
        self.assertIn("clarification", result.missing_fields)
        for intent in ("update", "create", "not_reminder"):
            with self.subTest(semantic_intent=intent):
                result = self.parse(
                    "不是明天，是後天，時間一樣",
                    parsed(intent=intent, title="寄報表", question=None),
                    context=context,
                )
                self.assertEqual("update", result.raw["intent"])
                self.assertEqual("是否只限週一至週五？", result.raw["condition_question"])
                self.assertIn("clarification", result.missing_fields)
        for text in ("下班後", "改成有空時"):
            with self.subTest(text=text):
                result = self.parse(
                    text, parsed(intent="update", title="模型誤改"), context=context
                )
                self.assertEqual("寄報表", result.title)
                self.assertIsNone(result.remind_at)
                self.assertIn("time", result.missing_fields)
                self.assertIn("clarification", result.missing_fields)

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
