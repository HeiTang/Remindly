from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from remindly.reminders.models import Participant, Reminder, ReminderStatus
from remindly.reminders.renderer import ReminderRenderer
from remindly.reminders.scheduler import ReminderScheduler


@dataclass
class SentMessage:
    chat_id: int
    text: str
    reply_markup: dict[str, Any] | None


@dataclass
class FakeTelegramClient:
    messages: list[SentMessage] = field(default_factory=list)

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        del parse_mode
        self.messages.append(SentMessage(chat_id, text, reply_markup))


class FakeDeliveryRepository:
    def __init__(self, reminder: Reminder) -> None:
        self.reminder = reminder
        self.marked_fired: list[str] = []

    def claim_due(self, now: datetime, limit: int = 20) -> list[Reminder]:
        del now, limit
        return [self.reminder]

    def list_participants(self, reminder_id: str) -> list[Participant]:
        del reminder_id
        return []

    def mark_fired(self, reminder_id: str, now: datetime) -> None:
        del now
        self.marked_fired.append(reminder_id)

    def mark_failed(self, reminder_id: str, now: datetime) -> None:
        raise AssertionError(f"Unexpected failure for {reminder_id} at {now}")


class ReminderSchedulerTest(unittest.TestCase):
    def test_delivery_message_contains_snooze_buttons(self) -> None:
        now = datetime.now(ZoneInfo("Asia/Taipei"))
        reminder = Reminder(
            id="rmd_due",
            short_id="R-DUE1",
            chat_id=100,
            chat_type="private",
            creator_user_id=7,
            title="洗衣服",
            remind_at=now - timedelta(minutes=1),
            timezone="Asia/Taipei",
            status=ReminderStatus.FIRING,
            source_text="提醒我要洗衣服",
            parse_result={},
            created_at=now - timedelta(minutes=5),
            updated_at=now,
        )
        repository = FakeDeliveryRepository(reminder)
        client = FakeTelegramClient()
        scheduler = ReminderScheduler(
            repository=repository,  # type: ignore[arg-type]
            client=client,  # type: ignore[arg-type]
            renderer=ReminderRenderer(),
            timezone="Asia/Taipei",
            interval_seconds=10,
        )

        scheduler.tick()

        self.assertEqual(["rmd_due"], repository.marked_fired)
        self.assertIn("ID：R-DUE1", client.messages[0].text)
        labels = [
            button["text"]
            for row in client.messages[0].reply_markup["inline_keyboard"]
            for button in row
        ]
        self.assertEqual(["10 分鐘後", "1 小時後", "明天同時間"], labels)


if __name__ == "__main__":
    unittest.main()
