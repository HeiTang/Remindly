from __future__ import annotations

from remindly.telegram.client import TelegramApiError, TelegramClient

GROUP_CHAT_TYPES = {"group", "supergroup"}
ADMIN_STATUSES = {"creator", "administrator"}


class GroupModeAuthorizer:
    def __init__(self, client: TelegramClient) -> None:
        self._client = client

    def can_change_group_settings(self, chat_id: int, user_id: int) -> bool | None:
        """向 Telegram 查詢操作者是否可變更群組設定；查詢失敗回傳 None。"""
        try:
            status = self._client.get_chat_member(chat_id, user_id)
        except TelegramApiError:
            return None

        return status in ADMIN_STATUSES
