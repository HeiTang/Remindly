from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from remindly.bot.router import BotRouter
from remindly.reminders.models import Reminder, ReminderStatus
from remindly.reminders.parser import ReminderParser
from remindly.reminders.renderer import ReminderRenderer, delivery_snooze_keyboard
from remindly.reminders.service import ReminderService
from remindly.storage.reports import ReportStore
from remindly.storage.session_stores import SqliteDraftStore, SqliteEditSessionStore
from remindly.storage.sqlite import ReminderRepository
from remindly.telegram.client import TelegramApiError
from remindly.telegram.models import (
    TelegramCallbackQuery,
    TelegramChat,
    TelegramMessage,
    TelegramUpdate,
    TelegramUser,
)

CHAT = TelegramChat(id=100, type="private")
GROUP_CHAT = TelegramChat(id=-100, type="group", title="Test Group")
USER = TelegramUser(id=7, first_name="Orange", username="orange")


@dataclass
class SentMessage:
    id: int
    chat_id: int
    text: str
    reply_markup: dict[str, Any] | None


@dataclass
class FakeTelegramClient:
    messages: list[SentMessage] = field(default_factory=list)
    callback_answers: list[str | None] = field(default_factory=list)
    deleted_messages: list[int] = field(default_factory=list)
    chat_member_statuses: dict[int, str] = field(default_factory=dict)
    fail_chat_member_lookup: bool = False
    fail_edit_message: bool = False
    next_message_id: int = 1000

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> int:
        del parse_mode
        message_id = self.next_message_id
        self.messages.append(SentMessage(message_id, chat_id, text, reply_markup))
        self.next_message_id += 1
        return message_id

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> int:
        del parse_mode
        if self.fail_edit_message:
            raise TelegramApiError("editMessageText", "forced failure")
        for index, message in enumerate(self.messages):
            if message.id == message_id:
                self.messages[index] = SentMessage(message_id, chat_id, text, reply_markup)
                return message_id
        return self.send_message(chat_id, text, reply_markup=reply_markup)

    def delete_message(self, chat_id: int, message_id: int) -> None:
        del chat_id
        self.deleted_messages.append(message_id)
        self.messages = [message for message in self.messages if message.id != message_id]

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        del callback_query_id
        self.callback_answers.append(text)

    def get_chat_member(self, chat_id: int, user_id: int) -> str:
        del chat_id
        if self.fail_chat_member_lookup:
            raise TelegramApiError("getChatMember", "forced failure")
        return self.chat_member_statuses.get(user_id, "member")


class BotRouterTest(unittest.TestCase):
    def test_report_flow_requires_owner_and_submit_and_preserves_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeTelegramClient()
            repository = ReminderRepository(Path(directory) / "test.db")
            router = build_router(client, repository, reports=True)

            send_text(router, 1, "@ReminderBot 明天下午三點提醒我拿包裹", chat=GROUP_CHAT)
            card = client.messages[-1]
            self.assertIn("使用 內建規則 解析</code> · <a", card.text)
            start = "/start " + card.text.split("?start=", 1)[1].split('"', 1)[0]
            send_text(router, 2, start, user=TelegramUser(8, "Other"))
            self.assertIn("你不是草稿建立者", client.messages[-1].text)
            send_text(router, 3, start, chat=GROUP_CHAT)
            self.assertEqual(CHAT.id, client.messages[-1].chat_id)
            send_text(router, 4, start)
            click_button(router, 5, client.messages[-1], "時間錯了")
            click_button(router, 6, client.messages[-1], "附上原句與結果")
            self.assertIn("拿包裹", client.messages[-1].text)
            click_button(router, 7, client.messages[-1], "移除附件")
            preview = client.messages[-1]
            self.assertNotIn("拿包裹", preview.text)
            self.assertIn("Telegram user ID：7", preview.text)
            self.assertIn("Telegram @username：@orange", preview.text)
            self.assertIn("送出時會記錄", preview.text)
            self.assertIn("維護者可在回報詳情查看", preview.text)
            legacy_button = preview.reply_markup["inline_keyboard"][-1][-1]
            legacy_button["callback_data"] = legacy_button["callback_data"].replace(
                "report:send_id:", "report:send:", 1
            )
            click_button(router, 8, preview, "送出回報")
            preview = client.messages[-1]
            self.assertIn("維護者可在回報詳情查看", preview.text)
            click_button(router, 9, preview, "送出回報", user=TelegramUser(8, "Other"))
            with repository.connect() as connection:
                self.assertEqual(
                    0,
                    connection.execute("select count(*) from reminder_parse_reports").fetchone()[0],
                )
            click_button(router, 10, preview, "送出回報")
            self.assertIn("已收到回報", client.messages[-1].text)
            click_button(router, 11, preview, "送出回報", user=TelegramUser(7, "Orange", "renamed"))
            self.assertIn("已回報過", client.messages[-1].text)
            self.assertEqual([], repository.list_pending(GROUP_CHAT.id))
            click_button(router, 12, card, "確認")
            self.assertEqual(1, len(repository.list_pending(GROUP_CHAT.id)))
            with repository.connect() as connection:
                rows = connection.execute(
                    "select reason, parser_source, attachment_json, "
                    "reporter_user_id, reporter_username from reminder_parse_reports"
                ).fetchall()
            self.assertEqual([("time", "rules", None, 7, "orange")], [tuple(row) for row in rows])

    def test_report_rejects_old_preview_after_draft_changes_and_saves_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeTelegramClient()
            repository = ReminderRepository(Path(directory) / "test.db")
            router = build_router(client, repository, reports=True)

            send_text(router, 1, "@ReminderBot 明天下午三點提醒我拿包裹", chat=GROUP_CHAT)
            card = client.messages[-1]
            start = "/start " + card.text.split("?start=", 1)[1].split('"', 1)[0]
            send_text(router, 2, start)
            click_button(router, 3, client.messages[-1], "時間錯了")
            stale_preview = client.messages[-1]
            send_text(router, 4, "改成下午四點", chat=GROUP_CHAT)
            revised_card = client.messages[-1]
            self.assertIn("16:00", revised_card.text)
            click_button(router, 5, stale_preview, "送出回報")
            self.assertIn("這張確認卡已失效", client.callback_answers[-1])

            start = "/start " + revised_card.text.split("?start=", 1)[1].split('"', 1)[0]
            send_text(router, 6, start)
            click_button(router, 7, client.messages[-1], "時間錯了")
            no_username = TelegramUser(USER.id, USER.first_name)
            click_button(router, 8, client.messages[-1], "附上原句與結果", user=no_username)
            self.assertIn("Telegram @username：未設定", client.messages[-1].text)
            click_button(router, 9, client.messages[-1], "送出回報", user=no_username)
            with repository.connect() as connection:
                rows = connection.execute(
                    "select attachment_json, reporter_user_id, reporter_username "
                    "from reminder_parse_reports"
                ).fetchall()
            self.assertEqual(1, len(rows))
            self.assertIn("16:00", rows[0]["attachment_json"])
            self.assertIn("拿包裹", rows[0]["attachment_json"])
            self.assertIn('"parsed_at"', rows[0]["attachment_json"])
            self.assertEqual(USER.id, rows[0]["reporter_user_id"])
            self.assertIsNone(rows[0]["reporter_username"])

    def test_group_plain_text_reminder_request_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeTelegramClient()
            repository = ReminderRepository(Path(directory) / "test.db")
            router = build_router(client, repository)

            send_text(router, 1, "提醒我要洗衣服", chat=GROUP_CHAT)

            self.assertEqual([], client.messages)

    def test_group_bot_mention_starts_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeTelegramClient()
            repository = ReminderRepository(Path(directory) / "test.db")
            router = build_router(client, repository)

            send_text(router, 1, "@ReminderBot 提醒我要洗衣服", chat=GROUP_CHAT)

            self.assertIn("什麼時候提醒？", client.messages[-1].text)

    def test_group_next_message_continues_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeTelegramClient()
            repository = ReminderRepository(Path(directory) / "test.db")
            router = build_router(client, repository)

            send_text(router, 1, "@ReminderBot 提醒我要洗衣服", chat=GROUP_CHAT)
            send_text(router, 2, "明天下午三點", chat=GROUP_CHAT)

            self.assertIn("確認建立提醒？", client.messages[-1].text)

    def test_groupmode_private_chat_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeTelegramClient()
            repository = ReminderRepository(Path(directory) / "test.db")
            router = build_router(client, repository)

            send_text(router, 1, "/groupmode status")

            self.assertEqual("這個設定只能在群組使用。", client.messages[-1].text)

    def test_groupmode_status_shows_toggle_panel_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeTelegramClient()
            repository = ReminderRepository(Path(directory) / "test.db")
            router = build_router(client, repository)

            send_text(router, 1, "/groupmode", chat=GROUP_CHAT)

            self.assertIn("群組自然語言模式：關閉", client.messages[-1].text)
            self.assertIn("這個開關的意思", client.messages[-1].text)
            self.assertIn("Group Privacy", client.messages[-1].text)
            self.assertEqual(["開啟"], button_labels(client.messages[-1].reply_markup))

    def test_groupmode_button_toggles_enabled_for_admin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeTelegramClient(chat_member_statuses={USER.id: "administrator"})
            repository = ReminderRepository(Path(directory) / "test.db")
            router = build_router(client, repository)

            send_text(router, 1, "/groupmode", chat=GROUP_CHAT)
            click_button(router, 2, client.messages[-1], "開啟")

            self.assertEqual("已開啟", client.callback_answers[-1])
            self.assertIn("群組自然語言模式：開啟", client.messages[-1].text)
            self.assertEqual(["關閉"], button_labels(client.messages[-1].reply_markup))

            send_text(router, 3, "提醒我要洗衣服", chat=GROUP_CHAT)

            self.assertIn("什麼時候提醒？", client.messages[-1].text)

    def test_groupmode_button_rejects_non_admin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeTelegramClient(chat_member_statuses={USER.id: "member"})
            repository = ReminderRepository(Path(directory) / "test.db")
            router = build_router(client, repository)

            send_text(router, 1, "/groupmode", chat=GROUP_CHAT)
            click_button(router, 2, client.messages[-1], "開啟")

            self.assertEqual("只有群組管理員可以切換。", client.callback_answers[-1])
            self.assertIn("群組自然語言模式：關閉", client.messages[-1].text)
            self.assertEqual(["開啟"], button_labels(client.messages[-1].reply_markup))

    def test_groupmode_admin_enables_plain_text_reminders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeTelegramClient(chat_member_statuses={USER.id: "administrator"})
            repository = ReminderRepository(Path(directory) / "test.db")
            router = build_router(client, repository)

            send_text(router, 1, "/groupmode on", chat=GROUP_CHAT)
            send_text(router, 2, "提醒我要洗衣服", chat=GROUP_CHAT)

            self.assertIn("群組自然語言模式：開啟", client.messages[0].text)
            self.assertIn("Group Privacy", client.messages[0].text)
            self.assertEqual(["關閉"], button_labels(client.messages[0].reply_markup))
            self.assertIn("什麼時候提醒？", client.messages[-1].text)

    def test_groupmode_off_disables_plain_text_reminders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeTelegramClient(chat_member_statuses={USER.id: "creator"})
            repository = ReminderRepository(Path(directory) / "test.db")
            router = build_router(client, repository)

            send_text(router, 1, "/groupmode on", chat=GROUP_CHAT)
            send_text(router, 2, "/groupmode off", chat=GROUP_CHAT)
            message_count = len(client.messages)
            send_text(router, 3, "提醒我要洗衣服", chat=GROUP_CHAT)

            self.assertIn("群組自然語言模式：關閉", client.messages[-1].text)
            self.assertEqual(["開啟"], button_labels(client.messages[-1].reply_markup))
            self.assertEqual(message_count, len(client.messages))

    def test_groupmode_rejects_non_admin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeTelegramClient(chat_member_statuses={USER.id: "member"})
            repository = ReminderRepository(Path(directory) / "test.db")
            router = build_router(client, repository)

            send_text(router, 1, "/groupmode on", chat=GROUP_CHAT)
            message_count = len(client.messages)
            send_text(router, 2, "提醒我要洗衣服", chat=GROUP_CHAT)

            self.assertEqual("只有群組管理員可以切換自然語言模式。", client.messages[-1].text)
            self.assertEqual(message_count, len(client.messages))

    def test_groupmode_rejects_permission_lookup_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeTelegramClient(fail_chat_member_lookup=True)
            repository = ReminderRepository(Path(directory) / "test.db")
            router = build_router(client, repository)

            send_text(router, 1, "/groupmode on", chat=GROUP_CHAT)

            self.assertEqual("無法確認你的群組權限，先不變更設定。", client.messages[-1].text)

    def test_group_plain_text_uses_strict_reminder_patterns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeTelegramClient(chat_member_statuses={USER.id: "administrator"})
            repository = ReminderRepository(Path(directory) / "test.db")
            router = build_router(client, repository)

            send_text(router, 1, "/groupmode on", chat=GROUP_CHAT)
            message_count = len(client.messages)
            send_text(router, 2, "你提醒我想到一件事", chat=GROUP_CHAT)
            send_text(router, 3, "明天提醒我倒垃圾", chat=GROUP_CHAT)

            self.assertEqual(message_count + 1, len(client.messages))
            self.assertIn("那天幾點？", client.messages[-1].text)

    def test_snooze_delivery_callback_requeues_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeTelegramClient()
            repository = ReminderRepository(Path(directory) / "test.db")
            router = build_router(client, repository)
            now = datetime.now(ZoneInfo("Asia/Taipei"))
            repository.create_reminder(
                Reminder(
                    id="rmd_snooze",
                    short_id="R-SNZ1",
                    chat_id=CHAT.id,
                    chat_type=CHAT.type,
                    creator_user_id=USER.id,
                    title="洗衣服",
                    remind_at=now - timedelta(minutes=1),
                    timezone="Asia/Taipei",
                    status=ReminderStatus.FIRED,
                    source_text="提醒我要洗衣服",
                    parse_result={},
                    created_at=now - timedelta(minutes=5),
                    updated_at=now,
                ),
                [],
            )
            delivery_message = SentMessage(
                id=2000,
                chat_id=CHAT.id,
                text="提醒：洗衣服",
                reply_markup=delivery_snooze_keyboard("R-SNZ1", "23:59"),
            )
            client.messages.append(delivery_message)

            click_button(router, 1, delivery_message, "10 分鐘後")

            self.assertEqual("已延後", client.callback_answers[-1])
            self.assertIn("已延後提醒 R-SNZ1", client.messages[-1].text)
            # 延後後原按鈕組應被清空，避免使用者重複按累積延後
            self.assertEqual(
                {"inline_keyboard": []},
                client.messages[-1].reply_markup,
            )
            pending = repository.list_pending(CHAT.id)
            self.assertEqual(1, len(pending))
            self.assertGreater(pending[0].remind_at, now)

    def test_new_reminder_overwrites_pending_draft_edits_old_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeTelegramClient()
            repository = ReminderRepository(Path(directory) / "test.db")
            router = build_router(client, repository)

            send_text(router, 1, "提醒我要洗衣服")
            a_prompt = client.messages[-1]
            self.assertIn("什麼時候提醒？", a_prompt.text)

            send_text(router, 2, "提醒我明天下午三點倒垃圾")

            # 舊的 A 提示應該被 editMessage 成已取消狀態，按鈕清空
            updated_a = next(m for m in client.messages if m.id == a_prompt.id)
            self.assertIn("已取消", updated_a.text)
            self.assertNotIn("什麼時候提醒？", updated_a.text)
            self.assertEqual({"inline_keyboard": []}, updated_a.reply_markup)

            # B 的確認卡不應該再帶「已取消上一個未完成的提醒」前綴
            b_message = client.messages[-1]
            self.assertNotIn("已取消上一個未完成的提醒", b_message.text)
            self.assertIn("確認建立提醒？", b_message.text)
            self.assertIn("倒垃圾", b_message.text)

    def test_new_reminder_overwrites_pending_edit_session_edits_old_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeTelegramClient()
            repository = ReminderRepository(Path(directory) / "test.db")
            router = build_router(client, repository)

            send_text(router, 1, "提醒我明天倒垃圾")
            click_button(router, 2, client.messages[-1], "09:00")
            click_button(router, 3, client.messages[-1], "確認")
            send_text(router, 4, "/list")
            click_button(router, 5, client.messages[-1], "R-")
            click_button(router, 6, client.messages[-1], "修改內容")
            edit_prompt = client.messages[-1]
            self.assertIn("請輸入新的提醒內容", edit_prompt.text)

            send_text(router, 7, "提醒我後天下午三點吃藥")

            updated_edit_prompt = next(m for m in client.messages if m.id == edit_prompt.id)
            self.assertIn("已取消", updated_edit_prompt.text)
            self.assertEqual({"inline_keyboard": []}, updated_edit_prompt.reply_markup)

            b_message = client.messages[-1]
            self.assertNotIn("已取消上一個未完成的提醒", b_message.text)
            self.assertIn("確認建立提醒？", b_message.text)
            self.assertIn("吃藥", b_message.text)

    def test_overwrite_fallback_to_inline_notice_when_edit_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeTelegramClient(fail_edit_message=True)
            repository = ReminderRepository(Path(directory) / "test.db")
            router = build_router(client, repository)

            send_text(router, 1, "提醒我要洗衣服")
            send_text(router, 2, "提醒我明天下午三點倒垃圾")

            b_message = client.messages[-1]
            self.assertIn("已取消上一個未完成的提醒", b_message.text)
            self.assertIn("確認建立提醒？", b_message.text)

    def test_sequential_new_reminders_get_distinct_confirmations(self) -> None:
        """回歸測試 Q1：連續三次「提醒我 20:20 要 TESTn」，每則新確認卡應顯示對應的 TESTn，
        且前一則的追問訊息應被 editMessage 標為已取消，避免舊行為（continue_draft 吃掉新訊息）。"""
        with tempfile.TemporaryDirectory() as directory:
            client = FakeTelegramClient()
            repository = ReminderRepository(Path(directory) / "test.db")
            router = build_router(client, repository)

            send_text(router, 1, "提醒我 20:20 要 TEST1")
            first_id = client.messages[-1].id
            self.assertIn("要 TEST1", client.messages[-1].text)
            self.assertIn("確認建立提醒？", client.messages[-1].text)

            send_text(router, 2, "提醒我 20:20 要 TEST2")
            second_id = client.messages[-1].id
            self.assertNotEqual(first_id, second_id)
            # 第一則應被就地標為已取消 + 清按鈕
            first_msg = next(m for m in client.messages if m.id == first_id)
            self.assertIn("已取消", first_msg.text)
            self.assertEqual({"inline_keyboard": []}, first_msg.reply_markup)
            # 第二則應顯示 TEST2，不是 TEST1
            self.assertIn("要 TEST2", client.messages[-1].text)
            self.assertNotIn("TEST1", client.messages[-1].text)

            send_text(router, 3, "提醒我 20:20 要 TEST3")
            second_msg = next(m for m in client.messages if m.id == second_id)
            self.assertIn("已取消", second_msg.text)
            self.assertIn("要 TEST3", client.messages[-1].text)
            self.assertNotIn("TEST2", client.messages[-1].text)

    def test_invalid_recurrence_gets_single_turn_reject(self) -> None:
        """X flow：使用者輸入無效日期的週期性 marker（例：每個月 45 號）→
        Bot 送具體錯誤訊息，不建 draft、不追問「什麼時候提醒？」。"""
        with tempfile.TemporaryDirectory() as directory:
            client = FakeTelegramClient()
            repository = ReminderRepository(Path(directory) / "test.db")
            router = build_router(client, repository)

            send_text(router, 1, "每個月 45 號 09:00 提醒我吃藥")

            self.assertEqual(1, len(client.messages))
            reply = client.messages[-1]
            self.assertIn("每個月 45 號", reply.text)
            self.assertIn("1-31", reply.text)
            self.assertIn("請重新輸入", reply.text)
            # 沒 inline keyboard（不是追問也不是確認卡）
            self.assertIsNone(reply.reply_markup)

    def test_invalid_recurrence_does_not_disturb_pending_draft(self) -> None:
        """使用者若正在建立別的提醒（有 pending draft），送出無效週期不應覆蓋。"""
        with tempfile.TemporaryDirectory() as directory:
            client = FakeTelegramClient()
            repository = ReminderRepository(Path(directory) / "test.db")
            router = build_router(client, repository)

            send_text(router, 1, "提醒我要洗衣服")
            self.assertIn("什麼時候提醒？", client.messages[-1].text)
            first_prompt_id = client.messages[-1].id

            # 送無效週期
            send_text(router, 2, "每個月 45 號 09:00 提醒我")

            # 舊 prompt 沒被 editMessage（沒被覆蓋通知）
            first_still = next(m for m in client.messages if m.id == first_prompt_id)
            self.assertIn("什麼時候提醒？", first_still.text)
            self.assertNotIn("已取消", first_still.text)

            # 錯誤訊息獨立送出（沒建 draft）
            self.assertIn("每個月 45 號", client.messages[-1].text)

    def test_message_without_from_user_is_ignored(self) -> None:
        """匿名管理員 / sender_chat 沒有 from_user，session-based 流程應直接跳過而非 crash。"""
        with tempfile.TemporaryDirectory() as directory:
            client = FakeTelegramClient()
            repository = ReminderRepository(Path(directory) / "test.db")
            router = build_router(client, repository)

            message = TelegramMessage(
                id=42,
                chat=CHAT,
                from_user=None,
                text="提醒我明天倒垃圾",
                entities=(),
            )
            # 不應拋例外
            router.handle_update(TelegramUpdate(id=42, message=message))

            self.assertEqual([], client.messages)

    def test_answering_pending_draft_does_not_trigger_notice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeTelegramClient()
            repository = ReminderRepository(Path(directory) / "test.db")
            router = build_router(client, repository)

            send_text(router, 1, "提醒我要洗衣服")
            self.assertIn("什麼時候提醒？", client.messages[-1].text)

            send_text(router, 2, "明天下午三點")

            last = client.messages[-1].text
            self.assertNotIn("已取消上一個未完成的提醒", last)
            self.assertIn("確認建立提醒？", last)
            self.assertIn("洗衣服", last)

    def test_create_list_edit_and_delete_flow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeTelegramClient()
            repository = ReminderRepository(Path(directory) / "test.db")
            router = build_router(client, repository)

            send_text(router, 1, "提醒我明天倒垃圾")
            quick_time_prompt = client.messages[-1]
            self.assertIn("那天幾點？", quick_time_prompt.text)

            click_button(router, 2, quick_time_prompt, "09:00")
            self.assertIn(quick_time_prompt.id, client.deleted_messages)
            confirmation_message = client.messages[-1]
            self.assertIn("確認建立提醒？", confirmation_message.text)

            click_button(router, 3, confirmation_message, "確認")
            self.assertIn(confirmation_message.id, client.deleted_messages)
            self.assertIn("已建立提醒", client.messages[-1].text)

            send_text(router, 4, "/list")
            self.assertIn("未到期提醒（全部", client.messages[-1].text)
            self.assertIn("今天", button_labels(client.messages[-1].reply_markup))

            click_button(router, 5, client.messages[-1], "我的")
            self.assertIn("未到期提醒（我的", client.messages[-1].text)

            click_button(router, 6, client.messages[-1], "R-")
            self.assertIn("提醒 R-", client.messages[-1].text)

            click_button(router, 7, client.messages[-1], "修改內容")
            send_text(router, 8, "倒回收")
            self.assertIn("已更新提醒內容。", client.messages[-1].text)
            self.assertIn("倒回收", client.messages[-1].text)

            click_button(router, 9, client.messages[-1], "刪除")
            click_button(router, 10, client.messages[-1], "確認刪除")

            self.assertIn("已刪除提醒", client.messages[-1].text)
            self.assertEqual([], repository.list_pending(CHAT.id))


def build_router(
    client: FakeTelegramClient,
    repository: ReminderRepository,
    *,
    reports: bool = False,
) -> BotRouter:
    repository.migrate()
    renderer = ReminderRenderer()
    service = ReminderService(
        repository=repository,
        parser=ReminderParser("Asia/Taipei"),
        draft_store=SqliteDraftStore(ttl_minutes=10, repository=repository),
        edit_store=SqliteEditSessionStore(ttl_minutes=10, repository=repository),
        default_timezone="Asia/Taipei",
    )
    return BotRouter(
        client=client,  # type: ignore[arg-type]
        reminder_service=service,
        renderer=renderer,
        bot_username="ReminderBot",
        default_timezone="Asia/Taipei",
        report_store=ReportStore(repository) if reports else None,
    )


def send_text(
    router: BotRouter,
    update_id: int,
    text: str,
    *,
    chat: TelegramChat = CHAT,
    user: TelegramUser = USER,
) -> None:
    message = TelegramMessage(
        id=update_id,
        chat=chat,
        from_user=user,
        text=text,
        entities=(),
    )
    router.handle_update(TelegramUpdate(id=update_id, message=message))


def click_button(
    router: BotRouter,
    update_id: int,
    message: SentMessage,
    label_contains: str,
    *,
    user: TelegramUser = USER,
) -> None:
    callback_chat = GROUP_CHAT if message.chat_id == GROUP_CHAT.id else CHAT
    callback_message = TelegramMessage(
        id=message.id,
        chat=callback_chat,
        from_user=None,
        text=message.text,
        entities=(),
    )
    callback = TelegramCallbackQuery(
        id=f"cb_{update_id}",
        from_user=user,
        message=callback_message,
        data=find_callback_data(message.reply_markup, label_contains),
    )
    router.handle_update(TelegramUpdate(id=update_id, callback_query=callback))


def button_labels(reply_markup: dict[str, Any] | None) -> list[str]:
    if not reply_markup:
        return []
    return [
        str(button["text"]) for row in reply_markup.get("inline_keyboard", []) for button in row
    ]


def find_callback_data(reply_markup: dict[str, Any] | None, label_contains: str) -> str:
    if not reply_markup:
        raise AssertionError(f"No reply_markup when looking for {label_contains!r}")
    for row in reply_markup.get("inline_keyboard", []):
        for button in row:
            if label_contains in button["text"]:
                return str(button["callback_data"])
    raise AssertionError(f"Button containing {label_contains!r} not found")
