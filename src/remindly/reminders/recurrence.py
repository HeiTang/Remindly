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


def is_valid_month_day(month: int, day: int) -> bool:
    """檢查 (month, day) 是否為存在的日期。用 2028（閏年）當試探年，
    這樣 2/29 會被視為合法（yearly recurrence 允許），但 2/30、4/31、13/1 都拒絕。
    parser 與 format_rule 共用，避免驗證邏輯漂移。"""
    try:
        datetime(2028, month, day)
    except ValueError:
        return False
    return True


_CHINESE_WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")


def format_rule(rule: RecurrenceRule) -> str:
    """把 RecurrenceRule 格式化成使用者可讀的中文字串，供確認卡 / 列表 / 詳情共用。

    Validation：以 `next_fire` 的 required-field 規則為底（空 weekdays、缺 yearly
    欄位都 raise），並額外做更嚴格的值域/日期檢查——weekday 必須在 0..6、
    month_day 必須在 1..31、yearly (month, day) 必須是實際存在的日期（透過
    `is_valid_month_day` 檢查，2/29 視為合法）。這是刻意比 `next_fire` 嚴格：
    使用者看得到的字串要立即拒絕 malformed 規則（否則會出現 "每週 09:00" 或
    "每年 None/None ..." 這種殘缺輸出）；`next_fire` 因為在 scheduler tick 內
    才呼叫，只保護到「一定找不到觸發時間」這層。

    weekdays / month_days 都會排序後再輸出以保證使用者看到穩定順序。

    範例：
    - DAILY 09:00                              → "每天 09:00"
    - WEEKLY weekdays=(0,)  09:00              → "每週一 09:00"
    - WEEKLY weekdays=(0,2,4) 09:00            → "每週一、三、五 09:00"
    - MONTHLY month_days=(15,) 09:00           → "每月 15 號 09:00"
    - MONTHLY month_days=(1,18,25) 09:00       → "每月 1, 18, 25 號 09:00"
    - YEARLY  year_month=12 year_day=25 08:00  → "每年 12/25 08:00"
    """
    hhmm = f"{rule.hour:02d}:{rule.minute:02d}"
    if rule.period == RecurrencePeriod.DAILY:
        return f"每天 {hhmm}"
    if rule.period == RecurrencePeriod.WEEKLY:
        if not rule.weekdays:
            raise ValueError("weekly recurrence requires at least one weekday")
        if any(d < 0 or d > 6 for d in rule.weekdays):
            raise ValueError(f"weekday out of range 0..6: {rule.weekdays}")
        days = "、".join(_CHINESE_WEEKDAYS[d] for d in sorted(set(rule.weekdays)))
        return f"每週{days} {hhmm}"
    if rule.period == RecurrencePeriod.MONTHLY:
        if not rule.month_days:
            raise ValueError("monthly recurrence requires at least one month_day")
        if any(d < 1 or d > 31 for d in rule.month_days):
            raise ValueError(f"month_day out of range 1..31: {rule.month_days}")
        days = ", ".join(str(d) for d in sorted(set(rule.month_days)))
        return f"每月 {days} 號 {hhmm}"
    if rule.period == RecurrencePeriod.YEARLY:
        if rule.year_month is None or rule.year_day is None:
            raise ValueError("yearly recurrence requires year_month and year_day")
        if not is_valid_month_day(rule.year_month, rule.year_day):
            raise ValueError(
                f"invalid yearly date: {rule.year_month}/{rule.year_day}"
            )
        return f"每年 {rule.year_month}/{rule.year_day} {hhmm}"
    raise ValueError(f"unknown recurrence period: {rule.period!r}")
