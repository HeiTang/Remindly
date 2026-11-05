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
    def test_explicit_additional_request_is_created_even_when_model_says_update(self):
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.extend(
            [
                parsed(title="拿包裹"),
                parsed(intent="update", title="繳水費", date="2026-10-24", time="09:00"),
            ]
        )
        self.send("提醒我明天下午三點拿包裹")
        old = self.service.peek_draft(CHAT.id, USER.id, NOW)
        source = "那件事取消，另外提醒我後天早上九點繳水費"
        self.send(source)
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.assertEqual("create", draft.parse_result["intent"])
        self.assertEqual(source, draft.source_text)
        self.assertEqual("繳水費", draft.title)
        self.assertIsNone(self.gemini.calls[-1][1]["context"])
        self.assertEqual("提醒我後天早上九點繳水費", self.gemini.calls[-1][1]["text"])
        self.assertIsNone(self.service.confirm(old.id, USER.id, NOW))
        self.assertEqual([], self.repo.list_pending(CHAT.id))
        self.confirm(draft.id)
        self.assertEqual("繳水費", self.repo.list_pending(CHAT.id)[0].title)

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

    def test_partial_time_guards_block_confirmation(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        for text, recurrence in (
            ("提醒我明天的晚上八點吃藥", rule(hour=20)),
            ("提醒我明天的晚上八點只在工作日吃藥", None),
        ):
            with self.subTest(text=text):
                self.gemini.responses.append(
                    parsed(title="吃藥", time="20:00", recurrence=recurrence)
                )
                self.send(text)
                draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
                self.assertIn("clarification", draft.missing_fields)
                self.assertIsNone(draft.recurrence)
                self.confirm(draft.id)
                self.assertEqual([], self.repo.list_pending(CHAT.id))

    def test_recurrence_saves_explicit_tomorrow_evening_start(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.append(parsed(title="吃藥", time="20:00", recurrence=rule(hour=20)))
        self.send("每天明晚八點提醒我吃藥")
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.assertTrue(draft.is_complete)
        self.assertEqual([], self.repo.list_pending(CHAT.id))
        self.confirm(draft.id)
        reminder = self.repo.list_pending(CHAT.id)[0]
        self.assertEqual(NOW.replace(day=23, hour=20), reminder.remind_at)
        self.assertEqual(20, reminder.recurrence.hour)

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

    def bind_group(self, *, owner=USER, secret=SECRET, provider="gemini"):
        key_id = self.keys.add(owner.id, provider=provider, key=secret)
        self.telegram.chat_member_statuses[USER.id] = "administrator"
        self.send("/ai group allow", chat=GROUP_CHAT)
        self.send(f"/ai group grant {GROUP_CHAT.id} {key_id}", user=owner)
        self.assertFalse(self.gateway.enabled(USER.id, chat_id=GROUP_CHAT.id))
        self.send("/ai group on", chat=GROUP_CHAT)
        self.assertTrue(self.gateway.enabled(USER.id, chat_id=GROUP_CHAT.id))
        return key_id

    def click_ai(self, data, *, user=USER, chat=CHAT):
        message = self.telegram.messages[-1]
        callback = TelegramCallbackQuery(
            id="ai-click",
            from_user=user,
            data=data,
            message=TelegramMessage(
                id=message.id, chat=chat, from_user=USER, text=message.text, entities=()
            ),
        )
        self.router.handle_update(TelegramUpdate(id=9000, callback_query=callback))

    def open_group_picker(self):
        self.telegram.chat_member_statuses[USER.id] = "administrator"
        self.send("/ai group allow", chat=GROUP_CHAT)
        button = self.telegram.messages[-1].reply_markup["inline_keyboard"][0][0]
        self.assertEqual("授權我的 Key", button["text"])
        self.assertEqual(f"https://t.me/ReminderBot?start=aigroup_{GROUP_CHAT.id}", button["url"])
        self.send("/start " + button["url"].split("?start=", 1)[1])
        return self.telegram.messages[-1]

    def test_group_button_private_key_picker_and_confirmation(self):
        first = self.keys.add(USER.id, provider="gemini", key=SECRET)
        second = self.keys.add(USER.id, provider="gemini", key=SECOND)
        self.keys.add(8, provider="gemini", key="other-owner-key")
        picker = self.open_group_picker()
        rows = picker.reply_markup["inline_keyboard"]
        self.assertEqual(2, len(rows))
        self.assertIn("費用與額度歸你", picker.text)
        self.assertIn("點選即同意", picker.text)
        for row in rows:
            self.assertLessEqual(len(row[0]["callback_data"].encode()), 64)
            self.assertNotIn(first, row[0]["text"])
            self.assertNotIn(second, row[0]["text"])
        self.click_ai(rows[1][0]["callback_data"])
        self.assertEqual((USER.id, second, False, False), self.keys.group_settings(GROUP_CHAT.id))
        self.assertEqual({"inline_keyboard": []}, self.telegram.messages[-1].reply_markup)
        self.send("/ai group on", chat=GROUP_CHAT)
        self.assertTrue(self.gateway.enabled(USER.id, chat_id=GROUP_CHAT.id))
        self.assertNotIn(SECRET, repr(self.telegram.messages))
        self.assertNotIn(SECOND, repr(self.telegram.messages))

    def test_group_picker_rejects_another_user_and_group_callbacks(self):
        key_id = self.keys.add(USER.id, provider="gemini", key=SECRET)
        picker = self.open_group_picker()
        data = picker.reply_markup["inline_keyboard"][0][0]["callback_data"]
        self.click_ai(data, user=TelegramUser(id=8, first_name="Other"))
        self.assertIn("自己的 Key", self.telegram.callback_answers[-1])
        self.click_ai(data, chat=GROUP_CHAT)
        self.assertIsNone(self.keys.group_settings(GROUP_CHAT.id)[0])
        self.assertIn("私聊", self.telegram.callback_answers[-1])
        other_key = self.keys.add(8, provider="gemini", key=SECOND)
        self.click_ai(data.replace(key_id, other_key))
        self.assertIsNone(self.keys.group_settings(GROUP_CHAT.id)[0])

    def test_group_picker_rechecks_membership_key_and_grant_window(self):
        key_id = self.keys.add(USER.id, provider="gemini", key=SECRET)
        picker = self.open_group_picker()
        data = picker.reply_markup["inline_keyboard"][0][0]["callback_data"]
        self.telegram.fail_chat_member_lookup = True
        self.click_ai(data)
        self.assertIn("無法確認", self.telegram.callback_answers[-1])
        self.telegram.fail_chat_member_lookup = False
        self.telegram.chat_member_statuses[USER.id] = "left"
        self.click_ai(data)
        self.assertIn("只有該群組成員", self.telegram.callback_answers[-1])
        self.telegram.chat_member_statuses[USER.id] = "administrator"
        self.send("/ai group off", chat=GROUP_CHAT)
        self.click_ai(data)
        self.assertIsNone(self.keys.group_settings(GROUP_CHAT.id)[0])
        self.send("/ai group allow", chat=GROUP_CHAT)
        self.keys.delete(USER.id, key_id)
        self.click_ai(data)
        self.assertIsNone(self.keys.group_settings(GROUP_CHAT.id)[0])

    def test_group_picker_empty_invalid_link_and_no_group_key_disclosure(self):
        picker = self.open_group_picker()
        self.assertIsNone(picker.reply_markup)
        self.assertIn("還沒有可選的 Key", picker.text)
        for target in ("abc", "100", str(-(2**80)), "-999"):
            self.send(f"/start aigroup_{target}")
            self.assertIsNone(self.telegram.messages[-1].reply_markup)
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.send(f"/start aigroup_{GROUP_CHAT.id}", chat=GROUP_CHAT)
        self.assertIsNone(self.telegram.messages[-1].reply_markup)
        self.assertIn("私聊", self.telegram.messages[-1].text)
        self.send("/ai group off", chat=GROUP_CHAT)
        self.send(f"/start aigroup_{GROUP_CHAT.id}")
        self.assertIsNone(self.telegram.messages[-1].reply_markup)

    def test_group_picker_consumed_button_cannot_regrant_and_edit_failure_is_safe(self):
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        picker = self.open_group_picker()
        data = picker.reply_markup["inline_keyboard"][0][0]["callback_data"]
        self.telegram.fail_edit_message = True
        self.click_ai(data)
        self.assertIn("已授權群組", self.telegram.messages[-1].text)
        self.click_ai(data)
        self.assertIn("開放綁定", self.telegram.callback_answers[-1])
        self.assertFalse(self.gateway.enabled(USER.id, chat_id=GROUP_CHAT.id))
        for malformed in ("ai:g", "ai:g:-100:missing", "ai:unknown:-100:7_key"):
            self.click_ai(malformed)
            self.assertIsNotNone(self.telegram.callback_answers[-1])

    def test_group_link_resolves_missing_bot_username_and_handles_lookup_failure(self):
        self.router._ai_commands._bot_username = None
        self.telegram.chat_member_statuses[USER.id] = "administrator"
        with patch.object(
            self.telegram, "call", create=True, return_value={"result": {"username": "ResolvedBot"}}
        ) as call:
            self.send("/ai group allow", chat=GROUP_CHAT)
            self.assertIn(
                "t.me/ResolvedBot?start=",
                self.telegram.messages[-1].reply_markup["inline_keyboard"][0][0]["url"],
            )
            self.send("/ai group", chat=GROUP_CHAT)
            call.assert_called_once_with("getMe", {})
        self.router._ai_commands._bot_username = None
        with patch.object(
            self.telegram, "call", create=True, side_effect=TelegramApiError("getMe", "failure")
        ):
            self.send("/ai group", chat=GROUP_CHAT)
            self.assertIsNone(self.telegram.messages[-1].reply_markup)
            self.assertIn("無法取得 Bot 帳號", self.telegram.messages[-1].text)

    def test_group_consent_permissions_and_private_key_management(self):
        key_id = self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.send(f"/ai group grant {GROUP_CHAT.id} {key_id}")
        self.assertIsNone(self.keys.group_settings(GROUP_CHAT.id)[0])
        self.send("/ai group allow", chat=GROUP_CHAT)
        self.assertIn("只有群組管理員", self.telegram.messages[-1].text)
        self.telegram.chat_member_statuses[USER.id] = "administrator"
        self.send("/ai group on", chat=GROUP_CHAT)
        self.assertIn("請先由 Key 持有人", self.telegram.messages[-1].text)
        self.send("/ai group allow", chat=GROUP_CHAT)
        self.assertEqual(
            "授權我的 Key", self.telegram.messages[-1].reply_markup["inline_keyboard"][0][0]["text"]
        )
        self.assertNotIn("grant <群組 ID>", self.telegram.messages[-1].text)
        self.send(f"/ai group grant {GROUP_CHAT.id} {key_id}", chat=GROUP_CHAT)
        self.assertIsNone(self.keys.group_settings(GROUP_CHAT.id)[0])
        self.send(f"/ai group grant {GROUP_CHAT.id} {key_id}")
        self.assertFalse(self.gateway.enabled(USER.id, chat_id=GROUP_CHAT.id))
        other = TelegramUser(id=8, first_name="Other")
        for action in ("on", "off", "allow", "unlink"):
            self.send(f"/ai group {action}", chat=GROUP_CHAT, user=other)
            self.assertIn("只有群組管理員", self.telegram.messages[-1].text)
        self.send("/ai group on", chat=GROUP_CHAT)
        self.send("/ai group", chat=GROUP_CHAT, user=other)
        self.assertNotIn(key_id, self.telegram.messages[-1].text)
        self.assertNotIn(SECRET, repr(self.telegram.messages))

    def test_group_lookup_failures_and_wrong_owner_fail_closed(self):
        key_id = self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.telegram.chat_member_statuses[USER.id] = "administrator"
        self.send("/ai group allow", chat=GROUP_CHAT)
        self.telegram.fail_chat_member_lookup = True
        self.send(f"/ai group grant {GROUP_CHAT.id} {key_id}")
        self.assertIn("無法確認", self.telegram.messages[-1].text)
        self.send("/ai group on", chat=GROUP_CHAT)
        self.assertIn("無法確認", self.telegram.messages[-1].text)
        self.telegram.fail_chat_member_lookup = False
        other = TelegramUser(id=8, first_name="Other")
        self.send(f"/ai group grant {GROUP_CHAT.id} {key_id}", user=other)
        self.assertIsNone(self.keys.group_settings(GROUP_CHAT.id)[0])
        self.telegram.chat_member_statuses[USER.id] = "left"
        self.send(f"/ai group grant {GROUP_CHAT.id} {key_id}")
        self.assertIn("只有該群組成員", self.telegram.messages[-1].text)
        for invalid in ("abc", "100", str(-(2**80))):
            self.send(f"/ai group grant {invalid} {key_id}")
            self.assertIn("群組 ID 不正確", self.telegram.messages[-1].text)
            self.send(f"/ai group revoke {invalid}")
            self.assertIn("群組 ID 不正確", self.telegram.messages[-1].text)
        self.assertIsNone(self.keys.group_settings(GROUP_CHAT.id)[0])

    def test_group_uses_only_shared_key_independent_of_personal_switch(self):
        owner = TelegramUser(id=9, first_name="Owner")
        key_id = self.bind_group(owner=owner, secret=SECOND)
        self.keys.set_enabled(owner.id, False)
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.keys.set_enabled(USER.id, False)
        self.gemini.responses.append(parsed())
        self.send("明天三點提醒我喝水", chat=GROUP_CHAT)
        self.assertEqual([], self.gemini.calls)
        self.send("@ReminderBot 明天下午三點提醒我喝水", chat=GROUP_CHAT)
        self.assertEqual(SECOND, self.gemini.calls[0][0])
        self.assertTrue(self.gemini.calls[0][1]["participants"][0]["is_self"])
        draft = self.service.peek_draft(GROUP_CHAT.id, USER.id, NOW)
        self.assertEqual("gemini", draft.parse_result["provider"])
        self.assertIn("使用 Test gemini 解析", self.telegram.messages[-1].text)
        self.assertEqual([], self.repo.list_pending(GROUP_CHAT.id))
        self.assertIsNone(self.service.confirm(draft.id, owner.id, NOW))
        self.assertIsNotNone(self.service.confirm(draft.id, USER.id, NOW))
        self.assertEqual(1, len(self.repo.list_pending(GROUP_CHAT.id)))
        self.assertEqual(key_id, self.keys.group_settings(GROUP_CHAT.id)[1])

    def test_group_failure_cools_only_selected_key_and_falls_back(self):
        key_id = self.bind_group()
        self.keys.add(USER.id, provider="gemini", key=SECOND)
        self.gemini.responses.append(ProviderError("rate"))
        self.send("/remind 明天下午三點提醒我喝水", chat=GROUP_CHAT)
        self.assertEqual([SECRET], [key for key, _ in self.gemini.calls])
        self.assertEqual("cooldown", self.keys.list(USER.id)[0].status)
        self.assertEqual("unchecked", self.keys.list(USER.id)[1].status)
        draft = self.service.peek_draft(GROUP_CHAT.id, USER.id, NOW)
        self.assertTrue(draft.is_complete)
        self.assertTrue(draft.parse_result["ai_fallback"])
        self.assertNotIn("provider", draft.parse_result)
        self.assertIn("已改用內建解析", self.telegram.messages[-1].text)
        self.assertNotIn(key_id, self.telegram.messages[-1].text)
        self.assertNotIn(SECRET, repr(self.telegram.messages))
        self.send("@ReminderBot 明天下午四點提醒我喝水", chat=GROUP_CHAT)
        self.assertEqual(1, len(self.gemini.calls))
        draft = self.service.peek_draft(GROUP_CHAT.id, USER.id, NOW)
        self.assertIsNotNone(self.service.confirm(draft.id, USER.id, NOW))
        self.gemini.responses.append(parsed())
        self.send("明天下午三點叫我喝水")
        self.assertEqual(SECOND, self.gemini.calls[-1][0])

    def test_group_revoke_delete_off_unlink_and_rebinding(self):
        key_id = self.bind_group()
        other = TelegramUser(id=8, first_name="Other")
        self.send(f"/ai group revoke {GROUP_CHAT.id}", user=other)
        self.assertTrue(self.gateway.enabled(USER.id, chat_id=GROUP_CHAT.id))
        self.send("/ai group off", chat=GROUP_CHAT)
        self.assertFalse(self.gateway.enabled(USER.id, chat_id=GROUP_CHAT.id))
        self.send("/ai group on", chat=GROUP_CHAT)
        self.send(f"/ai group revoke {GROUP_CHAT.id}")
        self.assertFalse(self.gateway.enabled(USER.id, chat_id=GROUP_CHAT.id))
        self.send("/ai group on", chat=GROUP_CHAT)
        self.assertFalse(self.gateway.enabled(USER.id, chat_id=GROUP_CHAT.id))
        self.send("/ai group allow", chat=GROUP_CHAT)
        self.send(f"/ai group grant {GROUP_CHAT.id} {key_id}")
        self.send("/ai group on", chat=GROUP_CHAT)
        second = self.keys.add(USER.id, provider="gemini", key=SECOND)
        self.send("/ai group allow", chat=GROUP_CHAT)
        self.send(f"/ai group grant {GROUP_CHAT.id} {second}")
        self.assertFalse(self.gateway.enabled(USER.id, chat_id=GROUP_CHAT.id))
        self.send("/ai group on", chat=GROUP_CHAT)
        self.send(f"/ai delete {second}")
        self.assertEqual((None, None, False, False), self.keys.group_settings(GROUP_CHAT.id))
        self.send("@ReminderBot 明天下午三點提醒我喝水", chat=GROUP_CHAT)
        self.assertEqual([], self.gemini.calls)
        self.send("/ai group unlink", chat=GROUP_CHAT)
        self.assertEqual([], self.keys.group_grants(USER.id))

    def test_group_unknown_provider_and_broken_encryption_fall_back(self):
        key_id = self.bind_group(provider="unknown")
        self.send("/remind 明天下午三點提醒我喝水", chat=GROUP_CHAT)
        self.assertIn("已改用內建解析", self.telegram.messages[-1].text)
        self.assertEqual([], self.gemini.calls)
        self.keys.delete(USER.id, key_id)
        self.bind_group()
        self.keys._cipher = Fernet(Fernet.generate_key())
        self.send("/remind 明天下午三點提醒我喝水", chat=GROUP_CHAT)
        self.assertIn("已改用內建解析", self.telegram.messages[-1].text)
        self.assertEqual([], self.gemini.calls)

    def test_group_fallback_keeps_draft_context_and_actual_parser_label(self):
        self.bind_group()
        self.gemini.responses.extend([parsed(title=None), ProviderError("temporary")])
        self.send("@ReminderBot 明天下午三點提醒我", chat=GROUP_CHAT)
        old = self.service.peek_draft(GROUP_CHAT.id, USER.id, NOW)
        self.send("喝水", chat=GROUP_CHAT)
        draft = self.service.peek_draft(GROUP_CHAT.id, USER.id, NOW)
        self.assertEqual(old.remind_at, draft.remind_at)
        self.assertEqual("喝水", draft.title)
        self.assertTrue(draft.is_complete)
        self.assertNotIn("provider", draft.parse_result)
        self.assertNotIn("使用 Test gemini", self.telegram.messages[-1].text)
        self.assertIsNotNone(self.service.confirm(draft.id, USER.id, NOW))

    def test_group_groq_isolation_and_natural_language_trigger(self):
        groq = ScriptedProvider([parsed()], provider_id="groq")
        self.gateway.providers["groq"] = groq
        self.bind_group(provider="groq")
        self.keys.add(USER.id, provider="gemini", key=SECOND)
        self.send("/groupmode on", chat=GROUP_CHAT)
        self.send("你提醒我想到一件事", chat=GROUP_CHAT)
        self.assertEqual([], groq.calls)
        self.send("明天下午三點提醒我喝水", chat=GROUP_CHAT)
        self.assertEqual(SECRET, groq.calls[0][0])
        self.assertEqual([], self.gemini.calls)
        self.assertEqual(
            "groq", self.service.peek_draft(GROUP_CHAT.id, USER.id, NOW).parse_result["provider"]
        )
        groq.responses.append(ProviderError("permission"))
        self.send("/remind 明天下午四點提醒我喝水", chat=GROUP_CHAT)
        self.assertEqual("invalid", self.keys.list(USER.id)[0].status)
        self.assertEqual([], self.gemini.calls)
        self.assertIn("已改用內建解析", self.telegram.messages[-1].text)

    def test_group_fallback_explicit_reminder_replaces_complete_draft(self):
        self.bind_group()
        self.gemini.responses.extend([parsed(title="交報告"), ProviderError("temporary")])
        self.send("@ReminderBot 提醒我明天下午三點交報告", chat=GROUP_CHAT)
        old = self.service.peek_draft(GROUP_CHAT.id, USER.id, NOW)
        self.send("@ReminderBot 提醒我後天下午四點繳費", chat=GROUP_CHAT)
        draft = self.service.peek_draft(GROUP_CHAT.id, USER.id, NOW)
        self.assertEqual("繳費", draft.title)
        self.assertEqual((24, 16), (draft.remind_at.day, draft.remind_at.hour))
        self.assertIsNone(self.service.confirm(old.id, USER.id, NOW))

    def test_group_fallback_new_request_does_not_merge_old_draft(self):
        self.bind_group()
        self.gemini.responses.extend([parsed(title=None), ProviderError("response")])
        self.send("@ReminderBot 明天下午三點提醒我", chat=GROUP_CHAT)
        self.send("@ReminderBot 另外提醒我後天下午四點喝水", chat=GROUP_CHAT)
        draft = self.service.peek_draft(GROUP_CHAT.id, USER.id, NOW)
        self.assertEqual("喝水", draft.title)
        self.assertEqual((24, 16), (draft.remind_at.day, draft.remind_at.hour))
        self.assertTrue(draft.parse_result["ai_fallback"])

    def test_group_fallback_time_edit_preserves_series(self):
        self.bind_group()
        self.gemini.responses.append(parsed(date=None, time=None, recurrence=rule()))
        self.send("/remind 每天九點提醒我吃藥", chat=GROUP_CHAT)
        draft = self.service.peek_draft(GROUP_CHAT.id, USER.id, NOW)
        self.service.confirm(draft.id, USER.id, NOW)
        reminder = self.repo.list_pending(GROUP_CHAT.id)[0]
        self.service.begin_edit(GROUP_CHAT.id, reminder.short_id, USER.id, "time", NOW)
        self.gemini.responses.append(ProviderError("timeout"))
        self.send("明天下午三點", chat=GROUP_CHAT)
        updated = self.repo.list_pending(GROUP_CHAT.id)[0]
        self.assertEqual(15, updated.remind_at.hour)
        self.assertEqual(reminder.recurrence, updated.recurrence)
        self.assertIn("已改用內建解析", self.telegram.messages[-1].text)

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

    def test_groq_fallback_uses_remaining_budget_and_requires_confirmation(self) -> None:
        import io
        import json

        from remindly.ai.groq import GroqProvider

        calls = []

        def opener(request, *, timeout):
            calls.append((request.get_header("Authorization"), timeout))
            return io.BytesIO(
                json.dumps(
                    {
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {"content": json.dumps(parsed(title="拿包裹"))},
                            }
                        ]
                    }
                ).encode()
            )

        self.gateway.providers["groq"] = GroqProvider(opener=opener)
        self.send(f"/ai add gemini {SECRET}")
        self.send(f"/ai add groq {SECOND}")
        self.assertIn("groq · Groq", self.telegram.messages[-1].text)
        self.gemini.responses.append(ProviderError("timeout"))
        with patch("remindly.ai.gateway.time.monotonic", side_effect=[100, 100, 105, 106]):
            self.send("明天下午三點叫我去拿包裹")
        self.assertEqual([(f"Bearer {SECOND}", 13)], calls)
        self.assertEqual([18], self.gemini.timeouts)
        first, second = self.keys.list(USER.id)
        self.assertEqual(("cooldown", "timeout"), (first.status, first.last_error))
        self.assertEqual(("ready", None), (second.status, second.last_error))
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.assertEqual("groq", draft.parse_result["provider"])
        self.assertEqual("openai/gpt-oss-120b", draft.parse_result["model"])
        self.assertIn("<code>使用 AI 解析</code>", self.telegram.messages[-1].text)
        self.assertEqual([], self.repo.list_pending(CHAT.id))
        self.confirm(draft.id)
        self.assertEqual("拿包裹", self.repo.list_pending(CHAT.id)[0].title)
        self.assertNotIn(SECOND, repr(self.telegram.messages))

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

    def test_negated_time_replaces_confirmation_with_question(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.extend(
            [parsed(title="寄報表"), parsed(intent="update", title="寄報表")]
        )
        self.send("提醒我明天下午三點寄報表")
        old = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.send("不是11/17 23:55")
        new = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.assertEqual("寄報表", new.title)
        self.assertIsNone(new.remind_at)
        self.assertIn("time", new.missing_fields)
        self.assertIn("未來日期與時間", self.telegram.messages[-1].text)
        self.confirm(old.id)
        self.confirm(new.id)
        self.assertEqual([], self.repo.list_pending(CHAT.id))

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

    def test_unbound_group_never_uses_actor_personal_keys(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.send("明晚八點叫我拿包裹", chat=GROUP_CHAT)
        self.assertEqual([], self.gemini.calls)
        other = TelegramUser(id=8, first_name="Other")
        self.send("@ReminderBot 明天三點提醒我喝水", chat=GROUP_CHAT, user=other)
        self.assertEqual([], self.gemini.calls)
        self.gemini.responses.append(parsed())
        self.send("@ReminderBot 明天三點叫我喝水", chat=GROUP_CHAT)
        self.assertEqual([], self.gemini.calls)

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
        self.assertEqual("那天幾點？", context["question"])
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

    def test_ai_off_cannot_clear_preserved_condition_by_only_supplying_time(self) -> None:
        self.keys.add(USER.id, provider="gemini", key=SECRET)
        self.gemini.responses.append(parsed(question="要不要排除假日？"))
        self.send("工作日提醒我喝水")
        self.send("/ai off")
        self.send("明天下午三點")
        draft = self.service.peek_draft(CHAT.id, USER.id, NOW)
        self.assertFalse(draft.is_complete)
        self.assertIn("clarification", draft.missing_fields)
        self.assertTrue(draft.parse_result["question"])
        self.assertEqual([], self.repo.list_pending(CHAT.id))

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
