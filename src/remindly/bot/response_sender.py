from __future__ import annotations

import logging
from datetime import datetime

from remindly.reminders.models import Reminder
from remindly.reminders.renderer import (
    ReminderRenderer,
    back_to_list_keyboard,
    confirmation_keyboard,
    delete_confirmation_keyboard,
    edit_cancel_keyboard,
    groupmode_keyboard,
    quick_time_keyboard,
    reminder_actions_keyboard,
    reminder_list_keyboard,
    render_groupmode_panel,
)
from remindly.reminders.service import (
    Confirmation,
    DraftPrompt,
    EditPrompt,
    EditResult,
    ExpiredPrompt,
    ReminderListFilter,
    ReminderService,
    SnoozeResult,
)
from remindly.reminders.text import html_escape
from remindly.telegram.client import TelegramApiError, TelegramClient

LOGGER = logging.getLogger(__name__)


class ResponseSender:
    def __init__(
        self,
        client: TelegramClient,
        reminder_service: ReminderService,
        renderer: ReminderRenderer,
    ) -> None:
        self._client = client
        self._reminder_service = reminder_service
        self._renderer = renderer

    def send_reminder_list(
        self,
        chat_id: int,
        *,
        viewer_user_id: int | None,
        now: datetime,
        active_filter: ReminderListFilter = ReminderListFilter.ALL,
        edit_message_id: int | None = None,
    ) -> None:
        """取得未到期提醒，並用可點擊列表回覆或更新原 callback 訊息。"""
        groups = self._reminder_service.list_pending_grouped(
            chat_id,
            viewer_user_id,
            now,
            active_filter,
        )
        self.edit_or_send(
            chat_id,
            edit_message_id,
            self._renderer.render_grouped_list(groups, active_filter),
            parse_mode="HTML",
            reply_markup=reminder_list_keyboard(groups, active_filter),
        )

    def show_reminder_detail(
        self,
        chat_id: int,
        short_id: str,
        *,
        edit_message_id: int | None,
    ) -> None:
        """顯示單一提醒詳情，並附上修改、刪除、返回列表按鈕。"""
        details = self._reminder_service.get_details(chat_id, short_id)
        if not details:
            self.edit_or_send(
                chat_id,
                edit_message_id,
                "找不到這個未到期提醒。",
                reply_markup=back_to_list_keyboard(),
            )
            return

        self.edit_or_send(
            chat_id,
            edit_message_id,
            self._renderer.render_detail(details.reminder, details.participants),
            parse_mode="HTML",
            reply_markup=reminder_actions_keyboard(details.reminder.short_id),
        )

    def show_delete_confirmation(
        self,
        chat_id: int,
        short_id: str,
        *,
        edit_message_id: int | None,
    ) -> None:
        """在真正刪除前顯示提醒摘要，避免使用者誤觸刪除按鈕。"""
        details = self._reminder_service.get_details(chat_id, short_id)
        if not details:
            self.edit_or_send(
                chat_id,
                edit_message_id,
                "找不到這個未到期提醒。",
                reply_markup=back_to_list_keyboard(),
            )
            return

        self.edit_or_send(
            chat_id,
            edit_message_id,
            "\n".join(
                [
                    "確認刪除這個提醒？",
                    "",
                    self._renderer.render_detail(details.reminder, details.participants),
                ]
            ),
            parse_mode="HTML",
            reply_markup=delete_confirmation_keyboard(details.reminder.short_id),
        )

    def show_edit_prompt(
        self,
        chat_id: int,
        message_id: int | None,
        prompt: EditPrompt,
        *,
        user_id: int | None = None,
    ) -> None:
        """提示使用者輸入新值，並保留取消修改的 inline button。
        送出後把 message_id 綁回 edit session，供 sweep 時 editMessage 用。"""
        text = (
            f"{prompt.question}\n\n"
            f"提醒：{html_escape(prompt.reminder.short_id)}｜{html_escape(prompt.reminder.title)}"
        )
        sent_id = self.edit_or_send(
            chat_id,
            message_id,
            text,
            parse_mode="HTML",
            reply_markup=edit_cancel_keyboard(prompt.reminder.short_id),
        )
        if sent_id is not None and user_id is not None:
            self._reminder_service.set_edit_prompt_message_id(chat_id, user_id, sent_id)

    def show_groupmode_panel(
        self,
        chat_id: int,
        enabled: bool,
        *,
        edit_message_id: int | None = None,
    ) -> None:
        """顯示群組自然語言模式狀態、用途說明與切換按鈕。"""
        self.edit_or_send(
            chat_id,
            edit_message_id,
            render_groupmode_panel(enabled),
            reply_markup=groupmode_keyboard(enabled),
        )

    def send_draft_result(
        self,
        chat_id: int,
        result: DraftPrompt | Confirmation,
        *,
        notice: str | None = None,
    ) -> None:
        """依草稿狀態回覆追問問題，或送出建立前的確認卡。
        `notice` 用來附加訊息前綴（例如覆蓋舊 draft 時的取消提示）。
        送出後把 message_id 綁回 draft，供 sweep 過期時 editMessage 用。"""
        if isinstance(result, Confirmation):
            message_id = self._client.send_message(
                chat_id,
                _prepend_notice(self._renderer.render_confirmation(result.draft), notice),
                parse_mode="HTML",
                reply_markup=confirmation_keyboard(result.draft.id),
            )
        else:
            message_id = self._client.send_message(
                chat_id,
                _prepend_notice(result.question, notice),
                reply_markup=(
                    quick_time_keyboard(result.draft.id) if result.wants_quick_time else None
                ),
            )
        if message_id is not None:
            self._reminder_service.set_draft_prompt_message_id(result.draft.id, message_id)

    def send_edit_result(
        self,
        chat_id: int,
        result: EditPrompt | EditResult,
        *,
        user_id: int | None = None,
    ) -> None:
        """依修改流程狀態回覆下一次追問，或顯示更新後的提醒詳情。
        當 result 為 EditPrompt 且已知 user_id 時，把 message_id 綁回 edit session。"""
        if isinstance(result, EditPrompt):
            message_id = self._client.send_message(
                chat_id,
                result.question,
                reply_markup=edit_cancel_keyboard(result.reminder.short_id),
            )
            if message_id is not None and user_id is not None:
                self._reminder_service.set_edit_prompt_message_id(chat_id, user_id, message_id)
            return

        self._client.send_message(
            chat_id,
            "\n".join(
                [
                    result.message,
                    "",
                    self._renderer.render_detail(
                        result.details.reminder,
                        result.details.participants,
                    ),
                ]
            ),
            parse_mode="HTML",
            reply_markup=reminder_actions_keyboard(result.details.reminder.short_id),
        )

    def send_created(self, chat_id: int, reminder: Reminder) -> None:
        """建立成功後送出最終訊息，讓確認訊息可以先被刪除。"""
        self._client.send_message(
            chat_id,
            self._renderer.render_created(reminder),
            parse_mode="HTML",
        )

    def show_snooze_result(
        self,
        chat_id: int,
        message_id: int | None,
        result: SnoozeResult,
    ) -> None:
        """把到期提醒訊息更新成已延後狀態，並清空延後按鈕避免重複觸發。"""
        self.edit_or_send(
            chat_id,
            message_id,
            self._renderer.render_snoozed(result.reminder),
            parse_mode="HTML",
            reply_markup={"inline_keyboard": []},
        )

    def show_skip_next_result(
        self,
        chat_id: int,
        message_id: int | None,
        result: SnoozeResult,
    ) -> None:
        """把週期性提醒的到期訊息更新成「已跳過下次」並清空按鈕。"""
        self.edit_or_send(
            chat_id,
            message_id,
            self._renderer.render_skipped_next(result.reminder),
            parse_mode="HTML",
            reply_markup={"inline_keyboard": []},
        )

    def show_series_cancelled(
        self,
        chat_id: int,
        message_id: int | None,
        reminder: Reminder,
    ) -> None:
        """把週期性提醒的到期訊息更新成「已取消整個系列」並清空按鈕。"""
        self.edit_or_send(
            chat_id,
            message_id,
            self._renderer.render_series_cancelled(reminder),
            parse_mode="HTML",
            reply_markup={"inline_keyboard": []},
        )

    def show_delete_result(
        self,
        chat_id: int,
        message_id: int | None,
        short_id: str,
        deleted: bool,
    ) -> None:
        """把刪除結果更新在原訊息上；無原訊息時退回 sendMessage。"""
        text = (
            f"已刪除提醒 {html_escape(short_id)}"
            if deleted
            else "找不到可刪除的提醒，或你不是建立者。"
        )
        self.edit_or_send(
            chat_id,
            message_id,
            text,
            parse_mode="HTML",
            reply_markup=back_to_list_keyboard(),
        )

    def edit_or_send(
        self,
        chat_id: int,
        message_id: int | None,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, object] | None = None,
    ) -> int | None:
        """優先編輯 callback 原訊息；編輯失敗時改送新訊息避免流程中斷。
        回傳最終訊息的 message_id（新送或已編輯的），供 caller 綁定 session。"""
        if message_id is None:
            return self._client.send_message(
                chat_id,
                text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )

        try:
            self._client.edit_message_text(
                chat_id,
                message_id,
                text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
            return message_id
        except TelegramApiError:
            LOGGER.exception("Failed to edit Telegram message; falling back to sendMessage")
            return self._client.send_message(
                chat_id,
                text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )

    def dismiss_prompts(self, prompts: list[ExpiredPrompt]) -> int:
        """把過期或被覆蓋的追問訊息 editMessage 標為已取消並清掉 inline 按鈕。
        回傳成功 edit 的數量，讓 caller 決定是否需要 inline notice 作為後備。"""
        succeeded = 0
        for prompt in prompts:
            try:
                self._client.edit_message_text(
                    prompt.chat_id,
                    prompt.message_id,
                    prompt.text,
                    reply_markup={"inline_keyboard": []},
                )
                succeeded += 1
            except TelegramApiError:
                LOGGER.exception(
                    "Failed to dismiss prompt %s in chat %s",
                    prompt.message_id,
                    prompt.chat_id,
                )
        return succeeded

    def delete_message_quietly(self, chat_id: int, message_id: int) -> None:
        """盡力刪除流程中的暫時訊息；失敗只記錄 log，不阻斷主流程。"""
        try:
            self._client.delete_message(chat_id, message_id)
        except TelegramApiError:
            LOGGER.exception("Failed to delete Telegram message %s in chat %s", message_id, chat_id)


def _prepend_notice(text: str, notice: str | None) -> str:
    if not notice:
        return text
    return f"{notice}{text}"
