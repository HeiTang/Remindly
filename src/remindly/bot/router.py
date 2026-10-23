from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from remindly.ai.contracts import AiUnavailable
from remindly.bot.ai_commands import AiCommands
from remindly.bot.callback_handlers import CallbackHandlers
from remindly.bot.command_handlers import CommandHandlers
from remindly.bot.response_sender import ResponseSender
from remindly.reminders.renderer import ReminderRenderer
from remindly.reminders.service import ReminderService
from remindly.reminders.text import looks_like_reminder_request, strip_bot_mention
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
        ai_commands: AiCommands | None = None,
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
        self._client = client
        self._ai_commands = ai_commands

    def handle_update(self, update: TelegramUpdate) -> None:
        """接收 Telegram update，依型別分派給 message 或 callback 流程。"""
        now = datetime.now(ZoneInfo(self._default_timezone))
        if update.callback_query:
            self._callback_handlers.handle(update.callback_query, now)
            return

        if update.message and update.message.text:
            try:
                self._handle_message(update.message, now)
            except AiUnavailable as exc:
                self._client.send_message(update.message.chat.id, str(exc))

    def _handle_message(self, message: TelegramMessage, now: datetime) -> None:
        """處理一般文字訊息：指令、修改流程、草稿追問、自然語言提醒。

        使用者意圖比 session state 更權威：若新訊息本身像新的提醒，優先斬斷舊 draft/edit。
        """
        text = strip_bot_mention(message.text, self._bot_username)
        if self._ai_commands is not None and self._ai_commands.handle(message):
            return
        self._reminder_service.record_message_context(message, now)

        if self._command_handlers.handle(message, text, now):
            return

        # 匿名管理員 / sender_chat 沒有 from_user，session-based 流程無從綁定，直接跳過。
        # Commands 已在上一步各自處理，這裡只影響 continue_edit / continue_draft / begin_create。
        if message.from_user is None:
            return

        actor_id = message.from_user.id
        starts_new_reminder = self._should_treat_as_reminder(message, text)
        if self._reminder_service.ai_enabled(actor_id):
            self._handle_ai_message(message, text, now, starts_new_reminder)
            return

        if not starts_new_reminder:
            edit_result = self._reminder_service.continue_edit(message, now)
            if edit_result:
                self._responses.send_edit_result(message.chat.id, edit_result, user_id=actor_id)
                return

            draft_result = self._reminder_service.continue_draft(message, now)
            if draft_result:
                self._responses.send_draft_result(message.chat.id, draft_result)
                return

        if starts_new_reminder:
            # 先 preview parse：若 parser 標記了 recurrence_error（例如「每個月 45 號」），
            # 走單輪拒絕流程 — 不清 pending draft/edit、不建新 draft、不追問，
            # 只送一句具體錯誤訊息讓使用者重打。
            parse_result = self._reminder_service.preview_parse(text, message, now)
            if parse_result.recurrence_error is not None:
                self._responses.send_recurrence_error(
                    message.chat.id, parse_result.recurrence_error
                )
                return

            draft = self._reminder_service.peek_draft(message.chat.id, actor_id, now)
            editing = self._reminder_service.peek_edit_session(message.chat.id, actor_id, now)
            # 重用 preview parse 的結果，保存成功後再清理舊對話。
            result = self._reminder_service.begin_create(
                text, message, now, parse_result=parse_result
            )
            cancelled = self._reminder_service.discard_conversation(draft, editing)
            edited = self._responses.dismiss_prompts(cancelled)
            # 若清了但 editMessage 沒全部成功（例如原訊息已刪或超過 48h），退回附上 inline notice。
            notice = (
                "（已取消上一個未完成的提醒）\n" if cancelled and edited < len(cancelled) else None
            )
            self._responses.send_draft_result(message.chat.id, result, notice=notice)

    def _handle_ai_message(
        self, message: TelegramMessage, text: str, now: datetime, triggered: bool
    ) -> None:
        actor_id = message.from_user.id
        draft = self._reminder_service.peek_draft(message.chat.id, actor_id, now)
        editing = self._reminder_service.peek_edit_session(message.chat.id, actor_id, now)
        if editing and not triggered:
            result = self._reminder_service.continue_edit(message, now)
            if result:
                self._responses.send_edit_result(message.chat.id, result, user_id=actor_id)
            return
        if not (message.chat.type == "private" or triggered or draft):
            return
        parsed = self._reminder_service.parse_ai(text, message, now, context=draft)
        if parsed.raw.get("intent") == "not_reminder":
            if (triggered or draft) and parsed.raw.get("question"):
                self._client.send_message(message.chat.id, str(parsed.raw["question"]))
            return
        source = draft.source_text if draft and parsed.raw.get("intent") == "update" else text
        result = self._reminder_service.begin_create(source, message, now, parse_result=parsed)
        # 新草稿保存成功後才清理舊對話，寫入失敗仍能重試原草稿。
        cancelled = self._reminder_service.discard_conversation(draft, editing)
        self._responses.dismiss_prompts(cancelled)
        self._responses.send_draft_result(message.chat.id, result)

    def _should_treat_as_reminder(self, message: TelegramMessage, text: str) -> bool:
        """判斷非指令文字是否應進入建立提醒流程。"""
        if message.chat.type == "private":
            return "提醒" in text

        if self._is_bot_mentioned(message):
            return True

        if not self._reminder_service.is_group_natural_language_enabled(message.chat.id):
            return False

        return looks_like_reminder_request(text)

    def _is_bot_mentioned(self, message: TelegramMessage) -> bool:
        """判斷群組訊息是否明確以 @bot 開頭呼叫 Remindly。"""
        return bool(
            self._bot_username
            and message.text.strip().lower().startswith(f"@{self._bot_username.lower()}")
        )
