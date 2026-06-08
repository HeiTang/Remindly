from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfoNotFoundError

from remindly.bot.groupmode import GROUP_CHAT_TYPES, GroupModeAuthorizer
from remindly.bot.response_sender import ResponseSender
from remindly.reminders.service import ReminderService
from remindly.reminders.text import command_body, html_escape
from remindly.telegram.client import TelegramClient
from remindly.telegram.models import TelegramMessage

CommandHandler = Callable[[TelegramMessage, str, datetime], None]


class CommandHandlers:
    def __init__(
        self,
        client: TelegramClient,
        reminder_service: ReminderService,
        responses: ResponseSender,
        *,
        bot_username: str | None,
    ) -> None:
        self._client = client
        self._reminder_service = reminder_service
        self._responses = responses
        self._bot_username = bot_username
        self._groupmode_authorizer = GroupModeAuthorizer(client)
        self._handlers: dict[str, CommandHandler] = {
            "start": self._start,
            "help": self._help,
            "remind": self._remind,
            "list": self._list,
            "cancel": self._cancel,
            "timezone": self._timezone,
            "groupmode": self._groupmode,
        }

    def handle(self, message: TelegramMessage, text: str, now: datetime) -> bool:
        """解析 slash command；若命中指令就執行並回傳 True。"""
        for command, handler in self._handlers.items():
            body = command_body(text, command, self._bot_username)
            if body is None:
                continue
            handler(message, body, now)
            return True

        if text.startswith("/"):
            self._client.send_message(message.chat.id, "未知指令。用 /help 看目前支援的東西。")
            return True

        return False

    def _start(self, message: TelegramMessage, body: str, now: datetime) -> None:
        """送出第一次互動的簡短範例，讓使用者知道可直接用自然語言建立提醒。"""
        del body, now
        self._client.send_message(
            message.chat.id,
            "\n".join(
                [
                    "嗨，我可以幫你建立提醒。",
                    "",
                    "範例：",
                    "/remind 明天下午三點提醒我倒垃圾",
                    "明天 15:00 提醒我和 @someone 開會",
                ]
            ),
        )

    def _help(self, message: TelegramMessage, body: str, now: datetime) -> None:
        """列出目前支援的 Bot 指令。"""
        del body, now
        self._client.send_message(
            message.chat.id,
            "\n".join(
                [
                    "/start - 開始",
                    "/help - 看指令",
                    "/remind - 建立提醒",
                    "/list - 列出未到期提醒",
                    "/cancel <id> - 取消提醒",
                    "/timezone <IANA timezone> - 設定時區",
                    "/groupmode on|off|status - 群組自然語言模式",
                ]
            ),
        )

    def _remind(self, message: TelegramMessage, body: str, now: datetime) -> None:
        """用 /remind 的內容開始建立提醒；資訊不足時交由 service 產生追問。"""
        if not body:
            self._client.send_message(
                message.chat.id,
                "要提醒什麼？例如：/remind 明天下午三點提醒我倒垃圾",
            )
            return

        result = self._reminder_service.begin_create(body, message, now)
        self._responses.send_draft_result(message.chat.id, result)

    def _list(self, message: TelegramMessage, body: str, now: datetime) -> None:
        """列出目前聊天室的未到期提醒，並依建立者分組。"""
        del body
        viewer_user_id = message.from_user.id if message.from_user else None
        self._responses.send_reminder_list(
            message.chat.id,
            viewer_user_id=viewer_user_id,
            now=now,
        )

    def _cancel(self, message: TelegramMessage, body: str, now: datetime) -> None:
        """用提醒 short id 取消提醒，只允許建立者取消。"""
        del now
        actor = message.from_user
        if actor is None:
            return

        short_id = body.strip()
        if not short_id:
            self._client.send_message(message.chat.id, "用法：/cancel R-8F3K")
            return

        reminder = self._reminder_service.cancel(message.chat.id, short_id, actor.id)
        if not reminder:
            self._client.send_message(message.chat.id, "找不到可取消的提醒，或你不是建立者。")
            return

        self._client.send_message(
            message.chat.id,
            f"已取消提醒 {html_escape(reminder.short_id)}",
            parse_mode="HTML",
        )

    def _timezone(self, message: TelegramMessage, body: str, now: datetime) -> None:
        """設定使用者偏好的 IANA timezone，後續自然語言時間會以此解析。"""
        del now
        actor = message.from_user
        if actor is None:
            return

        timezone = body.strip()
        if not timezone:
            self._client.send_message(message.chat.id, "用法：/timezone Asia/Taipei")
            return

        try:
            self._reminder_service.set_timezone(actor.id, timezone)
        except ZoneInfoNotFoundError:
            self._client.send_message(
                message.chat.id,
                "找不到這個時區。請使用 IANA timezone，例如 Asia/Taipei。",
            )
            return

        self._client.send_message(
            message.chat.id,
            f"已設定時區：{html_escape(timezone)}",
            parse_mode="HTML",
        )

    def _groupmode(self, message: TelegramMessage, body: str, now: datetime) -> None:
        """查看或切換群組自然語言模式，讓群組可選擇是否吃一般文字提醒。"""
        if message.chat.type not in GROUP_CHAT_TYPES:
            self._client.send_message(message.chat.id, "這個設定只能在群組使用。")
            return

        action = body.strip().lower() or "status"
        if action == "status":
            self._send_groupmode_panel(message.chat.id)
            return

        if action not in {"on", "off"}:
            self._client.send_message(message.chat.id, "用法：/groupmode on|off|status")
            return

        actor = message.from_user
        if actor is None:
            return

        can_change = self._is_group_admin(message.chat.id, actor.id)
        if can_change is None:
            self._client.send_message(message.chat.id, "無法確認你的群組權限，先不變更設定。")
            return
        if not can_change:
            self._client.send_message(message.chat.id, "只有群組管理員可以切換自然語言模式。")
            return

        enabled = action == "on"
        self._reminder_service.set_group_natural_language_enabled(
            message.chat.id,
            enabled,
            actor.id,
            now,
        )
        self._send_groupmode_panel(message.chat.id)

    def _send_groupmode_panel(self, chat_id: int) -> None:
        """回覆目前群組自然語言模式狀態與切換按鈕。"""
        enabled = self._reminder_service.is_group_natural_language_enabled(chat_id)
        self._responses.show_groupmode_panel(chat_id, enabled)

    def _is_group_admin(self, chat_id: int, user_id: int) -> bool | None:
        """向 Telegram 查詢操作者是否為群組管理員；查詢失敗回傳 None。"""
        return self._groupmode_authorizer.can_change_group_settings(chat_id, user_id)
