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
from remindly.storage.session_stores import SqliteDraftStore, SqliteEditSessionStore
from remindly.storage.sqlite import ReminderRepository
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
    next_message_id: int = 1000

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        del parse_mode
        self.messages.append(SentMessage(self.next_message_id, chat_id, text, reply_markup))
        self.next_message_id += 1

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        del parse_mode
        for index, message in enumerate(self.messages):
            if message.id == message_id:
                self.messages[index] = SentMessage(message_id, chat_id, text, reply_markup)
                return
        self.send_message(chat_id, text, reply_markup=reply_markup)

    def delete_message(self, chat_id: int, message_id: int) -> None:
        del chat_id
        self.deleted_messages.append(message_id)
        self.messages = [message for message in self.messages if message.id != message_id]

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        del callback_query_id
        self.callback_answers.append(text)


class BotRouterTest(unittest.TestCase):
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
                reply_markup=delivery_snooze_keyboard("R-SNZ1"),
            )
            client.messages.append(delivery_message)

            click_button(router, 1, delivery_message, "10 分鐘後")

            self.assertEqual("已延後", client.callback_answers[-1])
            self.assertIn("已延後提醒 R-SNZ1", client.messages[-1].text)
            pending = repository.list_pending(CHAT.id)
            self.assertEqual(1, len(pending))
            self.assertGreater(pending[0].remind_at, now)

    def test_create_list_edit_and_delete_flow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeTelegramClient()
            repository = ReminderRepository(Path(directory) / "test.db")
            router = build_router(client, repository)

            send_text(router, 1, "提醒我明天倒垃圾")
            self.assertIn("那天幾點？", client.messages[-1].text)

            click_button(router, 2, client.messages[-1], "09:00")
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
    )


def send_text(
    router: BotRouter,
    update_id: int,
    text: str,
    *,
    chat: TelegramChat = CHAT,
) -> None:
    message = TelegramMessage(
        id=update_id,
        chat=chat,
        from_user=USER,
        text=text,
        entities=(),
    )
    router.handle_update(TelegramUpdate(id=update_id, message=message))


def click_button(
    router: BotRouter,
    update_id: int,
    message: SentMessage,
    label_contains: str,
) -> None:
    callback_message = TelegramMessage(
        id=message.id,
        chat=CHAT,
        from_user=None,
        text=message.text,
        entities=(),
    )
    callback = TelegramCallbackQuery(
        id=f"cb_{update_id}",
        from_user=USER,
        message=callback_message,
        data=find_callback_data(message.reply_markup, label_contains),
    )
    router.handle_update(TelegramUpdate(id=update_id, callback_query=callback))


def button_labels(reply_markup: dict[str, Any] | None) -> list[str]:
    if not reply_markup:
        return []
    return [
        str(button["text"])
        for row in reply_markup.get("inline_keyboard", [])
        for button in row
    ]


def find_callback_data(reply_markup: dict[str, Any] | None, label_contains: str) -> str:
    if not reply_markup:
        raise AssertionError(f"No reply_markup when looking for {label_contains!r}")
    for row in reply_markup.get("inline_keyboard", []):
        for button in row:
            if label_contains in button["text"]:
                return str(button["callback_data"])
    raise AssertionError(f"Button containing {label_contains!r} not found")
