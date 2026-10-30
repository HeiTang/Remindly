from __future__ import annotations

import http.client
import io
import json
import unittest
import urllib.error
from copy import deepcopy
from email.message import Message

from test_ai import SECRET, parsed

from remindly.ai.contracts import ProviderError
from remindly.ai.groq import GroqProvider, groq_schema
from remindly.ai.parser import SCHEMA


def completion(data=None, *, finish="stop") -> bytes:
    return json.dumps(
        {"choices": [{"finish_reason": finish, "message": {"content": json.dumps(data)}}]}
    ).encode()


class GroqProviderTest(unittest.TestCase):
    def test_truncated_chunked_response_is_a_sanitized_connection_error(self) -> None:
        class Socket:
            def makefile(self, *args):
                return io.BytesIO(
                    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                    b"100\r\n" + SECRET.encode()
                )

        response = http.client.HTTPResponse(Socket())
        response.begin()
        provider = GroqProvider(opener=lambda *args, **kwargs: response)
        with self.assertRaises(ProviderError) as error:
            provider.generate(SECRET, "system", {}, SCHEMA)
        self.assertEqual("connection", error.exception.kind)
        self.assertNotIn(SECRET, str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)
        self.assertTrue(response.isclosed())

    def test_request_contract_and_schema_conversion(self) -> None:
        before = deepcopy(SCHEMA)

        def opener(request, *, timeout):
            self.assertEqual(12.5, timeout)
            self.assertEqual("POST", request.get_method())
            self.assertEqual("https://api.groq.com/openai/v1/chat/completions", request.full_url)
            self.assertEqual(f"Bearer {SECRET}", request.get_header("Authorization"))
            self.assertEqual("Remindly", request.get_header("User-agent"))
            self.assertNotIn(SECRET, request.full_url)
            body = json.loads(request.data)
            self.assertNotIn(SECRET, request.data.decode())
            self.assertEqual("openai/gpt-oss-120b", body["model"])
            self.assertEqual("system", body["messages"][0]["content"])
            self.assertEqual({"text": "提醒我"}, json.loads(body["messages"][1]["content"]))
            response_format = body["response_format"]
            self.assertEqual("json_schema", response_format["type"])
            self.assertTrue(response_format["json_schema"]["strict"])
            converted = response_format["json_schema"]["schema"]
            self.assertEqual(groq_schema(SCHEMA), converted)
            self.assertFalse(converted["additionalProperties"])
            recurrence = converted["properties"]["recurrence"]
            self.assertFalse(recurrence["additionalProperties"])
            self.assertEqual(["object", "null"], recurrence["type"])
            self.assertEqual(["integer", "null"], recurrence["properties"]["hour"]["type"])
            return io.BytesIO(completion(parsed()))

        provider = GroqProvider(opener=opener)
        self.assertEqual(
            parsed(), provider.generate(SECRET, "system", {"text": "提醒我"}, SCHEMA, timeout=12.5)
        )
        self.assertEqual(before, SCHEMA)
        self.assertIsNone(provider.model_display_name(SECRET, timeout=2))
        array = {"type": "array", "items": SCHEMA}
        self.assertFalse(groq_schema(array)["items"]["additionalProperties"])

    def test_http_errors_are_sanitized_and_quota_uses_only_headers(self) -> None:
        cases = [
            (401, {}, "invalid", 60),
            (403, {}, "permission", 60),
            (400, {}, "request", 60),
            (302, {}, "request", 60),
            (503, {}, "temporary", 60),
            (429, {}, "quota", 60),
            (429, {"retry-after": "120.5"}, "quota", 121),
            (429, {"retry-after": "nan"}, "quota", 60),
            (429, {"retry-after": "inf"}, "quota", 60),
            (429, {"retry-after": "bad"}, "quota", 60),
            (429, {"retry-after": "999999"}, "quota", 90001),
            (429, {"x-ratelimit-remaining-requests": "0"}, "daily_quota", 60),
            (429, {"x-ratelimit-remaining-tokens": "0"}, "rate", 60),
            (429, {"x-ratelimit-remaining-requests": "2"}, "quota", 60),
        ]
        for status, fields, kind, cooldown in cases:
            headers = Message()
            for name, value in fields.items():
                headers[name] = value
            stream = io.BytesIO(SECRET.encode())

            def opener(*args, status=status, headers=headers, stream=stream, **kwargs):
                raise urllib.error.HTTPError(SECRET, status, SECRET, headers, stream)

            with (
                self.subTest(status=status, fields=fields),
                self.assertRaises(ProviderError) as error,
            ):
                GroqProvider(opener=opener).generate(SECRET, "system", {}, SCHEMA)
            self.assertEqual((kind, cooldown), (error.exception.kind, error.exception.cooldown))
            self.assertNotIn(SECRET, str(error.exception))
            self.assertTrue(error.exception.__suppress_context__)
            self.assertTrue(stream.closed)

    def test_transport_errors_and_bad_responses(self) -> None:
        for failure, kind in (
            (TimeoutError(SECRET), "timeout"),
            (urllib.error.URLError(TimeoutError(SECRET)), "timeout"),
            (urllib.error.URLError(SECRET), "connection"),
            (OSError(SECRET), "connection"),
        ):

            def opener(*args, failure=failure, **kwargs):
                raise failure

            with self.subTest(kind=kind), self.assertRaises(ProviderError) as error:
                GroqProvider(opener=opener).generate(SECRET, "system", {}, SCHEMA)
            self.assertEqual(kind, error.exception.kind)
            self.assertNotIn(SECRET, str(error.exception))

        for body in (
            b"bad",
            b"[]",
            b"{}",
            b'{"choices": []}',
            b'{"choices": [null]}',
            completion([]),
            completion(parsed(), finish="length"),
            b'{"choices": [{"finish_reason": "stop", "message": null}]}',
            b'{"choices": [{"finish_reason": "stop", "message": {"content": "bad"}}]}',
            b'{"choices": [{"finish_reason": "stop", '
            b'"message": {"refusal": "no", "content": "{}"}}]}',
        ):
            with self.subTest(body=body), self.assertRaises(ProviderError) as error:
                GroqProvider(opener=lambda *a, body=body, **kw: io.BytesIO(body)).generate(
                    SECRET, "system", {}, SCHEMA
                )
            self.assertEqual("response", error.exception.kind)
