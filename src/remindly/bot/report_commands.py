from __future__ import annotations

import re
from datetime import datetime

from remindly.bot.response_sender import ResponseSender
from remindly.reminders.callback_data import CallbackData, CallbackDataError
from remindly.reminders.reports import (
    ATTACHMENT_FIELDS,
    PARSER_SOURCES,
    REPORT_REASONS,
    parser_source,
    report_attachment,
)
from remindly.reminders.text import command_body
from remindly.storage.reports import ReportStore
from remindly.telegram.client import TelegramClient
from remindly.telegram.models import TelegramCallbackQuery, TelegramMessage

_UNAVAILABLE = "這張確認卡已失效，或你不是草稿建立者。"
_REPORT_TARGET = re.compile(r"draft_[A-Za-z0-9_-]{16}_[0-9a-f]{10}")


class ReportCommands:
    def __init__(
        self, client: TelegramClient, store: ReportStore, responses: ResponseSender
    ) -> None:
        self._client = client
        self._store = store
        self._responses = responses

    def handle_start(
        self, message: TelegramMessage, text: str, bot_username: str | None, now: datetime
    ) -> bool:
        body = command_body(text, "start", bot_username)
        if body is None or not body.startswith("report_"):
            return False
        target = body.removeprefix("report_")
        if message.chat.type != "private" or message.from_user is None:
            return True
        draft = None
        if _REPORT_TARGET.fullmatch(target):
            draft_id, revision = target.rsplit("_", 1)
            draft = self._store.get_draft(draft_id, revision, message.from_user.id, now)
        if draft is None:
            self._client.send_message(message.chat.id, _UNAVAILABLE)
        else:
            self._show_reasons(message.chat.id, None, target)
        return True

    def handle_callback(self, callback: TelegramCallbackQuery, now: datetime) -> bool:
        if not callback.data.startswith("report:"):
            return False
        message = callback.message
        if message is None or message.chat.type != "private":
            self._client.answer_callback_query(callback.id, "請在 Bot 私聊中回報。")
            return True
        try:
            data = CallbackData.decode(callback.data)
        except CallbackDataError:
            self._client.answer_callback_query(callback.id, _UNAVAILABLE)
            return True
        if not _REPORT_TARGET.fullmatch(data.target_id):
            self._client.answer_callback_query(callback.id, _UNAVAILABLE)
            return True

        draft_id, revision = data.target_id.rsplit("_", 1)
        user_id = callback.from_user.id
        username = callback.from_user.username
        chat_id, message_id = message.chat.id, message.id
        reason, _, attach = (data.value or "").partition(",")
        if data.action == "send_id" and reason in REPORT_REASONS and attach in {"0", "1"}:
            result = self._store.submit(
                draft_id,
                revision,
                user_id,
                now,
                reporter_username=username,
                reason=reason,
                include_attachment=attach == "1",
            )
            if result is None:
                self._client.answer_callback_query(callback.id, _UNAVAILABLE)
                return True
            self._client.answer_callback_query(callback.id)
            text = "已收到回報。" if result else "這張確認卡已回報過。"
            self._responses.edit_or_send(
                chat_id,
                message_id,
                text + "\n你可以回原本的聊天確認或取消提醒。",
                reply_markup={"inline_keyboard": []},
            )
            return True

        draft = self._store.get_draft(draft_id, revision, user_id, now)
        if draft is None:
            self._client.answer_callback_query(callback.id, _UNAVAILABLE)
            return True

        self._client.answer_callback_query(callback.id)
        if data.action == "back":
            self._show_reasons(chat_id, message_id, data.target_id)
        # A send button issued before the disclosure must open the updated preview first.
        elif (
            data.action in {"preview", "send"} and reason in REPORT_REASONS and attach in {"0", "1"}
        ):
            included = attach == "1"
            lines = [
                "送出回報前確認",
                f"原因：{REPORT_REASONS[reason]}",
                f"解析來源：{PARSER_SOURCES[parser_source(draft)]}",
                f"Bot 版本：{draft.parse_result['report_bot_version']}",
            ]
            if parser_source(draft) == "ai":
                lines.append(
                    f"供應商／模型：{draft.parse_result.get('provider') or '未知'}"
                    f"／{draft.parse_result.get('model') or '未知'}"
                )
            if included:
                lines.extend(
                    f"{ATTACHMENT_FIELDS[field]}：{value}"
                    for field, value in report_attachment(draft).items()
                )
            else:
                lines.append("目前未附原句和解析結果。")
            lines.extend(
                [
                    "",
                    "送出時會記錄下列身分資訊，維護者可在回報詳情查看。",
                    f"Telegram user ID：{user_id}",
                    f"Telegram @username：{'@' + username if username else '未設定'}",
                ]
            )
            self._responses.edit_or_send(
                chat_id,
                message_id,
                "\n".join(lines),
                reply_markup={
                    "inline_keyboard": [
                        [
                            _button(
                                "移除附件" if included else "附上原句與結果",
                                "preview",
                                data.target_id,
                                f"{reason},{int(not included)}",
                            )
                        ],
                        [
                            _button("返回", "back", data.target_id),
                            _button("送出回報", "send_id", data.target_id, f"{reason},{attach}"),
                        ],
                    ]
                },
            )
        return True

    def _show_reasons(self, chat_id: int, message_id: int | None, target: str) -> None:
        buttons = [
            _button(label, "preview", target, f"{reason},0")
            for reason, label in REPORT_REASONS.items()
        ]
        self._responses.edit_or_send(
            chat_id,
            message_id,
            "回報解析問題\n哪裡解析錯了？",
            reply_markup={"inline_keyboard": [buttons[:2], buttons[2:4], buttons[4:]]},
        )


def _button(text: str, action: str, target: str, value: str | None = None) -> dict[str, str]:
    return {"text": text, "callback_data": CallbackData("report", action, target, value).encode()}
