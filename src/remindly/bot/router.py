from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from remindly.bot.callback_handlers import CallbackHandlers
from remindly.bot.command_handlers import CommandHandlers
from remindly.bot.response_sender import ResponseSender
from remindly.reminders.renderer import ReminderRenderer
from remindly.reminders.service import ReminderService
from remindly.reminders.text import strip_bot_mention
from remindly.telegram.client import TelegramClient
from remindly.telegram.models import TelegramMessage, TelegramUpdate


class BotRouter:
    def __init__(
        self,
        client: TelegramClient,
        reminder_service: ReminderService,
        renderer: ReminderRenderer,
        *,
        bot_username: str | None,
        default_timezone: str,
    ) -> None:
        self._reminder_service = reminder_service
        self._bot_username = bot_username
        self._default_timezone = default_timezone
        self._responses = ResponseSender(client, reminder_service, renderer)
        self._command_handlers = CommandHandlers(
            client,
            reminder_service,
            self._responses,
            bot_username=bot_username,
        )
        self._callback_handlers = CallbackHandlers(client, reminder_service, self._responses)

    def handle_update(self, update: TelegramUpdate) -> None:
        """接收 Telegram update，依型別分派給 message 或 callback 流程。"""
        now = datetime.now(ZoneInfo(self._default_timezone))
        if update.callback_query:
            self._callback_handlers.handle(update.callback_query, now)
            return

        if update.message and update.message.text:
            self._handle_message(update.message, now)

    def _handle_message(self, message: TelegramMessage, now: datetime) -> None:
        """處理一般文字訊息：指令、修改流程、草稿追問、自然語言提醒。"""
        text = strip_bot_mention(message.text, self._bot_username)
        self._reminder_service.record_message_context(message, now)

        if self._command_handlers.handle(message, text, now):
            return

        edit_result = self._reminder_service.continue_edit(message, now)
        if edit_result:
            self._responses.send_edit_result(message.chat.id, edit_result)
            return

        draft_result = self._reminder_service.continue_draft(message, now)
        if draft_result:
            self._responses.send_draft_result(message.chat.id, draft_result)
            return

        if self._should_treat_as_reminder(message, text):
            result = self._reminder_service.begin_create(text, message, now)
            self._responses.send_draft_result(message.chat.id, result)

    def _should_treat_as_reminder(self, message: TelegramMessage, text: str) -> bool:
        """判斷非指令文字是否應進入建立提醒流程。"""
        if message.chat.type == "private":
            return "提醒" in text

        if self._bot_username and message.text.strip().lower().startswith(
            f"@{self._bot_username.lower()}"
        ):
            return True

        return bool(message.reply_to_message and message.reply_to_message.from_user is None)
