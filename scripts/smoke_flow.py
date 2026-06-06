from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

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
ZONE = ZoneInfo("Asia/Taipei")


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
                self.messages[index] = SentMessage(message_id, message.chat_id, text, reply_markup)
                return
        self.send_message(chat_id, text, reply_markup=reply_markup)

    def delete_message(self, chat_id: int, message_id: int) -> None:
        del chat_id
        self.deleted_messages.append(message_id)
        self.messages = [message for message in self.messages if message.id != message_id]

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        del callback_query_id
        self.callback_answers.append(text)


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        client = FakeTelegramClient()
        router, repository = build_router(client, Path(directory) / "smoke.db")

        send_text(router, 1, "提醒我明天倒垃圾")
        click_button(router, 2, client.messages[-1], "09:00")
        click_button(router, 3, client.messages[-1], "確認")
        send_text(router, 4, "/list")
        click_button(router, 5, client.messages[-1], "R-")
        click_button(router, 6, client.messages[-1], "修改內容")
        send_text(router, 7, "倒回收")
        click_button(router, 8, client.messages[-1], "刪除")
        click_button(router, 9, client.messages[-1], "確認刪除")

        print_transcript(client)
        print_database_summary(repository)


def build_router(
    client: FakeTelegramClient,
    database_path: Path,
) -> tuple[BotRouter, ReminderRepository]:
    repository = ReminderRepository(database_path)
    repository.migrate()
    parser = ReminderParser("Asia/Taipei")
    renderer = ReminderRenderer()
    service = ReminderService(
        repository=repository,
        parser=parser,
        draft_store=SqliteDraftStore(ttl_minutes=10, repository=repository),
        edit_store=SqliteEditSessionStore(ttl_minutes=10, repository=repository),
        default_timezone="Asia/Taipei",
    )
    router = BotRouter(
        client=client,  # type: ignore[arg-type]
        reminder_service=service,
        renderer=renderer,
        bot_username="ReminderBot",
        default_timezone="Asia/Taipei",
    )
    return router, repository


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
    callback_data = find_callback_data(message.reply_markup, label_contains)
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
        data=callback_data,
    )
    router.handle_update(TelegramUpdate(id=update_id, callback_query=callback))


def find_callback_data(reply_markup: dict[str, Any] | None, label_contains: str) -> str:
    if not reply_markup:
        raise RuntimeError(f"No reply_markup when looking for {label_contains!r}")
    for row in reply_markup.get("inline_keyboard", []):
        for button in row:
            if label_contains in button["text"]:
                return str(button["callback_data"])
    raise RuntimeError(f"Button containing {label_contains!r} not found")


def print_transcript(client: FakeTelegramClient) -> None:
    print("== Sent / Edited Messages ==")
    for message in client.messages:
        print(f"#{message.id}")
        print(message.text)
        if message.reply_markup:
            labels = [
                button["text"]
                for row in message.reply_markup["inline_keyboard"]
                for button in row
            ]
            print(f"buttons: {', '.join(labels)}")
        print("---")
    print(f"callback answers: {client.callback_answers}")
    print(f"deleted messages: {client.deleted_messages}")


def print_database_summary(repository: ReminderRepository) -> None:
    now = datetime.now(ZONE)
    pending = repository.list_pending(CHAT.id)
    due = repository.claim_due(now + timedelta(days=2))
    print("== DB Summary ==")
    print(f"pending reminders: {len(pending)}")
    print(f"claimable reminders after +2d: {len(due)}")


if __name__ == "__main__":
    main()
