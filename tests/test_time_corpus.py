from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from remindly.reminders.parser import ReminderParser
from remindly.reminders.recurrence import serialize_rule
from remindly.reminders.service import (
    Confirmation,
    DraftPrompt,
    EditPrompt,
    EditResult,
    ReminderService,
)
from remindly.storage.session_stores import SqliteDraftStore, SqliteEditSessionStore
from remindly.storage.sqlite import ReminderRepository
from remindly.telegram.models import TelegramChat, TelegramMessage, TelegramUser

CORPUS = Path(__file__).parent / "fixtures" / "time_regressions.jsonl"


class TimeRegressionCorpusTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = [json.loads(line) for line in CORPUS.read_text().splitlines() if line.strip()]

    def setUp(self) -> None:
        self.parser = ReminderParser("Asia/Taipei")
        self.message = TelegramMessage(
            id=1,
            chat=TelegramChat(id=100, type="private"),
            from_user=TelegramUser(id=7, first_name="Orange", username="orange"),
            text="",
            entities=(),
        )

    def _reference(self, case: dict) -> datetime:
        return datetime.fromisoformat(case["now"]).astimezone(ZoneInfo(case["timezone"]))

    def _assert_time(self, expected: str | None, actual: datetime | None) -> None:
        self.assertEqual(datetime.fromisoformat(expected) if expected is not None else None, actual)

    def _service(
        self, case: dict, directory: str
    ) -> tuple[ReminderService, ReminderRepository, SqliteDraftStore]:
        repository = ReminderRepository(Path(directory) / "corpus.db")
        repository.migrate()
        repository.set_user_timezone(7, case["timezone"])
        drafts = SqliteDraftStore(ttl_minutes=10, repository=repository)
        service = ReminderService(
            repository=repository,
            parser=self.parser,
            draft_store=drafts,
            edit_store=SqliteEditSessionStore(ttl_minutes=10, repository=repository),
            default_timezone="Asia/Taipei",
        )
        return service, repository, drafts

    def test_corpus_structure(self) -> None:
        self.assertGreaterEqual(len(self.cases), 107, "既有 107 筆回歸案例不可縮減")
        ids = [case["id"] for case in self.cases]
        self.assertEqual(len(ids), len(set(ids)), "案例 id 不可重複")
        required = CORPUS.with_suffix(".required_ids.txt").read_text().splitlines()
        self.assertEqual(107, len(set(required)))
        self.assertFalse(set(required) - set(ids), "原本的回歸案例不可被其他案例取代")
        fields = {
            "time": {"text", "at", "missing_time", "past", "existing_date", "cross_entry_points"},
            "reminder": {"text", "title", "at", "missing", "recurrence", "error", "participants"},
            "conversation": {"turns"},
            "edit": {"initial", "text", "state", "at", "title"},
        }
        self.assertEqual(set(fields), {case["kind"] for case in self.cases})
        for case in self.cases:
            with self.subTest(case=case["id"]):
                self.assertIn(case["kind"], fields)
                self.assertEqual(
                    {"kind", "id", "now", "timezone"} | fields[case["kind"]], set(case)
                )
                self.assertIsNotNone(datetime.fromisoformat(case["now"]).tzinfo)
                self._reference(case)

    def test_time_formats_across_entry_points(self) -> None:
        for case in self.cases:
            if case["kind"] != "time":
                continue
            now = self._reference(case)
            existing = case["existing_date"]
            with self.subTest(case=case["id"], path="time_only"):
                parsed = self.parser.parse_time_only(
                    case["text"], now, datetime.fromisoformat(existing) if existing else None
                )
                self._assert_time(case["at"], parsed.remind_at)
                self.assertEqual(case["missing_time"], parsed.missing_time)
                self.assertEqual(case["past"], parsed.is_past)
                if parsed.remind_at is not None:
                    self.assertEqual(ZoneInfo(case["timezone"]), parsed.remind_at.tzinfo)
            if not case["cross_entry_points"]:
                continue
            for source in (
                f"{case['text']}提醒我買湯",
                f"提醒我在{case['text']}買湯",
                f"{case['text']}買湯",
            ):
                with self.subTest(case=case["id"], source=source):
                    parsed = self.parser.parse(source, self.message, now)
                    self._assert_time(case["at"], parsed.remind_at)
                    missing = case["at"] is None or case["missing_time"] or case["past"]
                    self.assertEqual(["time"] if missing else [], list(parsed.missing_fields))
                    if case["at"] is not None:
                        self.assertEqual("買湯", parsed.title)

    def test_reminder_content_and_recurrence(self) -> None:
        for case in self.cases:
            if case["kind"] != "reminder":
                continue
            with self.subTest(case=case["id"]):
                parsed = self.parser.parse(case["text"], self.message, self._reference(case))
                self._assert_time(case["at"], parsed.remind_at)
                self.assertEqual(case["title"], parsed.title)
                self.assertEqual(case["missing"], list(parsed.missing_fields))
                self.assertEqual(case["error"], parsed.recurrence_error is not None)
                self.assertEqual(
                    case["recurrence"],
                    json.loads(serialize_rule(parsed.recurrence)) if parsed.recurrence else None,
                )
                self.assertEqual(
                    case["participants"], [p.display_name for p in parsed.participants]
                )

    def test_conversations_persist_each_answer(self) -> None:
        for case in self.cases:
            if case["kind"] != "conversation":
                continue
            with self.subTest(case=case["id"]), tempfile.TemporaryDirectory() as directory:
                service, repository, drafts = self._service(case, directory)
                now = self._reference(case)
                for index, turn in enumerate(case["turns"]):
                    with self.subTest(turn=index, text=turn["text"]):
                        message = replace(self.message, id=index + 1, text=turn["text"])
                        result = (
                            service.begin_create(message.text, message, now)
                            if index == 0
                            else service.continue_draft(message, now)
                        )
                        self.assertIn(turn["state"], ("prompt", "confirmation"))
                        self.assertIsInstance(
                            result, DraftPrompt if turn["state"] == "prompt" else Confirmation
                        )
                        self.assertEqual(turn["question"], getattr(result, "question", None))
                        saved = drafts.get_by_id(result.draft.id, now)
                        self.assertIsNotNone(saved)
                        self.assertEqual(turn["title"], saved.title)
                        self._assert_time(turn["at"], saved.remind_at)
                        self.assertEqual(turn["missing"], saved.missing_fields)
                        self.assertEqual([], repository.list_pending(100))
                self.assertIsInstance(result, Confirmation)
                created = service.confirm(result.draft.id, 7, now)
                self.assertIsNotNone(created)
                saved = repository.get_by_short_id(100, created.reminder.short_id)
                self._assert_time(turn["at"], saved.remind_at)
                self.assertEqual(turn["title"], saved.title)
                self.assertIsNone(drafts.get_by_id(result.draft.id, now))

    def test_edits_update_or_preserve_stored_reminders(self) -> None:
        for case in self.cases:
            if case["kind"] != "edit":
                continue
            with self.subTest(case=case["id"]), tempfile.TemporaryDirectory() as directory:
                service, repository, _ = self._service(case, directory)
                now = self._reference(case)
                draft = service.begin_create(case["initial"], self.message, now)
                self.assertIsInstance(draft, Confirmation)
                created = service.confirm(draft.draft.id, 7, now).reminder
                self.assertIsNotNone(service.begin_edit(100, created.short_id, 7, "time", now))
                result = service.continue_edit(replace(self.message, text=case["text"]), now)
                self.assertIn(case["state"], ("updated", "prompt"))
                self.assertIsInstance(
                    result, EditResult if case["state"] == "updated" else EditPrompt
                )
                saved = repository.get_by_short_id(100, created.short_id)
                self._assert_time(case["at"], saved.remind_at)
                self.assertEqual(case["title"], saved.title)
                if case["state"] == "prompt":
                    self.assertEqual(created, saved)
