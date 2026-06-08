from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from remindly.bot.response_sender import ResponseSender
from remindly.reminders.callback_data import (
    CallbackData,
    CallbackDataError,
    parse_reminder_callback,
)
from remindly.reminders.service import ReminderListFilter, ReminderService
from remindly.telegram.client import TelegramClient
from remindly.telegram.models import TelegramCallbackQuery

CallbackHandler = Callable[["CallbackContext"], None]


@dataclass(frozen=True)
class CallbackContext:
    callback: TelegramCallbackQuery
    data: CallbackData
    chat_id: int
    message_id: int | None
    now: datetime


class CallbackHandlers:
    def __init__(
        self,
        client: TelegramClient,
        reminder_service: ReminderService,
        responses: ResponseSender,
    ) -> None:
        self._client = client
        self._reminder_service = reminder_service
        self._responses = responses
        self._handlers: dict[str, CallbackHandler] = {
            "noop": self._noop,
            "list": self._list,
            "view": self._view,
            "delete_confirm": self._delete_confirm,
            "edit_cancel": self._edit_cancel,
            "delete": self._delete,
            "edit_time": self._edit_time,
            "edit_title": self._edit_title,
            "confirm": self._confirm,
            "discard": self._discard,
            "time": self._time,
            "snooze": self._snooze,
        }

    def handle(self, callback: TelegramCallbackQuery, now: datetime) -> None:
        """解析 inline keyboard callback，並分派到對應的互動流程。"""
        self._reminder_service.record_callback_context(callback, now)
        try:
            callback_data = parse_reminder_callback(callback.data)
        except CallbackDataError:
            self._client.answer_callback_query(callback.id, "這個操作已失效。")
            return

        chat_id = callback.message.chat.id if callback.message else None
        message_id = callback.message.id if callback.message else None
        if chat_id is None:
            self._client.answer_callback_query(callback.id, "找不到訊息 context。")
            return

        context = CallbackContext(
            callback=callback,
            data=callback_data,
            chat_id=chat_id,
            message_id=message_id,
            now=now,
        )
        handler = self._handlers.get(callback_data.action)
        if handler is None:
            self._client.answer_callback_query(callback.id, "未知操作。")
            return

        handler(context)

    def _noop(self, context: CallbackContext) -> None:
        """處理純標題列按鈕，僅關閉 Telegram 的 loading 狀態。"""
        self._client.answer_callback_query(context.callback.id)

    def _list(self, context: CallbackContext) -> None:
        """從詳情頁返回列表，並保持 viewer 視角顯示「你」。"""
        self._client.answer_callback_query(context.callback.id)
        self._responses.send_reminder_list(
            context.chat_id,
            viewer_user_id=context.callback.from_user.id,
            now=context.now,
            active_filter=parse_list_filter(context.data.value),
            edit_message_id=context.message_id,
        )

    def _view(self, context: CallbackContext) -> None:
        """顯示使用者在列表中點選的提醒詳情。"""
        self._client.answer_callback_query(context.callback.id)
        self._responses.show_reminder_detail(
            context.chat_id,
            context.data.target_id,
            edit_message_id=context.message_id,
        )

    def _delete_confirm(self, context: CallbackContext) -> None:
        """進入刪除確認頁，真正刪除由 delete action 執行。"""
        self._client.answer_callback_query(context.callback.id)
        self._responses.show_delete_confirmation(
            context.chat_id,
            context.data.target_id,
            edit_message_id=context.message_id,
        )

    def _edit_cancel(self, context: CallbackContext) -> None:
        """取消目前使用者在此聊天室中的修改 session，並回到提醒詳情。"""
        self._reminder_service.cancel_edit(context.chat_id, context.callback.from_user.id)
        self._client.answer_callback_query(context.callback.id, "已取消修改")
        self._responses.show_reminder_detail(
            context.chat_id,
            context.data.target_id,
            edit_message_id=context.message_id,
        )

    def _delete(self, context: CallbackContext) -> None:
        """刪除提醒；service 會檢查提醒存在、狀態與建立者權限。"""
        deleted = (
            self._reminder_service.cancel(
                context.chat_id,
                context.data.target_id,
                context.callback.from_user.id,
            )
            is not None
        )
        self._client.answer_callback_query(
            context.callback.id,
            "已刪除" if deleted else "找不到可刪除的提醒",
        )
        self._responses.show_delete_result(
            context.chat_id,
            context.message_id,
            context.data.target_id,
            deleted,
        )

    def _edit_time(self, context: CallbackContext) -> None:
        """開始修改提醒時間，下一則使用者訊息會被當成新時間解析。"""
        self._begin_edit(context, "time")

    def _edit_title(self, context: CallbackContext) -> None:
        """開始修改提醒內容，下一則使用者訊息會被當成新內容。"""
        self._begin_edit(context, "title")

    def _confirm(self, context: CallbackContext) -> None:
        """確認草稿並建立正式提醒，成功後刪除原本的確認訊息。"""
        result = self._reminder_service.confirm(
            context.data.target_id,
            context.callback.from_user.id,
            context.now,
        )
        if not result:
            self._client.answer_callback_query(context.callback.id, "提醒草稿已過期。")
            return

        self._client.answer_callback_query(context.callback.id, "已建立")
        if context.message_id is not None:
            self._responses.delete_message_quietly(context.chat_id, context.message_id)
        self._responses.send_created(context.chat_id, result.reminder)

    def _discard(self, context: CallbackContext) -> None:
        """取消提醒草稿；若原確認訊息仍在，就一併刪除。"""
        discarded = self._reminder_service.discard_draft(
            context.data.target_id,
            context.callback.from_user.id,
            context.now,
        )
        self._client.answer_callback_query(
            context.callback.id,
            "已取消" if discarded else "草稿已過期",
        )
        if discarded and context.message_id is not None:
            self._responses.delete_message_quietly(context.chat_id, context.message_id)
        elif discarded:
            self._client.send_message(context.chat_id, "已取消建立提醒。")

    def _time(self, context: CallbackContext) -> None:
        """套用快捷時間按鈕，例如 09:00、12:00、15:00、18:00。"""
        if context.data.value is None:
            self._client.answer_callback_query(context.callback.id, "未知操作。")
            return

        result = self._reminder_service.apply_quick_time(
            context.data.target_id,
            context.data.value,
            context.callback.from_user.id,
            context.now,
        )
        if not result:
            self._client.answer_callback_query(context.callback.id, "草稿已過期")
            return

        self._client.answer_callback_query(context.callback.id, "已套用時間")
        # 使用者已選完快捷時間，原本的追問按鈕訊息就不需要留在聊天室。
        if context.message_id is not None:
            self._responses.delete_message_quietly(context.chat_id, context.message_id)
        self._responses.send_draft_result(context.chat_id, result)

    def _snooze(self, context: CallbackContext) -> None:
        """延後已送出的提醒，並把原到期訊息更新成延後結果。"""
        if context.data.value is None:
            self._client.answer_callback_query(context.callback.id, "未知操作。")
            return

        result = self._reminder_service.snooze(
            context.chat_id,
            context.data.target_id,
            context.callback.from_user.id,
            context.data.value,
            context.now,
        )
        if not result:
            self._client.answer_callback_query(context.callback.id, "找不到可延後的提醒")
            return

        self._client.answer_callback_query(context.callback.id, "已延後")
        self._responses.show_snooze_result(context.chat_id, context.message_id, result)

    def _begin_edit(self, context: CallbackContext, field: str) -> None:
        """建立修改 session，讓下一則訊息可以安全地接續到指定欄位。"""
        prompt = self._reminder_service.begin_edit(
            context.chat_id,
            context.data.target_id,
            context.callback.from_user.id,
            field,
            context.now,
        )
        if not prompt:
            self._client.answer_callback_query(context.callback.id, "找不到可修改的提醒")
            return

        self._client.answer_callback_query(context.callback.id, "請輸入新值")
        self._responses.show_edit_prompt(context.chat_id, context.message_id, prompt)


def parse_list_filter(value: str | None) -> ReminderListFilter:
    if value is None:
        return ReminderListFilter.ALL

    try:
        return ReminderListFilter(value)
    except ValueError:
        return ReminderListFilter.ALL
