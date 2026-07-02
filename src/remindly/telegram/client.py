from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from remindly.telegram.models import TelegramUpdate

LOGGER = logging.getLogger(__name__)


class TelegramApiError(RuntimeError):
    def __init__(
        self,
        method: str,
        description: str,
        *,
        error_code: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        self.method = method
        self.description = description
        self.error_code = error_code
        self.retry_after = retry_after
        super().__init__(self._message())

    def _message(self) -> str:
        code = f" error_code={self.error_code}" if self.error_code is not None else ""
        retry = f" retry_after={self.retry_after}" if self.retry_after is not None else ""
        return f"Telegram API {self.method} failed:{code}{retry} {self.description}"


@dataclass(frozen=True)
class RetryConfig:
    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0


@dataclass(frozen=True)
class BotCommand:
    command: str
    description: str

    def to_payload(self) -> dict[str, str]:
        return {"command": self.command, "description": self.description}


class TelegramClient:
    def __init__(
        self,
        token: str,
        *,
        request_timeout_seconds: int = 60,
        retry_config: RetryConfig | None = None,
        opener: Callable[..., object] = urllib.request.urlopen,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._request_timeout_seconds = request_timeout_seconds
        self._retry_config = retry_config or RetryConfig()
        self._opener = opener
        self._sleeper = sleeper

    def call(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        request = self._build_request(method, payload or {})
        last_error: TelegramApiError | None = None

        for attempt in range(1, self._retry_config.max_attempts + 1):
            try:
                body = self._execute_request(request)
            except urllib.error.HTTPError as exc:
                body = read_error_body(exc)
                retry_after = extract_retry_after(body)
                error_code = extract_error_code(body) or exc.code
                description = extract_description(body) or exc.reason or "HTTP error"
                if self._should_retry(error_code, attempt):
                    self._sleep_before_retry(method, attempt, retry_after=retry_after)
                    continue
                raise TelegramApiError(
                    method,
                    description,
                    error_code=error_code,
                    retry_after=retry_after,
                ) from exc
            except urllib.error.URLError as exc:
                reason = exc.reason if hasattr(exc, "reason") else exc
                last_error = TelegramApiError(method, str(reason))
                if self._has_attempt_left(attempt):
                    self._sleep_before_retry(method, attempt)
                    continue
                raise last_error from exc

            if body.get("ok"):
                return body

            retry_after = extract_retry_after(body)
            error_code = extract_error_code(body)
            description = extract_description(body) or "Telegram API returned ok=false"
            if self._should_retry(error_code, attempt):
                self._sleep_before_retry(method, attempt, retry_after=retry_after)
                continue
            raise TelegramApiError(
                method,
                description,
                error_code=error_code,
                retry_after=retry_after,
            )

        if last_error:
            raise last_error
        raise TelegramApiError(method, "Retry attempts exhausted")

    def get_updates(self, offset: int | None, timeout: int) -> list[TelegramUpdate]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset

        body = self.call("getUpdates", payload)
        return [
            TelegramUpdate.from_payload(update)
            for update in body.get("result", [])
            if isinstance(update, dict)
        ]

    def get_chat_member(self, chat_id: int, user_id: int) -> str:
        body = self.call("getChatMember", {"chat_id": chat_id, "user_id": user_id})
        result = body.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("status"), str):
            raise TelegramApiError("getChatMember", "Telegram API returned invalid chat member")
        return str(result["status"])

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> int | None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup

        body = self.call("sendMessage", payload)
        return _extract_message_id(body)

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> int | None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        body = self.call("editMessageText", payload)
        return _extract_message_id(body)

    def delete_message(self, chat_id: int, message_id: int) -> None:
        self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text

        self.call("answerCallbackQuery", payload)

    def set_my_commands(self, commands: list[BotCommand]) -> None:
        self.call("setMyCommands", {"commands": [command.to_payload() for command in commands]})

    def delete_webhook(self) -> None:
        try:
            self.call("deleteWebhook", {"drop_pending_updates": False})
        except TelegramApiError:
            LOGGER.exception("Failed to delete webhook before long polling")

    def _build_request(self, method: str, payload: dict[str, Any]) -> urllib.request.Request:
        encoded = json.dumps(payload).encode("utf-8")
        return urllib.request.Request(
            f"{self._base_url}/{method}",
            data=encoded,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

    def _execute_request(self, request: urllib.request.Request) -> dict[str, Any]:
        with self._opener(request, timeout=self._request_timeout_seconds) as response:
            try:
                return json.loads(response.read().decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise TelegramApiError("unknown", "Telegram API returned invalid JSON") from exc

    def _should_retry(self, error_code: int | None, attempt: int) -> bool:
        if not self._has_attempt_left(attempt):
            return False
        if error_code == 429:
            return True
        return error_code is not None and 500 <= error_code <= 599

    def _has_attempt_left(self, attempt: int) -> bool:
        return attempt < self._retry_config.max_attempts

    def _sleep_before_retry(
        self,
        method: str,
        attempt: int,
        *,
        retry_after: int | None = None,
    ) -> None:
        delay = retry_after if retry_after is not None else self._backoff_delay(attempt)
        LOGGER.warning("Retrying Telegram API %s after %.2fs", method, delay)
        self._sleeper(delay)

    def _backoff_delay(self, attempt: int) -> float:
        delay = self._retry_config.base_delay_seconds * (2 ** (attempt - 1))
        return min(delay, self._retry_config.max_delay_seconds)


def read_error_body(exc: urllib.error.HTTPError) -> dict[str, Any]:
    try:
        raw = exc.read().decode("utf-8")
        body = json.loads(raw)
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


def extract_error_code(body: dict[str, Any]) -> int | None:
    value = body.get("error_code")
    return int(value) if isinstance(value, int) else None


def extract_description(body: dict[str, Any]) -> str | None:
    value = body.get("description")
    return str(value) if isinstance(value, str) else None


def _extract_message_id(body: dict[str, Any]) -> int | None:
    result = body.get("result")
    if isinstance(result, dict):
        raw = result.get("message_id")
        if isinstance(raw, int):
            return raw
    return None


def extract_retry_after(body: dict[str, Any]) -> int | None:
    parameters = body.get("parameters")
    if not isinstance(parameters, dict):
        return None
    value = parameters.get("retry_after")
    return int(value) if isinstance(value, int) else None
