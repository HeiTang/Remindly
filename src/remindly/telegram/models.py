from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TelegramUser:
    id: int
    first_name: str
    username: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> TelegramUser:
        return cls(
            id=int(payload["id"]),
            first_name=str(payload.get("first_name") or payload.get("username") or payload["id"]),
            username=payload.get("username"),
        )

    @property
    def display_name(self) -> str:
        return self.username and f"@{self.username}" or self.first_name


@dataclass(frozen=True)
class TelegramChat:
    id: int
    type: str
    title: str | None = None
    username: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> TelegramChat:
        return cls(
            id=int(payload["id"]),
            type=str(payload.get("type", "private")),
            title=payload.get("title"),
            username=payload.get("username"),
        )


@dataclass(frozen=True)
class TelegramMessageEntity:
    type: str
    offset: int
    length: int
    user: TelegramUser | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> TelegramMessageEntity:
        user = payload.get("user")
        return cls(
            type=str(payload["type"]),
            offset=int(payload["offset"]),
            length=int(payload["length"]),
            user=TelegramUser.from_payload(user) if user else None,
        )


@dataclass(frozen=True)
class TelegramMessage:
    id: int
    chat: TelegramChat
    from_user: TelegramUser | None
    text: str
    entities: tuple[TelegramMessageEntity, ...]
    reply_to_message: TelegramMessage | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> TelegramMessage:
        reply_payload = payload.get("reply_to_message")
        entities = tuple(
            TelegramMessageEntity.from_payload(entity)
            for entity in payload.get("entities", [])
            if isinstance(entity, dict)
        )
        from_payload = payload.get("from")
        return cls(
            id=int(payload["message_id"]),
            chat=TelegramChat.from_payload(payload["chat"]),
            from_user=TelegramUser.from_payload(from_payload) if from_payload else None,
            text=str(payload.get("text") or ""),
            entities=entities,
            reply_to_message=cls.from_payload(reply_payload) if reply_payload else None,
        )


@dataclass(frozen=True)
class TelegramCallbackQuery:
    id: str
    from_user: TelegramUser
    message: TelegramMessage | None
    data: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> TelegramCallbackQuery:
        message_payload = payload.get("message")
        return cls(
            id=str(payload["id"]),
            from_user=TelegramUser.from_payload(payload["from"]),
            message=TelegramMessage.from_payload(message_payload) if message_payload else None,
            data=str(payload.get("data") or ""),
        )


@dataclass(frozen=True)
class TelegramUpdate:
    id: int
    message: TelegramMessage | None = None
    callback_query: TelegramCallbackQuery | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> TelegramUpdate:
        message_payload = payload.get("message")
        callback_payload = payload.get("callback_query")
        return cls(
            id=int(payload["update_id"]),
            message=TelegramMessage.from_payload(message_payload) if message_payload else None,
            callback_query=(
                TelegramCallbackQuery.from_payload(callback_payload) if callback_payload else None
            ),
        )
