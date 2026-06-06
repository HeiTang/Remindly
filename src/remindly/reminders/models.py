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

    @property
    def is_complete(self) -> bool:
        return bool(self.title and self.remind_at and self.participants and not self.missing_fields)


@dataclass(frozen=True)
class ParseResult:
    title: str | None
    remind_at: datetime | None
    participants: tuple[Participant, ...]
    missing_fields: tuple[str, ...]
    confidence: float
    raw: dict[str, object]
