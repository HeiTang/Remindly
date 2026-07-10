from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from remindly.reminders.recurrence import next_fire
from remindly.reminders.renderer import (
    ReminderRenderer,
    delivery_recurring_keyboard,
    delivery_snooze_keyboard,
)
from remindly.reminders.repositories import ReminderDeliveryRepository
from remindly.reminders.service import ExpiredPrompt
from remindly.telegram.client import TelegramApiError, TelegramClient

LOGGER = logging.getLogger(__name__)

EMPTY_INLINE_KEYBOARD: dict[str, object] = {"inline_keyboard": []}


class ExpiredPromptSweeper(Protocol):
    def sweep_expired_prompts(self, now: datetime) -> list[ExpiredPrompt]: ...


class ReminderScheduler:
    def __init__(
        self,
        repository: ReminderDeliveryRepository,
        client: TelegramClient,
        renderer: ReminderRenderer,
        *,
        timezone: str,
        interval_seconds: int,
        prompt_sweeper: ExpiredPromptSweeper | None = None,
    ) -> None:
        self._repository = repository
        self._client = client
        self._renderer = renderer
        self._timezone = timezone
        self._interval_seconds = interval_seconds
        self._prompt_sweeper = prompt_sweeper

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                self.tick()
            except Exception:
                LOGGER.exception("Reminder scheduler tick failed")
            stop_event.wait(self._interval_seconds)

    def tick(self) -> None:
        now = datetime.now(ZoneInfo(self._timezone))
        self._sweep_expired_prompts(now)
        due_reminders = self._repository.claim_due(now)
        for reminder in due_reminders:
            try:
                participants = self._repository.list_participants(reminder.id)
                # 週期性提醒到期只有兩個動作（跳過下次 / 取消整個系列），沒有延後
                # 按鈕，所以連 next_day_time_label 都省得算；一次性提醒才需要
                # 「明天 HH:MM」的標籤。
                if reminder.recurrence is not None:
                    reply_markup = delivery_recurring_keyboard(reminder.short_id)
                else:
                    next_day_time_label = reminder.remind_at.astimezone(
                        ZoneInfo(reminder.timezone)
                    ).strftime("%H:%M")
                    reply_markup = delivery_snooze_keyboard(
                        reminder.short_id, next_day_time_label
                    )
                self._client.send_message(
                    reminder.chat_id,
                    self._renderer.render_delivery(reminder, participants),
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
                if reminder.recurrence is not None:
                    # 週期性提醒：計算下次觸發並轉回 PENDING，不標 FIRED。
                    # `next_fire` 用 `after.tzinfo` 為基準構造 HH:MM，因此必須先把 `now`
                    # 轉到 reminder 自己的時區，否則使用者設定「每天 09:00」在 scheduler
                    # 主機時區跟 reminder 時區不同時會漂到 scheduler 時區的 09:00。
                    reminder_now = now.astimezone(ZoneInfo(reminder.timezone))
                    next_at = next_fire(reminder.recurrence, reminder_now)
                    self._repository.reschedule(reminder.id, next_at, now)
                else:
                    self._repository.mark_fired(reminder.id, now)
            except Exception:
                LOGGER.exception("Failed to send reminder %s", reminder.short_id)
                self._repository.mark_failed(reminder.id, datetime.now(ZoneInfo(self._timezone)))

    def _sweep_expired_prompts(self, now: datetime) -> None:
        """把過期的追問/確認訊息 editMessage 標記為已過期，並清掉 inline 按鈕。"""
        if self._prompt_sweeper is None:
            return
        for prompt in self._prompt_sweeper.sweep_expired_prompts(now):
            try:
                self._client.edit_message_text(
                    prompt.chat_id,
                    prompt.message_id,
                    prompt.text,
                    reply_markup=EMPTY_INLINE_KEYBOARD,
                )
            except TelegramApiError:
                LOGGER.exception(
                    "Failed to mark expired prompt %s in chat %s",
                    prompt.message_id,
                    prompt.chat_id,
                )


def start_scheduler_thread(
    scheduler: ReminderScheduler,
) -> tuple[threading.Thread, threading.Event]:
    stop_event = threading.Event()
    thread = threading.Thread(
        target=scheduler.run_forever,
        args=(stop_event,),
        name="reminder-scheduler",
        daemon=True,
    )
    thread.start()
    time.sleep(0)
    return thread, stop_event
