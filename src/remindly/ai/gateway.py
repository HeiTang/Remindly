from __future__ import annotations

import time
from collections.abc import Mapping

from remindly.ai.contracts import AiProvider, AiUnavailable, GeneratedResponse, ProviderError
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
        deadline = time.monotonic() + 18
        # shortcut: 未提供專案資訊時無法合併跨 Key 配額，最多嘗試三組避免重複轟炸。
        available = [k for k in self.keys.available(user_id) if k.provider in self.providers]
        for key in available[:3]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            provider = self.providers[key.provider]
            secret = self.keys.decrypt(key)
            try:
                result = provider.generate(
                    secret, system, prompt, schema, timeout=min(6, remaining)
                )
            except ProviderError as exc:
                if exc.kind == "invalid":
                    self.keys.mark(user_id, key.id, "invalid")
                elif exc.kind in {"quota", "temporary"}:
                    self.keys.mark(user_id, key.id, "cooldown", exc.cooldown)
                else:
                    raise AiUnavailable("AI 回應無法使用，請稍後重試；原草稿已保留。") from None
                continue
            self.keys.mark(user_id, key.id, "ready")
            remaining = deadline - time.monotonic()
            display_name = (
                provider.model_display_name(secret, timeout=min(2, remaining))
                if remaining > 0
                else None
            )
            return GeneratedResponse(result, provider.id, provider.model, display_name)
        raise AiUnavailable(
            "你的 AI Key 目前都無法使用（額度、速率、憑證、供應商未啟用或連線問題）。"
            "草稿已保留；可稍後重試，或用 /ai off 改用基本解析後重送。"
        )
