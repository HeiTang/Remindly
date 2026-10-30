from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

AI_TIME_BUDGET_SECONDS = 18


class AiUnavailable(RuntimeError):
    """Only fixed, credential-free messages may cross the bot boundary."""


PROVIDER_ERRORS = {
    "invalid": "API Key 無效或已撤銷",
    "permission": "供應商拒絕存取，請檢查 Key 限制與專案權限（HTTP 403）",
    "quota": "供應商限制額度或速率（HTTP 429，未能確認是哪一種）",
    "daily_quota": "供應商回報每日額度限制",
    "rate": "供應商回報每分鐘或每秒速率限制",
    "timeout": "AI 請求逾時",
    "connection": "無法連線至 AI 供應商（網路、DNS 或 TLS 問題）",
    "temporary": "AI 供應商服務暫時異常（HTTP 5xx）",
    "request": "供應商拒絕請求，請由 Bot 維運者檢查模型與請求設定",
    "response": "AI 回應格式錯誤或內容不完整",
}


class ProviderError(Exception):
    def __init__(
        self,
        kind: Literal[
            "invalid",
            "permission",
            "quota",
            "daily_quota",
            "rate",
            "timeout",
            "connection",
            "temporary",
            "request",
            "response",
        ],
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
        timeout: float = AI_TIME_BUDGET_SECONDS,
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
