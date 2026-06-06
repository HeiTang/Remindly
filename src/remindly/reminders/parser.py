from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from remindly.reminders.models import MentionKind, ParseResult, Participant
from remindly.reminders.text import normalize_spaces
from remindly.telegram.models import TelegramMessage, TelegramUser

WEEKDAY_MAP = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
}

CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "兩": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

TIME_RE = re.compile(
    r"(?P<period>凌晨|早上|上午|中午|下午|晚上|夜晚)?\s*"
    r"(?P<hour>\d{1,2}|[零〇一二兩两三四五六七八九十]{1,3})"
    r"\s*(?:點|点|:|：)"
    r"\s*(?P<minute>\d{1,2}|半|[零〇一二兩两三四五六七八九十]{1,3})?"
    r"\s*(?:分)?"
)

RELATIVE_RE = re.compile(
    r"(?P<amount>半|\d+|[零〇一二兩两三四五六七八九十]{1,3})\s*"
    r"(?P<unit>分鐘|分|小時|小时|個小時|个小时|天|日)\s*後"
)

ABSOLUTE_RE = re.compile(
    r"(?P<year>\d{4})[-/](?P<month>\d{1,2})[-/](?P<day>\d{1,2})"
    r"(?:\s+(?P<hour>\d{1,2})[:：](?P<minute>\d{1,2}))?"
)


PART_OF_DAY_RE = re.compile(
    r"(?P<day>今|明|後)?"
    r"(?P<period>早上|早晨|早|上午|中午|下午|晚上|晚|今晚)"
)

WEEKDAY_RE = re.compile(
    r"(?P<prefix>這|本|下)?"
    r"(?:週|周|禮拜|礼拜|星期)"
    r"(?P<weekday>[一二三四五六日天])"
)

DEFAULT_PERIOD_TIME = {
    "早上": (8, 0),
    "早晨": (8, 0),
    "早": (8, 0),
    "上午": (9, 0),
    "中午": (12, 0),
    "下午": (12, 0),
    "晚上": (20, 0),
    "晚": (20, 0),
    "今晚": (20, 0),
}


@dataclass(frozen=True)
class TimeParse:
    remind_at: datetime | None
    consumed_text: str
    grain: str
    missing_time: bool = False
    is_past: bool = False


class ReminderParser:
    def __init__(self, default_timezone: str) -> None:
        self._default_timezone = default_timezone

    def parse(
        self,
        text: str,
        message: TelegramMessage,
        now: datetime | None = None,
    ) -> ParseResult:
        zone = ZoneInfo(self._default_timezone)
        reference = now or datetime.now(zone)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=zone)

        cleaned = normalize_spaces(text)
        participants = self._extract_participants(cleaned, message)
        time_parse = self._parse_time(cleaned, reference, zone)
        title = self._extract_title(cleaned, time_parse.consumed_text, participants)

        missing_fields: list[str] = []
        if time_parse.remind_at is None or time_parse.missing_time or time_parse.is_past:
            missing_fields.append("time")

        if not title:
            missing_fields.append("title")

        if not participants:
            missing_fields.append("participants")

        confidence = 0.95 if not missing_fields else 0.55
        raw = {
            "source_text": text,
            "consumed_time_text": time_parse.consumed_text,
            "grain": time_parse.grain,
            "missing_fields": missing_fields,
            "is_past": time_parse.is_past,
            "confidence": confidence,
        }
        return ParseResult(
            title=title,
            remind_at=time_parse.remind_at,
            participants=tuple(participants),
            missing_fields=tuple(missing_fields),
            confidence=confidence,
            raw=raw,
        )

    def parse_time_only(
        self,
        text: str,
        now: datetime,
        existing_date: datetime | None = None,
    ) -> TimeParse:
        zone = ZoneInfo(self._default_timezone)
        reference = now.astimezone(zone)
        parsed = self._parse_time(text, reference, zone, existing_date=existing_date)
        if parsed.remind_at and parsed.remind_at <= reference:
            return TimeParse(
                remind_at=parsed.remind_at,
                consumed_text=parsed.consumed_text,
                grain=parsed.grain,
                is_past=True,
            )
        return parsed

    def _parse_time(
        self,
        text: str,
        now: datetime,
        zone: ZoneInfo,
        *,
        existing_date: datetime | None = None,
    ) -> TimeParse:
        relative = RELATIVE_RE.search(text)
        if relative:
            amount = parse_relative_amount(relative.group("amount"), relative.group("unit"))
            unit = relative.group("unit")
            delta = timedelta(minutes=amount)
            if "小時" in unit or "小时" in unit:
                delta = timedelta(hours=amount)
            elif unit in {"天", "日"}:
                delta = timedelta(days=amount)
            return TimeParse(now + delta, relative.group(0), "minute")

        absolute = ABSOLUTE_RE.search(text)
        if absolute:
            year = int(absolute.group("year"))
            month = int(absolute.group("month"))
            day = int(absolute.group("day"))
            hour = absolute.group("hour")
            minute = absolute.group("minute")
            if hour is None:
                return TimeParse(
                    datetime(year, month, day, tzinfo=zone),
                    absolute.group(0),
                    "day",
                    missing_time=True,
                )
            remind_at = datetime(year, month, day, int(hour), int(minute), tzinfo=zone)
            return TimeParse(remind_at, absolute.group(0), "minute", is_past=remind_at <= now)

        base_date, date_text, missing_time = self._parse_date(text, now)
        if existing_date is not None and not date_text:
            base_date = existing_date

        time_match = TIME_RE.search(text)
        if time_match:
            hour, minute = self._parse_clock(time_match)
            remind_at = base_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if not date_text and remind_at <= now:
                remind_at += timedelta(days=1)
            return TimeParse(
                remind_at,
                normalize_spaces(f"{date_text} {time_match.group(0)}"),
                "minute",
                is_past=remind_at <= now,
            )

        part_of_day = self._parse_part_of_day(text, now, base_date, date_text)
        if part_of_day:
            return part_of_day

        if date_text and missing_time:
            return TimeParse(base_date, date_text, "day", missing_time=True)

        return TimeParse(None, "", "unknown")

    def _parse_date(self, text: str, now: datetime) -> tuple[datetime, str, bool]:
        if "後天" in text:
            return now + timedelta(days=2), "後天", True
        if "明天" in text or "明日" in text:
            matched = "明天" if "明天" in text else "明日"
            return now + timedelta(days=1), matched, True
        if "今天" in text or "今日" in text:
            matched = "今天" if "今天" in text else "今日"
            return now, matched, True

        weekday = WEEKDAY_RE.search(text)
        if weekday:
            target = WEEKDAY_MAP[weekday.group("weekday")]
            days = days_until_weekday(now.weekday(), target, weekday.group("prefix"))
            return now + timedelta(days=days), weekday.group(0), True

        return now, "", False

    def _parse_part_of_day(
        self,
        text: str,
        now: datetime,
        base_date: datetime,
        date_text: str,
    ) -> TimeParse | None:
        match = PART_OF_DAY_RE.search(text)
        if not match:
            return None

        period_text = match.group("period")
        joined_text = f"{date_text}{match.group(0)}"
        consumed_text = joined_text if joined_text in text else normalize_spaces(
            f"{date_text} {match.group(0)}"
        )
        if period_text == "今晚":
            period_text = "晚上"
            consumed_text = "今晚"

        target_date = base_date
        if match.group("day") == "明" and not date_text:
            target_date = now + timedelta(days=1)
        elif match.group("day") == "後" and not date_text:
            target_date = now + timedelta(days=2)

        hour, minute = DEFAULT_PERIOD_TIME[period_text]
        remind_at = target_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if date_text and match.group("day") is None:
            return TimeParse(remind_at, consumed_text, "period", missing_time=True)

        if not date_text and match.group("day") is None and remind_at <= now:
            remind_at += timedelta(days=1)

        return TimeParse(remind_at, consumed_text, "period", is_past=remind_at <= now)

    def _parse_clock(self, match: re.Match[str]) -> tuple[int, int]:
        hour = parse_number(match.group("hour"))
        minute_text = match.group("minute")
        minute = 0
        if minute_text == "半":
            minute = 30
        elif minute_text:
            minute = parse_number(minute_text)

        period = match.group("period") or ""
        if period in {"下午", "晚上", "夜晚"} and hour < 12:
            hour += 12
        if period == "中午" and hour < 11:
            hour += 12
        if period == "凌晨" and hour == 12:
            hour = 0

        return hour, minute

    def _extract_participants(self, text: str, message: TelegramMessage) -> list[Participant]:
        participants: list[Participant] = []
        if "我" in text or not re.search(r"@\w+", text):
            from_user = message.from_user
            if from_user:
                participants.append(participant_from_user(from_user))

        for username in re.findall(r"@([A-Za-z0-9_]{5,32})", text):
            participants.append(
                Participant(
                    display_name=f"@{username}",
                    mention_kind=MentionKind.USERNAME,
                    username=username,
                )
            )

        if message.reply_to_message and message.reply_to_message.from_user:
            participants.append(participant_from_user(message.reply_to_message.from_user))

        return dedupe_participants(participants)

    def _extract_title(
        self,
        text: str,
        consumed_time_text: str,
        participants: list[Participant],
    ) -> str | None:
        title = text
        if consumed_time_text:
            title = title.replace(consumed_time_text, " ")

        title = RELATIVE_RE.sub(" ", title)
        title = ABSOLUTE_RE.sub(" ", title)
        title = TIME_RE.sub(" ", title)
        title = re.sub(r"(今天|今日|明天|明日|後天|今晚|明早|明晚|後早|後晚)", " ", title)
        title = WEEKDAY_RE.sub(" ", title)
        title = re.sub(r"^@\w+\s*", " ", title)
        title = re.sub(r"@\w+", " ", title)
        title = re.sub(r"\b提醒(?:我|我們|大家)?\b", " ", title)
        title = title.replace("提醒我", " ")
        title = title.replace("提醒我們", " ")
        title = title.replace("提醒大家", " ")
        title = re.sub(r"(我|我們|大家|和|跟|與|要)", " ", title)

        for participant in participants:
            title = title.replace(participant.display_name, " ")

        title = normalize_spaces(title)
        return title or None


def participant_from_user(user: TelegramUser) -> Participant:
    if user.username:
        return Participant(
            display_name=f"@{user.username}",
            mention_kind=MentionKind.USERNAME,
            user_id=user.id,
            username=user.username,
        )

    return Participant(
        display_name=user.first_name,
        mention_kind=MentionKind.TEXT_MENTION,
        user_id=user.id,
    )


def dedupe_participants(participants: list[Participant]) -> list[Participant]:
    seen: set[str] = set()
    deduped: list[Participant] = []
    for participant in participants:
        key = participant.stable_key
        if key in seen:
            continue
        seen.add(key)
        deduped.append(participant)
    return deduped


def parse_relative_amount(value: str, unit: str) -> float:
    if value == "半":
        return 30 if unit in {"分鐘", "分"} else 0.5
    return parse_number(value)


def days_until_weekday(current_weekday: int, target_weekday: int, prefix: str | None) -> int:
    days = (target_weekday - current_weekday) % 7
    if prefix in {"下"} or days == 0:
        return days + 7
    return days


def parse_number(value: str) -> int:
    value = value.strip()
    if value.isdigit():
        return int(value)
    if value == "十":
        return 10
    if value.startswith("十"):
        return 10 + CHINESE_DIGITS.get(value[-1], 0)
    if "十" in value:
        tens, ones = value.split("十", 1)
        return CHINESE_DIGITS.get(tens, 1) * 10 + (CHINESE_DIGITS.get(ones, 0) if ones else 0)

    total = 0
    for char in value:
        total = total * 10 + CHINESE_DIGITS[char]
    return total
