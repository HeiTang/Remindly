from __future__ import annotations

import re
from datetime import datetime

from remindly.ai.contracts import PROVIDER_ERRORS
from remindly.ai.gateway import AiGateway
from remindly.bot.groupmode import GROUP_CHAT_TYPES, GroupModeAuthorizer
from remindly.reminders.callback_data import CallbackData
from remindly.reminders.text import command_body
from remindly.telegram.client import TelegramApiError, TelegramClient
from remindly.telegram.models import TelegramCallbackQuery, TelegramMessage


class AiCommands:
    def __init__(self, client: TelegramClient, gateway: AiGateway | None, bot_username: str | None):
        self._client = client
        self._gateway = gateway
        self._keys = gateway.keys if gateway else None
        self._bot_username = bot_username

    def handle(self, message: TelegramMessage) -> bool:
        start = command_body(message.text, "start", self._bot_username)
        if start is not None and start.startswith("aigroup_"):
            self._show_group_keys(message, start.removeprefix("aigroup_"))
            return True
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
        if parts and parts[0] == "group":
            self._handle_group(message, parts[1:])
            return True
        if message.chat.type != "private":
            self._send(message, "API Key 只能在與 Bot 的私聊設定；請勿在群組貼上 Key。")
            return True
        if message.from_user is None:
            return True
        if self._keys is None:
            self._send(message, "伺服器尚未設定 AI_ENCRYPTION_KEY，目前使用基本解析。")
            return True
        user_id = message.from_user.id
        try:
            action = parts[0] if parts else "status"
            if not self._keys.configured and action in {"add", "on"}:
                raise ValueError("伺服器尚未設定 AI_ENCRYPTION_KEY，目前使用基本解析。")
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

    def _handle_group(self, message: TelegramMessage, parts: list[str]) -> None:
        if message.from_user is None:
            return
        if self._keys is None:
            self._send(message, "伺服器尚未設定 AI，目前使用內建解析。")
            return
        user_id = message.from_user.id
        markup = None
        try:
            if message.chat.type == "private":
                if len(parts) == 3 and parts[0] == "grant":
                    chat_id = group_chat_id(parts[1])
                    self._grant_group(user_id, chat_id, parts[2])
                    text = (
                        f"已授權群組 {chat_id} 使用指定 Key，等待管理員在群組 /ai group on。"
                        "觸發訊息及必要上下文將送至該 Key 的供應商；費用與額度歸你。"
                        f"隨時撤銷：/ai group revoke {chat_id}"
                    )
                elif len(parts) == 2 and parts[0] == "revoke":
                    revoked = self._keys.revoke_group(user_id, group_chat_id(parts[1]))
                    text = "已撤銷群組授權。" if revoked else "找不到你的群組授權。"
                else:
                    text = (
                        "請管理員在目標群組輸入 /ai group allow，"
                        "再點「授權我的 Key」進入私聊選擇既有 Key。\n"
                        "觸發訊息及必要上下文"
                        "會送至指定 Key 的供應商，費用與額度歸持有人。\n"
                        "撤銷：/ai group revoke <群組 ID>\n"
                        "只授權選定 Key，不使用其他備援 Key；刪 Key 立即失效。"
                    )
                    for chat_id, key_id, enabled in self._keys.group_grants(user_id):
                        text += (
                            f"\n群組 {chat_id} · Key {key_id} · "
                            f"{'啟用' if enabled else '待啟用／關閉'}"
                        )
            elif message.chat.type in GROUP_CHAT_TYPES:
                action = parts[0] if parts else "status"
                if len(parts) > 1 or action not in {"status", "allow", "on", "off", "unlink"}:
                    raise ValueError("用法：/ai group status|allow|on|off|unlink")
                if action != "status":
                    allowed = GroupModeAuthorizer(self._client).can_change_group_settings(
                        message.chat.id, user_id
                    )
                    if allowed is None:
                        raise ValueError("無法確認你的群組權限，未變更設定。")
                    if not allowed:
                        raise ValueError("只有群組管理員可以管理群組 AI。")
                    self._keys.manage_group(message.chat.id, action)
                owner, _, enabled, accepting = self._keys.group_settings(message.chat.id)
                markup = None
                if accepting:
                    markup = {
                        "inline_keyboard": [
                            [
                                {
                                    "text": "授權我的 Key",
                                    "url": self._group_link(message.chat.id),
                                }
                            ]
                        ]
                    }
                text = (
                    f"群組 ID：{message.chat.id}\n"
                    f"群組 AI：{'啟用' if enabled else '關閉（內建解析）'}\n"
                    f"Key 持有人：{owner if owner is not None else '尚未綁定'}\n"
                    f"接受新授權：{'是' if accepting else '否'}\n"
                    "管理員先用 /ai group allow 開放一次綁定。\n"
                    + (
                        "Key 持有人點下方「授權我的 Key」，進入私聊選擇既有 Key。\n"
                        if accepting
                        else "開放綁定後，Key 持有人即可點按鈕進私聊選 Key。\n"
                    )
                    + "授權後，管理員在此群組 /ai group on 啟用。\n"
                    "關閉：/ai group off；解除：/ai group unlink。\n"
                    "觸發訊息及必要上下文會送至指定 Key 的供應商；費用與額度歸持有人。"
                    "AI 不可用時使用內建解析；仍須發起者確認才建立提醒。"
                )
            else:
                raise ValueError("僅支援私聊與群組。")
        except ValueError as exc:
            text = str(exc)
            markup = None
        self._client.send_message(
            message.chat.id,
            text,
            reply_markup=markup,
        )

    def _group_link(self, chat_id: int) -> str:
        if not self._bot_username:
            try:
                result = self._client.call("getMe", {}).get("result", {})
            except TelegramApiError:
                raise ValueError("無法取得 Bot 帳號，請稍後重試 /ai group。") from None
            username = result.get("username") if isinstance(result, dict) else None
            if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_]+", username):
                raise ValueError("無法取得 Bot 帳號，請設定 BOT_USERNAME 後重試。")
            self._bot_username = username
        return f"https://t.me/{self._bot_username.lstrip('@')}?start=aigroup_{chat_id}"

    def _check_group_member(self, user_id: int, chat_id: int) -> None:
        if self._keys is None or not self._keys.configured:
            raise ValueError("伺服器尚未設定 AI_ENCRYPTION_KEY。")
        try:
            status = self._client.get_chat_member(chat_id, user_id)
        except TelegramApiError:
            raise ValueError("無法確認你的群組成員資格，未變更授權。") from None
        if status not in {"creator", "administrator", "member"}:
            raise ValueError("只有該群組成員能授權 Key。")

    def _grant_group(self, user_id: int, chat_id: int, key_id: str) -> None:
        self._check_group_member(user_id, chat_id)
        self._keys.grant_group(user_id, chat_id, key_id)

    def _show_group_keys(self, message: TelegramMessage, target: str) -> None:
        try:
            if message.chat.type != "private" or message.from_user is None:
                raise ValueError("請從群組按「授權我的 Key」，在 Bot 私聊選擇 Key。")
            chat_id = group_chat_id(target)
            user_id = message.from_user.id
            self._check_group_member(user_id, chat_id)
            if not self._keys.group_settings(chat_id)[3]:
                raise ValueError("此群組尚未開放或已完成綁定，請管理員重新 /ai group allow。")
            keys = [k for k in self._keys.list(user_id) if k.provider in self._gateway.providers]
            if not keys:
                raise ValueError("你還沒有可選的 Key，請先在私聊用 /ai 新增，再回群組按授權按鈕。")
            rows = [
                [
                    {
                        "text": (
                            f"{i}. {self._gateway.providers[key.provider].name} · {key.fingerprint}"
                        ),
                        "callback_data": CallbackData(
                            "ai", "g", str(chat_id), f"{user_id}_{key.id}"
                        ).encode(),
                    }
                ]
                for i, key in enumerate(keys, 1)
            ]
            self._client.send_message(
                message.chat.id,
                f"授權群組 {chat_id}：請選擇你的一組既有 Key。\n"
                "點選即同意將群組觸發訊息及必要上下文送至此 Key 的供應商；"
                "費用與額度歸你，不會使用其他備援 Key。仍須群組管理員確認啟用。",
                reply_markup={"inline_keyboard": rows},
            )
        except ValueError as exc:
            self._send(message, str(exc))

    def handle_callback(self, callback: TelegramCallbackQuery) -> bool:
        if not callback.data.startswith("ai:"):
            return False
        try:
            data = CallbackData.decode(callback.data)
            if data.action != "g" or not data.value or "_" not in data.value:
                raise ValueError("授權按鈕不正確，請回群組重新開始。")
            owner, key_id = data.value.split("_", 1)
            if (
                callback.message is None
                or callback.message.chat.type != "private"
                or owner != str(callback.from_user.id)
            ):
                raise ValueError("請在你與 Bot 的私聊選擇自己的 Key。")
            chat_id = group_chat_id(data.target_id)
            self._grant_group(callback.from_user.id, chat_id, key_id)
        except ValueError as exc:
            self._client.answer_callback_query(callback.id, str(exc))
            return True
        self._client.answer_callback_query(callback.id, "已授權，等待群組管理員啟用。")
        text = (
            f"已授權群組 {chat_id} 使用你選擇的 Key，"
            "請管理員在群組 /ai group on 啟用。\n"
            "觸發訊息及必要上下文將送至該 Key 的供應商，費用與額度歸你。\n"
            f"隨時撤銷：/ai group revoke {chat_id}"
        )
        try:
            self._client.edit_message_text(
                callback.message.chat.id,
                callback.message.id,
                text,
                reply_markup={"inline_keyboard": []},
            )
        except TelegramApiError:
            self._send(callback.message, text)
        return True

    def _panel(self, user_id: int) -> str:
        enabled = self._keys.enabled(user_id)
        lines = [f"AI 語意解析：{'開啟' if enabled else '關閉（使用基本解析）'}"]
        lines.extend(f"供應商：{p.id} · {p.name}" for p in self._gateway.providers.values())
        if not self._keys.configured:
            lines.append("Bot 伺服器尚未設定 AI_ENCRYPTION_KEY；可刪除已存的 Key，但無法使用 AI。")
        for index, key in enumerate(self._keys.list(user_id), 1):
            status = "已設定（尚未驗證）"
            if key.status == "ready":
                status = "最近呼叫成功"
            elif key.status == "invalid":
                status = "憑證無效／權限不足；修正後請刪除並重新新增"
            elif (
                key.cooldown_until
                and datetime.fromisoformat(key.cooldown_until) > datetime.now().astimezone()
            ):
                status = f"冷卻至 {key.cooldown_until}"
            if key.last_error in PROVIDER_ERRORS:
                if key.status == "unchecked":
                    status = "最近呼叫失敗"
                status += f"；最近失敗：{PROVIDER_ERRORS[key.last_error]}"
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
                "群組授權與撤銷：/ai group（與個人開關獨立）",
                "Key 會經 Telegram 傳給此 Bot，於伺服器加密保存。",
                "個人啟用後，一般私聊文字及必要上下文會送至 Key 所屬供應商；群組需另行授權。",
                "費用和共用額度依供應商帳戶設定。",
            ]
        )
        return "\n".join(lines)

    def _send(self, message: TelegramMessage, text: str) -> None:
        self._client.send_message(message.chat.id, text)


def group_chat_id(value: str) -> int:
    try:
        chat_id = int(value)
    except ValueError:
        raise ValueError("群組 ID 不正確，請在目標群組用 /ai group 查看。") from None
    if not -(2**63) <= chat_id < 0:
        raise ValueError("群組 ID 不正確，請在目標群組用 /ai group 查看。")
    return chat_id
