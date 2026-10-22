from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


class AiUnavailable(RuntimeError):
    """Only fixed, credential-free messages may cross the bot boundary."""


class ProviderError(Exception):
    def __init__(
        self,
        kind: Literal["invalid", "quota", "temporary", "request", "response"],
        cooldown: int = 60,
    ) -> None:
        self.kind = kind
        self.cooldown = cooldown
        super().__init__(kind)


class AiProvider(Protocol):
    """Adapters accept the app's JSON Schema subset and sanitize upstream errors."""

    id: str
    name: str
    model: str

    def model_display_name(self, key: str, *, timeout: float) -> str | None: ...

    def generate(
        self,
        key: str,
        system: str,
        prompt: dict[str, object],
        schema: dict[str, object],
        *,
        timeout: float = 6,
    ) -> dict[str, object]: ...


@dataclass(frozen=True)
class GeneratedResponse:
    data: dict[str, object]
    provider: str
    model: str
    model_display_name: str | None = None


class SemanticGenerator(Protocol):
    def enabled(self, user_id: int) -> bool: ...

    def generate(
        self, user_id: int, system: str, prompt: dict[str, object], schema: dict[str, object]
    ) -> GeneratedResponse: ...
