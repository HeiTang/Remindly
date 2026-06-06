from __future__ import annotations

import html
import re


def html_escape(value: str) -> str:
    return html.escape(value, quote=False)


def normalize_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def strip_bot_mention(text: str, bot_username: str | None) -> str:
    if not bot_username:
        return text.strip()

    return re.sub(rf"^@{re.escape(bot_username)}\b\s*", "", text.strip(), flags=re.IGNORECASE)


def command_body(text: str, command: str, bot_username: str | None = None) -> str | None:
    command_pattern = rf"^/{re.escape(command)}(?:@{re.escape(bot_username)})?\b"
    if bot_username is None:
        command_pattern = rf"^/{re.escape(command)}(?:@\w+)?\b"

    match = re.match(command_pattern, text.strip(), flags=re.IGNORECASE)
    if not match:
        return None

    return text[match.end() :].strip()
