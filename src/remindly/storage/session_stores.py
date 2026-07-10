from __future__ import annotations

import json
from datetime import datetime, timedelta

from remindly.reminders.drafts import EditSession
from remindly.reminders.models import MentionKind, Participant, ReminderDraft
from remindly.reminders.recurrence import deserialize_rule, serialize_rule
from remindly.storage.sqlite import ReminderRepository


class SqliteDraftStore:
    def __init__(
        self,
        ttl_minutes: int,
        repository: ReminderRepository,
        *,
        confirming_ttl_minutes: int | None = None,
    ) -> None:
        self._asking_ttl = timedelta(minutes=ttl_minutes)
        self._confirming_ttl = timedelta(
            minutes=confirming_ttl_minutes if confirming_ttl_minutes is not None else ttl_minutes
        )
        self._repository = repository

    def save(self, draft: ReminderDraft, now: datetime) -> ReminderDraft:
        self.delete_for_context(draft.chat_id, draft.creator_user_id)
        # 每次 save 都以當下時間重算 TTL，讓活躍對話不會因初始時戳而過期；
        # 也讓 draft 從 asking 進入 confirming 時能升級到較長的 TTL。
        draft.expires_at = now + self._default_ttl(draft)
        with self._repository.connect() as connection:
            connection.execute(
                """
                insert into reminder_drafts (
                    id, chat_id, chat_type, creator_user_id, timezone, source_text, title,
                    remind_at, participants, missing_fields, parse_result, expires_at,
                    prompt_message_id, recurrence
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft.id,
                    draft.chat_id,
                    draft.chat_type,
                    draft.creator_user_id,
                    draft.timezone,
                    draft.source_text,
                    draft.title,
                    draft.remind_at.isoformat() if draft.remind_at else None,
                    json.dumps(
                        [participant_to_dict(participant) for participant in draft.participants],
                        ensure_ascii=False,
                    ),
                    json.dumps(draft.missing_fields, ensure_ascii=False),
                    json.dumps(draft.parse_result, ensure_ascii=False),
                    draft.expires_at.isoformat() if draft.expires_at else None,
                    draft.prompt_message_id,
                    serialize_rule(draft.recurrence) if draft.recurrence else None,
                ),
            )
        return draft

    def get_by_id(self, draft_id: str, now: datetime) -> ReminderDraft | None:
        with self._repository.connect() as connection:
            row = connection.execute(
                "select * from reminder_drafts where id = ? limit 1",
                (draft_id,),
            ).fetchone()
        draft = row_to_draft(row) if row else None
        if not draft:
            return None
        if draft.expires_at and draft.expires_at <= now:
            self.delete(draft_id)
            return None
        return draft

    def get_for_context(self, chat_id: int, user_id: int, now: datetime) -> ReminderDraft | None:
        with self._repository.connect() as connection:
            row = connection.execute(
                """
                select * from reminder_drafts
                where chat_id = ? and creator_user_id = ?
                order by expires_at desc
                limit 1
                """,
                (chat_id, user_id),
            ).fetchone()
        draft = row_to_draft(row) if row else None
        if not draft:
            return None
        return self.get_by_id(draft.id, now)

    def delete(self, draft_id: str) -> None:
        with self._repository.connect() as connection:
            connection.execute("delete from reminder_drafts where id = ?", (draft_id,))

    def delete_for_context(self, chat_id: int, user_id: int) -> None:
        with self._repository.connect() as connection:
            connection.execute(
                "delete from reminder_drafts where chat_id = ? and creator_user_id = ?",
                (chat_id, user_id),
            )

    def set_prompt_message_id(self, draft_id: str, message_id: int) -> None:
        with self._repository.connect() as connection:
            connection.execute(
                "update reminder_drafts set prompt_message_id = ? where id = ?",
                (message_id, draft_id),
            )

    def list_expired(self, now: datetime) -> list[ReminderDraft]:
        """撈出所有已過期但仍留在表裡的 draft，用於 scheduler sweep。"""
        with self._repository.connect() as connection:
            rows = connection.execute(
                "select * from reminder_drafts where expires_at <= ?",
                (now.isoformat(),),
            ).fetchall()
        return [row_to_draft(row) for row in rows]

    def _default_ttl(self, draft: ReminderDraft) -> timedelta:
        """依 draft 狀態決定 TTL：等使用者按按鈕的『確認』狀態給比較長時間。"""
        return self._confirming_ttl if draft.is_complete else self._asking_ttl


class SqliteEditSessionStore:
    def __init__(self, ttl_minutes: int, repository: ReminderRepository) -> None:
        self._ttl = timedelta(minutes=ttl_minutes)
        self._repository = repository

    def save(self, session: EditSession, now: datetime) -> EditSession:
        # 每次 save 刷新 TTL，避免修改流程中途因初始時戳而過期。
        session.expires_at = now + self._ttl
        with self._repository.connect() as connection:
            connection.execute(
                """
                insert into edit_sessions (
                    chat_id, user_id, short_id, field, expires_at, prompt_message_id
                )
                values (?, ?, ?, ?, ?, ?)
                on conflict(chat_id, user_id) do update set
                    short_id = excluded.short_id,
                    field = excluded.field,
                    expires_at = excluded.expires_at,
                    prompt_message_id = excluded.prompt_message_id
                """,
                (
                    session.chat_id,
                    session.user_id,
                    session.short_id,
                    session.field,
                    session.expires_at.isoformat() if session.expires_at else None,
                    session.prompt_message_id,
                ),
            )
        return session

    def get_for_context(self, chat_id: int, user_id: int, now: datetime) -> EditSession | None:
        with self._repository.connect() as connection:
            row = connection.execute(
                """
                select * from edit_sessions
                where chat_id = ? and user_id = ?
                limit 1
                """,
                (chat_id, user_id),
            ).fetchone()
        session = row_to_edit_session(row) if row else None
        if not session:
            return None
        if session.expires_at and session.expires_at <= now:
            self.delete(chat_id, user_id)
            return None
        return session

    def delete(self, chat_id: int, user_id: int) -> None:
        with self._repository.connect() as connection:
            connection.execute(
                "delete from edit_sessions where chat_id = ? and user_id = ?",
                (chat_id, user_id),
            )

    def set_prompt_message_id(
        self, chat_id: int, user_id: int, message_id: int
    ) -> None:
        with self._repository.connect() as connection:
            connection.execute(
                """
                update edit_sessions
                set prompt_message_id = ?
                where chat_id = ? and user_id = ?
                """,
                (message_id, chat_id, user_id),
            )

    def list_expired(self, now: datetime) -> list[EditSession]:
        with self._repository.connect() as connection:
            rows = connection.execute(
                "select * from edit_sessions where expires_at <= ?",
                (now.isoformat(),),
            ).fetchall()
        return [row_to_edit_session(row) for row in rows]


def participant_to_dict(participant: Participant) -> dict[str, object]:
    return {
        "display_name": participant.display_name,
        "mention_kind": participant.mention_kind.value,
        "user_id": participant.user_id,
        "username": participant.username,
    }


def participant_from_dict(data: dict[str, object]) -> Participant:
    return Participant(
        display_name=str(data["display_name"]),
        mention_kind=MentionKind(str(data["mention_kind"])),
        user_id=int(data["user_id"]) if data.get("user_id") is not None else None,
        username=str(data["username"]) if data.get("username") is not None else None,
    )


def row_to_draft(row) -> ReminderDraft:
    recurrence_raw = _optional_str(row, "recurrence")
    return ReminderDraft(
        id=str(row["id"]),
        chat_id=int(row["chat_id"]),
        chat_type=str(row["chat_type"]),
        creator_user_id=int(row["creator_user_id"]),
        timezone=str(row["timezone"]),
        source_text=str(row["source_text"]),
        title=row["title"],
        remind_at=datetime.fromisoformat(str(row["remind_at"])) if row["remind_at"] else None,
        participants=[participant_from_dict(item) for item in json.loads(str(row["participants"]))],
        missing_fields=list(json.loads(str(row["missing_fields"]))),
        parse_result=dict(json.loads(str(row["parse_result"]))),
        expires_at=datetime.fromisoformat(str(row["expires_at"])) if row["expires_at"] else None,
        prompt_message_id=_optional_int(row, "prompt_message_id"),
        recurrence=deserialize_rule(recurrence_raw) if recurrence_raw else None,
    )


def row_to_edit_session(row) -> EditSession:
    return EditSession(
        chat_id=int(row["chat_id"]),
        user_id=int(row["user_id"]),
        short_id=str(row["short_id"]),
        field=str(row["field"]),
        expires_at=datetime.fromisoformat(str(row["expires_at"])) if row["expires_at"] else None,
        prompt_message_id=_optional_int(row, "prompt_message_id"),
    )


def _optional_int(row, column: str) -> int | None:
    keys = row.keys() if hasattr(row, "keys") else ()
    if column not in keys:
        return None
    value = row[column]
    return int(value) if value is not None else None


def _optional_str(row, column: str) -> str | None:
    keys = row.keys() if hasattr(row, "keys") else ()
    if column not in keys:
        return None
    value = row[column]
    return str(value) if value is not None else None
