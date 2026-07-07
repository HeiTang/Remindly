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
        self.rescheduled: list[tuple[str, datetime]] = []

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

    def reschedule(self, reminder_id: str, next_at: datetime, now: datetime) -> None:
        del now
        self.rescheduled.append((reminder_id, next_at))


class ReminderSchedulerTest(unittest.TestCase):
    def test_delivery_message_contains_snooze_buttons(self) -> None:
        zone = ZoneInfo("Asia/Taipei")
        # 用固定原提醒時間，讓「明天 HH:MM」按鈕文字可以精確斷言
        remind_at = datetime(2026, 6, 3, 9, 0, tzinfo=zone)
        now = remind_at - timedelta(minutes=1)
        reminder = Reminder(
            id="rmd_due",
            short_id="R-DUE1",
            chat_id=100,
            chat_type="private",
            creator_user_id=7,
            title="洗衣服",
            remind_at=remind_at,
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
        self.assertEqual(["10 分鐘後", "1 小時後", "明天 09:00"], labels)


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

    def test_tick_uses_reminder_timezone_for_next_fire(self) -> None:
        """回歸：scheduler tz 若與 reminder tz 不同，`next_fire` 必須以 reminder tz 為準
        算 HH:MM，否則「每天 09:00」會漂到 scheduler tz 的 09:00。"""
        from remindly.reminders.models import RecurrencePeriod, RecurrenceRule

        scheduler_zone = ZoneInfo("UTC")
        reminder_zone = ZoneInfo("Asia/Taipei")
        # UTC 剛好觸發時：使用者設每天 09:00 TP → 應算出明天 09:00 TP
        remind_at = datetime(2026, 7, 3, 9, 0, tzinfo=reminder_zone)
        reminder = Reminder(
            id="rmd_tz",
            short_id="R-TZ",
            chat_id=100,
            chat_type="private",
            creator_user_id=7,
            title="吃藥",
            remind_at=remind_at,
            timezone="Asia/Taipei",
            status=ReminderStatus.FIRING,
            source_text="",
            parse_result={},
            created_at=remind_at,
            updated_at=remind_at,
            recurrence=RecurrenceRule(period=RecurrencePeriod.DAILY, hour=9, minute=0),
        )
        repository = FakeDeliveryRepository(reminder)
        client = FakeTelegramClient()
        scheduler = ReminderScheduler(
            repository=repository,  # type: ignore[arg-type]
            client=client,  # type: ignore[arg-type]
            renderer=ReminderRenderer(),
            timezone="UTC",  # 刻意跟 reminder tz 不同
            interval_seconds=10,
        )

        # Mock `datetime.now` 讓 tick 時的 now 剛好是 2026-07-03 09:00 TP (= 01:00 UTC)
        import unittest.mock

        with unittest.mock.patch(
            "remindly.reminders.scheduler.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 3, 1, 0, tzinfo=scheduler_zone)
            scheduler.tick()

        self.assertEqual(1, len(repository.rescheduled))
        _, next_at = repository.rescheduled[0]
        # 明天 09:00 TP，不是明天 09:00 UTC (= 17:00 TP)
        expected = datetime(2026, 7, 4, 9, 0, tzinfo=reminder_zone)
        self.assertEqual(expected, next_at)

    def test_recurring_delivery_uses_recurring_keyboard(self) -> None:
        """Phase 4a：週期性提醒的到期訊息要有『跳過下次』與『取消整個系列』按鈕。"""
        from remindly.reminders.models import RecurrencePeriod, RecurrenceRule

        zone = ZoneInfo("Asia/Taipei")
        remind_at = datetime(2026, 7, 4, 9, 0, tzinfo=zone)
        reminder = Reminder(
            id="rmd_rec_kbd",
            short_id="R-RKB",
            chat_id=100,
            chat_type="private",
            creator_user_id=7,
            title="繳信用卡",
            remind_at=remind_at,
            timezone="Asia/Taipei",
            status=ReminderStatus.FIRING,
            source_text="",
            parse_result={},
            created_at=remind_at,
            updated_at=remind_at,
            recurrence=RecurrenceRule(
                period=RecurrencePeriod.MONTHLY,
                hour=9,
                minute=0,
                month_days=(1, 18, 25),
            ),
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

        labels = [
            button["text"]
            for row in client.messages[0].reply_markup["inline_keyboard"]
            for button in row
        ]
        self.assertIn("跳過下次", labels)
        self.assertIn("取消整個系列", labels)
        # 一次性延後按鈕仍存在
        self.assertIn("10 分鐘後", labels)

    def test_one_off_delivery_does_not_show_skip_or_cancel_buttons(self) -> None:
        """一次性提醒不應該有『跳過下次』或『取消整個系列』按鈕。"""
        zone = ZoneInfo("Asia/Taipei")
        remind_at = datetime(2026, 7, 4, 9, 0, tzinfo=zone)
        reminder = Reminder(
            id="rmd_once_kbd",
            short_id="R-OKB",
            chat_id=100,
            chat_type="private",
            creator_user_id=7,
            title="洗衣服",
            remind_at=remind_at,
            timezone="Asia/Taipei",
            status=ReminderStatus.FIRING,
            source_text="",
            parse_result={},
            created_at=remind_at,
            updated_at=remind_at,
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

        labels = [
            button["text"]
            for row in client.messages[0].reply_markup["inline_keyboard"]
            for button in row
        ]
        self.assertNotIn("跳過下次", labels)
        self.assertNotIn("取消整個系列", labels)

    def test_tick_reschedules_recurring_reminder_instead_of_marking_fired(self) -> None:
        from remindly.reminders.models import (
            RecurrencePeriod,
            RecurrenceRule,
        )

        zone = ZoneInfo("Asia/Taipei")
        # 使用者例子：每月 1, 18, 25 號 09:00。tick 剛好碰到 7/1 09:00 → 下次 7/18 09:00
        remind_at = datetime(2026, 7, 1, 9, 0, tzinfo=zone)
        reminder = Reminder(
            id="rmd_rec",
            short_id="R-REC",
            chat_id=100,
            chat_type="private",
            creator_user_id=7,
            title="繳信用卡",
            remind_at=remind_at,
            timezone="Asia/Taipei",
            status=ReminderStatus.FIRING,
            source_text="",
            parse_result={},
            created_at=remind_at,
            updated_at=remind_at,
            recurrence=RecurrenceRule(
                period=RecurrencePeriod.MONTHLY,
                hour=9,
                minute=0,
                month_days=(1, 18, 25),
            ),
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

        self.assertEqual([], repository.marked_fired)  # 不標 FIRED
        self.assertEqual(1, len(repository.rescheduled))
        rmd_id, next_at = repository.rescheduled[0]
        self.assertEqual("rmd_rec", rmd_id)
        self.assertEqual(datetime(2026, 7, 18, 9, 0, tzinfo=zone), next_at)
        # 訊息仍然照送
        self.assertIn("ID：R-REC", client.messages[0].text)


if __name__ == "__main__":
    unittest.main()
