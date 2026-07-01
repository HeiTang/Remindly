from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from remindly.reminders.drafts import (
    EditSession,
    ReminderDraftStore,
    ReminderEditSessionStore,
)
from remindly.reminders.models import (
    Participant,
    Reminder,
    ReminderDraft,
    ReminderStatus,
)
from remindly.reminders.parser import ReminderParser
from remindly.reminders.repositories import ReminderRepository
from remindly.telegram.models import (
    TelegramCallbackQuery,
    TelegramMessage,
    TelegramUser,
)


@dataclass(frozen=True)
class DraftPrompt:
    draft: ReminderDraft
    question: str
    wants_quick_time: bool = False


@dataclass(frozen=True)
class Confirmation:
    draft: ReminderDraft


@dataclass(frozen=True)
class CreateResult:
    reminder: Reminder


@dataclass(frozen=True)
class ReminderDetails:
    reminder: Reminder
    participants: list[Participant]


@dataclass(frozen=True)
class ReminderListGroup:
    creator_user_id: int
    creator_label: str
    reminders: list[Reminder]


class ReminderListFilter(StrEnum):
    ALL = "all"
    TODAY = "today"
    WEEK = "week"
    MINE = "mine"


@dataclass(frozen=True)
class EditPrompt:
    reminder: Reminder
    question: str


@dataclass(frozen=True)
class EditResult:
    details: ReminderDetails
    message: str


@dataclass(frozen=True)
class SnoozeResult:
    reminder: Reminder


@dataclass(frozen=True)
class ExpiredPrompt:
    """Sweep 掃出來的過期互動訊息，交給 scheduler 用 editMessageText 標記。"""
    chat_id: int
    message_id: int
    text: str


SNOOZE_DELAYS = {
    "10m": timedelta(minutes=10),
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
}

EXPIRED_DRAFT_TEXT = "（已過期）未完成的提醒建立流程已取消。"
EXPIRED_EDIT_TEXT = "（已過期）未完成的修改流程已取消。"
CANCELLED_BY_NEW_DRAFT_TEXT = "（已取消）此提醒建立流程已被新的提醒取代。"
CANCELLED_BY_NEW_EDIT_TEXT = "（已取消）此修改流程已被新的提醒取代。"


class ReminderService:
    def __init__(
        self,
        repository: ReminderRepository,
        parser: ReminderParser,
        draft_store: ReminderDraftStore,
        edit_store: ReminderEditSessionStore,
        default_timezone: str,
    ) -> None:
        self._repository = repository
        self._parser = parser
        self._draft_store = draft_store
        self._edit_store = edit_store
        self._default_timezone = default_timezone

    def record_message_context(self, message: TelegramMessage, now: datetime) -> None:
        self._repository.upsert_chat(
            message.chat.id,
            message.chat.type,
            message.chat.title,
            message.chat.username,
            now,
        )
        if message.from_user:
            self._record_user(message.from_user, now)
        if message.reply_to_message and message.reply_to_message.from_user:
            self._record_user(message.reply_to_message.from_user, now)
        for entity in message.entities:
            if entity.user:
                self._record_user(entity.user, now)

    def record_callback_context(self, callback: TelegramCallbackQuery, now: datetime) -> None:
        self._record_user(callback.from_user, now)
        if callback.message:
            self.record_message_context(callback.message, now)

    def is_group_natural_language_enabled(self, chat_id: int) -> bool:
        """確認群組是否允許一般文字直接觸發自然語言提醒。"""
        return self._repository.is_chat_natural_language_enabled(chat_id)

    def set_group_natural_language_enabled(
        self,
        chat_id: int,
        enabled: bool,
        updated_by_user_id: int,
        now: datetime,
    ) -> None:
        """更新群組自然語言模式，由 command layer 先完成權限檢查。"""
        self._repository.set_chat_natural_language_enabled(
            chat_id,
            enabled,
            updated_by_user_id,
            now,
        )

    def begin_create(
        self,
        text: str,
        message: TelegramMessage,
        now: datetime,
    ) -> DraftPrompt | Confirmation:
        creator = require_user(message)
        timezone = self._repository.get_user_timezone(creator.id, self._default_timezone)
        parse_result = self._parser.parse(text, message, now=now.astimezone(ZoneInfo(timezone)))
        draft = ReminderDraft(
            id=new_id("draft"),
            chat_id=message.chat.id,
            chat_type=message.chat.type,
            creator_user_id=creator.id,
            timezone=timezone,
            source_text=text,
            title=parse_result.title,
            remind_at=parse_result.remind_at,
            participants=list(parse_result.participants),
            missing_fields=list(parse_result.missing_fields),
            parse_result=parse_result.raw,
        )

        if draft.is_complete:
            self._draft_store.save(draft, now)
            return Confirmation(draft)

        self._draft_store.save(draft, now)
        return DraftPrompt(
            draft=draft,
            question=self._question_for(draft),
            wants_quick_time=draft.missing_fields == ["time"] and draft.remind_at is not None,
        )

    def continue_draft(
        self,
        message: TelegramMessage,
        now: datetime,
    ) -> DraftPrompt | Confirmation | None:
        creator = require_user(message)
        draft = self._draft_store.get_for_context(message.chat.id, creator.id, now)
        if not draft:
            return None

        self._merge_answer(draft, message.text, message, now)
        if draft.is_complete:
            self._draft_store.save(draft, now)
            return Confirmation(draft)

        self._draft_store.save(draft, now)
        return DraftPrompt(
            draft=draft,
            question=self._question_for(draft),
            wants_quick_time=draft.missing_fields == ["time"] and draft.remind_at is not None,
        )

    def apply_quick_time(
        self,
        draft_id: str,
        hour_minute: str,
        user_id: int,
        now: datetime,
    ) -> Confirmation | DraftPrompt | None:
        draft = self._draft_store.get_by_id(draft_id, now)
        if not draft or draft.creator_user_id != user_id or draft.remind_at is None:
            return None

        hour, minute = [int(part) for part in hour_minute.split(":", 1)]
        remind_at = draft.remind_at.replace(hour=hour, minute=minute, second=0, microsecond=0)
        draft.remind_at = remind_at
        draft.missing_fields = [field for field in draft.missing_fields if field != "time"]
        if remind_at <= now.astimezone(remind_at.tzinfo):
            draft.missing_fields.append("time")

        self._draft_store.save(draft, now)
        if draft.is_complete:
            return Confirmation(draft)
        return DraftPrompt(draft=draft, question=self._question_for(draft))

    def confirm(self, draft_id: str, user_id: int, now: datetime) -> CreateResult | None:
        draft = self._draft_store.get_by_id(draft_id, now)
        if not draft or draft.creator_user_id != user_id or not draft.is_complete:
            return None

        reminder = Reminder(
            id=new_id("rmd"),
            short_id=new_short_id(),
            chat_id=draft.chat_id,
            chat_type=draft.chat_type,
            creator_user_id=draft.creator_user_id,
            title=draft.title or "",
            remind_at=require_datetime(draft.remind_at),
            timezone=draft.timezone,
            status=ReminderStatus.PENDING,
            source_text=draft.source_text,
            parse_result=draft.parse_result,
            created_at=now,
            updated_at=now,
        )
        self._repository.create_reminder(reminder, draft.participants)
        self._draft_store.delete(draft_id)
        return CreateResult(reminder)

    def discard_draft(self, draft_id: str, user_id: int, now: datetime) -> bool:
        draft = self._draft_store.get_by_id(draft_id, now)
        if not draft or draft.creator_user_id != user_id:
            return False
        self._draft_store.delete(draft_id)
        return True

    def peek_draft(
        self, chat_id: int, user_id: int, now: datetime
    ) -> ReminderDraft | None:
        """純讀當前未完成 draft，不做任何合併或狀態改變。"""
        return self._draft_store.get_for_context(chat_id, user_id, now)

    def peek_edit_session(
        self, chat_id: int, user_id: int, now: datetime
    ) -> EditSession | None:
        """純讀當前未完成 edit session，不做狀態改變。"""
        return self._edit_store.get_for_context(chat_id, user_id, now)

    def clear_pending_conversation(
        self, chat_id: int, user_id: int
    ) -> list[ExpiredPrompt]:
        """建立新提醒前呼叫：清掉舊 draft/edit session。
        回傳每個仍需 editMessage 標記的舊 prompt（含 prompt_message_id 的才會回傳）。"""
        cancelled: list[ExpiredPrompt] = []
        draft = self._draft_store.get_for_context(chat_id, user_id, self._min_datetime())
        if draft:
            if draft.prompt_message_id is not None:
                cancelled.append(
                    ExpiredPrompt(
                        chat_id=draft.chat_id,
                        message_id=draft.prompt_message_id,
                        text=CANCELLED_BY_NEW_DRAFT_TEXT,
                    )
                )
            self._draft_store.delete(draft.id)
        # peek EditSession 是否存在 — 用 min 時間避開過期檢查
        existing_edit = self._edit_store.get_for_context(chat_id, user_id, self._min_datetime())
        if existing_edit:
            if existing_edit.prompt_message_id is not None:
                cancelled.append(
                    ExpiredPrompt(
                        chat_id=existing_edit.chat_id,
                        message_id=existing_edit.prompt_message_id,
                        text=CANCELLED_BY_NEW_EDIT_TEXT,
                    )
                )
            self._edit_store.delete(chat_id, user_id)
        return cancelled

    def set_draft_prompt_message_id(self, draft_id: str, message_id: int) -> None:
        """在 Telegram 送出追問/確認訊息後綁定 message_id，供 sweep 時 editMessage 用。"""
        self._draft_store.set_prompt_message_id(draft_id, message_id)

    def set_edit_prompt_message_id(
        self, chat_id: int, user_id: int, message_id: int
    ) -> None:
        self._edit_store.set_prompt_message_id(chat_id, user_id, message_id)

    def sweep_expired_prompts(self, now: datetime) -> list[ExpiredPrompt]:
        """撈出所有過期的 draft/edit session，回傳給 scheduler 標記。
        回傳後 store 內對應資料會被刪除。"""
        expired: list[ExpiredPrompt] = []
        for draft in self._draft_store.list_expired(now):
            if draft.prompt_message_id is not None:
                expired.append(
                    ExpiredPrompt(
                        chat_id=draft.chat_id,
                        message_id=draft.prompt_message_id,
                        text=EXPIRED_DRAFT_TEXT,
                    )
                )
            self._draft_store.delete(draft.id)
        for session in self._edit_store.list_expired(now):
            if session.prompt_message_id is not None:
                expired.append(
                    ExpiredPrompt(
                        chat_id=session.chat_id,
                        message_id=session.prompt_message_id,
                        text=EXPIRED_EDIT_TEXT,
                    )
                )
            self._edit_store.delete(session.chat_id, session.user_id)
        return expired

    def _min_datetime(self) -> datetime:
        """給 store 用的極小 datetime，避免因 TTL 過期而讀不到 pending 資料。"""
        return datetime.min.replace(tzinfo=ZoneInfo(self._default_timezone))

    def get_details(self, chat_id: int, short_id: str) -> ReminderDetails | None:
        reminder = self._repository.get_by_short_id(chat_id, short_id)
        if not reminder or reminder.status != ReminderStatus.PENDING:
            return None
        return ReminderDetails(reminder, self._repository.list_participants(reminder.id))

    def list_pending(self, chat_id: int) -> list[Reminder]:
        return self._repository.list_pending(chat_id)

    def list_pending_grouped(
        self,
        chat_id: int,
        viewer_user_id: int | None,
        now: datetime,
        list_filter: ReminderListFilter = ReminderListFilter.ALL,
    ) -> list[ReminderListGroup]:
        groups_by_creator: dict[int, ReminderListGroup] = {}
        viewer_zone = self._viewer_zone(viewer_user_id)
        for reminder in self._repository.list_pending(chat_id, limit=50):
            if not self._matches_list_filter(
                reminder,
                viewer_user_id,
                now,
                viewer_zone,
                list_filter,
            ):
                continue

            group = groups_by_creator.get(reminder.creator_user_id)
            if group is None:
                group = ReminderListGroup(
                    creator_user_id=reminder.creator_user_id,
                    creator_label=self._creator_label(reminder, viewer_user_id),
                    reminders=[],
                )
                groups_by_creator[reminder.creator_user_id] = group
            group.reminders.append(reminder)
        return list(groups_by_creator.values())

    def cancel(self, chat_id: int, short_id: str, actor_user_id: int) -> Reminder | None:
        return self._repository.cancel(chat_id, short_id, actor_user_id)

    def begin_edit(
        self,
        chat_id: int,
        short_id: str,
        actor_user_id: int,
        field: str,
        now: datetime,
    ) -> EditPrompt | None:
        reminder = self._repository.get_by_short_id(chat_id, short_id)
        if (
            not reminder
            or reminder.status != ReminderStatus.PENDING
            or reminder.creator_user_id != actor_user_id
        ):
            return None

        self._edit_store.save(
            EditSession(chat_id=chat_id, user_id=actor_user_id, short_id=short_id, field=field),
            now,
        )
        question = "請輸入新的提醒時間。" if field == "time" else "請輸入新的提醒內容。"
        return EditPrompt(reminder=reminder, question=question)

    def continue_edit(
        self,
        message: TelegramMessage,
        now: datetime,
    ) -> EditPrompt | EditResult | None:
        actor = require_user(message)
        session = self._edit_store.get_for_context(message.chat.id, actor.id, now)
        if not session:
            return None

        reminder = self._repository.get_by_short_id(message.chat.id, session.short_id)
        if (
            not reminder
            or reminder.status != ReminderStatus.PENDING
            or reminder.creator_user_id != actor.id
        ):
            self._edit_store.delete(message.chat.id, actor.id)
            return None

        if session.field == "title":
            title = message.text.strip()
            if not title:
                return EditPrompt(
                    reminder=reminder,
                    question="提醒內容不能是空的，請再輸入一次。",
                )
            updated = self._repository.update_title(
                message.chat.id,
                session.short_id,
                actor.id,
                title,
                now,
            )
            self._edit_store.delete(message.chat.id, actor.id)
            return self._edit_result(updated, "已更新提醒內容。")

        if session.field == "time":
            parsed_time = self._parser.parse_time_only(
                message.text,
                now.astimezone(ZoneInfo(reminder.timezone)),
                existing_date=reminder.remind_at,
            )
            if not parsed_time.remind_at or parsed_time.missing_time or parsed_time.is_past:
                return EditPrompt(
                    reminder=reminder,
                    question="時間不夠明確或已經過去，請再輸入一次。",
                )
            updated = self._repository.update_remind_at(
                message.chat.id,
                session.short_id,
                actor.id,
                parsed_time.remind_at,
                now,
            )
            self._edit_store.delete(message.chat.id, actor.id)
            return self._edit_result(updated, "已更新提醒時間。")

        self._edit_store.delete(message.chat.id, actor.id)
        return None

    def cancel_edit(self, chat_id: int, user_id: int) -> None:
        self._edit_store.delete(chat_id, user_id)

    def snooze(
        self,
        chat_id: int,
        short_id: str,
        actor_user_id: int,
        delay_key: str,
        now: datetime,
    ) -> SnoozeResult | None:
        """依固定延後選項重新排程已送出的提醒。"""
        delay = SNOOZE_DELAYS.get(delay_key)
        if delay is None:
            return None

        current = self._repository.get_by_short_id(chat_id, short_id)
        if not current:
            return None

        if delay_key == "1d":
            remind_at = current.remind_at + delay
        else:
            remind_at = now.astimezone(ZoneInfo(current.timezone)) + delay

        reminder = self._repository.snooze(chat_id, short_id, actor_user_id, remind_at, now)
        return SnoozeResult(reminder) if reminder else None

    def set_timezone(self, user_id: int, timezone: str) -> None:
        ZoneInfo(timezone)
        self._repository.set_user_timezone(user_id, timezone)

    def _edit_result(self, reminder: Reminder | None, message: str) -> EditResult | None:
        if reminder is None:
            return None
        return EditResult(
            details=ReminderDetails(reminder, self._repository.list_participants(reminder.id)),
            message=message,
        )

    def _matches_list_filter(
        self,
        reminder: Reminder,
        viewer_user_id: int | None,
        now: datetime,
        viewer_zone: ZoneInfo,
        list_filter: ReminderListFilter,
    ) -> bool:
        if list_filter == ReminderListFilter.ALL:
            return True

        if list_filter == ReminderListFilter.MINE:
            return viewer_user_id is not None and reminder.creator_user_id == viewer_user_id

        viewer_today = now.astimezone(viewer_zone).date()
        reminder_date = reminder.remind_at.astimezone(viewer_zone).date()
        if list_filter == ReminderListFilter.TODAY:
            return reminder_date == viewer_today

        week_start = viewer_today - timedelta(days=viewer_today.weekday())
        return week_start <= reminder_date < week_start + timedelta(days=7)

    def _viewer_zone(self, viewer_user_id: int | None) -> ZoneInfo:
        if viewer_user_id is None:
            return ZoneInfo(self._default_timezone)

        return ZoneInfo(self._repository.get_user_timezone(viewer_user_id, self._default_timezone))

    def _creator_label(self, reminder: Reminder, viewer_user_id: int | None) -> str:
        if viewer_user_id == reminder.creator_user_id:
            return "你"

        display_name = self._repository.get_user_display_name(reminder.creator_user_id)
        if display_name:
            return display_name

        for participant in self._repository.list_participants(reminder.id):
            if participant.user_id == reminder.creator_user_id:
                return participant.display_name

        return f"user:{reminder.creator_user_id}"

    def _record_user(self, user: TelegramUser, now: datetime) -> None:
        self._repository.upsert_user(user.id, user.username, user.first_name, now)

    def _merge_answer(
        self,
        draft: ReminderDraft,
        answer: str,
        message: TelegramMessage,
        now: datetime,
    ) -> None:
        missing = list(draft.missing_fields)
        parsed_answer = self._parser.parse(
            answer,
            message,
            now=now.astimezone(ZoneInfo(draft.timezone)),
        )

        if "time" in missing:
            parsed_time = self._parser.parse_time_only(
                answer,
                now.astimezone(ZoneInfo(draft.timezone)),
                existing_date=draft.remind_at,
            )
            if parsed_time.remind_at and not parsed_time.missing_time and not parsed_time.is_past:
                draft.remind_at = parsed_time.remind_at
                missing.remove("time")

        if "title" in missing:
            title = parsed_answer.title
            if title:
                draft.title = title
                missing.remove("title")

        if "participants" in missing and parsed_answer.participants:
            draft.participants = list(parsed_answer.participants)
            missing.remove("participants")

        draft.missing_fields = missing

    def _question_for(self, draft: ReminderDraft) -> str:
        if "time" in draft.missing_fields:
            return "什麼時候提醒？" if draft.remind_at is None else "那天幾點？"
        if "title" in draft.missing_fields:
            return "要提醒什麼？"
        if "participants" in draft.missing_fields:
            return "要提醒誰？"
        return "還缺一些資訊，請再補充。"


def require_user(message: TelegramMessage):
    if message.from_user is None:
        raise ValueError("Telegram message has no sender")
    return message.from_user


def require_datetime(value: datetime | None) -> datetime:
    if value is None:
        raise ValueError("Expected datetime")
    return value


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(12)}"


def new_short_id() -> str:
    return f"R-{secrets.token_hex(2).upper()}"
