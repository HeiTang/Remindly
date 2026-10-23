from __future__ import annotations

import time
from collections.abc import Mapping

from remindly.ai.contracts import (
    AI_TIME_BUDGET_SECONDS,
    PROVIDER_ERRORS,
    AiProvider,
    AiUnavailable,
    GeneratedResponse,
    ProviderError,
)
from remindly.ai.keys import ApiKeyStore


class AiGateway:
    def __init__(
        self, keys: ApiKeyStore, providers: Mapping[str, AiProvider], *, default_provider: str
    ) -> None:
        if default_provider not in providers or any(k != p.id for k, p in providers.items()):
            raise ValueError("Invalid AI provider registration")
        self.keys = keys
        self.providers = dict(providers)
        self.default_provider = default_provider

    def enabled(self, user_id: int) -> bool:
        return self.keys.enabled(user_id)

    def add_key(self, user_id: int, key: str, provider: str | None = None) -> str:
        provider = provider or self.default_provider
        if provider not in self.providers:
            raise ValueError("此供應商尚未啟用，請使用 /ai 查看可用選項。")
        return self.keys.add(user_id, key, provider=provider)

    def generate(
        self, user_id: int, system: str, prompt: dict[str, object], schema: dict[str, object]
    ) -> GeneratedResponse:
        deadline = time.monotonic() + AI_TIME_BUDGET_SECONDS
        # shortcut: 未提供專案資訊時無法合併跨 Key 配額，最多嘗試三組避免重複轟炸。
        available = [k for k in self.keys.available(user_id) if k.provider in self.providers]
        for key in available[:3]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            provider = self.providers[key.provider]
            secret = self.keys.decrypt(key)
            try:
                result = provider.generate(secret, system, prompt, schema, timeout=remaining)
            except ProviderError as exc:
                if exc.kind in {"invalid", "permission"}:
                    self.keys.mark(user_id, key.id, "invalid", last_error=exc.kind)
                elif exc.kind in {
                    "quota",
                    "daily_quota",
                    "rate",
                    "temporary",
                    "connection",
                    "timeout",
                }:
                    self.keys.mark(user_id, key.id, "cooldown", exc.cooldown, last_error=exc.kind)
                else:
                    self.keys.mark(user_id, key.id, "unchecked", last_error=exc.kind)
                    raise AiUnavailable(
                        f"Key {key.id}：{PROVIDER_ERRORS[exc.kind]}。草稿已保留。"
                    ) from None
                continue
            self.keys.mark(user_id, key.id, "ready")
            remaining = deadline - time.monotonic()
            display_name = (
                provider.model_display_name(secret, timeout=min(2, remaining))
                if remaining > 0
                else None
            )
            return GeneratedResponse(result, provider.id, provider.model, display_name)
        lines = ["AI 解析未成功："]
        for key in self.keys.list(user_id):
            if key.provider not in self.providers:
                reason = "供應商尚未啟用"
            else:
                reason = PROVIDER_ERRORS.get(
                    key.last_error,
                    "憑證無效／權限不足"
                    if key.status == "invalid"
                    else "未取得失敗原因（本次未嘗試或舊版紀錄）",
                )
            cooldown = f"；冷卻至 {key.cooldown_until}" if key.cooldown_until else ""
            lines.append(f"Key {key.id}：{reason}{cooldown}")
        lines.append(
            f"每次最多嘗試三組 Key，共用 {AI_TIME_BUDGET_SECONDS} 秒等待預算。"
            "草稿已保留；可稍後重試，或用 /ai off 後重送。"
        )
        raise AiUnavailable("\n".join(lines))
