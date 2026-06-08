from __future__ import annotations

from datetime import datetime
from typing import Protocol

from remindly.reminders.models import Participant, Reminder


class ReminderDeliveryRepository(Protocol):
    def claim_due(self, now: datetime, limit: int = 20) -> list[Reminder]:
        """取出已到期且可發送的提醒，並把狀態標成 firing。"""
        ...

    def list_participants(self, reminder_id: str) -> list[Participant]:
        """查詢某個提醒要標註的對象清單。"""
        ...

    def mark_fired(self, reminder_id: str, now: datetime) -> None:
        """將成功送出的提醒標記為 fired。"""
        ...

    def mark_failed(self, reminder_id: str, now: datetime) -> None:
        """將送出失敗的提醒標記為 failed，方便後續追蹤或重試。"""
        ...


class ReminderRepository(ReminderDeliveryRepository, Protocol):
    def upsert_user(
        self,
        user_id: int,
        username: str | None,
        first_name: str | None,
        now: datetime,
    ) -> None:
        """保存或更新 Telegram 使用者資訊，供 mention 與建立者顯示使用。"""
        ...

    def upsert_chat(
        self,
        chat_id: int,
        chat_type: str,
        title: str | None,
        username: str | None,
        now: datetime,
    ) -> None:
        """保存或更新 Telegram chat context，讓提醒能回到原 chat。"""
        ...

    def is_chat_natural_language_enabled(self, chat_id: int) -> bool:
        """讀取群組自然語言模式；沒有設定時預設關閉。"""
        ...

    def set_chat_natural_language_enabled(
        self,
        chat_id: int,
        enabled: bool,
        updated_by_user_id: int,
        now: datetime,
    ) -> None:
        """更新群組是否允許一般文字觸發自然語言提醒。"""
        ...

    def get_user_display_name(self, user_id: int) -> str | None:
        """取得建立者顯示名稱，用於 /list 依建立者分組。"""
        ...

    def create_reminder(self, reminder: Reminder, participants: list[Participant]) -> None:
        """建立正式提醒與提醒對象。"""
        ...

    def list_pending(self, chat_id: int, limit: int = 10) -> list[Reminder]:
        """列出指定 chat 中尚未到期且未取消的提醒。"""
        ...

    def get_by_short_id(self, chat_id: int, short_id: str) -> Reminder | None:
        """用使用者可見的 short id 查詢單一提醒。"""
        ...

    def cancel(self, chat_id: int, short_id: str, actor_user_id: int) -> Reminder | None:
        """取消提醒；實作層需檢查提醒存在、狀態與操作者權限。"""
        ...

    def update_title(
        self,
        chat_id: int,
        short_id: str,
        actor_user_id: int,
        title: str,
        now: datetime,
    ) -> Reminder | None:
        """更新提醒內容；實作層需限制只有建立者可修改。"""
        ...

    def update_remind_at(
        self,
        chat_id: int,
        short_id: str,
        actor_user_id: int,
        remind_at: datetime,
        now: datetime,
    ) -> Reminder | None:
        """更新提醒時間；實作層需限制只有建立者可修改。"""
        ...

    def snooze(
        self,
        chat_id: int,
        short_id: str,
        actor_user_id: int,
        remind_at: datetime,
        now: datetime,
    ) -> Reminder | None:
        """把已送出的提醒延後，重新排回 pending 狀態。"""
        ...

    def get_user_timezone(self, user_id: int, default_timezone: str) -> str:
        """取得使用者偏好時區；沒有設定時回傳系統預設時區。"""
        ...

    def set_user_timezone(self, user_id: int, timezone: str) -> None:
        """保存使用者偏好時區。"""
        ...
