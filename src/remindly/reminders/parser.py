from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

from remindly.reminders.models import (
    MentionKind,
    ParseResult,
    Participant,
    RecurrenceError,
    RecurrencePeriod,
    RecurrenceRule,
)
from remindly.reminders.recurrence import (
    INTERVAL_MIN_SECONDS,
    is_valid_month_day,
    next_fire,
)
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

# 時間片語包含尾接的「（的）時候」，消耗範圍與時間辨識共用。
TIME_SUFFIX = r"(?:\s*(?:的\s*)?時候)?"

TIME_RE = re.compile(
    r"(?P<period>凌晨|早上|上午|中午|下午|晚上|夜晚)?\s*"
    r"(?P<hour>\d{1,2}|[零〇一二兩两三四五六七八九十]{1,3})"
    r"\s*(?:點|点|:|：)"
    r"\s*(?P<minute>\d{1,2}|半|[零〇一二兩两三四五六七八九十]{1,3})?"
    r"\s*(?:分)?" + TIME_SUFFIX
)

RELATIVE_RE = re.compile(
    r"(?P<amount>半|\d+|[零〇一二兩两三四五六七八九十]{1,3})\s*"
    r"(?P<unit>分鐘|分|小時|小时|個小時|个小时"
    r"|個星期|个星期|星期|週|周"
    r"|個月|个月|天|日)\s*後" + TIME_SUFFIX
)

# 「N 分」（不含 分鐘），用於「19 分要...」= 當前小時 + N 分。
# 若當前分鐘已過就滾到下一個整點。
PARTIAL_MINUTE_RE = re.compile(r"(?P<minute>\d{1,2})\s*分(?![鐘钟])" + TIME_SUFFIX)

ABSOLUTE_RE = re.compile(
    r"(?P<year>\d{4})[-/](?P<month>\d{1,2})[-/](?P<day>\d{1,2})"
    r"(?:\s+(?P<hour>\d{1,2})[:：](?P<minute>\d{1,2}))?" + TIME_SUFFIX
)

# 短日期 M/D 或 M-D，邊界排除日期分隔符，避免吃到完整年份日期的一部分。
SHORT_DATE_RE = re.compile(r"(?<![\d/-])(?P<month>\d{1,2})[/-](?P<day>\d{1,2})(?![\d/-])")

# 中文日期 M月D號 或 M月D日
CHINESE_DATE_RE = re.compile(
    r"(?P<month>\d{1,2}|[一二三四五六七八九十]+)月"
    r"(?P<day>\d{1,2}|[一二三四五六七八九十]+)[號号日]"
)


PART_OF_DAY_RE = re.compile(
    r"(?P<day>今|明|後)?"
    r"(?P<period>早上|早晨|上午|中午|下午|晚上|今晚|"
    r"[早晚](?(day)|(?=$|\s|(?:的\s*)?時候|"
    r"[\d零〇一二兩两三四五六七八九十]+\s*[點点:：])))" + TIME_SUFFIX
)

WEEKDAY_RE = re.compile(
    r"(?P<prefix>(?:下)+|這|本)?"
    r"(?:週|周|禮拜|礼拜|星期)"
    r"(?P<weekday>[一二三四五六日天])"
)

REL_DAY_RE = re.compile(r"(?P<kw>後天|明天|明日|今天|今日)")

# 週期性提醒 marker：以 "每" 開頭。
# 每天 / 每日 — daily
RECURRENCE_DAILY_RE = re.compile(r"每(?:天|日)")

# 每週一 / 每禮拜一二三 / 每星期一、三、五 — weekly
# 群組 chars 允許用 、 , ，或空白分隔
RECURRENCE_WEEKLY_RE = re.compile(
    r"每(?:週|周|禮拜|礼拜|星期)\s*"
    r"(?P<weekdays>[一二三四五六日天](?:[、,，\s]*[一二三四五六日天])*)"
)

# 每個月 1 號 / 每月 1, 18, 25 號 — monthly
RECURRENCE_MONTHLY_RE = re.compile(
    r"每(?:個)?月\s*"
    r"(?P<days>\d{1,2}(?:\s*[、,，]\s*\d{1,2})*)"
    r"\s*[號号日]"
)

# 每年 12月25號 / 每年 12/25 — yearly
RECURRENCE_YEARLY_RE = re.compile(
    r"每年\s*"
    r"(?:(?P<cn_month>\d{1,2})月\s*(?P<cn_day>\d{1,2})\s*[號号日]?"
    r"|(?P<slash_month>\d{1,2})/(?P<slash_day>\d{1,2}))"
)

# 每 N 分鐘 / 每 N 小時 / 每 N 天 / 每 N 週 — interval
# 「日/週」是常見別名。n 用 \d+ 是為了 catch「每 0 分鐘」這種明顯錯誤而給
# 專屬 reason（如果用 [1-9]\d* 排除 0，「每 0 分鐘」會 fall through 到一次性
# parser 然後追問「什麼時候提醒？」，體驗更差）；下界 policy 由
# INTERVAL_MIN_SECONDS 控，太大的 n 由 OverflowError catch。
RECURRENCE_INTERVAL_RE = re.compile(r"每\s*(?P<n>\d+)\s*(?P<unit>分鐘|小時|天|日|週|周)")

# INTERVAL 單位 → 秒數。日/天、週/周為別名。
_INTERVAL_UNIT_SECONDS: dict[str, int] = {
    "分鐘": 60,
    "小時": 3600,
    "天": 24 * 3600,
    "日": 24 * 3600,
    "週": 7 * 24 * 3600,
    "周": 7 * 24 * 3600,
}

# 切割 '提醒我/我們/大家' 的錨點；用於「以動詞切段」策略
PROMPT_SPLIT_RE = re.compile(r"提醒(?:我們|我|大家)?")

# 內容區前綴清理：連接詞 / 主詞代名詞
LEADING_CONNECTOR_RE = re.compile(r"^\s*(?:和|跟|與|我們|我|大家)\s*")

# 時間區前綴：允許 '在' 開頭
LEADING_ZAI_RE = re.compile(r"^\s*(?:在\s*)?")

DEFAULT_PERIOD_TIME = {
    "早上": (8, 0),
    "早晨": (8, 0),
    "早": (8, 0),
    "上午": (9, 0),
    "中午": (12, 0),
    "下午": (15, 0),
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
    consumed_spans: tuple[tuple[int, int], ...] = ()


class ReminderParser:
    def __init__(self, default_timezone: str) -> None:
        self._default_timezone = default_timezone

    def parse(
        self,
        text: str,
        message: TelegramMessage,
        now: datetime | None = None,
    ) -> ParseResult:
        zone = now.tzinfo if now and now.tzinfo else ZoneInfo(self._default_timezone)
        reference = now or datetime.now(zone)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=zone)

        cleaned = normalize_spaces(text)
        participants = self.extract_participants(cleaned, message)

        # 週期性提醒優先：認出 每天/每週X/每月X號/每年 X 就直接產出 RecurrenceRule。
        recurrence = self._try_recurrence_parse(cleaned, reference, participants)
        if isinstance(recurrence, RecurrenceError):
            # marker 匹配但語法無效 — 讓 caller 走單輪拒絕 flow（不建 draft、不追問）。
            return ParseResult(
                title=None,
                remind_at=None,
                participants=tuple(participants),
                missing_fields=(),
                confidence=0.0,
                raw={"source_text": text, "grain": "recurrence_error"},
                recurrence_error=recurrence,
            )
        if recurrence is not None:
            rule, remind_at, title = recurrence
            missing_fields: list[str] = []
            if not title:
                missing_fields.append("title")
            if not participants:
                missing_fields.append("participants")
            confidence = 0.95 if not missing_fields else 0.55
            raw = {
                "source_text": text,
                "grain": "recurrence",
                "missing_fields": missing_fields,
                "confidence": confidence,
            }
            return ParseResult(
                title=title,
                remind_at=remind_at,
                participants=tuple(participants),
                missing_fields=tuple(missing_fields),
                confidence=confidence,
                raw=raw,
                recurrence=rule,
            )

        split = self._try_split_parse(cleaned, reference, zone, participants)
        if split is not None:
            time_parse, title = split
        else:
            time_parse = self._parse_time(cleaned, reference, zone)
            title = self._extract_title(cleaned, time_parse, participants)

        missing_fields = []
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
        zone = now.tzinfo or ZoneInfo(self._default_timezone)
        reference = now.replace(tzinfo=zone) if now.tzinfo is None else now
        return self._parse_time(text, reference, zone, existing_date=existing_date)

    def explicit_time(
        self, text: str, now: datetime, existing_date: datetime | None = None
    ) -> tuple[TimeParse, bool] | None:
        """只認明確提醒的時間區或整句時間；不搜尋事項中的日期。"""
        cleaned = normalize_spaces(text)
        zone = now.tzinfo or ZoneInfo(self._default_timezone)
        prompt = PROMPT_SPLIT_RE.search(cleaned)
        if prompt:
            left = cleaned[: prompt.start()].strip()
            left_result = self._parse_datetime(left, now, zone) if left else None
            if left_result and left_result[1] == len(left):
                if not left_result[0].missing_time:
                    return left_result[0], False
            elif prompt.start() != 0:
                return None
            right = self._parse_datetime(
                cleaned[prompt.end() :],
                now,
                zone,
                existing_date=left_result[0].remind_at if left_result else None,
            )
            if right:
                return right[0], False
            return (left_result[0], False) if left_result else None
        parsed = self._parse_datetime(cleaned, now, zone, existing_date=existing_date)
        if parsed is None:
            return None
        value, end = parsed
        if end == len(cleaned):
            return value, True
        if value.grain == "invalid":
            remainder = cleaned[end:].strip()
            tail = self._parse_datetime(remainder, now, zone)
            if tail and tail[1] == len(remainder):
                return value, True
        return None

    def _try_split_parse(
        self,
        cleaned: str,
        now: datetime,
        zone: tzinfo,
        participants: list[Participant],
    ) -> tuple[TimeParse, str | None] | None:
        """以『提醒(我/我們/大家)』為錨點切段。左邊當時間區，右邊當內容區。
        優先左邊完整時間；否則右邊前緣時間；否則左邊部分時間。"""
        match = PROMPT_SPLIT_RE.search(cleaned)
        if not match:
            return None

        time_zone_raw = cleaned[: match.start()]
        content_zone = cleaned[match.end() :]

        left_stripped = LEADING_ZAI_RE.sub("", time_zone_raw).strip()
        left_result = self._parse_datetime(left_stripped, now, zone) if left_stripped else None
        right_result = self._parse_datetime(content_zone, now, zone)

        # 1. 左邊有完整時間（含時鐘或明確 period+day 標記）
        if left_result and left_result[0].remind_at and not left_result[0].missing_time:
            time_parse = left_result[0]
            title = self._clean_content_title(content_zone, participants)
            return time_parse, title

        # 2. 右邊前緣有完整或部分時間
        if right_result and right_result[0].remind_at:
            time_parse, consumed_len = right_result
            remaining = content_zone[consumed_len:]
            title = self._clean_content_title(remaining, participants)
            return time_parse, title

        # 3. 左邊只有部分時間（date-only）
        if left_result and left_result[0].remind_at:
            time_parse = left_result[0]
            title = self._clean_content_title(content_zone, participants)
            return time_parse, title

        # 4. 都沒時間資訊
        if content_zone.strip():
            title = self._clean_content_title(content_zone, participants)
            return TimeParse(None, "", "unknown"), title

        return None

    def _try_recurrence_parse(
        self,
        cleaned: str,
        now: datetime,
        participants: list[Participant],
    ) -> tuple[RecurrenceRule, datetime, str | None] | RecurrenceError | None:
        """認出「每天 / 每週X / 每個月 X 號 / 每年 M/D」這類 marker，產出 RecurrenceRule。
        時鐘或時段必須在同一句出現；缺少時就 fall through 給一次性 parser。
        時區資訊由 `now.tzinfo` 帶著（`next_fire` 也用 `after.tzinfo`），
        不需要額外的 zone 參數。

        若 `recurrence_marker` 回傳 `RecurrenceError`（marker 匹配但無效），
        直接把 error 往上傳；`parse()` 會把它塞到 `ParseResult.recurrence_error` 讓
        router 做「單輪拒絕」的處理，不建 draft、不追問。
        """
        marker = self.recurrence_marker(cleaned)
        if marker is None:
            return None
        if isinstance(marker, RecurrenceError):
            return marker
        marker_text, period, extras = marker

        # INTERVAL 沒有 time-of-day，marker 自身已含所有時間資訊，next_fire 直接
        # 從 now 加 interval 就是首發時間。不需要 TIME_RE 命中。
        # 巨大的 n（例如「每 99999999999 週」）會讓 now + timedelta 超出
        # datetime.max，Python 拋 OverflowError；接住並轉成 RecurrenceError，
        # 避免 begin_create 崩潰。
        if period == RecurrencePeriod.INTERVAL:
            rule = RecurrenceRule(period=period, **extras)
            try:
                remind_at = next_fire(rule, now)
            except OverflowError:
                return RecurrenceError(
                    marker_text=marker_text,
                    reason="間隔太大，超出可支援範圍",
                )
            remaining = cleaned.replace(marker_text, " ", 1)
            remaining = re.sub(r"提醒(?:我們|我|大家)?", " ", remaining)
            title = self._clean_content_title(remaining, participants)
            return rule, remind_at, title

        time_text = cleaned.replace(marker_text, " ", 1)
        parsed_time = None
        for segment in PROMPT_SPLIT_RE.split(time_text, maxsplit=1):
            segment = LEADING_ZAI_RE.sub("", segment)
            if TIME_RE.match(segment) or PART_OF_DAY_RE.match(segment):
                parsed_time, _ = self._parse_datetime(segment, now, now.tzinfo)
                break
        if parsed_time is None:
            clock = TIME_RE.search(time_text)
            if clock:
                parsed_time, _ = self._parse_datetime(time_text[clock.start() :], now, now.tzinfo)
        if parsed_time is None:
            return None
        if parsed_time.grain == "invalid":
            return RecurrenceError(marker_text=marker_text, reason="時間格式無效")
        if parsed_time.remind_at is None or parsed_time.missing_time:
            return None
        hour, minute = parsed_time.remind_at.hour, parsed_time.remind_at.minute

        rule = RecurrenceRule(period=period, hour=hour, minute=minute, **extras)
        remind_at = next_fire(rule, now)

        # 標題：把 marker、時間片段、提醒(我/我們/大家)? 從 cleaned 挖掉，
        # 剩下的 participant / mention / 連接詞清理交給 `_clean_content_title`，
        # 讓週期與一次性路徑的 title 產出方式一致（例如都保留 `要`）。
        remaining = cleaned
        remaining = remaining.replace(marker_text, " ", 1)
        remaining = remaining.replace(parsed_time.consumed_text, " ", 1)
        remaining = re.sub(r"提醒(?:我們|我|大家)?", " ", remaining)
        title = self._clean_content_title(remaining, participants)
        return rule, remind_at, title

    def recurrence_marker(
        self, cleaned: str
    ) -> tuple[str, RecurrencePeriod, dict[str, object]] | RecurrenceError | None:
        """依序試 monthly / yearly / weekly / daily。回傳：
        - `(匹配字串, period, 額外欄位 dict)` — 規則有效，用來建 RecurrenceRule。
        - `RecurrenceError(marker_text, reason)` — regex 有匹配到 marker 但語法無效
          （日期超出範圍、月/日組合不存在等）。用來給 caller 產出「單輪拒絕」錯誤訊息。
        - `None` — 沒有 marker 匹配，fall through 給一次性 parser。

        Monthly 部分合法時（例：`1, 45 號` 只有 1 是合法）會保留合法日期、丟掉無效的；
        只有**全部無效**才會回傳 RecurrenceError（避免使用者一個 typo 就要重打整段）。

        INTERVAL 優先於其他 period 檢查：`每 10 分鐘` 不會被 DAILY/WEEKLY/YEARLY 的
        regex 撿到（它們只認「每天/每日」或「每年」等固定字），但保險起見放最前面。
        INTERVAL 若間隔低於 `INTERVAL_MIN_SECONDS` 一律回 RecurrenceError。
        """
        interval = RECURRENCE_INTERVAL_RE.search(cleaned)
        if interval:
            n = int(interval.group("n"))
            unit_seconds = _INTERVAL_UNIT_SECONDS[interval.group("unit")]
            seconds = n * unit_seconds
            if seconds <= 0:
                return RecurrenceError(
                    marker_text=interval.group(0),
                    reason="間隔需為正整數",
                )
            if seconds < INTERVAL_MIN_SECONDS:
                return RecurrenceError(
                    marker_text=interval.group(0),
                    reason=f"太頻繁，最低支援 {INTERVAL_MIN_SECONDS // 60} 分鐘",
                )
            return (
                interval.group(0),
                RecurrencePeriod.INTERVAL,
                {"interval_seconds": seconds},
            )

        monthly = RECURRENCE_MONTHLY_RE.search(cleaned)
        if monthly:
            raw_days = {int(d) for d in re.split(r"[、,，\s]+", monthly.group("days")) if d}
            # 過濾出 1..31 的合法日期。像「每個月 0 號」或「每個月 45 號」會被跳過。
            days = tuple(sorted(d for d in raw_days if 1 <= d <= 31))
            if days:
                return monthly.group(0), RecurrencePeriod.MONTHLY, {"month_days": days}
            # 全部日期都非法，回錯誤讓 Bot 具體告知使用者
            return RecurrenceError(
                marker_text=monthly.group(0),
                reason="日期無效，需在 1-31 範圍",
            )

        yearly = RECURRENCE_YEARLY_RE.search(cleaned)
        if yearly:
            month = int(yearly.group("cn_month") or yearly.group("slash_month"))
            day = int(yearly.group("cn_day") or yearly.group("slash_day"))
            if is_valid_month_day(month, day):
                return (
                    yearly.group(0),
                    RecurrencePeriod.YEARLY,
                    {"year_month": month, "year_day": day},
                )
            # 產出具體錯誤原因：月份先檢查，通過再檢查該月的天數上限
            if not 1 <= month <= 12:
                reason = "月份無效，需在 1-12 範圍"
            else:
                # 2028 是閏年：2 月最多 29 天（跟 `is_valid_month_day` 一致）。
                # 前綴「日期無效」跟 monthly/interval 的錯誤訊息維持一致，讓
                # render_recurrence_error 產出「『每年 2/30』日期無效，2 月最多
                # 29 天。」而不是像陳述事實的「『每年 2/30』2 月最多 29 天。」。
                max_day = calendar.monthrange(2028, month)[1]
                reason = f"日期無效，{month} 月最多 {max_day} 天"
            return RecurrenceError(marker_text=yearly.group(0), reason=reason)

        weekly = RECURRENCE_WEEKLY_RE.search(cleaned)
        if weekly:
            weekdays = tuple(
                sorted({WEEKDAY_MAP[c] for c in weekly.group("weekdays") if c in WEEKDAY_MAP})
            )
            if weekdays:
                return weekly.group(0), RecurrencePeriod.WEEKLY, {"weekdays": weekdays}

        daily = RECURRENCE_DAILY_RE.search(cleaned)
        if daily:
            return daily.group(0), RecurrencePeriod.DAILY, {}

        return None

    def _parse_datetime(
        self,
        text: str,
        now: datetime,
        zone: tzinfo,
        *,
        leading: bool = True,
        existing_date: datetime | None = None,
    ) -> tuple[TimeParse, int] | None:
        """共用時間文法；leading 只控制從前緣比對或在文字中搜尋。"""
        anchor = LEADING_ZAI_RE.match(text)
        start = anchor.end() if anchor else 0
        parts: list[re.Match[str]] = []

        def match(pattern: re.Pattern[str], pos: int = start) -> re.Match[str] | None:
            return (pattern.match if leading else pattern.search)(text, pos)

        def result(
            target: datetime | None, end: int, grain: str, *, missing: bool = False
        ) -> tuple[TimeParse, int]:
            spans: list[tuple[int, int]] = []
            for begin, finish in sorted(part.span() for part in parts):
                if spans and begin <= spans[-1][1]:
                    spans[-1] = (spans[-1][0], max(spans[-1][1], finish))
                else:
                    spans.append((begin, finish))
            consumed = text[:end] if leading else " ".join(text[a:b] for a, b in spans)
            return TimeParse(
                target,
                consumed,
                grain,
                missing_time=missing,
                is_past=target is not None and target.astimezone(UTC) <= now.astimezone(UTC),
                consumed_spans=tuple(spans),
            ), end

        relative = match(RELATIVE_RE)
        if relative:
            parts.append(relative)
            try:
                amount = parse_relative_amount(relative.group("amount"), relative.group("unit"))
                target = apply_relative_offset(now, amount, relative.group("unit"))
            except (ValueError, OverflowError):
                return result(None, relative.end(), "invalid")
            return result(target, relative.end(), "minute")

        date_dt: datetime | None = None
        date_end = start
        absolute = match(ABSOLUTE_RE)
        if absolute:
            parts.append(absolute)
            try:
                date_dt = datetime(
                    int(absolute.group("year")),
                    int(absolute.group("month")),
                    int(absolute.group("day")),
                    tzinfo=zone,
                )
                if absolute.group("hour") is not None:
                    target = date_dt.replace(
                        hour=int(absolute.group("hour")), minute=int(absolute.group("minute"))
                    )
                    return result(target, absolute.end(), "minute")
            except ValueError:
                return result(None, absolute.end(), "invalid")
            date_end = absolute.end()

        if date_dt is None:
            for pattern, convert in (
                (SHORT_DATE_RE, _short_date_to_datetime),
                (CHINESE_DATE_RE, _chinese_date_to_datetime),
            ):
                date_match = match(pattern)
                if date_match:
                    parts.append(date_match)
                    date_dt = convert(date_match, now, zone)
                    if date_dt is None:
                        return result(None, date_match.end(), "invalid")
                    date_end = date_match.end()
                    break

        if date_dt is None:
            weekday = match(WEEKDAY_RE)
            rel_day = match(REL_DAY_RE)
            if weekday:
                days = days_until_weekday(
                    now.weekday(), WEEKDAY_MAP[weekday.group("weekday")], weekday.group("prefix")
                )
                date_dt = now + timedelta(days=days)
                date_end = weekday.end()
                parts.append(weekday)
            elif rel_day:
                kw = rel_day.group("kw")
                days = 2 if kw == "後天" else 1 if kw in ("明天", "明日") else 0
                date_dt = now + timedelta(days=days)
                date_end = rel_day.end()
                parts.append(rel_day)

        cursor = date_end
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1

        # 搜尋模式也接受「23:55 明天」；前緣模式只消耗連續的時間片語。
        def match_time(pattern: re.Pattern[str]) -> re.Match[str] | None:
            found = match(pattern, cursor)
            if found is None and not leading and date_dt is not None:
                date_start = min(part.start() for part in parts)
                return pattern.search(text, start, date_start)
            return found

        clock = match_time(TIME_RE)
        pod = match_time(PART_OF_DAY_RE)
        if (
            pod
            and clock
            and (
                pod.start() > clock.start()
                or (pod.end() < clock.start() and text[pod.end() : clock.start()].strip())
            )
        ):
            pod = None
        pod_day = pod.group("day") if pod else None
        if pod:
            period = pod.group("period")
            period = "晚上" if period == "今晚" else period
            if not clock:
                clock = match(TIME_RE, pod.end())
        else:
            period = ""

        base = date_dt or (existing_date.astimezone(zone) if existing_date else now)
        if date_dt is None and pod_day:
            days = 1 if pod_day == "明" else 2 if pod_day == "後" else 0
            base = now + timedelta(days=days)

        if clock or pod:
            end = max(date_end, clock.end() if clock else 0, pod.end() if pod else 0)
            if clock:
                parts.append(clock)
            if pod and (not clock or pod.start() < clock.start()):
                parts.append(pod)
            try:
                hour, minute = (
                    self._parse_clock(clock, period=period)
                    if clock
                    else DEFAULT_PERIOD_TIME[period]
                )
                target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
            except ValueError:
                return result(None, end, "invalid")
            missing = clock is None and date_dt is not None and not pod_day
            if (
                date_dt is None
                and pod_day not in ("明", "後")
                and existing_date is None
                and target <= now
            ):
                target += timedelta(days=1)
            return result(target, end, "minute" if clock else "period", missing=missing)

        partial = match(PARTIAL_MINUTE_RE, cursor if leading else start)
        if partial and date_dt is None:
            parts.append(partial)
            minute = int(partial.group("minute"))
            if not 0 <= minute <= 59:
                return result(None, partial.end(), "invalid")
            target = base.replace(minute=minute, second=0, microsecond=0)
            if existing_date is None and target <= now:
                target += timedelta(hours=1)
            return result(target, partial.end(), "minute")

        if date_dt is not None:
            return result(date_dt, date_end, "day", missing=True)
        return None

    def _clean_content_title(
        self,
        content_zone: str,
        participants: list[Participant],
    ) -> str | None:
        title = content_zone
        for participant in participants:
            title = title.replace(participant.display_name, " ")
        title = re.sub(r"@\w+", " ", title)
        title = LEADING_CONNECTOR_RE.sub("", title)
        title = re.sub(r"(?:^|\s)(?:和|跟|與)(?=\s|$)", " ", title)
        title = normalize_spaces(title)
        return title or None

    def _parse_time(
        self,
        text: str,
        now: datetime,
        zone: tzinfo,
        *,
        existing_date: datetime | None = None,
    ) -> TimeParse:
        parsed = self._parse_datetime(text, now, zone, leading=False, existing_date=existing_date)
        return parsed[0] if parsed else TimeParse(None, "", "unknown")

    def _parse_clock(self, match: re.Match[str], *, period: str = "") -> tuple[int, int]:
        hour = parse_number(match.group("hour"))
        minute_text = match.group("minute")
        minute = 0
        if minute_text == "半":
            minute = 30
        elif minute_text:
            minute = parse_number(minute_text)

        period = match.group("period") or period
        if period in {"下午", "晚上", "晚", "夜晚"} and hour < 12:
            hour += 12
        if period == "中午" and hour < 11:
            hour += 12
        if period == "凌晨" and hour == 12:
            hour = 0

        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("invalid clock")
        return hour, minute

    def extract_participants(self, text: str, message: TelegramMessage) -> list[Participant]:
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
        time_parse: TimeParse,
        participants: list[Participant],
    ) -> str | None:
        title = text
        for start, end in reversed(time_parse.consumed_spans):
            title = title[:start] + " " + title[end:]
        title = PROMPT_SPLIT_RE.sub(" ", title)
        title = LEADING_ZAI_RE.sub("", title)
        return self._clean_content_title(title, participants)


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


def apply_relative_offset(now: datetime, amount: float, unit: str) -> datetime:
    """RELATIVE_RE 命中的單位轉換為 delta 並套用。
    月份使用 calendar 加減，避免用 30 天近似造成的月底漂移。"""
    elapsed = None
    if unit in {"分鐘", "分"}:
        elapsed = timedelta(minutes=amount)
    elif "小時" in unit or "小时" in unit:
        elapsed = timedelta(hours=amount)
    if elapsed is not None:
        return (
            (now.astimezone(UTC) + elapsed).astimezone(now.tzinfo) if now.tzinfo else now + elapsed
        )
    if unit in {"天", "日"}:
        return now + timedelta(days=amount)
    if unit in {"週", "周"} or "星期" in unit:
        return now + timedelta(days=amount * 7)
    if "個月" in unit or "个月" in unit:
        return _add_months(now, int(amount))
    raise ValueError(f"unknown relative unit: {unit!r}")


def _add_months(dt: datetime, months: int) -> datetime:
    """在 calendar 月刻度上加 N 個月，日期溢位時 clamp 到當月最後一天。"""
    import calendar

    total = dt.month - 1 + months
    year = dt.year + total // 12
    month = total % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def days_until_weekday(current_weekday: int, target_weekday: int, prefix: str | None) -> int:
    """有前綴時以週一為週界；無前綴時取最近的目標星期。"""
    days = target_weekday - current_weekday
    if prefix and "下" in prefix:
        return days + prefix.count("下") * 7
    return days if prefix in ("這", "本") else days % 7


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


def _short_date_to_datetime(match: re.Match[str], now: datetime, zone: tzinfo) -> datetime | None:
    month = int(match.group("month"))
    day = int(match.group("day"))
    try:
        candidate = datetime(now.year, month, day, tzinfo=zone)
    except ValueError:
        return None
    if candidate.date() < now.date():
        try:
            candidate = candidate.replace(year=now.year + 1)
        except ValueError:
            return None
    return candidate


def _chinese_date_to_datetime(match: re.Match[str], now: datetime, zone: tzinfo) -> datetime | None:
    try:
        month = parse_number(match.group("month"))
        day = parse_number(match.group("day"))
    except KeyError:
        return None
    try:
        candidate = datetime(now.year, month, day, tzinfo=zone)
    except ValueError:
        return None
    if candidate.date() < now.date():
        try:
            candidate = candidate.replace(year=now.year + 1)
        except ValueError:
            return None
    return candidate
