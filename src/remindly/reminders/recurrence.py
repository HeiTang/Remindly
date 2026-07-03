from __future__ import annotations

import calendar
import json
from datetime import datetime, timedelta

from remindly.reminders.models import RecurrencePeriod, RecurrenceRule


def serialize_rule(rule: RecurrenceRule) -> str:
    """把 RecurrenceRule 存成 JSON 字串，供 DB 儲存。
    只寫入 period 用到的欄位，讓資料庫檔案人眼可讀。"""
    payload: dict[str, object] = {
        "period": rule.period.value,
        "hour": rule.hour,
        "minute": rule.minute,
    }
    if rule.period == RecurrencePeriod.WEEKLY:
        payload["weekdays"] = list(rule.weekdays)
    elif rule.period == RecurrencePeriod.MONTHLY:
        payload["month_days"] = list(rule.month_days)
    elif rule.period == RecurrencePeriod.YEARLY:
        payload["year_month"] = rule.year_month
        payload["year_day"] = rule.year_day
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def deserialize_rule(payload: str) -> RecurrenceRule:
    """反向：從 DB 讀回 JSON 字串，還原成 RecurrenceRule。"""
    data = json.loads(payload)
    period = RecurrencePeriod(str(data["period"]))
    return RecurrenceRule(
        period=period,
        hour=int(data["hour"]),
        minute=int(data["minute"]),
        weekdays=tuple(int(x) for x in data.get("weekdays", [])),
        month_days=tuple(int(x) for x in data.get("month_days", [])),
        year_month=int(data["year_month"]) if data.get("year_month") is not None else None,
        year_day=int(data["year_day"]) if data.get("year_day") is not None else None,
    )


def next_fire(rule: RecurrenceRule, after: datetime) -> datetime:
    """算出 `after` 之後最近一次觸發時間。回傳結果保留 `after` 的 tzinfo。

    `after` 通常是 scheduler tick 時的 now。回傳時間嚴格 > after，
    避免無限迴圈（例如 daily 提醒剛好落在 tick 那一秒）。
    """
    zone = after.tzinfo
    if rule.period == RecurrencePeriod.DAILY:
        candidate = after.replace(hour=rule.hour, minute=rule.minute, second=0, microsecond=0)
        if candidate <= after:
            candidate += timedelta(days=1)
        return candidate

    if rule.period == RecurrencePeriod.WEEKLY:
        if not rule.weekdays:
            raise ValueError("weekly recurrence requires at least one weekday")
        # 從今天開始往後掃 8 天，總會命中至少一個規則裡的 weekday。
        for delta_days in range(0, 8):
            candidate = (after + timedelta(days=delta_days)).replace(
                hour=rule.hour, minute=rule.minute, second=0, microsecond=0
            )
            if candidate.weekday() not in rule.weekdays:
                continue
            if candidate > after:
                return candidate
        raise RuntimeError("unreachable: 8-day scan must find a weekly slot")

    if rule.period == RecurrencePeriod.MONTHLY:
        if not rule.month_days:
            raise ValueError("monthly recurrence requires at least one month_day")
        # 掃當月、下月、下下月；超出當月天數的日子略過（例如 31 號在二月）。
        for offset in range(0, 3):
            year, month = _month_offset(after.year, after.month, offset)
            _, last_day = calendar.monthrange(year, month)
            for day in sorted(rule.month_days):
                if day > last_day:
                    continue
                candidate = datetime(year, month, day, rule.hour, rule.minute, tzinfo=zone)
                if candidate > after:
                    return candidate
        raise RuntimeError("unreachable: 3-month scan must find a monthly slot")

    if rule.period == RecurrencePeriod.YEARLY:
        if rule.year_month is None or rule.year_day is None:
            raise ValueError("yearly recurrence requires year_month and year_day")
        # 往後掃 9 年：閏日 2/29 在非閏年會 ValueError 被跳過；橫跨世紀時
        # 閏年 gap 可能達 8 年（2096 leap → 2100 non-leap → 2104 leap），
        # 9 年給留一年餘裕確保命中。其他日期最多在 1 年內就命中。
        for year in range(after.year, after.year + 9):
            try:
                candidate = datetime(
                    year, rule.year_month, rule.year_day, rule.hour, rule.minute, tzinfo=zone
                )
            except ValueError:
                continue
            if candidate > after:
                return candidate
        raise RuntimeError("unreachable: 9-year scan must find a yearly slot")

    raise ValueError(f"unknown recurrence period: {rule.period!r}")


def _month_offset(year: int, month: int, offset: int) -> tuple[int, int]:
    """回傳 (year+offset_months, month+offset_months) 處理跨年進位。"""
    total = (month - 1) + offset
    return year + total // 12, total % 12 + 1
