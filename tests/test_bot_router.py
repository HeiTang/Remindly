from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from remindly.bot.router import BotRouter
from remindly.reminders.parser import ReminderParser
from remindly.reminders.renderer import ReminderRenderer
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
            self.assertIn("未到期提醒", client.messages[-1].text)

            click_button(router, 5, client.messages[-1], "R-")
            self.assertIn("提醒 R-", client.messages[-1].text)

            click_button(router, 6, client.messages[-1], "修改內容")
            send_text(router, 7, "倒回收")
            self.assertIn("已更新提醒內容。", client.messages[-1].text)
            self.assertIn("倒回收", client.messages[-1].text)

            click_button(router, 8, client.messages[-1], "刪除")
            click_button(router, 9, client.messages[-1], "確認刪除")

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


def send_text(router: BotRouter, update_id: int, text: str) -> None:
    message = TelegramMessage(
        id=update_id,
        chat=CHAT,
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


def find_callback_data(reply_markup: dict[str, Any] | None, label_contains: str) -> str:
    if not reply_markup:
        raise AssertionError(f"No reply_markup when looking for {label_contains!r}")
    for row in reply_markup.get("inline_keyboard", []):
        for button in row:
            if label_contains in button["text"]:
                return str(button["callback_data"])
    raise AssertionError(f"Button containing {label_contains!r} not found")
