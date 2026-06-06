from __future__ import annotations

from dataclasses import dataclass

NAMESPACE = "reminder"
MAX_CALLBACK_DATA_BYTES = 64


class CallbackDataError(ValueError):
    pass


@dataclass(frozen=True)
class CallbackData:
    namespace: str
    action: str
    target_id: str = "_"
    value: str | None = None

    def encode(self) -> str:
        validate_part("namespace", self.namespace)
        validate_part("action", self.action)
        validate_part("target_id", self.target_id)

        parts = [self.namespace, self.action, self.target_id]
        if self.value is not None:
            parts.append(self.value)

        data = ":".join(parts)
        if len(data.encode("utf-8")) > MAX_CALLBACK_DATA_BYTES:
            raise CallbackDataError("callback_data exceeds Telegram's 64 byte limit")
        return data

    @classmethod
    def decode(cls, data: str) -> CallbackData:
        parts = data.split(":", 3)
        if len(parts) < 2:
            raise CallbackDataError("callback_data must contain namespace and action")

        namespace = parts[0]
        action = parts[1]
        target_id = parts[2] if len(parts) >= 3 and parts[2] else "_"
        value = parts[3] if len(parts) == 4 else None
        validate_part("namespace", namespace)
        validate_part("action", action)
        validate_part("target_id", target_id)
        return cls(namespace=namespace, action=action, target_id=target_id, value=value)


def reminder_callback(action: str, target_id: str = "_", value: str | None = None) -> str:
    return CallbackData(NAMESPACE, action, target_id, value).encode()


def parse_reminder_callback(data: str) -> CallbackData:
    callback = CallbackData.decode(data)
    if callback.namespace != NAMESPACE:
        raise CallbackDataError("callback_data namespace is not reminder")
    return callback


def validate_part(name: str, value: str) -> None:
    if not value:
        raise CallbackDataError(f"{name} cannot be empty")
    if ":" in value:
        raise CallbackDataError(f"{name} cannot contain ':'")
