from __future__ import annotations

import html
import re

WEEKDAY_RE = re.compile(
    r"(?:(?P<prefix>(?:下)+|這|本)個?)?"
    r"(?:週|周|禮拜|礼拜|星期)"
    r"(?P<weekday>[一二三四五六日天])"
)
PROMPT_SPLIT_RE = re.compile(r"提醒(?:我們|我|大家)?")
NEW_REMINDER_RE = re.compile(
    r"(?:^|[,，;；。!?！？])\s*(?:請\s*)?"
    r"(?:(?:另外|另|再)\s*(?:新增|建立|開|設)?|新增|新建|建立|另開)\s*"
    r"(?:一(?:個|筆|則|條)\s*)?(?P<request>提醒(?:我們|我|大家))"
)


def new_reminder_text(text: str) -> str | None:
    # 多筆提醒保留完整輸入交由追問，不擷取其中一筆。
    if len(PROMPT_SPLIT_RE.findall(text)) != 1:
        return None
    match = NEW_REMINDER_RE.search(text)
    return text[match.start("request") :] if match else None


def html_escape(value: str) -> str:
    return html.escape(value, quote=False)


def normalize_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


REMINDER_LEADING_TIME_RE = re.compile(
    r"^(?:"
    r"今天|今日|明天|明日|後天|今晚|明早|明晚|後早|後晚|" + WEEKDAY_RE.pattern + r"|"
    r"(?:半|\d+|[零〇一二兩两三四五六七八九十]{1,3})\s*"
    r"(?:分鐘|分|小時|小时|個小時|个小时|天|日)\s*後|"
    r"\d{1,2}\s*(?:點|点|:|：)"
    r")"
)


def looks_like_reminder_request(text: str) -> bool:
    """用保守句型判斷群組一般訊息是否像提醒，降低閒聊誤觸。"""
    normalized = normalize_spaces(text)
    if "提醒" not in normalized:
        return False

    if normalized.startswith(("提醒我", "提醒我們", "提醒大家")) or new_reminder_text(normalized):
        return True

    if re.search(r"提醒\s*@[A-Za-z0-9_]{5,32}\b", normalized):
        return True

    return bool(REMINDER_LEADING_TIME_RE.match(normalized))


def strip_bot_mention(text: str, bot_username: str | None) -> str:
    if not bot_username:
        return text.strip()

    return re.sub(rf"^@{re.escape(bot_username)}\b\s*", "", text.strip(), flags=re.IGNORECASE)


def command_body(text: str, command: str, bot_username: str | None = None) -> str | None:
    """比對 `/command` 或 `/command@bot`。當 `bot_username=None`（部署未設 `BOT_USERNAME`）
    時，接受任意 `@\\w+` 尾綴避免拒絕合法輸入。"""
    # 先分支再構造 pattern：`re.escape(None)` 會 TypeError，過去 bot_username 未設時
    # 每個 slash command 都會 crash 掉整個 handle_update。
    if bot_username is None:
        command_pattern = rf"^/{re.escape(command)}(?:@\w+)?\b"
    else:
        command_pattern = rf"^/{re.escape(command)}(?:@{re.escape(bot_username)})?\b"

    match = re.match(command_pattern, text.strip(), flags=re.IGNORECASE)
    if not match:
        return None

    return text[match.end() :].strip()
