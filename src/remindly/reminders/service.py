from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime
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


@dataclass(frozen=True)
class EditPrompt:
    reminder: Reminder
    question: str


@dataclass(frozen=True)
class EditResult:
    details: ReminderDetails
    message: str


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
    ) -> list[ReminderListGroup]:
        groups_by_creator: dict[int, ReminderListGroup] = {}
        for reminder in self._repository.list_pending(chat_id):
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
