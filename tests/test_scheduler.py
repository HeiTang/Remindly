from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from remindly.reminders.models import Participant, Reminder, ReminderStatus
from remindly.reminders.renderer import ReminderRenderer
from remindly.reminders.scheduler import ReminderScheduler
from remindly.reminders.service import ExpiredPrompt


@dataclass
class SentMessage:
    chat_id: int
    text: str
    reply_markup: dict[str, Any] | None


@dataclass
class EditedMessage:
    chat_id: int
    message_id: int
    text: str
    reply_markup: dict[str, Any] | None


@dataclass
class FakeTelegramClient:
    messages: list[SentMessage] = field(default_factory=list)
    edits: list[EditedMessage] = field(default_factory=list)

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> int:
        del parse_mode
        self.messages.append(SentMessage(chat_id, text, reply_markup))
        return len(self.messages)

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> int:
        del parse_mode
        self.edits.append(EditedMessage(chat_id, message_id, text, reply_markup))
        return message_id


class FakePromptSweeper:
    def __init__(self, prompts: list[ExpiredPrompt]) -> None:
        self._prompts = prompts
        self.calls: list[datetime] = []

    def sweep_expired_prompts(self, now: datetime) -> list[ExpiredPrompt]:
        self.calls.append(now)
        return list(self._prompts)


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


    def test_tick_marks_expired_prompts_with_empty_keyboard(self) -> None:
        now = datetime.now(ZoneInfo("Asia/Taipei"))
        reminder = Reminder(
            id="rmd_none",
            short_id="R-NONE",
            chat_id=100,
            chat_type="private",
            creator_user_id=7,
            title="洗衣服",
            remind_at=now + timedelta(hours=1),
            timezone="Asia/Taipei",
            status=ReminderStatus.PENDING,
            source_text="",
            parse_result={},
            created_at=now,
            updated_at=now,
        )

        class NoDueRepo(FakeDeliveryRepository):
            def claim_due(self, now: datetime, limit: int = 20) -> list[Reminder]:
                del now, limit
                return []

        repository = NoDueRepo(reminder)
        client = FakeTelegramClient()
        sweeper = FakePromptSweeper(
            [
                ExpiredPrompt(chat_id=100, message_id=555, text="（已過期）流程取消"),
                ExpiredPrompt(chat_id=200, message_id=777, text="（已過期）edit 取消"),
            ]
        )
        scheduler = ReminderScheduler(
            repository=repository,  # type: ignore[arg-type]
            client=client,  # type: ignore[arg-type]
            renderer=ReminderRenderer(),
            timezone="Asia/Taipei",
            interval_seconds=10,
            prompt_sweeper=sweeper,
        )

        scheduler.tick()

        self.assertEqual(1, len(sweeper.calls))
        self.assertEqual(2, len(client.edits))
        self.assertEqual(555, client.edits[0].message_id)
        self.assertIn("已過期", client.edits[0].text)
        self.assertEqual({"inline_keyboard": []}, client.edits[0].reply_markup)


if __name__ == "__main__":
    unittest.main()
