from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from remindly.ai.contracts import AI_TIME_BUDGET_SECONDS, ProviderError


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 不讓帶憑證的請求跟隨到其他主機。
        return None


class GeminiProvider:
    id = "gemini"
    name = "Google Gemini"
    model = "gemini-3.5-flash-lite"

    def __init__(self, *, opener: Callable[..., Any] | None = None) -> None:
        self._opener = opener or urllib.request.build_opener(NoRedirect()).open
        self._model_names: dict[str, str] = {}

    def model_display_name(self, key: str, *, timeout: float) -> str | None:
        if self.model in self._model_names:
            return self._model_names[self.model]
        request = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}",
            headers={"x-goog-api-key": key},
        )
        try:
            with self._opener(request, timeout=timeout) as response:
                metadata = json.loads(response.read(65537))
            name = metadata.get("displayName") if isinstance(metadata, dict) else None
            if isinstance(name, str) and name.strip() and len(name) <= 128:
                self._model_names[self.model] = name.strip()
                return name.strip()
        except urllib.error.HTTPError as exc:
            exc.close()
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, UnicodeError):
            # 名稱查詢不是解析成功的必要條件，不記錄可能含 Key 的上游錯誤。
            pass
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
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [
                {"role": "user", "parts": [{"text": json.dumps(prompt, ensure_ascii=False)}]}
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": gemini_schema(schema),
                "maxOutputTokens": 2048,
                "temperature": 0,
            },
        }
        request = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": key},
        )
        try:
            with self._opener(request, timeout=timeout) as response:
                body = json.loads(response.read(131073))
        except urllib.error.HTTPError as exc:
            # 上游錯誤可能回顯 Key，不保留 message、URL 或原始 exception。
            try:
                body = json.loads(exc.read(65536))
            except (ValueError, OSError):
                body = {}
            error = body.get("error") if isinstance(body, dict) else None
            error = error if isinstance(error, dict) else {}
            status = error.get("status")
            if exc.code == 401 or status == "API_KEY_INVALID":
                raise ProviderError("invalid") from None
            if exc.code == 403:
                raise ProviderError("permission") from None
            if exc.code == 400:
                details = error.get("details")
                details = details if isinstance(details, list) else []
                if any(
                    isinstance(d, dict) and d.get("reason") == "API_KEY_INVALID" for d in details
                ):
                    raise ProviderError("invalid") from None
                raise ProviderError("request") from None
            if exc.code == 429:
                details = error.get("details")
                quotas = [
                    str(v.get("quotaId", "")).lower().replace("_", "")
                    for d in (details if isinstance(details, list) else [])
                    if isinstance(d, dict) and isinstance(d.get("violations"), list)
                    for v in d["violations"]
                    if isinstance(v, dict)
                ]
                if any("perday" in q for q in quotas):
                    raise ProviderError("daily_quota", quota_cooldown(body)) from None
                if any("perminute" in q or "persecond" in q for q in quotas):
                    raise ProviderError("rate", quota_cooldown(body)) from None
                raise ProviderError("quota", quota_cooldown(body)) from None
            if exc.code >= 500:
                raise ProviderError("temporary") from None
            raise ProviderError("request") from None
        except TimeoutError:
            raise ProviderError("timeout") from None
        except urllib.error.URLError as exc:
            kind = "timeout" if isinstance(exc.reason, TimeoutError) else "connection"
            raise ProviderError(kind) from None
        except OSError:
            raise ProviderError("connection") from None
        except (ValueError, UnicodeError):
            raise ProviderError("response") from None
        try:
            candidate = body["candidates"][0]
            if candidate.get("finishReason") != "STOP":
                raise ValueError("incomplete")
            parts = candidate["content"]["parts"]
            text = "".join(p["text"] for p in parts if not p.get("thought"))
            result = json.loads(text)
            if not isinstance(result, dict):
                raise ValueError("not an object")
            return result
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise ProviderError("response") from None


def gemini_schema(schema: dict[str, object]) -> dict[str, object]:
    """Convert our JSON Schema subset at the adapter boundary, leaving the source untouched."""
    result = dict(schema)
    kind = schema.get("type")
    if isinstance(kind, list):
        result["nullable"] = "null" in kind
        kind = next(item for item in kind if item != "null")
    if isinstance(kind, str):
        result["type"] = kind.upper()
    if "properties" in schema:
        result["properties"] = {
            name: gemini_schema(value) for name, value in schema["properties"].items()
        }
    if "items" in schema:
        result["items"] = gemini_schema(schema["items"])
    return result


def quota_cooldown(body: object) -> int:
    """只利用結構化 quota/retry metadata；無法辨別時不宣稱每日額度耗盡。"""
    seconds = 60
    if not isinstance(body, dict) or not isinstance(body.get("error"), dict):
        return seconds
    details = body["error"].get("details")
    for detail in details if isinstance(details, list) else []:
        if not isinstance(detail, dict):
            continue
        delay = detail.get("retryDelay")
        if isinstance(delay, str) and delay.endswith("s"):
            with suppress(ValueError, OverflowError):
                seconds = max(seconds, int(float(delay[:-1])) + 1)
        violations = detail.get("violations")
        for violation in violations if isinstance(violations, list) else []:
            if not isinstance(violation, dict):
                continue
            quota = str(violation.get("quotaId", "")).lower()
            if "perday" in quota or "per_day" in quota:
                now = datetime.now(ZoneInfo("America/Los_Angeles"))
                reset = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                seconds = max(seconds, int(reset.timestamp() - now.timestamp()) + 1)
    return min(seconds, 90001)
