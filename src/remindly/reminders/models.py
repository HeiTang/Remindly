from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class ReminderStatus(StrEnum):
    PENDING = "pending"
    FIRING = "firing"
    FIRED = "fired"
    CANCELLED = "cancelled"
    FAILED = "failed"


class MentionKind(StrEnum):
    USERNAME = "username"
    TEXT_MENTION = "text_mention"
    PLAIN = "plain"


class RecurrencePeriod(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    YEARLY = "yearly"


@dataclass(frozen=True)
class RecurrenceRule:
    """描述週期性提醒的觸發規則。

    各 period 用到的欄位：
    - `daily`：只用 `hour` / `minute`
    - `weekly`：`weekdays`（0=Mon, 6=Sun，可多選）+ `hour` / `minute`
    - `monthly`：`month_days`（1-31，可多選；超出當月天數自動跳過）+ `hour` / `minute`
    - `yearly`：`year_month` + `year_day` + `hour` / `minute`
    """
    period: RecurrencePeriod
    hour: int
    minute: int
    weekdays: tuple[int, ...] = ()
    month_days: tuple[int, ...] = ()
    year_month: int | None = None
    year_day: int | None = None


@dataclass(frozen=True)
class Participant:
    display_name: str
    mention_kind: MentionKind
    user_id: int | None = None
    username: str | None = None

    @property
    def stable_key(self) -> str:
        if self.user_id is not None:
            return f"user:{self.user_id}"
        if self.username:
            return f"username:{self.username.lower()}"
        return f"plain:{self.display_name}"


@dataclass(frozen=True)
class Reminder:
    id: str
    short_id: str
    chat_id: int
    chat_type: str
    creator_user_id: int
    title: str
    remind_at: datetime
    timezone: str
    status: ReminderStatus
    source_text: str
    parse_result: dict[str, object]
    created_at: datetime
    updated_at: datetime
    recurrence: RecurrenceRule | None = None


@dataclass
class ReminderDraft:
    id: str
    chat_id: int
    chat_type: str
    creator_user_id: int
    timezone: str
    source_text: str
    title: str | None = None
    remind_at: datetime | None = None
    participants: list[Participant] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    parse_result: dict[str, object] = field(default_factory=dict)
    expires_at: datetime | None = None
    prompt_message_id: int | None = None
    recurrence: RecurrenceRule | None = None

    @property
    def is_complete(self) -> bool:
        return bool(self.title and self.remind_at and self.participants and not self.missing_fields)


@dataclass(frozen=True)
class RecurrenceError:
    """Parser 偵測到週期性提醒 marker 但語法無效時的信號。
    `marker_text` 是使用者實際輸入的錯誤片段（原話回顯到訊息裡），
    `reason` 是給使用者看的具體錯誤原因（例：`日期需在 1-31 範圍`）。"""
    marker_text: str
    reason: str


@dataclass(frozen=True)
class ParseResult:
    title: str | None
    remind_at: datetime | None
    participants: tuple[Participant, ...]
    missing_fields: tuple[str, ...]
    confidence: float
    raw: dict[str, object]
    recurrence: RecurrenceRule | None = None
    recurrence_error: RecurrenceError | None = None
