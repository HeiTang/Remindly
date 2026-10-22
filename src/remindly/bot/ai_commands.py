from __future__ import annotations

from datetime import datetime

from remindly.ai.gateway import AiGateway
from remindly.reminders.text import command_body
from remindly.telegram.client import TelegramApiError, TelegramClient
from remindly.telegram.models import TelegramMessage


class AiCommands:
    def __init__(self, client: TelegramClient, gateway: AiGateway | None, bot_username: str | None):
        self._client = client
        self._gateway = gateway
        self._keys = gateway.keys if gateway else None
        self._bot_username = bot_username

    def handle(self, message: TelegramMessage) -> bool:
        body = command_body(message.text, "ai", self._bot_username)
        if body is None:
            return False
        parts = body.split()
        deletion_failed = False
        if parts and parts[0] == "add" and len(parts) > 1:
            try:
                self._client.delete_message(message.chat.id, message.id)
            except TelegramApiError:
                deletion_failed = True
        if message.chat.type != "private":
            self._send(message, "API Key 只能在與 Bot 的私聊設定；請勿在群組貼上 Key。")
            return True
        if message.from_user is None:
            return True
        if self._keys is None:
            self._send(message, "管理員尚未設定 AI_ENCRYPTION_KEY，目前使用基本解析。")
            return True
        user_id = message.from_user.id
        try:
            action = parts[0] if parts else "status"
            if not self._keys.configured and action in {"add", "on"}:
                raise ValueError("管理員尚未設定 AI_ENCRYPTION_KEY，目前使用基本解析。")
            if action == "add" and len(parts) in {2, 3}:
                provider = parts[1] if len(parts) == 3 else None
                key_id = self._gateway.add_key(user_id, parts[-1], provider)
                text = f"已加密保存 Key {key_id}。\n" + self._panel(user_id)
            elif action in {"on", "off"} and len(parts) == 1:
                self._keys.set_enabled(user_id, action == "on")
                text = self._panel(user_id)
            elif action == "delete" and len(parts) == 2:
                deleted = self._keys.delete(user_id, parts[1])
                text = "已刪除 Key。" if deleted else "找不到你的這組 Key。"
                text += "\n" + self._panel(user_id)
            elif action == "move" and len(parts) == 3 and parts[2].isdigit():
                self._keys.move(user_id, parts[1], int(parts[2]))
                text = self._panel(user_id)
            elif action == "status" and len(parts) <= 1:
                text = self._panel(user_id)
            else:
                text = (
                    "用法：/ai、/ai add [供應商] <Key>、/ai on|off、"
                    "/ai delete <ID>、/ai move <ID> <順位>"
                )
        except ValueError as exc:
            text = str(exc)
        if deletion_failed:
            text += "\n未能刪除含 Key 的訊息，請手動刪除；Key 已經過 Telegram 傳輸。"
        self._send(message, text)
        return True

    def _panel(self, user_id: int) -> str:
        enabled = self._keys.enabled(user_id)
        lines = [f"AI 語意解析：{'開啟' if enabled else '關閉（使用基本解析）'}"]
        lines.extend(f"供應商：{p.id} · {p.name}" for p in self._gateway.providers.values())
        if not self._keys.configured:
            lines.append("管理員尚未設定 AI_ENCRYPTION_KEY；可刪除已存的 Key，但無法使用 AI。")
        for index, key in enumerate(self._keys.list(user_id), 1):
            status = "已設定（尚未驗證）"
            if key.status == "ready":
                status = "最近呼叫成功"
            elif key.status == "invalid":
                status = "憑證無效／權限不足，請刪除後重加"
            elif (
                key.cooldown_until
                and datetime.fromisoformat(key.cooldown_until) > datetime.now().astimezone()
            ):
                status = f"冷卻至 {key.cooldown_until}"
            if key.provider not in self._gateway.providers:
                status = "供應商尚未啟用"
            lines.append(f"{index}. {key.id} · {key.provider} · 指紋 {key.fingerprint} · {status}")
        lines.extend(
            [
                "",
                "新增：/ai add [供應商] <API Key>（省略供應商使用預設，訊息會嘗試刪除）",
                "開關：/ai on 或 /ai off",
                "刪除：/ai delete <ID>",
                "排序：/ai move <ID> <順位>",
                "Key 會經 Telegram 傳給此 Bot，於伺服器加密保存。",
                "啟用後，一般私聊文字、群組觸發訊息及必要上下文會送至 Key 所屬供應商。",
                "費用和共用額度依供應商帳戶設定。",
            ]
        )
        return "\n".join(lines)

    def _send(self, message: TelegramMessage, text: str) -> None:
        self._client.send_message(message.chat.id, text)
