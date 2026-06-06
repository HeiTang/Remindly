from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from remindly.reminders.models import ReminderDraft


class ReminderDraftStore(Protocol):
    def save(self, draft: ReminderDraft, now: datetime) -> ReminderDraft: ...

    def get_by_id(self, draft_id: str, now: datetime) -> ReminderDraft | None: ...

    def get_for_context(
        self,
        chat_id: int,
        user_id: int,
        now: datetime,
    ) -> ReminderDraft | None: ...

    def delete(self, draft_id: str) -> None: ...

    def delete_for_context(self, chat_id: int, user_id: int) -> None: ...


class ReminderEditSessionStore(Protocol):
    def save(self, session: EditSession, now: datetime) -> EditSession: ...

    def get_for_context(self, chat_id: int, user_id: int, now: datetime) -> EditSession | None: ...

    def delete(self, chat_id: int, user_id: int) -> None: ...


class DraftStore:
    def __init__(self, ttl_minutes: int) -> None:
        self._ttl = timedelta(minutes=ttl_minutes)
        self._drafts_by_id: dict[str, ReminderDraft] = {}
        self._draft_ids_by_context: dict[tuple[int, int], str] = {}

    def save(self, draft: ReminderDraft, now: datetime) -> ReminderDraft:
        self.delete_for_context(draft.chat_id, draft.creator_user_id)
        draft.expires_at = now + self._ttl
        self._drafts_by_id[draft.id] = draft
        self._draft_ids_by_context[(draft.chat_id, draft.creator_user_id)] = draft.id
        return draft

    def get_by_id(self, draft_id: str, now: datetime) -> ReminderDraft | None:
        draft = self._drafts_by_id.get(draft_id)
        if not draft:
            return None
        if draft.expires_at and draft.expires_at <= now:
            self.delete(draft_id)
            return None
        return draft

    def get_for_context(self, chat_id: int, user_id: int, now: datetime) -> ReminderDraft | None:
        draft_id = self._draft_ids_by_context.get((chat_id, user_id))
        if not draft_id:
            return None
        return self.get_by_id(draft_id, now)

    def delete(self, draft_id: str) -> None:
        draft = self._drafts_by_id.pop(draft_id, None)
        if draft:
            self._draft_ids_by_context.pop((draft.chat_id, draft.creator_user_id), None)

    def delete_for_context(self, chat_id: int, user_id: int) -> None:
        draft_id = self._draft_ids_by_context.pop((chat_id, user_id), None)
        if draft_id:
            self._drafts_by_id.pop(draft_id, None)


@dataclass
class EditSession:
    chat_id: int
    user_id: int
    short_id: str
    field: str
    expires_at: datetime | None = None


class EditSessionStore:
    def __init__(self, ttl_minutes: int) -> None:
        self._ttl = timedelta(minutes=ttl_minutes)
        self._sessions_by_context: dict[tuple[int, int], EditSession] = {}

    def save(self, session: EditSession, now: datetime) -> EditSession:
        session.expires_at = now + self._ttl
        self._sessions_by_context[(session.chat_id, session.user_id)] = session
        return session

    def get_for_context(self, chat_id: int, user_id: int, now: datetime) -> EditSession | None:
        session = self._sessions_by_context.get((chat_id, user_id))
        if not session:
            return None
        if session.expires_at and session.expires_at <= now:
            self.delete(chat_id, user_id)
            return None
        return session

    def delete(self, chat_id: int, user_id: int) -> None:
        self._sessions_by_context.pop((chat_id, user_id), None)
