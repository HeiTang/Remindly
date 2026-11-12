from __future__ import annotations

import secrets
from datetime import UTC, datetime

from remindly import __version__
from remindly.reminders.models import ReminderDraft
from remindly.reminders.recurrence import format_rule

REPORT_REASONS = {
    "time": "時間錯了",
    "title": "事項錯了",
    "participant": "對象錯了",
    "recurrence": "重複設定錯了",
    "other": "其他",
}

PARSER_SOURCES = {
    "ai": "AI",
    "rules": "內建規則",
    "rules_fallback": "AI 降級後使用內建規則",
}

ATTACHMENT_FIELDS = {
    "source_text": "原句",
    "title": "解析事項",
    "remind_at": "解析時間",
    "participants": "解析對象",
    "recurrence": "重複設定",
    "timezone": "時區",
    "parsed_at": "解析當下",
}


def stamp_report_context(draft: ReminderDraft, now: datetime) -> None:
    draft.parse_result.update(
        report_revision=secrets.token_hex(5),
        report_parsed_at=now.astimezone(UTC).isoformat(),
        report_bot_version=__version__,
    )


def parser_source(draft: ReminderDraft) -> str:
    if draft.parse_result.get("grain") == "ai":
        return "ai"
    return "rules_fallback" if draft.parse_result.get("ai_fallback") else "rules"


def report_attachment(draft: ReminderDraft) -> dict[str, str]:
    # Keep the exact preview within Telegram's message limit, even for long originals.
    return {
        "source_text": _excerpt(draft.source_text, 750),
        "title": _excerpt(draft.title or "", 500),
        "remind_at": draft.remind_at.isoformat() if draft.remind_at else "",
        "participants": _excerpt(", ".join(p.display_name for p in draft.participants), 250),
        "recurrence": _excerpt(format_rule(draft.recurrence), 250) if draft.recurrence else "無",
        "timezone": draft.timezone,
        "parsed_at": str(draft.parse_result["report_parsed_at"]),
    }


def _excerpt(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"
