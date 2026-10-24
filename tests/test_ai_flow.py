from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from test_ai import NOW, SECOND, SECRET, ScriptedProvider, parsed, rule
from test_bot_router import CHAT, GROUP_CHAT, USER, FakeTelegramClient

from remindly.ai.contracts import ProviderError
from remindly.ai.gateway import AiGateway
from remindly.ai.keys import ApiKeyStore
from remindly.ai.parser import SemanticParser
from remindly.bot.ai_commands import AiCommands
from remindly.bot.router import BotRouter
from remindly.reminders.parser import ReminderParser
from remindly.reminders.renderer import ReminderRenderer
from remindly.reminders.service import ReminderService
from remindly.storage.session_stores import SqliteDraftStore, SqliteEditSessionStore
from remindly.storage.sqlite import ReminderRepository
from remindly.telegram.client import TelegramApiError
from remindly.telegram.models import (
    TelegramCallbackQuery,
    TelegramMessage,
    TelegramUpdate,
    TelegramUser,
)


class AiFlowTest(unittest.TestCase):
    def test_pending_title_time_correction_then_literal_answer(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.extend(
            [
                parsed(title=None),
                parsed(intent="update", title="模型誤填事項"),
                parsed(intent="update", title="匄出報表", date="2027-01-01"),
            ]
        )
        self.send("提醒我明天下午三點")
        self.send("時間改到11/17 23:55")
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.assertIsNone(draft.title)
        self.assertEqual("要提醒什麼？", self.telegram.messages[-1].text)
        self.assertIsNone(self.service.confirm(draft.id, USER.id, NOW))
        self.send("寄出報表")
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.assertTrue(draft.is_complete)
        self.confirm(draft.id)
        saved = self.repo.list_pending(CHAT.id)[0]
        self.assertEqual("寄出報表", saved.title)
        self.assertEqual(
            (2026, 11, 17, 23, 55),
            (
                saved.remind_at.year,
                saved.remind_at.month,
                saved.remind_at.day,
                saved.remind_at.hour,
                saved.remind_at.minute,
            ),
        )

    def test_recorded_garbled_question_is_not_displayed(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "gemini_question_regressions.jsonl"
        row = json.loads(fixture.read_text())
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.append(row["response"])
        self.send(row["text"])
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.assertEqual(row["response"]["question"], draft.parse_result["question"])
        self.assertEqual(row["expected_question"], self.telegram.messages[-1].text)
        self.assertIn("title", draft.missing_fields)
        self.assertIsNone(self.service.confirm(draft.id, USER.id, NOW))
        self.gemini.responses.append(parsed(intent="update", title="寄出報表"))
        self.send("寄出報表")
        corrected = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.assertTrue(corrected.is_complete)
        self.assertEqual("寄出報表", corrected.title)
        self.assertNotIn(row["response"]["question"], self.telegram.messages[-1].text)

    def test_garbled_question_cannot_leak_from_not_reminder_or_edit(self) -> None:
        garbage = "\ufffd\u0001職耇與䐂蕩耇䐂略耇䐂耇䐂"
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.extend(
            [
                parsed(question=garbage),
                parsed(intent="not_reminder", question=garbage),
                parsed(),
                parsed(intent="update", date=None, time=None, question=garbage),
            ]
        )
        self.send("提醒我明天下午三點有空才交報告")
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.assertIn("提醒資訊未釐清", self.telegram.messages[-1].text)
        self.assertNotIn(garbage, self.telegram.messages[-1].text)
        self.send("還沒確定")
        self.assertEqual(draft.id, self.service.peek_draft(CHAT.id, USER.id, NOW).id)
        self.assertNotIn(garbage, self.telegram.messages[-1].text)
        self.send("提醒我明天下午三點寄報表")
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.confirm(draft.id)
        reminder = self.repo.list_pending(CHAT.id)[0]
        self.service.begin_edit(CHAT.id, reminder.short_id, USER.id, "time", NOW)
        self.send("等有空時")
        self.assertNotIn(garbage, self.telegram.messages[-1].text)
        self.assertIn("未來日期與時間", self.telegram.messages[-1].text)
        self.assertEqual(reminder.remind_at, self.repo.list_pending(CHAT.id)[0].remind_at)
        self.assertIsNotNone(self.service.peek_edit_session(CHAT.id, USER.id, NOW))

    def test_missing_participant_uses_local_question_and_blocks_confirmation(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.append(parsed(participant_indices=[], question="\ufffd\u0001"))
        self.send("提醒小明明天下午三點交報告")
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.assertIn("participants", draft.missing_fields)
        self.assertEqual("要提醒誰？請提供 @username。", self.telegram.messages[-1].text)
        self.assertIsNone(self.service.confirm(draft.id, USER.id, NOW))

    def test_quick_time_keeps_condition_and_does_not_display_model_question(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.append(parsed(time=None, question="\ufffd\u0001"))
        self.send("提醒我明天只在工作日喝水")
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        result = self.service.apply_quick_time(draft.id, "23:55", USER.id, NOW)
        self.assertIn("clarification", result.draft.missing_fields)
        self.assertIn("提醒資訊未釐清", result.question)
        self.assertNotIn("\ufffd", result.question)
        self.assertIsNone(self.service.confirm(draft.id, USER.id, NOW))

    def test_edit_condition_survives_date_clock_and_store_reload(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.extend(
            [
                parsed(),
                parsed(intent="update", question="是否只限工作日？"),
                parsed(intent="update"),
                parsed(intent="update"),
            ]
        )
        self.send("明天下午三點提醒我交報告")
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.confirm(draft.id)
        original = self.repo.list_pending(CHAT.id)[0]
        self.service.begin_edit(CHAT.id, original.short_id, USER.id, "time", NOW)
        self.send("改成工作日")
        self.send("11/17")
        self.service._edit_store = SqliteEditSessionStore(10, self.repo)
        self.send("23:55")
        self.assertIn("提醒資訊未釐清", self.telegram.messages[-1].text)
        self.assertEqual(original.remind_at, self.repo.list_pending(CHAT.id)[0].remind_at)
        self.assertIsNotNone(self.service.peek_edit_session(CHAT.id, USER.id, NOW))

    def test_date_and_clock_do_not_confirm_unresolved_condition(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.extend(
            [
                parsed(question="是否只限工作日？"),
                parsed(intent="update", question=None),
                parsed(intent="update", question=None),
            ]
        )
        for text in ("提醒我工作日喝水", "11/17", "23:55"):
            self.send(text)
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.assertIn("clarification", draft.missing_fields)
        self.assertIn("提醒資訊未釐清", self.telegram.messages[-1].text)
        self.assertIsNone(self.service.confirm(draft.id, USER.id, NOW))
        self.assertEqual([], self.repo.list_pending(CHAT.id))

    def test_unrequested_recurrence_cannot_be_confirmed(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.append(parsed(recurrence=rule()))
        self.send("提醒我明天下午三點買湯")
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.assertIsNone(draft.recurrence)
        self.assertIn("clarification", draft.missing_fields)
        self.assertIsNone(self.service.confirm(draft.id, USER.id, NOW))
        self.assertEqual([], self.repo.list_pending(CHAT.id))

    def test_wrong_model_dates_cannot_change_followup_date(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.extend(
            [
                parsed(title="要領蝦幣", date=None, time=None, question="什麼時候提醒？"),
                parsed(intent="update", date="2026-09-17", time=None, question="那天幾點？"),
                parsed(intent="create", date="2026-10-22", time="23:55", title="其他事項"),
            ]
        )
        for text in ("提醒我要領蝦幣", "09-17", "23:55"):
            self.send(text)
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.assertEqual(datetime(2027, 9, 17, 23, 55, tzinfo=NOW.tzinfo), draft.remind_at)
        self.assertEqual("要領蝦幣", draft.title)
        self.assertTrue(draft.is_complete)
        self.assertEqual([], self.repo.list_pending(CHAT.id))

    def test_invalid_reminder_is_not_silently_dropped(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.append(
            parsed(
                intent="not_reminder",
                title=None,
                date=None,
                time=None,
                question="九月沒有31日",
                participant_indices=[],
            )
        )
        self.send("提醒我09-31 23:55買湯")
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.assertIsNotNone(draft)
        self.assertFalse(draft.is_complete)
        self.assertIn("未來日期與時間", self.telegram.messages[-1].text)
        self.assertEqual([], self.repo.list_pending(CHAT.id))

    def test_explicit_new_reminder_keeps_its_source(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.extend(
            [
                parsed(title="舊事項", date=None, time=None, question="什麼時候？"),
                parsed(title="買湯", intent="create"),
            ]
        )
        self.send("提醒我舊事項")
        self.send("提醒我明天下午三點買湯")
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.assertEqual("提醒我明天下午三點買湯", draft.source_text)
        self.assertEqual("買湯", draft.title)
        self.assertEqual("create", draft.parse_result["intent"])

    def test_not_reminder_question_is_shown_without_replacing_draft(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.append(parsed(time=None, question="幾點？"))
        self.send("提醒我下班後買湯")
        original = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.gemini.responses.append(
            parsed(
                intent="not_reminder",
                date=None,
                time=None,
                question="請提供明確的時間。",
            )
        )
        self.send("下班的時候")
        self.assertIn("未來日期與時間", self.telegram.messages[-1].text)
        self.assertEqual(original.id, self.service.peek_draft(CHAT.id, USER.id, NOW).id)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = ReminderRepository(Path(self.tmp.name) / "test.db")
        self.repo.migrate()
        self.keys = ApiKeyStore(self.repo, Fernet.generate_key().decode())
        self.gemini = ScriptedProvider([])
        self.telegram = FakeTelegramClient()
        self.gateway = AiGateway(self.keys, {"gemini": self.gemini}, default_provider="gemini")
        self.service = ReminderService(
            repository=self.repo,
            parser=ReminderParser("Asia/Taipei"),
            draft_store=SqliteDraftStore(10, self.repo),
            edit_store=SqliteEditSessionStore(10, self.repo),
            default_timezone="Asia/Taipei",
            ai_parser=SemanticParser(self.gateway),
        )
        self.router = BotRouter(
            self.telegram,
            self.service,
            ReminderRenderer(),
            bot_username="ReminderBot",
            default_timezone="Asia/Taipei",
            ai_commands=AiCommands(self.telegram, self.gateway, "ReminderBot"),
        )
        self.sequence = 0

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def send(self, text, *, chat=CHAT, user=USER):
        self.sequence += 1
        message = TelegramMessage(
            id=self.sequence,
            chat=chat,
            from_user=user,
            text=text,
            entities=(),
        )
        with patch("remindly.bot.router.datetime") as clock:
            clock.now.return_value = NOW
            self.router.handle_update(TelegramUpdate(id=self.sequence, message=message))

    def confirm(self, draft_id, *, user=USER):
        from remindly.reminders.callback_data import reminder_callback

        self.sequence += 1
        callback = TelegramCallbackQuery(
            id=str(self.sequence),
            from_user=user,
            data=reminder_callback("confirm", draft_id),
            message=TelegramMessage(id=1000, chat=CHAT, from_user=USER, text="確認", entities=()),
        )
        with patch("remindly.bot.router.datetime") as clock:
            clock.now.return_value = NOW
            self.router.handle_update(TelegramUpdate(id=self.sequence, callback_query=callback))

    def test_private_settings_add_order_delete_and_do_not_echo_key(self) -> None:
        self.send("/ai")
        self.send(f"/ai add {SECRET}")
        self.send(f"/ai add {SECOND}")
        first, second = self.keys.list(USER.id)
        self.send(f"/ai move {second.id} 1")
        self.assertEqual(second.id, self.keys.list(USER.id)[0].id)
        self.send("/ai off")
        self.assertFalse(self.keys.enabled(USER.id))
        self.send("/ai on")
        self.assertTrue(self.keys.enabled(USER.id))
        self.send(f"/ai delete {first.id}")
        self.assertEqual([second.id], [k.id for k in self.keys.list(USER.id)])
        self.assertIn(2, self.telegram.deleted_messages)
        self.assertIn(3, self.telegram.deleted_messages)
        self.assertNotIn(SECRET, repr(self.telegram.messages))
        self.assertNotIn(SECOND, repr(self.telegram.messages))
        self.assertEqual([], self.gemini.calls)

    def test_group_key_setup_is_rejected_and_secret_message_deleted(self) -> None:
        self.send(f"/ai add {SECRET}", chat=GROUP_CHAT)
        self.assertEqual([], self.keys.list(USER.id))
        self.assertIn(1, self.telegram.deleted_messages)
        self.assertIn("只能", self.telegram.messages[-1].text)

    def test_explicit_provider_setup_and_unregistered_provider_are_safe(self) -> None:
        self.send(f"/ai add gemini {SECRET}")
        self.assertEqual("gemini", self.keys.list(USER.id)[0].provider)
        self.send(f"/ai add unregistered {SECOND}")
        self.assertEqual(1, len(self.keys.list(USER.id)))
        self.assertNotIn(SECOND, repr(self.telegram.messages))
        self.assertIn("尚未啟用", self.telegram.messages[-1].text)

    def test_registered_second_provider_works_through_unchanged_bot_confirmation(self) -> None:
        other = ScriptedProvider([parsed(title="拿包裹")], provider_id="other")
        self.gateway.providers["other"] = other
        self.send(f"/ai add other {SECOND}")
        self.assertIn("other", self.telegram.messages[-1].text)
        self.send("明天下午三點叫我去拿包裹")
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.assertEqual("other", draft.parse_result["provider"])
        self.assertEqual([], self.repo.list_pending(CHAT.id))
        self.assertIn("<code>使用 Test other 解析</code>", self.telegram.messages[-1].text)
        self.confirm(draft.id)
        self.assertEqual("拿包裹", self.repo.list_pending(CHAT.id)[0].title)
        self.assertEqual(SECOND, other.calls[0][0])
        self.assertEqual([], self.gemini.calls)

    def test_failed_key_message_deletion_is_reported_without_echoing(self) -> None:
        with patch.object(
            self.telegram, "delete_message", side_effect=TelegramApiError("deleteMessage", "no")
        ):
            self.send(f"/ai add {SECRET}")
        self.assertIn("手動刪除", self.telegram.messages[-1].text)
        self.assertNotIn(SECRET, self.telegram.messages[-1].text)

    def test_ai_without_keyword_only_creates_after_confirmation(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.append(parsed(title="拿包裹", time="20:00"))
        self.send("明晚八點叫我去拿包裹")
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.assertIsNotNone(draft)
        self.assertEqual([], self.repo.list_pending(CHAT.id))
        self.assertIn("確認建立提醒", self.telegram.messages[-1].text)
        self.confirm(draft.id, user=TelegramUser(id=99, first_name="Other"))
        self.assertEqual([], self.repo.list_pending(CHAT.id))
        self.confirm(draft.id)
        self.assertEqual("拿包裹", self.repo.list_pending(CHAT.id)[0].title)

    def test_followup_replaces_confirmation_and_old_button_cannot_create(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.extend(
            [
                parsed(),
                parsed(intent="update", date="2026-10-24"),
            ]
        )
        self.send("明天下午三點提醒我後天要交報告")
        old = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.send("不是明天，是後天，時間一樣")
        new = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.assertNotEqual(old.id, new.id)
        self.assertEqual(24, new.remind_at.day)
        self.assertEqual("後天要交報告", new.title)
        context = self.gemini.calls[-1][1]["context"]
        self.assertEqual(old.title, context["title"])
        self.assertEqual(old.source_text, new.source_text)
        self.assertEqual(old.source_text, context["source_text"])
        self.assertEqual([0], context["participant_indices"])
        self.confirm(old.id)
        self.assertEqual([], self.repo.list_pending(CHAT.id))
        self.confirm(new.id)
        self.assertEqual(24, self.repo.list_pending(CHAT.id)[0].remind_at.day)

    def test_ambiguity_followup_and_recurrence_survive_persistence(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.extend(
            [
                parsed(date=None, time=None, question="你幾點下班？"),
                parsed(intent="update", time="18:00", question=None),
            ]
        )
        self.send("下班後提醒我買牛奶")
        self.assertIn("未來日期與時間", self.telegram.messages[-1].text)
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.assertFalse(draft.is_complete)
        self.send("明天十八點")
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.assertTrue(draft.is_complete)
        self.gemini.responses.append(parsed(date=None, time=None, recurrence=rule()))
        self.send("每天九點叫我吃藥")
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.confirm(draft.id)
        self.assertIsNotNone(self.repo.list_pending(CHAT.id)[0].recurrence)

    def test_errors_preserve_draft_and_do_not_silently_use_basic_parser(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.extend([parsed(), ProviderError("temporary")])
        self.send("明天下午叫我交報告")
        old = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.send("改成後天")
        self.assertEqual(old.id, self.service.peek_draft(CHAT.id, USER.id, NOW).id)
        self.assertEqual([], self.repo.list_pending(CHAT.id))
        self.assertIn("/ai off", self.telegram.messages[-1].text)
        self.assertNotIn(SECRET, repr(self.telegram.messages))

    def test_non_reminder_keeps_draft_and_no_key_keeps_existing_parser(self) -> None:
        self.send("明天下午三點提醒我倒垃圾")
        old = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.assertEqual([], self.gemini.calls)
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.append(parsed(intent="not_reminder"))
        self.send("你好，謝謝")
        self.assertEqual(old.id, self.service.peek_draft(CHAT.id, USER.id, NOW).id)

    def test_explicit_command_uses_ai_once_and_respects_user_timezone(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.service.set_timezone(USER.id, "Asia/Tokyo")
        self.gemini.responses.append(parsed())
        self.send("/remind 明天下午三點叫我交報告")
        self.assertEqual(1, len(self.gemini.calls))
        self.assertEqual("Asia/Tokyo", self.gemini.calls[0][1]["timezone"])
        self.assertEqual("Asia/Tokyo", self.service.peek_draft(CHAT.id, USER.id, NOW).timezone)

    def test_group_trigger_and_keys_are_scoped_to_actor(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.send("明晚八點叫我拿包裹", chat=GROUP_CHAT)
        self.assertEqual([], self.gemini.calls)
        other = TelegramUser(id=8, first_name="Other")
        self.send("@ReminderBot 明天三點提醒我喝水", chat=GROUP_CHAT, user=other)
        self.assertEqual([], self.gemini.calls)
        self.gemini.responses.append(parsed())
        self.send("@ReminderBot 明天三點叫我喝水", chat=GROUP_CHAT)
        self.assertEqual(SECRET, self.gemini.calls[0][0])

    def test_time_edit_passes_context_and_preserves_series(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.append(parsed(date=None, time=None, recurrence=rule()))
        self.send("每天九點叫我吃藥")
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.confirm(draft.id)
        reminder = self.repo.list_pending(CHAT.id)[0]
        self.service.begin_edit(CHAT.id, reminder.short_id, USER.id, "time", NOW)
        self.gemini.responses.append(parsed(intent="update", time="10:00", recurrence=rule()))
        self.send("明天晚一小時，同樣那件事")
        updated = self.repo.list_pending(CHAT.id)[0]
        self.assertEqual(10, updated.remind_at.hour)
        self.assertEqual(9, updated.recurrence.hour)
        self.assertEqual("edit_time", self.gemini.calls[-1][1]["purpose"])

    def test_remind_command_replaces_pending_edit_in_both_modes(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        for enabled in (True, False):
            with self.subTest(ai_enabled=enabled):
                self.keys.set_enabled(USER.id, enabled)
                if enabled:
                    self.gemini.responses.extend([parsed(), parsed(title="拿包裹")])
                self.send("/remind 明天下午三點提醒我交報告")
                first = self.service.peek_draft(CHAT.id, USER.id, NOW)
                original = self.service.confirm(first.id, USER.id, NOW).reminder
                self.service.begin_edit(CHAT.id, original.short_id, USER.id, "time", NOW)
                self.send("/remind 明天下午三點提醒我拿包裹")
                replacement = self.service.peek_draft(CHAT.id, USER.id, NOW)
                self.assertNotEqual(first.id, replacement.id)
                self.assertIn("拿包裹", replacement.title)
                self.assertIsNone(self.service.peek_edit_session(CHAT.id, USER.id, NOW))

    def test_rejected_remind_command_keeps_pending_edit(self) -> None:
        self.send("/remind 明天下午三點提醒我交報告")
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        original = self.service.confirm(draft.id, USER.id, NOW).reminder
        self.service.begin_edit(CHAT.id, original.short_id, USER.id, "time", NOW)
        self.send("/remind 每個月45號09:00提醒我拿包裹")
        self.assertIsNotNone(self.service.peek_edit_session(CHAT.id, USER.id, NOW))
        self.assertIsNone(self.service.peek_draft(CHAT.id, USER.id, NOW))

    def test_confirmation_does_not_create_past_reminder(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.append(parsed())
        self.send("明天下午三點提醒我交報告")
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        draft.remind_at = NOW - timedelta(minutes=1)
        self.service._draft_store.save(draft, NOW)
        self.confirm(draft.id)
        self.assertEqual([], self.repo.list_pending(CHAT.id))

    def test_time_edit_keeps_partial_date_and_question_after_store_reload(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.extend(
            [
                parsed(),
                parsed(intent="update", date="2026-10-24", time=None, question="後天幾點？"),
                parsed(intent="update", date="2026-10-24", time="09:00"),
            ]
        )
        self.send("明天下午三點叫我交報告")
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.confirm(draft.id)
        original = self.repo.list_pending(CHAT.id)[0]
        self.service.begin_edit(CHAT.id, original.short_id, USER.id, "time", NOW)
        self.send("延到後天")
        self.assertEqual(original.remind_at, self.repo.list_pending(CHAT.id)[0].remind_at)
        self.service._edit_store = SqliteEditSessionStore(10, self.repo)
        self.send("早上九點")
        context = self.gemini.calls[-1][1]["context"]
        self.assertEqual("2026-10-24", context["remind_at"][:10])
        self.assertEqual(["time"], context["missing_fields"])
        self.assertEqual("後天幾點？", context["question"])
        updated = self.repo.list_pending(CHAT.id)[0]
        self.assertEqual((24, 9), (updated.remind_at.day, updated.remind_at.hour))
        self.assertIsNone(self.service.peek_edit_session(CHAT.id, USER.id, NOW))

    def test_confirmation_rolls_back_on_draft_or_participant_write_failure(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        for operation in ("delete on reminder_drafts", "insert on reminder_participants"):
            with self.subTest(operation=operation):
                before = len(self.repo.list_pending(CHAT.id))
                self.gemini.responses.append(parsed())
                self.send("明天下午三點叫我交報告")
                draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
                with self.repo.connect() as connection:
                    connection.execute(
                        f"""create trigger reject_write before {operation}
                        begin select raise(abort, 'injected failure'); end"""
                    )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "injected failure"):
                    self.service.confirm(draft.id, USER.id, NOW)
                self.assertEqual(before, len(self.repo.list_pending(CHAT.id)))
                self.assertEqual(draft.id, self.service.peek_draft(CHAT.id, USER.id, NOW).id)
                with self.repo.connect() as connection:
                    connection.execute("drop trigger reject_write")
                result = self.service.confirm(draft.id, USER.id, NOW)
                self.assertIsNotNone(result)
                self.assertEqual(before + 1, len(self.repo.list_pending(CHAT.id)))
                self.assertIsNone(self.service.confirm(draft.id, USER.id, NOW))
                # 另一個請求已讀到的舊草稿也只能消耗一次。
                self.assertFalse(self.service._draft_store.confirm(draft, result.reminder))

    def test_failed_draft_replacement_keeps_previous_confirmation(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.extend([parsed(), parsed(intent="update", time="16:00")])
        for enabled in (True, False):
            with self.subTest(ai_enabled=enabled):
                self.keys.set_enabled(USER.id, enabled)
                self.send("明天下午三點提醒我交報告")
                original = self.service.peek_draft(CHAT.id, USER.id, NOW)
                with self.repo.connect() as connection:
                    connection.execute(
                        """create trigger reject_draft before insert on reminder_drafts
                        begin select raise(abort, 'injected failure'); end"""
                    )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "injected failure"):
                    self.send("明天下午四點提醒我交報告")
                preserved = self.service.peek_draft(CHAT.id, USER.id, NOW)
                self.assertEqual(original.id, preserved.id)
                self.assertEqual(original.remind_at, preserved.remind_at)
                with self.repo.connect() as connection:
                    connection.execute("drop trigger reject_draft")

    def test_no_master_key_preserves_basic_mode(self) -> None:
        self.keys._cipher = None
        self.send("/ai")
        self.assertIn("AI_ENCRYPTION_KEY", self.telegram.messages[-1].text)
        self.send("明天下午三點提醒我倒垃圾")
        self.assertIsNotNone(self.service.peek_draft(CHAT.id, USER.id, NOW))
        self.assertEqual([], self.gemini.calls)

    def test_ai_status_shows_saved_timeout_reason(self) -> None:
        key_id = self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.append(ProviderError("timeout"))
        self.send("明天下午三點提醒我倒垃圾")
        self.assertIn("AI 請求逾時", self.telegram.messages[-1].text)
        self.assertIn(key_id, self.telegram.messages[-1].text)
        self.send("/ai")
        self.assertIn("冷卻至", self.telegram.messages[-1].text)
        self.assertIn("最近失敗：AI 請求逾時", self.telegram.messages[-1].text)
        self.assertNotIn(SECRET, self.telegram.messages[-1].text)

    def test_ai_off_can_finish_preserved_ambiguous_draft_with_basic_parser(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.append(parsed(question="要不要排除假日？"))
        self.send("工作日提醒我喝水")
        self.send("/ai off")
        self.send("明天下午三點")
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.assertTrue(draft.is_complete)
        self.assertNotIn("question", draft.parse_result)

    def test_update_keeps_other_participants_and_original_timezone(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.extend(
            [parsed(participant_indices=[1]), parsed(intent="update", participant_indices=[1])]
        )
        self.send("明天十五點叫 @alice 交報告")
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.assertEqual("alice", draft.participants[0].username)
        self.service.set_timezone(USER.id, "Asia/Tokyo")
        self.send("日期一樣，晚點再做")
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.assertEqual("Asia/Taipei", draft.timezone)
        self.assertEqual("alice", draft.participants[0].username)

    def test_failed_time_edit_preserves_edit_session_and_reminder(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.extend([parsed(), ProviderError("temporary")])
        self.send("明天十五點提醒我交報告")
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.confirm(draft.id)
        original = self.repo.list_pending(CHAT.id)[0]
        self.service.begin_edit(CHAT.id, original.short_id, USER.id, "time", NOW)
        self.send("延到後天")
        self.assertIsNotNone(self.service.peek_edit_session(CHAT.id, USER.id, NOW))
        self.assertEqual(original.remind_at, self.repo.list_pending(CHAT.id)[0].remind_at)

    def test_lost_master_key_still_allows_removing_saved_credentials(self) -> None:
        key_id = self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.keys._cipher = None
        self.send(f"/ai delete {key_id}")
        self.assertEqual([], self.keys.list(USER.id))

    def test_missing_recurrence_time_preserves_source_for_followup(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.extend(
            [
                parsed(date=None, time=None, question="每天幾點吃藥？"),
                parsed(intent="update", date=None, time=None, recurrence=rule()),
            ]
        )
        self.send("每天叫我吃藥")
        self.send("早上九點")
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.assertEqual("每天叫我吃藥", self.gemini.calls[-1][1]["context"]["source_text"])
        self.assertEqual(9, draft.recurrence.hour)
        self.assertTrue(draft.is_complete)
