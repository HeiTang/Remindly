from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from remindly.reminders.models import ReminderDraft
from remindly.reminders.reports import REPORT_REASONS, parser_source, report_attachment
from remindly.storage.session_stores import row_to_draft
from remindly.storage.sqlite import ReminderRepository


class ReportStore:
    def __init__(self, repository: ReminderRepository) -> None:
        self._repository = repository

    def get_draft(
        self, draft_id: str, revision: str, user_id: int, now: datetime
    ) -> ReminderDraft | None:
        with self._repository.connect() as connection:
            return self._get_draft(connection, draft_id, revision, user_id, now)

    def submit(
        self,
        draft_id: str,
        revision: str,
        user_id: int,
        now: datetime,
        *,
        reporter_username: str | None,
        reason: str,
        include_attachment: bool,
    ) -> bool | None:
        """Return True on insert, False on duplicate, None for an unavailable draft."""
        if reason not in REPORT_REASONS:
            return None
        with self._repository.connect() as connection:
            connection.execute("begin immediate")
            draft = self._get_draft(connection, draft_id, revision, user_id, now)
            if draft is None:
                return None
            source = parser_source(draft)
            result = draft.parse_result
            inserted = connection.execute(
                """insert into reminder_parse_reports
                (draft_id, reported_at, reason, parser_source,
                 provider, model, bot_version, attachment_json, reporter_user_id, reporter_username)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(draft_id) do nothing""",
                (
                    draft.id,
                    now.astimezone(UTC).isoformat(),
                    reason,
                    source,
                    result.get("provider") if source == "ai" else None,
                    result.get("model") if source == "ai" else None,
                    result["report_bot_version"],
                    json.dumps(report_attachment(draft), ensure_ascii=False)
                    if include_attachment
                    else None,
                    user_id,
                    reporter_username or None,
                ),
            )
            return inserted.rowcount == 1

    @staticmethod
    def _get_draft(
        connection: sqlite3.Connection, draft_id: str, revision: str, user_id: int, now: datetime
    ) -> ReminderDraft | None:
        row = connection.execute(
            "select * from reminder_drafts where id = ? and creator_user_id = ?",
            (draft_id, user_id),
        ).fetchone()
        if row is None:
            return None
        draft = row_to_draft(row)
        if (
            not draft.is_complete
            or draft.expires_at is None
            or draft.expires_at <= now
            or draft.parse_result.get("report_revision") != revision
        ):
            return None
        return draft
