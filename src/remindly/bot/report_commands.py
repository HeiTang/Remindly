from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime

from remindly.bot.response_sender import ResponseSender
from remindly.reminders.callback_data import CallbackData, CallbackDataError
from remindly.reminders.models import ReminderDraft
from remindly.reminders.reports import (
    ATTACHMENT_FIELDS,
    EXPECTED_RESULT_LIMIT,
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


@dataclass
class ReportSession:
    target: str
    reason: str
    expires_at: datetime
    expected_result: str | None = None
    waiting: bool = False
    token: str = ""


class ReportCommands:
    def __init__(
        self, client: TelegramClient, store: ReportStore, responses: ResponseSender
    ) -> None:
        self._client = client
        self._store = store
        self._responses = responses
        # shortcut: unsent supplements are in memory; persist only if restart recovery is needed.
        self._sessions: dict[int, ReportSession] = {}

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
            self._sessions.pop(message.from_user.id, None)
            self._show_reasons(message.chat.id, None, target)
        return True

    def _expire_sessions(self, now: datetime) -> None:
        self._sessions = {
            key: value
            for key, value in self._sessions.items()
            if value.expires_at > now or value.waiting
        }

    def handle_text(self, message: TelegramMessage, now: datetime) -> bool:
        self._expire_sessions(now)
        if message.chat.type != "private" or message.from_user is None:
            return False
        user = message.from_user
        state = self._sessions.get(user.id)
        if state is None or not state.waiting:
            return False
        text = message.text or ""
        if state.expires_at <= now:
            self._sessions.pop(user.id, None)
            self._client.send_message(message.chat.id, _UNAVAILABLE)
            return True
        if text.startswith("/"):
            self._sessions.pop(user.id, None)
            return False
        draft_id, revision = state.target.rsplit("_", 1)
        draft = self._store.get_draft(draft_id, revision, user.id, now)
        if draft is None:
            self._sessions.pop(user.id, None)
            self._client.send_message(message.chat.id, _UNAVAILABLE)
            return True
        if not text.strip() or len(text) > EXPECTED_RESULT_LIMIT:
            self._client.send_message(message.chat.id, "請輸入 1～500 字的預期結果，不會自動截短。")
            return True
        state.expected_result = text.strip()
        state.waiting = False
        self._show_preview(message.chat.id, None, user.id, user.username, draft, state)
        return True

    def handle_callback(self, callback: TelegramCallbackQuery, now: datetime) -> bool:
        if not callback.data.startswith("report:"):
            return False
        self._expire_sessions(now)
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
        user = callback.from_user
        draft = self._store.get_draft(draft_id, revision, user.id, now)
        if draft is None:
            self._client.answer_callback_query(callback.id, _UNAVAILABLE)
            return True
        chat_id, message_id = message.chat.id, message.id
        reason, _, attach = (data.value or "").partition(",")
        state = self._sessions.get(user.id)
        if data.action in {"submit", "expect", "remove", "cancel"}:
            if (
                state is None
                or state.target != data.target_id
                or state.token != data.value
                or (state.waiting and data.action != "cancel")
            ):
                self._client.answer_callback_query(callback.id, "回報預覽已更新，請使用最新畫面。")
                return True
            if data.action == "submit":
                result = self._store.submit(
                    draft_id,
                    revision,
                    user.id,
                    now,
                    reporter_username=user.username,
                    reason=state.reason,
                    expected_result=state.expected_result,
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
            if data.action == "expect":
                state.waiting = True
                state.token = secrets.token_hex(4)
                prompts = {
                    "time": "你原本希望在什麼時間提醒？",
                    "title": "正確的提醒事項是什麼？",
                    "participant": "你原本希望提醒誰？",
                    "recurrence": "你希望如何重複？例如：只提醒一次、每天、每週五。",
                    "other": "你希望 Bot 如何處理這句話？",
                }
                self._client.answer_callback_query(callback.id)
                self._responses.edit_or_send(
                    chat_id,
                    message_id,
                    prompts[state.reason]
                    + "\n請用下一則文字回覆，最多 500 字；只補充回報，不會修改提醒。",
                    reply_markup={
                        "inline_keyboard": [
                            [_button("取消補充", "cancel", state.target, state.token)]
                        ]
                    },
                )
                return True
            if data.action == "remove":
                state.expected_result = None
            state.waiting = False
        elif data.action == "back":
            if state is not None:
                state.waiting = False
                state.token = secrets.token_hex(4)
            self._client.answer_callback_query(callback.id)
            self._show_reasons(chat_id, message_id, data.target_id)
            return True
        elif (
            data.action in {"preview", "send", "send_id"}
            and reason in REPORT_REASONS
            and attach in {"0", "1"}
        ):
            # Old send buttons must open the current disclosure before submitting.
            if state is None or state.target != data.target_id:
                state = ReportSession(data.target_id, reason, draft.expires_at)
                self._sessions[user.id] = state
            state.reason = reason
            state.waiting = False
        else:
            self._client.answer_callback_query(callback.id, _UNAVAILABLE)
            return True
        self._client.answer_callback_query(callback.id)
        self._show_preview(chat_id, message_id, user.id, user.username, draft, state)
        return True

    def _show_preview(
        self,
        chat_id: int,
        message_id: int | None,
        user_id: int,
        username: str | None,
        draft: ReminderDraft,
        state: ReportSession,
    ) -> None:
        state.token = secrets.token_hex(4)
        lines = [
            "送出回報前確認",
            f"原因：{REPORT_REASONS[state.reason]}",
            f"解析來源：{PARSER_SOURCES[parser_source(draft)]}",
            f"Bot 版本：{draft.parse_result['report_bot_version']}",
        ]
        if parser_source(draft) == "ai":
            lines.append(
                f"供應商／模型：{draft.parse_result.get('provider') or '未知'}"
                f"／{draft.parse_result.get('model') or '未知'}"
            )
        lines.extend(["", "回報會包含以下原句與解析結果，供維護者排查問題。"])
        lines.extend(
            f"{ATTACHMENT_FIELDS[field]}：{value}"
            for field, value in report_attachment(draft).items()
        )
        lines.extend(
            [
                "",
                "使用者預期結果：" + (state.expected_result or "使用者未提供預期結果"),
                "",
                "送出時會記錄下列身分資訊，維護者可在回報詳情查看。",
                f"Telegram user ID：{user_id}",
                f"Telegram @username：{'@' + username if username else '未設定'}",
            ]
        )
        supplement = [
            _button(
                "修改預期結果" if state.expected_result else "補充預期結果",
                "expect",
                state.target,
                state.token,
            )
        ]
        if state.expected_result:
            supplement.append(_button("移除預期結果", "remove", state.target, state.token))
        text = "\n".join(lines)
        chunks = (
            [text[index : index + 2000] for index in range(0, len(text), 2000)]
            if len(text.encode("utf-16-le")) > 8192
            else [text]
        )
        for index, chunk in enumerate(chunks):
            self._responses.edit_or_send(
                chat_id,
                message_id if index == 0 else None,
                chunk,
                reply_markup={
                    "inline_keyboard": [
                        supplement,
                        [
                            _button("返回", "back", state.target),
                            _button("送出回報", "submit", state.target, state.token),
                        ],
                    ]
                    if index == len(chunks) - 1
                    else []
                },
            )

    def _show_reasons(self, chat_id: int, message_id: int | None, target: str) -> None:
        buttons = [
            _button(label, "preview", target, f"{reason},1")
            for reason, label in REPORT_REASONS.items()
        ]
        self._responses.edit_or_send(
            chat_id,
            message_id,
            "回報解析問題\n哪裡解析錯了？\n回報會包含原句與解析結果，送出前可預覽確認。",
            reply_markup={"inline_keyboard": [buttons[:2], buttons[2:4], buttons[4:]]},
        )


def _button(text: str, action: str, target: str, value: str | None = None) -> dict[str, str]:
    return {"text": text, "callback_data": CallbackData("report", action, target, value).encode()}
