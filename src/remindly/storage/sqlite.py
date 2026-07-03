from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from remindly.reminders.models import (
    MentionKind,
    Participant,
    Reminder,
    ReminderStatus,
)
from remindly.reminders.recurrence import deserialize_rule, serialize_rule
from remindly.storage.migrations import migrate_sqlite_database


class ReminderRepository:
    """ReminderRepository Protocol 的 SQLite 實作。"""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._database_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> None:
        with self.connect() as connection:
            migrate_sqlite_database(connection)

    def upsert_user(
        self,
        user_id: int,
        username: str | None,
        first_name: str | None,
        now: datetime,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                insert into users (id, username, first_name, updated_at)
                values (?, ?, ?, ?)
                on conflict(id) do update set
                    username = excluded.username,
                    first_name = excluded.first_name,
                    updated_at = excluded.updated_at
                """,
                (user_id, username, first_name, now.isoformat()),
            )

    def upsert_chat(
        self,
        chat_id: int,
        chat_type: str,
        title: str | None,
        username: str | None,
        now: datetime,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                insert into chats (id, type, title, username, updated_at)
                values (?, ?, ?, ?, ?)
                on conflict(id) do update set
                    type = excluded.type,
                    title = excluded.title,
                    username = excluded.username,
                    updated_at = excluded.updated_at
                """,
                (chat_id, chat_type, title, username, now.isoformat()),
            )

    def is_chat_natural_language_enabled(self, chat_id: int) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "select natural_language_enabled from chat_settings where chat_id = ?",
                (chat_id,),
            ).fetchone()
        return bool(row and int(row["natural_language_enabled"]) == 1)

    def set_chat_natural_language_enabled(
        self,
        chat_id: int,
        enabled: bool,
        updated_by_user_id: int,
        now: datetime,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                insert into chat_settings (
                    chat_id, natural_language_enabled, updated_by_user_id, created_at, updated_at
                )
                values (?, ?, ?, ?, ?)
                on conflict(chat_id) do update set
                    natural_language_enabled = excluded.natural_language_enabled,
                    updated_by_user_id = excluded.updated_by_user_id,
                    updated_at = excluded.updated_at
                """,
                (chat_id, int(enabled), updated_by_user_id, now.isoformat(), now.isoformat()),
            )

    def get_user_display_name(self, user_id: int) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "select username, first_name from users where id = ? limit 1",
                (user_id,),
            ).fetchone()
        if not row:
            return None
        if row["username"]:
            return f"@{row['username']}"
        if row["first_name"]:
            return str(row["first_name"])
        return None

    def create_reminder(self, reminder: Reminder, participants: list[Participant]) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                insert into reminders (
                    id, short_id, chat_id, chat_type, creator_user_id, title, remind_at,
                    timezone, status, source_text, parse_result, created_at, updated_at,
                    recurrence
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reminder.id,
                    reminder.short_id,
                    reminder.chat_id,
                    reminder.chat_type,
                    reminder.creator_user_id,
                    reminder.title,
                    reminder.remind_at.isoformat(),
                    reminder.timezone,
                    reminder.status.value,
                    reminder.source_text,
                    json.dumps(reminder.parse_result, ensure_ascii=False),
                    reminder.created_at.isoformat(),
                    reminder.updated_at.isoformat(),
                    serialize_rule(reminder.recurrence) if reminder.recurrence else None,
                ),
            )

            connection.executemany(
                """
                insert into reminder_participants (
                    id, reminder_id, user_id, username, display_name, mention_kind
                )
                values (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        f"{reminder.id}:{index}",
                        reminder.id,
                        participant.user_id,
                        participant.username,
                        participant.display_name,
                        participant.mention_kind.value,
                    )
                    for index, participant in enumerate(participants)
                ],
            )

    def list_pending(self, chat_id: int, limit: int = 10) -> list[Reminder]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                select * from reminders
                where chat_id = ? and status = ?
                order by julianday(remind_at) asc
                limit ?
                """,
                (chat_id, ReminderStatus.PENDING.value, limit),
            ).fetchall()

        return [row_to_reminder(row) for row in rows]

    def get_by_short_id(self, chat_id: int, short_id: str) -> Reminder | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                select * from reminders
                where chat_id = ? and upper(short_id) = upper(?)
                limit 1
                """,
                (chat_id, short_id),
            ).fetchone()

        return row_to_reminder(row) if row else None

    def list_participants(self, reminder_id: str) -> list[Participant]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                select * from reminder_participants
                where reminder_id = ?
                order by id asc
                """,
                (reminder_id,),
            ).fetchall()

        return [row_to_participant(row) for row in rows]

    def cancel(self, chat_id: int, short_id: str, actor_user_id: int) -> Reminder | None:
        now = datetime.now().astimezone().isoformat()
        with self.connect() as connection:
            row = connection.execute(
                """
                select * from reminders
                where chat_id = ? and upper(short_id) = upper(?) and status = ?
                limit 1
                """,
                (chat_id, short_id, ReminderStatus.PENDING.value),
            ).fetchone()
            if not row:
                return None

            if int(row["creator_user_id"]) != actor_user_id:
                return None

            connection.execute(
                """
                update reminders
                set status = ?, updated_at = ?
                where id = ? and status = ?
                """,
                (
                    ReminderStatus.CANCELLED.value,
                    now,
                    row["id"],
                    ReminderStatus.PENDING.value,
                ),
            )

        return replace(row_to_reminder(row), status=ReminderStatus.CANCELLED)

    def claim_due(self, now: datetime, limit: int = 20) -> list[Reminder]:
        claimed: list[Reminder] = []
        now_text = now.isoformat()
        with self.connect() as connection:
            # `julianday()` 把 ISO 字串轉成絕對時間（Julian Day 浮點），
            # 讓不同 tz offset（例如 `+08:00` vs `+00:00`）的相同瞬間能正確比對。
            # 只靠字典序比對 ISO 字串在跨時區部署時會漏掉 reminders。
            rows = connection.execute(
                """
                select * from reminders
                where status = ? and julianday(remind_at) <= julianday(?)
                order by julianday(remind_at) asc
                limit ?
                """,
                (ReminderStatus.PENDING.value, now_text, limit),
            ).fetchall()

            for row in rows:
                result = connection.execute(
                    """
                    update reminders
                    set status = ?, updated_at = ?
                    where id = ? and status = ?
                    """,
                    (
                        ReminderStatus.FIRING.value,
                        now_text,
                        row["id"],
                        ReminderStatus.PENDING.value,
                    ),
                )
                if result.rowcount == 1:
                    claimed.append(replace(row_to_reminder(row), status=ReminderStatus.FIRING))

        return claimed

    def mark_fired(self, reminder_id: str, now: datetime) -> None:
        self._mark(reminder_id, ReminderStatus.FIRED, now, fired_at=now)

    def mark_failed(self, reminder_id: str, now: datetime) -> None:
        self._mark(reminder_id, ReminderStatus.FAILED, now)

    def reschedule(self, reminder_id: str, next_at: datetime, now: datetime) -> None:
        """把已送出的週期性提醒轉回 PENDING 並更新到下一次觸發時間。
        只有處於 FIRING 狀態才會被更新，避免競爭條件重複重排。"""
        with self.connect() as connection:
            connection.execute(
                """
                update reminders
                set status = ?, remind_at = ?, fired_at = null, updated_at = ?
                where id = ? and status = ?
                """,
                (
                    ReminderStatus.PENDING.value,
                    next_at.isoformat(),
                    now.isoformat(),
                    reminder_id,
                    ReminderStatus.FIRING.value,
                ),
            )

    def _mark(
        self,
        reminder_id: str,
        status: ReminderStatus,
        now: datetime,
        *,
        fired_at: datetime | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                update reminders
                set status = ?, updated_at = ?, fired_at = coalesce(?, fired_at)
                where id = ? and status = ?
                """,
                (
                    status.value,
                    now.isoformat(),
                    fired_at.isoformat() if fired_at else None,
                    reminder_id,
                    ReminderStatus.FIRING.value,
                ),
            )

    def snooze(
        self,
        chat_id: int,
        short_id: str,
        actor_user_id: int,
        remind_at: datetime,
        now: datetime,
    ) -> Reminder | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                select * from reminders
                where chat_id = ?
                  and upper(short_id) = upper(?)
                  and status in (?, ?)
                limit 1
                """,
                (
                    chat_id,
                    short_id,
                    ReminderStatus.FIRING.value,
                    ReminderStatus.FIRED.value,
                ),
            ).fetchone()
            if not row or int(row["creator_user_id"]) != actor_user_id:
                return None

            connection.execute(
                """
                update reminders
                set status = ?, remind_at = ?, fired_at = null, updated_at = ?
                where id = ? and status in (?, ?)
                """,
                (
                    ReminderStatus.PENDING.value,
                    remind_at.isoformat(),
                    now.isoformat(),
                    row["id"],
                    ReminderStatus.FIRING.value,
                    ReminderStatus.FIRED.value,
                ),
            )

        return replace(
            row_to_reminder(row),
            status=ReminderStatus.PENDING,
            remind_at=remind_at,
            updated_at=now,
        )

    def update_title(
        self,
        chat_id: int,
        short_id: str,
        actor_user_id: int,
        title: str,
        now: datetime,
    ) -> Reminder | None:
        with self.connect() as connection:
            row = self._pending_for_actor(connection, chat_id, short_id, actor_user_id)
            if not row:
                return None

            connection.execute(
                """
                update reminders
                set title = ?, updated_at = ?
                where id = ? and status = ?
                """,
                (title, now.isoformat(), row["id"], ReminderStatus.PENDING.value),
            )

        return replace(row_to_reminder(row), title=title, updated_at=now)

    def update_remind_at(
        self,
        chat_id: int,
        short_id: str,
        actor_user_id: int,
        remind_at: datetime,
        now: datetime,
    ) -> Reminder | None:
        with self.connect() as connection:
            row = self._pending_for_actor(connection, chat_id, short_id, actor_user_id)
            if not row:
                return None

            connection.execute(
                """
                update reminders
                set remind_at = ?, updated_at = ?
                where id = ? and status = ?
                """,
                (remind_at.isoformat(), now.isoformat(), row["id"], ReminderStatus.PENDING.value),
            )

        return replace(row_to_reminder(row), remind_at=remind_at, updated_at=now)

    def _pending_for_actor(
        self,
        connection: sqlite3.Connection,
        chat_id: int,
        short_id: str,
        actor_user_id: int,
    ) -> sqlite3.Row | None:
        row = connection.execute(
            """
            select * from reminders
            where chat_id = ? and upper(short_id) = upper(?) and status = ?
            limit 1
            """,
            (chat_id, short_id, ReminderStatus.PENDING.value),
        ).fetchone()
        if not row or int(row["creator_user_id"]) != actor_user_id:
            return None
        return row

    def get_user_timezone(self, user_id: int, default_timezone: str) -> str:
        with self.connect() as connection:
            row = connection.execute(
                "select timezone from user_settings where user_id = ?",
                (user_id,),
            ).fetchone()
        return str(row["timezone"]) if row else default_timezone

    def set_user_timezone(self, user_id: int, timezone: str) -> None:
        now = datetime.now().astimezone().isoformat()
        with self.connect() as connection:
            connection.execute(
                """
                insert into user_settings (user_id, timezone, created_at, updated_at)
                values (?, ?, ?, ?)
                on conflict(user_id) do update set
                    timezone = excluded.timezone,
                    updated_at = excluded.updated_at
                """,
                (user_id, timezone, now, now),
            )


def row_to_reminder(row: sqlite3.Row) -> Reminder:
    # sqlite3.Row 的 `in` 迭代 values 而非 keys，所以必須顯式取 .keys()。
    recurrence_raw = row["recurrence"] if "recurrence" in row.keys() else None  # noqa: SIM118
    return Reminder(
        id=str(row["id"]),
        short_id=str(row["short_id"]),
        chat_id=int(row["chat_id"]),
        chat_type=str(row["chat_type"]),
        creator_user_id=int(row["creator_user_id"]),
        title=str(row["title"]),
        remind_at=datetime.fromisoformat(str(row["remind_at"])),
        timezone=str(row["timezone"]),
        status=ReminderStatus(str(row["status"])),
        source_text=str(row["source_text"]),
        parse_result=json.loads(str(row["parse_result"])),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
        recurrence=deserialize_rule(str(recurrence_raw)) if recurrence_raw else None,
    )


def row_to_participant(row: sqlite3.Row) -> Participant:
    user_id = row["user_id"]
    return Participant(
        user_id=int(user_id) if user_id is not None else None,
        username=row["username"],
        display_name=str(row["display_name"]),
        mention_kind=MentionKind(str(row["mention_kind"])),
    )
