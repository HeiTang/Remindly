from __future__ import annotations

import io
import json
import logging
import unittest
import urllib.error
from dataclasses import dataclass
from typing import Any

from remindly.telegram.client import RetryConfig, TelegramApiError, TelegramClient


class FakeResponse:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._body).encode("utf-8")


@dataclass
class FakeOpener:
    responses: list[object]

    def __post_init__(self) -> None:
        self.calls = 0

    def __call__(self, _request, *, timeout: int):
        del timeout
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class TelegramClientRetryTest(unittest.TestCase):
    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)

    def tearDown(self) -> None:
        logging.disable(logging.NOTSET)

    def test_retries_429_using_retry_after(self) -> None:
        sleeps: list[float] = []
        opener = FakeOpener(
            [
                FakeResponse(
                    {
                        "ok": False,
                        "error_code": 429,
                        "description": "Too Many Requests",
                        "parameters": {"retry_after": 2},
                    }
                ),
                FakeResponse({"ok": True, "result": True}),
            ]
        )
        client = self._client(opener, sleeps)

        result = client.call("sendMessage", {"chat_id": 1, "text": "hi"})

        self.assertTrue(result["ok"])
        self.assertEqual(2, opener.calls)
        self.assertEqual([2], sleeps)

    def test_retries_5xx_with_backoff(self) -> None:
        sleeps: list[float] = []
        opener = FakeOpener(
            [
                self._http_error(500, {"ok": False, "error_code": 500, "description": "Internal"}),
                FakeResponse({"ok": True, "result": True}),
            ]
        )
        client = self._client(opener, sleeps)

        result = client.call("sendMessage", {"chat_id": 1, "text": "hi"})

        self.assertTrue(result["ok"])
        self.assertEqual([0.1], sleeps)

    def test_does_not_retry_400(self) -> None:
        sleeps: list[float] = []
        opener = FakeOpener(
            [self._http_error(400, {"ok": False, "error_code": 400, "description": "Bad Request"})]
        )
        client = self._client(opener, sleeps)

        with self.assertRaises(TelegramApiError) as ctx:
            client.call("sendMessage", {"chat_id": 1, "text": "hi"})

        self.assertEqual(1, opener.calls)
        self.assertEqual([], sleeps)
        self.assertEqual(400, ctx.exception.error_code)

    def test_network_error_retries_without_leaking_token(self) -> None:
        sleeps: list[float] = []
        opener = FakeOpener(
            [
                urllib.error.URLError("temporary failure"),
                urllib.error.URLError("still down"),
            ]
        )
        client = self._client(opener, sleeps, token="123:SECRET")

        with self.assertRaises(TelegramApiError) as ctx:
            client.call("sendMessage", {"chat_id": 1, "text": "hi"})

        self.assertNotIn("123:SECRET", str(ctx.exception))
        self.assertEqual([0.1], sleeps)

    def _client(
        self,
        opener: FakeOpener,
        sleeps: list[float],
        *,
        token: str = "token",
    ) -> TelegramClient:
        return TelegramClient(
            token,
            retry_config=RetryConfig(max_attempts=2, base_delay_seconds=0.1, max_delay_seconds=1),
            opener=opener,
            sleeper=sleeps.append,
        )

    def _http_error(self, status: int, body: dict[str, Any]) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            url="https://api.telegram.org/botTOKEN/sendMessage",
            code=status,
            msg="error",
            hdrs=None,
            fp=io.BytesIO(json.dumps(body).encode("utf-8")),
        )


if __name__ == "__main__":
    unittest.main()
