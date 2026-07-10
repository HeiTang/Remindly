from __future__ import annotations

from datetime import datetime

from remindly.reminders.callback_data import reminder_callback
from remindly.reminders.models import (
    MentionKind,
    Participant,
    RecurrenceError,
    Reminder,
    ReminderDraft,
)
from remindly.reminders.recurrence import format_rule
from remindly.reminders.service import ReminderListFilter, ReminderListGroup
from remindly.reminders.text import html_escape


class ReminderRenderer:
    def render_confirmation(self, draft: ReminderDraft) -> str:
        participants = self.render_participants(draft.participants)
        lines = ["確認建立提醒？"]
        if draft.recurrence is not None:
            lines.append(f"重複：{html_escape(format_rule(draft.recurrence))}")
            lines.append(f"下次：{format_datetime(draft.remind_at)}")
        else:
            lines.append(f"時間：{format_datetime(draft.remind_at)}")
        lines.append(f"對象：{participants}")
        lines.append(f"事項：{html_escape(draft.title or '')}")
        return "\n".join(lines)

    def render_created(self, reminder: Reminder) -> str:
        lines = [f"已建立提醒 {html_escape(reminder.short_id)}"]
        if reminder.recurrence is not None:
            lines.append(f"重複：{html_escape(format_rule(reminder.recurrence))}")
            lines.append(f"下次：{format_datetime(reminder.remind_at)}")
        else:
            lines.append(f"時間：{format_datetime(reminder.remind_at)}")
        lines.append(f"事項：{html_escape(reminder.title)}")
        lines.append(f"取消：/cancel {html_escape(reminder.short_id)}")
        return "\n".join(lines)

    def render_detail(self, reminder: Reminder, participants: list[Participant]) -> str:
        mention_text = self.render_participants(participants)
        lines = [f"提醒 {html_escape(reminder.short_id)}"]
        if reminder.recurrence is not None:
            lines.append(f"重複：{html_escape(format_rule(reminder.recurrence))}")
            lines.append(f"下次：{format_datetime(reminder.remind_at)}")
        else:
            lines.append(f"時間：{format_datetime(reminder.remind_at)}")
        lines.append(f"事項：{html_escape(reminder.title)}")
        lines.append(f"對象：{mention_text or '未設定'}")
        return "\n".join(lines)

    def render_delivery(self, reminder: Reminder, participants: list[Participant]) -> str:
        mention_text = self.render_participants(participants)
        lines = [f"提醒：{html_escape(reminder.title)}", f"ID：{html_escape(reminder.short_id)}"]
        if reminder.recurrence is not None:
            lines.append(f"重複：{html_escape(format_rule(reminder.recurrence))}")
        if mention_text:
            lines.extend(["", f"對象：{mention_text}"])
        return "\n".join(lines)

    def render_snoozed(self, reminder: Reminder) -> str:
        return "\n".join(
            [
                f"已延後提醒 {html_escape(reminder.short_id)}",
                f"時間：{format_datetime(reminder.remind_at)}",
                f"事項：{html_escape(reminder.title)}",
            ]
        )

    def render_skipped_next(self, reminder: Reminder) -> str:
        return "\n".join(
            [
                f"已跳過下次 {html_escape(reminder.short_id)}",
                f"下次：{format_datetime(reminder.remind_at)}",
                f"事項：{html_escape(reminder.title)}",
            ]
        )

    def render_recurrence_error(self, error: RecurrenceError) -> str:
        """單輪拒絕：使用者輸入了週期性提醒 marker 但語法無效（例如「每個月 45 號」、
        「每 1 分鐘」），直接告知具體原因並要求重打，不進入追問流程。
        `reason` 由 parser 產生，自帶語意（例：「日期無效，需在 1-31 範圍」、
        「太頻繁，最低支援 10 分鐘」），renderer 只負責包裝。"""
        return "\n".join(
            [
                f"『{html_escape(error.marker_text)}』{html_escape(error.reason)}。",
                "請重新輸入完整的提醒。",
            ]
        )

    def render_series_cancelled(self, reminder: Reminder) -> str:
        return "\n".join(
            [
                f"已取消整個系列 {html_escape(reminder.short_id)}",
                f"事項：{html_escape(reminder.title)}",
            ]
        )

    def render_grouped_list(
        self,
        groups: list[ReminderListGroup],
        list_filter: ReminderListFilter,
    ) -> str:
        lines = [f"未到期提醒（{list_filter_label(list_filter)}｜依建立者）："]
        if not groups:
            lines.extend(["", "目前沒有符合條件的提醒。"])
            return "\n".join(lines)

        for group in groups:
            lines.extend(["", f"建立者：{html_escape(group.creator_label)}"])
            for reminder in group.reminders:
                prefix = "[重複] " if reminder.recurrence is not None else ""
                lines.append(
                    f"- {prefix}{html_escape(reminder.short_id)}｜"
                    f"{format_datetime(reminder.remind_at)}｜"
                    f"{html_escape(reminder.title)}"
                )
        lines.extend(["", "點選下方提醒可查看、修改或刪除。"])
        return "\n".join(lines)

    def render_participants(self, participants: list[Participant]) -> str:
        return " ".join(self.render_participant(participant) for participant in participants)

    def render_participant(self, participant: Participant) -> str:
        if participant.mention_kind == MentionKind.TEXT_MENTION and participant.user_id is not None:
            label = html_escape(participant.display_name)
            return f'<a href="tg://user?id={participant.user_id}">{label}</a>'
        if participant.username:
            return f"@{html_escape(participant.username)}"
        return html_escape(participant.display_name)


def format_datetime(value: datetime | None) -> str:
    if value is None:
        return "未設定"
    return value.strftime("%Y-%m-%d %H:%M")


def truncate_button_text(value: str, limit: int = 42) -> str:
    return value if len(value) <= limit else f"{value[: limit - 1]}…"


def list_filter_label(list_filter: ReminderListFilter) -> str:
    labels = {
        ReminderListFilter.ALL: "全部",
        ReminderListFilter.TODAY: "今天",
        ReminderListFilter.WEEK: "本週",
        ReminderListFilter.MINE: "我的",
    }
    return labels[list_filter]


def render_groupmode_panel(enabled: bool) -> str:
    status = "開啟" if enabled else "關閉"
    return "\n".join(
        [
            f"群組自然語言模式：{status}",
            "",
            "這個開關的意思：",
            "開啟：群組一般文字如果像提醒，例如「提醒我明天倒垃圾」，Remindly 會嘗試建立提醒。",
            "關閉：一般聊天會被忽略；仍可用 /remind 或 @bot 建立提醒。",
            "",
            "注意：若 BotFather Group Privacy 沒關，開啟後 bot 仍收不到一般群組訊息。",
        ]
    )


def filter_button_text(list_filter: ReminderListFilter, active_filter: ReminderListFilter) -> str:
    label = list_filter_label(list_filter)
    return f"{label} ✓" if list_filter == active_filter else label


def confirmation_keyboard(draft_id: str) -> dict[str, object]:
    return {
        "inline_keyboard": [
            [
                {"text": "確認", "callback_data": reminder_callback("confirm", draft_id)},
                {"text": "取消", "callback_data": reminder_callback("discard", draft_id)},
            ]
        ]
    }


def quick_time_keyboard(draft_id: str) -> dict[str, object]:
    return {
        "inline_keyboard": [
            [
                {"text": "09:00", "callback_data": reminder_callback("time", draft_id, "09:00")},
                {"text": "12:00", "callback_data": reminder_callback("time", draft_id, "12:00")},
            ],
            [
                {"text": "15:00", "callback_data": reminder_callback("time", draft_id, "15:00")},
                {"text": "18:00", "callback_data": reminder_callback("time", draft_id, "18:00")},
            ],
        ]
    }


def reminder_list_keyboard(
    groups: list[ReminderListGroup],
    active_filter: ReminderListFilter,
) -> dict[str, object]:
    rows: list[list[dict[str, str]]] = [list_filter_keyboard_row(active_filter)]
    for group in groups:
        rows.append(
            [
                {
                    "text": truncate_button_text(f"建立者：{group.creator_label}", limit=50),
                    "callback_data": reminder_callback("noop"),
                }
            ]
        )
        for reminder in group.reminders:
            prefix = "[重複] " if reminder.recurrence is not None else ""
            rows.append(
                [
                    {
                        "text": truncate_button_text(
                            f"{prefix}{reminder.short_id}｜"
                            f"{format_datetime(reminder.remind_at)}｜{reminder.title}"
                        ),
                        "callback_data": reminder_callback("view", reminder.short_id),
                    }
                ]
            )
    return {"inline_keyboard": rows}


def list_filter_keyboard_row(active_filter: ReminderListFilter) -> list[dict[str, str]]:
    return [
        {
            "text": filter_button_text(list_filter, active_filter),
            "callback_data": reminder_callback("list", "_", list_filter.value),
        }
        for list_filter in (
            ReminderListFilter.TODAY,
            ReminderListFilter.WEEK,
            ReminderListFilter.MINE,
            ReminderListFilter.ALL,
        )
    ]


def groupmode_keyboard(enabled: bool) -> dict[str, object]:
    next_value = "off" if enabled else "on"
    button_text = "關閉" if enabled else "開啟"
    return {
        "inline_keyboard": [
            [
                {
                    "text": button_text,
                    "callback_data": reminder_callback("groupmode", "_", next_value),
                }
            ]
        ]
    }


def delivery_snooze_keyboard(short_id: str, next_day_time_label: str) -> dict[str, object]:
    """到期提醒的延後按鈕。第三顆帶入原提醒的 HH:MM，避免「明天同時間」的語意歧義。"""
    return {
        "inline_keyboard": [
            [
                {
                    "text": "10 分鐘後",
                    "callback_data": reminder_callback("snooze", short_id, "10m"),
                },
                {
                    "text": "1 小時後",
                    "callback_data": reminder_callback("snooze", short_id, "1h"),
                },
            ],
            [
                {
                    "text": f"明天 {next_day_time_label}",
                    "callback_data": reminder_callback("snooze", short_id, "1d"),
                }
            ],
        ]
    }


def delivery_recurring_keyboard(short_id: str, next_day_time_label: str) -> dict[str, object]:
    """週期性提醒的到期按鈕：延用一次性提醒的三顆延後按鈕，再多一列
    「跳過下次 / 取消整個系列」。使用者拿到通知後可以：
    - 延後這次到 10 分/1 小時/明天 HH:MM（Bot 送完後仍會照規則排下次）
    - 跳過下次（直接把 remind_at 推到「下下次」，例如每月 1/18/25 剛推到 18 號可再按跳過推到 25 號）
    - 取消整個系列（把提醒標為 CANCELLED，不會再收到）

    Delegate 給 `delivery_snooze_keyboard` 取延後按鈕，避免文字或 callback 規則
    在兩邊漂移。
    """
    base = delivery_snooze_keyboard(short_id, next_day_time_label)
    return {
        "inline_keyboard": [
            *base["inline_keyboard"],  # type: ignore[misc]
            [
                {
                    "text": "跳過下次",
                    "callback_data": reminder_callback("skip_next", short_id),
                },
                {
                    "text": "取消整個系列",
                    "callback_data": reminder_callback("cancel_series", short_id),
                },
            ],
        ]
    }


def reminder_actions_keyboard(short_id: str) -> dict[str, object]:
    return {
        "inline_keyboard": [
            [
                {"text": "修改時間", "callback_data": reminder_callback("edit_time", short_id)},
                {"text": "修改內容", "callback_data": reminder_callback("edit_title", short_id)},
            ],
            [
                {"text": "刪除", "callback_data": reminder_callback("delete_confirm", short_id)},
                {"text": "返回列表", "callback_data": reminder_callback("list")},
            ],
        ]
    }


def delete_confirmation_keyboard(short_id: str) -> dict[str, object]:
    return {
        "inline_keyboard": [
            [
                {"text": "確認刪除", "callback_data": reminder_callback("delete", short_id)},
                {"text": "返回", "callback_data": reminder_callback("view", short_id)},
            ]
        ]
    }


def edit_cancel_keyboard(short_id: str) -> dict[str, object]:
    return {
        "inline_keyboard": [
            [{"text": "取消修改", "callback_data": reminder_callback("edit_cancel", short_id)}]
        ]
    }


def back_to_list_keyboard() -> dict[str, object]:
    return {"inline_keyboard": [[{"text": "返回列表", "callback_data": reminder_callback("list")}]]}
