from __future__ import annotations

from datetime import datetime

from remindly.reminders.callback_data import reminder_callback
from remindly.reminders.models import MentionKind, Participant, Reminder, ReminderDraft
from remindly.reminders.service import ReminderListFilter, ReminderListGroup
from remindly.reminders.text import html_escape


class ReminderRenderer:
    def render_confirmation(self, draft: ReminderDraft) -> str:
        participants = self.render_participants(draft.participants)
        return "\n".join(
            [
                "確認建立提醒？",
                f"時間：{format_datetime(draft.remind_at)}",
                f"對象：{participants}",
                f"事項：{html_escape(draft.title or '')}",
            ]
        )

    def render_created(self, reminder: Reminder) -> str:
        return "\n".join(
            [
                f"已建立提醒 {html_escape(reminder.short_id)}",
                f"時間：{format_datetime(reminder.remind_at)}",
                f"事項：{html_escape(reminder.title)}",
                f"取消：/cancel {html_escape(reminder.short_id)}",
            ]
        )

    def render_detail(self, reminder: Reminder, participants: list[Participant]) -> str:
        mention_text = self.render_participants(participants)
        return "\n".join(
            [
                f"提醒 {html_escape(reminder.short_id)}",
                f"時間：{format_datetime(reminder.remind_at)}",
                f"事項：{html_escape(reminder.title)}",
                f"對象：{mention_text or '未設定'}",
            ]
        )

    def render_delivery(self, reminder: Reminder, participants: list[Participant]) -> str:
        mention_text = self.render_participants(participants)
        lines = [f"提醒：{html_escape(reminder.title)}", f"ID：{html_escape(reminder.short_id)}"]
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
                lines.append(
                    f"- {html_escape(reminder.short_id)}｜{format_datetime(reminder.remind_at)}｜"
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
            rows.append(
                [
                    {
                        "text": truncate_button_text(
                            f"{reminder.short_id}｜{format_datetime(reminder.remind_at)}｜{reminder.title}"
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


def delivery_snooze_keyboard(short_id: str) -> dict[str, object]:
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
                    "text": "明天同時間",
                    "callback_data": reminder_callback("snooze", short_id, "1d"),
                }
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
