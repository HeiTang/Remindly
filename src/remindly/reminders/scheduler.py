from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from remindly.reminders.renderer import ReminderRenderer, delivery_snooze_keyboard
from remindly.reminders.repositories import ReminderDeliveryRepository
from remindly.telegram.client import TelegramClient

LOGGER = logging.getLogger(__name__)


class ReminderScheduler:
    def __init__(
        self,
        repository: ReminderDeliveryRepository,
        client: TelegramClient,
        renderer: ReminderRenderer,
        *,
        timezone: str,
        interval_seconds: int,
    ) -> None:
        self._repository = repository
        self._client = client
        self._renderer = renderer
        self._timezone = timezone
        self._interval_seconds = interval_seconds

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                self.tick()
            except Exception:
                LOGGER.exception("Reminder scheduler tick failed")
            stop_event.wait(self._interval_seconds)

    def tick(self) -> None:
        now = datetime.now(ZoneInfo(self._timezone))
        due_reminders = self._repository.claim_due(now)
        for reminder in due_reminders:
            try:
                participants = self._repository.list_participants(reminder.id)
                self._client.send_message(
                    reminder.chat_id,
                    self._renderer.render_delivery(reminder, participants),
                    parse_mode="HTML",
                    reply_markup=delivery_snooze_keyboard(reminder.short_id),
                )
                self._repository.mark_fired(reminder.id, now)
            except Exception:
                LOGGER.exception("Failed to send reminder %s", reminder.short_id)
                self._repository.mark_failed(reminder.id, datetime.now(ZoneInfo(self._timezone)))


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
