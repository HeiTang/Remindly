from __future__ import annotations

import http.client
import json
import math
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from remindly.ai.contracts import AI_TIME_BUDGET_SECONDS, ProviderError
from remindly.ai.gemini import NoRedirect


class GroqProvider:
    id = "groq"
    name = "Groq"
    model = "openai/gpt-oss-120b"

    def __init__(self, *, opener: Callable[..., Any] | None = None) -> None:
        self._opener = opener or urllib.request.build_opener(NoRedirect()).open

    def model_display_name(self, key: str, *, timeout: float) -> str | None:
        # Groq 的模型 metadata 沒有可讀名稱欄位，沿用共用 AI 標籤。
        return None

    def generate(
        self,
        key: str,
        system: str,
        prompt: dict[str, object],
        schema: dict[str, object],
        *,
        timeout: float = AI_TIME_BUDGET_SECONDS,
    ) -> dict[str, object]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "reminder", "strict": True, "schema": groq_schema(schema)},
            },
            "reasoning_effort": "low",
            "max_completion_tokens": 2048,
            "temperature": 0,
        }
        request = urllib.request.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        try:
            with self._opener(request, timeout=timeout) as response:
                body = json.loads(response.read(131073))
        except urllib.error.HTTPError as exc:
            # 不讀取可能回顯 Key 的錯誤內文，只採用狀態碼與結構化 headers。
            try:
                if exc.code == 401:
                    raise ProviderError("invalid") from None
                if exc.code == 403:
                    raise ProviderError("permission") from None
                if exc.code == 429:
                    headers = exc.headers or {}
                    cooldown = 60
                    with suppress(TypeError, ValueError, OverflowError):
                        cooldown = max(
                            60, min(90001, math.ceil(float(headers.get("retry-after", "60"))))
                        )
                    if headers.get("x-ratelimit-remaining-requests") == "0":
                        raise ProviderError("daily_quota", cooldown) from None
                    if headers.get("x-ratelimit-remaining-tokens") == "0":
                        raise ProviderError("rate", cooldown) from None
                    raise ProviderError("quota", cooldown) from None
                if exc.code >= 500:
                    raise ProviderError("temporary") from None
                raise ProviderError("request") from None
            finally:
                exc.close()
        except TimeoutError:
            raise ProviderError("timeout") from None
        except urllib.error.URLError as exc:
            kind = "timeout" if isinstance(exc.reason, TimeoutError) else "connection"
            raise ProviderError(kind) from None
        except (OSError, http.client.HTTPException):
            raise ProviderError("connection") from None
        except (ValueError, UnicodeError):
            raise ProviderError("response") from None
        try:
            choice = body["choices"][0]
            if choice["finish_reason"] != "stop" or choice["message"].get("refusal"):
                raise ValueError("incomplete")
            result = json.loads(choice["message"]["content"])
            if not isinstance(result, dict):
                raise ValueError("not an object")
            return result
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise ProviderError("response") from None


def groq_schema(schema: dict[str, object]) -> dict[str, object]:
    result = dict(schema)
    if "properties" in schema:
        result["properties"] = {
            name: groq_schema(value) for name, value in schema["properties"].items()
        }
        result["additionalProperties"] = False
    if "items" in schema:
        result["items"] = groq_schema(schema["items"])
    return result
