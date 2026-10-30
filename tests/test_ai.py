from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet

from remindly.ai.contracts import (
    AI_TIME_BUDGET_SECONDS,
    PROVIDER_ERRORS,
    AiUnavailable,
    GeneratedResponse,
    ProviderError,
)
from remindly.ai.gateway import AiGateway
from remindly.ai.gemini import GeminiProvider, gemini_schema, quota_cooldown
from remindly.ai.keys import ApiKeyStore
from remindly.ai.parser import SCHEMA, SemanticParser, validate_result
from remindly.reminders.models import MentionKind, Participant, RecurrencePeriod, RecurrenceRule
from remindly.storage.sqlite import ReminderRepository

NOW = datetime(2026, 10, 22, 12, tzinfo=ZoneInfo("Asia/Taipei"))
SECRET = "AIza" + "a" * 35
SECOND = "AIza" + "b" * 35
PERSON = Participant("Orange", MentionKind.TEXT_MENTION, user_id=7)


def parsed(**changes) -> dict[str, object]:
    return {
        "intent": "create",
        "title": "後天要交報告",
        "date": "2026-10-23",
        "time": "15:00",
        "relative_minutes": None,
        "question": None,
        "recurrence": None,
        "participant_indices": [0],
        **changes,
    }


def rule(**changes) -> dict[str, object]:
    return {
        "period": "daily",
        "hour": 9,
        "minute": 0,
        "weekdays": [],
        "month_days": [],
        "year_month": None,
        "year_day": None,
        "interval_seconds": None,
        **changes,
    }


class ScriptedProvider:
    id = "gemini"
    name = "Google Gemini"
    model = "test-gemini"

    def __init__(self, responses: list[object], *, provider_id: str = "gemini") -> None:
        self.id = provider_id
        self.name = provider_id
        self.model = f"test-{provider_id}"
        self.display_name = f"Test {provider_id}"
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.schemas: list[dict[str, object]] = []
        self.timeouts: list[float] = []

    def model_display_name(self, key, *, timeout):
        return self.display_name

    def generate(self, key, system, prompt, schema, *, timeout=AI_TIME_BUDGET_SECONDS):
        self.calls.append((key, prompt))
        self.timeouts.append(timeout)
        self.schemas.append(schema)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class KeyStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = ReminderRepository(Path(self.tmp.name) / "test.db")
        self.repo.migrate()
        self.master = Fernet.generate_key().decode()
        self.keys = ApiKeyStore(self.repo, self.master)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_encryption_survives_reopen_and_scopes_every_operation(self) -> None:
        first = self.keys.add(7, provider="gemini", key=SECRET)
        self.assertTrue(self.keys.enabled(7))
        self.assertFalse(self.keys.enabled(8))
        self.assertEqual([], self.keys.list(8))
        self.assertFalse(self.keys.delete(8, first))
        with self.assertRaises(ValueError):
            self.keys.move(8, first, 1)
        with self.repo.connect() as db:
            encrypted = bytes(db.execute("select encrypted_key from ai_keys").fetchone()[0])
        self.assertNotIn(SECRET.encode(), encrypted)
        self.assertNotIn(SECRET, repr(self.keys.list(7)))
        reopened = ApiKeyStore(self.repo, self.master)
        self.assertEqual(SECRET, reopened.decrypt(reopened.list(7)[0]))
        self.assertTrue(reopened.delete(7, first))
        self.assertFalse(reopened.enabled(7))

    def test_order_duplicates_switch_and_invalid_input(self) -> None:
        first = self.keys.add(7, provider="gemini", key=SECRET)
        second = self.keys.add(7, provider="gemini", key=SECOND)
        self.keys.move(7, second, 1)
        self.assertEqual([second, first], [k.id for k in self.keys.list(7)])
        with self.assertRaises(ValueError):
            self.keys.add(7, provider="gemini", key=SECRET)
        with self.assertRaises(ValueError):
            self.keys.add(7, provider="gemini", key="bad\nkey")
        self.keys.set_enabled(7, False)
        self.keys.add(7, provider="gemini", key="AIza" + "c" * 35)
        self.assertFalse(self.keys.enabled(7))
        self.keys.set_enabled(7, True)
        self.assertTrue(self.keys.enabled(7))

    def test_missing_or_changed_master_key_fails_closed(self) -> None:
        unavailable = ApiKeyStore(self.repo, "")
        self.assertFalse(unavailable.enabled(7))
        with self.assertRaises(ValueError):
            unavailable.add(7, provider="gemini", key=SECRET)
        self.keys.add(7, provider="gemini", key=SECRET)
        changed = ApiKeyStore(self.repo, Fernet.generate_key().decode())
        with self.assertRaises(AiUnavailable) as error:
            changed.decrypt(changed.list(7)[0])
        self.assertNotIn(SECRET, str(error.exception))

    def test_quota_falls_back_and_cooldown_survives_restart(self) -> None:
        first = self.keys.add(7, provider="gemini", key=SECRET)
        self.keys.add(7, provider="gemini", key=SECOND)
        client = ScriptedProvider([ProviderError("quota", 3600), parsed()])
        gateway = AiGateway(self.keys, {"gemini": client}, default_provider="gemini")
        self.assertEqual(parsed(), gateway.generate(7, "system", {}, SCHEMA).data)
        self.assertEqual([SECRET, SECOND], [call[0] for call in client.calls])
        reopened = ApiKeyStore(self.repo, self.master)
        self.assertEqual([SECOND], [reopened.decrypt(k) for k in reopened.available(7)])
        self.assertEqual("cooldown", next(k for k in reopened.list(7) if k.id == first).status)

    def test_invalid_key_skips_future_calls_and_never_borrows_another_users_key(self) -> None:
        self.keys.add(7, provider="gemini", key=SECRET)
        self.keys.add(8, provider="gemini", key=SECOND)
        client = ScriptedProvider([ProviderError("invalid")])
        gateway = AiGateway(self.keys, {"gemini": client}, default_provider="gemini")
        for _ in range(2):
            with self.assertRaises(AiUnavailable):
                gateway.generate(7, "system", {}, SCHEMA)
        self.assertEqual(1, len(client.calls))
        self.assertEqual("invalid", self.keys.list(7)[0].status)
        self.assertEqual(1, len(self.keys.available(8)))

    def test_failure_reasons_survive_reopen_and_success_clears_them(self) -> None:
        key_id = self.keys.add(7, provider="gemini", key=SECRET)
        for kind, label in PROVIDER_ERRORS.items():
            with self.subTest(kind=kind):
                self.keys.mark(7, key_id, "unchecked")
                client = ScriptedProvider([ProviderError(kind, 60)])
                gateway = AiGateway(self.keys, {"gemini": client}, default_provider="gemini")
                with self.assertRaises(AiUnavailable) as error:
                    gateway.generate(7, "system", {}, SCHEMA)
                self.assertIn(label, str(error.exception))
                self.assertNotIn(SECRET, str(error.exception))
                reopened = ApiKeyStore(self.repo, self.master)
                self.assertEqual(kind, reopened.list(7)[0].last_error)
                if kind not in {"request", "response"}:
                    with self.assertRaises(AiUnavailable) as retry:
                        AiGateway(reopened, {"gemini": client}, default_provider="gemini").generate(
                            7, "system", {}, SCHEMA
                        )
                    self.assertIn(label, str(retry.exception))
                    self.assertEqual(1, len(client.calls))
        self.keys.mark(7, key_id, "unchecked", last_error="timeout")
        gateway = AiGateway(
            self.keys, {"gemini": ScriptedProvider([parsed()])}, default_provider="gemini"
        )
        gateway.generate(7, "system", {}, SCHEMA)
        self.assertIsNone(self.keys.list(7)[0].last_error)

    def test_attempts_are_bounded_and_bad_response_does_not_rotate(self) -> None:
        for i in range(5):
            self.keys.add(7, provider="gemini", key="AIza" + str(i) * 35)
        client = ScriptedProvider([ProviderError("temporary")] * 3)
        with self.assertRaises(AiUnavailable):
            AiGateway(self.keys, {"gemini": client}, default_provider="gemini").generate(
                7, "system", {}, SCHEMA
            )
        self.assertEqual(3, len(client.calls))
        client = ScriptedProvider([ProviderError("response")])
        with self.assertRaises(AiUnavailable):
            AiGateway(self.keys, {"gemini": client}, default_provider="gemini").generate(
                7, "system", {}, SCHEMA
            )
        self.assertEqual(1, len(client.calls))

    def test_no_new_attempt_starts_after_time_budget(self) -> None:
        self.keys.add(7, provider="gemini", key=SECRET)
        self.keys.add(7, provider="gemini", key=SECOND)
        client = ScriptedProvider([ProviderError("temporary")])
        with (
            patch("remindly.ai.gateway.time.monotonic", side_effect=[0, 0, 19]),
            self.assertRaises(AiUnavailable),
        ):
            AiGateway(self.keys, {"gemini": client}, default_provider="gemini").generate(
                7, "system", {}, SCHEMA
            )
        self.assertEqual(1, len(client.calls))

    def test_single_key_can_wait_longer_than_six_seconds(self) -> None:
        class SlowProvider(ScriptedProvider):
            def generate(self, key, system, prompt, schema, *, timeout):
                if timeout < 8:
                    raise ProviderError("timeout")
                return super().generate(key, system, prompt, schema, timeout=timeout)

        self.keys.add(7, provider="gemini", key=SECRET)
        client = SlowProvider([parsed()])
        gateway = AiGateway(self.keys, {"gemini": client}, default_provider="gemini")
        with patch("remindly.ai.gateway.time.monotonic", return_value=0):
            self.assertEqual(parsed(), gateway.generate(7, "system", {}, SCHEMA).data)
        self.assertEqual([18], client.timeouts)
        self.assertEqual("ready", self.keys.list(7)[0].status)

    def test_fallback_uses_only_remaining_shared_time(self) -> None:
        self.keys.add(7, provider="gemini", key=SECRET)
        self.keys.add(7, provider="gemini", key=SECOND)
        client = ScriptedProvider([ProviderError("quota"), parsed()])
        gateway = AiGateway(self.keys, {"gemini": client}, default_provider="gemini")
        with patch("remindly.ai.gateway.time.monotonic", side_effect=[0, 0, 4, 9]):
            self.assertEqual(parsed(), gateway.generate(7, "system", {}, SCHEMA).data)
        self.assertEqual([18, 14], client.timeouts)
        self.assertEqual([SECRET, SECOND], [call[0] for call in client.calls])

    def test_short_remaining_time_is_not_extended_for_generation_or_metadata(self) -> None:
        self.keys.add(7, provider="gemini", key=SECRET)
        client = ScriptedProvider([parsed()])
        gateway = AiGateway(self.keys, {"gemini": client}, default_provider="gemini")
        with (
            patch("remindly.ai.gateway.time.monotonic", side_effect=[0, 16, 18]),
            patch.object(client, "model_display_name") as metadata,
        ):
            self.assertEqual(parsed(), gateway.generate(7, "system", {}, SCHEMA).data)
            metadata.assert_not_called()
        self.assertEqual([2], client.timeouts)

    def test_cooldown_expiry_makes_key_available(self) -> None:
        key_id = self.keys.add(7, provider="gemini", key=SECRET)
        self.keys.mark(7, key_id, "cooldown", 60)
        self.assertEqual([], self.keys.available(7))
        with self.repo.connect() as db:
            db.execute(
                "update ai_keys set cooldown_until = ?", ((NOW - timedelta(days=1)).isoformat(),)
            )
        self.assertEqual(1, len(self.keys.available(7)))

    def test_second_provider_reuses_parser_without_receiving_gemini_key(self) -> None:
        self.keys.add(7, SECRET, provider="gemini")
        key_id = self.keys.add(7, SECOND, provider="other")
        self.keys.move(7, key_id, 1)
        gemini = ScriptedProvider([])
        other = ScriptedProvider([parsed()], provider_id="other")
        gateway = AiGateway(
            self.keys, {"gemini": gemini, "other": other}, default_provider="gemini"
        )
        result = SemanticParser(gateway).parse("明天下午三點提醒我後天交報告", 7, NOW, [PERSON])
        self.assertEqual("other", result.raw["provider"])
        self.assertEqual("test-other", result.raw["model"])
        self.assertEqual("Test other", result.raw["model_display_name"])
        self.assertEqual(SECOND, other.calls[0][0])
        self.assertEqual([], gemini.calls)
        self.assertEqual("object", other.schemas[0]["type"])
        self.assertEqual(["string", "null"], other.schemas[0]["properties"]["title"]["type"])
        self.assertEqual(15, result.remind_at.hour)

    def test_cross_provider_fallback_shares_policy_and_reports_actual_model(self) -> None:
        self.keys.add(7, SECRET, provider="gemini")
        self.keys.add(7, SECOND, provider="other")
        gemini = ScriptedProvider([ProviderError("quota")])
        other = ScriptedProvider([parsed()], provider_id="other")
        gateway = AiGateway(
            self.keys, {"gemini": gemini, "other": other}, default_provider="gemini"
        )
        result = gateway.generate(7, "system", {}, SCHEMA)
        self.assertEqual("other", result.provider)
        self.assertEqual("test-other", result.model)
        self.assertEqual("Test other", result.model_display_name)
        self.assertEqual(SECRET, gemini.calls[0][0])
        self.assertEqual(SECOND, other.calls[0][0])

    def test_removed_or_unknown_provider_never_receives_another_providers_key(self) -> None:
        self.keys.add(7, SECRET, provider="unregistered")
        gemini = ScriptedProvider([])
        gateway = AiGateway(self.keys, {"gemini": gemini}, default_provider="gemini")
        with self.assertRaises(AiUnavailable):
            gateway.generate(7, "system", {}, SCHEMA)
        self.assertEqual([], gemini.calls)
        with self.assertRaises(ValueError):
            gateway.add_key(7, SECOND, "unregistered")
        with self.assertRaises(ValueError):
            AiGateway(self.keys, {"wrong": gemini}, default_provider="wrong")


class GeminiClientTest(unittest.TestCase):
    def test_schema_conversion_is_recursive_and_does_not_modify_shared_schema(self) -> None:
        before = deepcopy(SCHEMA)
        converted = gemini_schema(SCHEMA)
        self.assertEqual("OBJECT", converted["type"])
        self.assertEqual(True, converted["properties"]["recurrence"]["nullable"])
        hour = converted["properties"]["recurrence"]["properties"]["hour"]
        self.assertEqual({"type": "INTEGER", "nullable": True}, hour)
        self.assertEqual(SCHEMA, before)

    def test_real_http_request_contract_and_thinking_parts(self) -> None:
        captured = []

        def opener(request, *, timeout):
            captured.append(request)
            return io.BytesIO(
                json.dumps(
                    {
                        "candidates": [
                            {
                                "finishReason": "STOP",
                                "content": {
                                    "parts": [
                                        {"thought": True, "text": "reasoning"},
                                        {"text": json.dumps(parsed())},
                                    ]
                                },
                            }
                        ]
                    }
                ).encode()
            )

        result = GeminiProvider(opener=opener).generate(
            SECRET, "system", {"text": "提醒我"}, SCHEMA
        )
        self.assertEqual(parsed(), result)
        request = captured[0]
        self.assertIn(GeminiProvider.model, request.full_url)
        self.assertNotIn(SECRET, request.full_url)
        self.assertEqual(SECRET, request.get_header("X-goog-api-key"))
        body = json.loads(request.data)
        self.assertEqual(gemini_schema(SCHEMA), body["generationConfig"]["responseSchema"])
        self.assertNotIn(SECRET, request.data.decode())

    def test_model_display_name_uses_metadata_and_caches_by_model(self) -> None:
        captured = []

        def opener(request, *, timeout):
            captured.append(request)
            self.assertEqual("GET", request.get_method())
            self.assertEqual(SECRET, request.get_header("X-goog-api-key"))
            self.assertNotIn(SECRET, request.full_url)
            self.assertEqual(2, timeout)
            return io.BytesIO(
                json.dumps({"displayName": f"Readable Model {len(captured)}"}).encode()
            )

        provider = GeminiProvider(opener=opener)
        self.assertEqual("Readable Model 1", provider.model_display_name(SECRET, timeout=2))
        self.assertEqual("Readable Model 1", provider.model_display_name(SECOND, timeout=2))
        self.assertEqual(1, len(captured))
        provider.model = "another-model"
        self.assertEqual("Readable Model 2", provider.model_display_name(SECRET, timeout=2))
        self.assertIn("/models/another-model", captured[-1].full_url)

    def test_metadata_failure_or_missing_name_is_optional(self) -> None:
        for body in (b"bad", b"[]", b"{}", b'{"displayName": " "}', b'{"displayName": 42}'):
            with self.subTest(body=body):
                provider = GeminiProvider(opener=lambda *a, body=body, **kw: io.BytesIO(body))
                self.assertIsNone(provider.model_display_name(SECRET, timeout=2))

        def opener(*args, **kwargs):
            raise urllib.error.HTTPError(SECRET, 403, SECRET, {}, io.BytesIO(b"denied"))

        self.assertIsNone(GeminiProvider(opener=opener).model_display_name(SECRET, timeout=2))

    def test_errors_do_not_echo_upstream_credentials(self) -> None:
        cases = [
            (401, {}, "invalid"),
            (403, {}, "permission"),
            (500, {}, "temporary"),
            (429, {}, "quota"),
            (
                429,
                {"error": {"details": [{"violations": [{"quotaId": "RequestsPerDay"}]}]}},
                "daily_quota",
            ),
            (
                429,
                {"error": {"details": [{"violations": [{"quotaId": "RequestsPerMinute"}]}]}},
                "rate",
            ),
            (400, {"error": {"details": [{"reason": "API_KEY_INVALID"}]}}, "invalid"),
            (400, {"error": None}, "request"),
        ]
        for code, details, kind in cases:
            with self.subTest(code=code, kind=kind):

                def opener(request, *, timeout, code=code, details=details):
                    raise urllib.error.HTTPError(
                        SECRET, code, SECRET, {}, io.BytesIO(json.dumps(details).encode())
                    )

                with self.assertRaises(ProviderError) as error:
                    GeminiProvider(opener=opener).generate(SECRET, "system", {}, SCHEMA)
                self.assertEqual(kind, error.exception.kind)
                self.assertNotIn(SECRET, str(error.exception))
                self.assertTrue(error.exception.__suppress_context__)

    def test_timeout_and_malformed_or_truncated_outputs(self) -> None:
        for output in (
            b"bad",
            b"[]",
            b'{"candidates": []}',
            b'{"candidates": [null]}',
            b'{"candidates": [{"finishReason": "STOP", "content": {"parts": [null]}}]}',
            json.dumps(
                {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": []}}]}
            ).encode(),
        ):
            with self.subTest(output=output), self.assertRaises(ProviderError):
                GeminiProvider(opener=lambda *a, output=output, **kw: io.BytesIO(output)).generate(
                    SECRET, "system", {}, SCHEMA
                )

        def timeout(*args, **kwargs):
            raise TimeoutError(SECRET)

        with self.assertRaises(ProviderError) as error:
            GeminiProvider(opener=timeout).generate(SECRET, "system", {}, SCHEMA)
        self.assertEqual("timeout", error.exception.kind)

    def test_connection_and_wrapped_timeout_are_distinct(self) -> None:
        for failure, kind in (
            (urllib.error.URLError(TimeoutError(SECRET)), "timeout"),
            (urllib.error.URLError(SECRET), "connection"),
            (OSError(SECRET), "connection"),
        ):

            def opener(*args, failure=failure, **kwargs):
                raise failure

            with self.subTest(kind=kind), self.assertRaises(ProviderError) as error:
                GeminiProvider(opener=opener).generate(SECRET, "system", {}, SCHEMA)
            self.assertEqual(kind, error.exception.kind)
            self.assertNotIn(SECRET, str(error.exception))

    def test_retry_and_daily_reset_are_distinguished(self) -> None:
        self.assertEqual(60, quota_cooldown({}))
        self.assertEqual(121, quota_cooldown({"error": {"details": [{"retryDelay": "120s"}]}}))
        with patch("remindly.ai.gemini.datetime") as clock:
            clock.now.return_value = NOW.astimezone(ZoneInfo("America/Los_Angeles"))
            delay = quota_cooldown(
                {
                    "error": {
                        "details": [
                            {
                                "violations": [
                                    {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}
                                ]
                            }
                        ]
                    }
                }
            )
        self.assertGreater(delay, 60)
        self.assertLessEqual(delay, 86401)

    def test_daily_quota_handles_25_hour_pacific_day(self) -> None:
        with patch("remindly.ai.gemini.datetime") as clock:
            clock.now.return_value = datetime(2026, 11, 1, tzinfo=ZoneInfo("America/Los_Angeles"))
            delay = quota_cooldown(
                {"error": {"details": [{"violations": [{"quotaId": "RequestsPerDayPerModel"}]}]}}
            )
        self.assertEqual(90001, delay)


class SemanticTimeGuardTest(unittest.TestCase):
    def test_date_then_clock_cannot_clear_pending_condition(self):
        context = self.draft(
            missing_fields=["time", "clarification"],
            parse_result={"question": "是否只限工作日？"},
        )
        for text in ("11/17", "23:55"):
            with self.subTest(text=text):
                result = self.parse(parsed(intent="update"), text, context=context)
                self.assertIn("clarification", result.missing_fields)
                self.assertIn("是否只限工作日？", result.raw["question"])
                context.remind_at = result.remind_at
                context.missing_fields = list(result.missing_fields)
                context.parse_result = result.raw
        self.assertEqual("是否只限工作日？", result.raw["question"])

    def test_unrequested_or_changed_recurrence_requires_confirmation(self):
        for text, recurrence in (
            ("提醒我明天下午三點買湯", rule()),
            ("提醒我明天下午三點每週一吃藥", rule()),
        ):
            with self.subTest(text=text):
                result = self.parse(parsed(recurrence=recurrence), text)
                self.assertIsNone(result.recurrence)
                self.assertIn("clarification", result.missing_fields)
                self.assertIn("週期", result.raw["question"])

    def test_missing_recipients_are_not_guessed_from_multiple_candidates(self):
        other = Participant("Alice", MentionKind.TEXT_MENTION, user_id=8)
        result = self.parse(
            parsed(date="2030-09-17", participant_indices=[]),
            "提醒我09-17 23:55和 @alice 買湯",
            participants=[PERSON, other],
        )
        self.assertIn("participants", result.missing_fields)
        self.assertEqual((), result.participants)

    def parse(self, data, text, *, context=None, now=NOW, participants=None, purpose="reminder"):
        class Gateway:
            def generate(self, *args):
                return GeneratedResponse(data, "test", "variable-model")

        return SemanticParser(Gateway()).parse(
            text, 7, now, participants or [PERSON], context=context, purpose=purpose
        )

    def draft(self, **changes):
        from remindly.reminders.models import ReminderDraft

        values = dict(
            id="guard",
            chat_id=100,
            chat_type="private",
            creator_user_id=7,
            timezone="Asia/Taipei",
            source_text="提醒我要領蝦幣",
            title="要領蝦幣",
            participants=[PERSON],
            missing_fields=["time"],
        )
        values.update(changes)
        return ReminderDraft(**values)

    def test_explicit_date_is_independent_of_model_year(self):
        for text in ("提醒我09-17 23:55領蝦幣", "9/17 23:55提醒我領蝦幣"):
            for year in (2025, 2026, 2027, 2030):
                with self.subTest(text=text, year=year):
                    result = self.parse(parsed(date=f"{year}-10-24", time="09:00"), text)
                    self.assertEqual(
                        datetime(2027, 9, 17, 23, 55, tzinfo=NOW.tzinfo), result.remind_at
                    )
                    self.assertEqual((), result.missing_fields)
                    self.assertEqual("rules", result.raw["time_validation"])

    def test_date_then_clock_cannot_change_date_or_other_fields(self):
        context = self.draft()
        first = self.parse(
            parsed(intent="update", date="2026-09-17", time="12:00"), "09-17", context=context
        )
        self.assertEqual(datetime(2027, 9, 17, tzinfo=NOW.tzinfo), first.remind_at)
        self.assertIn("time", first.missing_fields)
        context.remind_at = first.remind_at
        other = Participant("Alice", MentionKind.TEXT_MENTION, user_id=8)
        for day in ("2026-10-22", "2026-09-17", "2030-01-01"):
            for intent in ("create", "update", "not_reminder"):
                with self.subTest(day=day, intent=intent):
                    result = self.parse(
                        parsed(
                            intent=intent,
                            date=day,
                            time="01:00",
                            title="轉帳",
                            recurrence=rule(),
                            participant_indices=[1],
                            question="改成今天嗎？",
                        ),
                        "23:55",
                        context=context,
                        participants=[PERSON, other],
                    )
                    self.assertEqual(
                        datetime(2027, 9, 17, 23, 55, tzinfo=NOW.tzinfo), result.remind_at
                    )
                    self.assertEqual("要領蝦幣", result.title)
                    self.assertEqual((PERSON,), result.participants)
                    self.assertIsNone(result.recurrence)
                    self.assertEqual("update", result.raw["intent"])
                    self.assertEqual((), result.missing_fields)

    def test_explicit_year_is_never_rolled_forward(self):
        result = self.parse(parsed(date="2030-09-17"), "提醒我2026/09/17 23:55買湯")
        self.assertEqual(datetime(2026, 9, 17, 23, 55, tzinfo=NOW.tzinfo), result.remind_at)
        self.assertIn("time", result.missing_fields)

    def test_invalid_date_cannot_be_replaced_with_valid_date(self):
        for data in (
            parsed(date="2030-09-30"),
            parsed(
                intent="not_reminder",
                title=None,
                date=None,
                time=None,
                question="九月沒有31日",
                participant_indices=[],
            ),
        ):
            with self.subTest(data=data):
                result = self.parse(data, "提醒我09-31 23:55買湯")
                self.assertIsNone(result.remind_at)
                self.assertIn("time", result.missing_fields)
                self.assertEqual("create", result.raw["intent"])
                self.assertIsNotNone(result.raw["question"])
        context = self.draft(
            recurrence=RecurrenceRule(period=RecurrencePeriod.DAILY, hour=9, minute=0)
        )
        result = self.parse(parsed(recurrence=rule()), "09-31 23:55", context=context)
        self.assertIsNone(result.remind_at)
        self.assertIn("time", result.missing_fields)
        self.assertEqual(context.recurrence, result.recurrence)

    def test_subject_date_does_not_override_reminder_time(self):
        result = self.parse(parsed(), "明天下午三點提醒我後天要交報告")
        self.assertEqual(datetime(2026, 10, 23, 15, tzinfo=NOW.tzinfo), result.remind_at)
        self.assertEqual("後天要交報告", result.title)
        data = parsed(date=None, time=None, question="什麼時候提醒？")
        result = self.parse(data, "提醒我買11/17要用的湯匙")
        self.assertNotIn("time_validation", result.raw)

    def test_relative_time_preserves_seconds_and_elapsed_time(self):
        for now in (
            NOW.replace(second=37),
            datetime(2027, 3, 14, 1, 45, tzinfo=ZoneInfo("America/New_York")),
            datetime(2027, 11, 7, 1, 45, tzinfo=ZoneInfo("America/New_York")),
        ):
            with self.subTest(now=now):
                result = self.parse(
                    parsed(date=None, time=None, relative_minutes=30), "半小時後提醒我買湯", now=now
                )
                self.assertEqual(1800, result.remind_at.timestamp() - now.timestamp())
                self.assertEqual(now.second, result.remind_at.second)
                self.assertEqual((), result.missing_fields)

    def test_dst_ambiguity_is_still_blocked(self):
        now = datetime(2027, 11, 6, 12, tzinfo=ZoneInfo("America/New_York"))
        result = self.parse(parsed(date="2027-11-07", time="01:30"), "提醒我明天01:30買湯", now=now)
        self.assertIn("time", result.missing_fields)
        self.assertIn("日光節約", result.raw["question"])

    def test_unresolved_condition_is_not_cleared_by_clock_reply(self):
        context = self.draft(
            missing_fields=["time", "clarification"], parse_result={"question": "是否只限工作日？"}
        )
        result = self.parse(parsed(intent="update"), "23:55", context=context)
        self.assertIn("clarification", result.missing_fields)
        self.assertEqual("是否只限工作日？", result.raw["question"])

    def test_pending_recurrence_uses_original_weekdays(self):
        context = self.draft(source_text="每週一提醒我吃藥", title="吃藥")
        result = self.parse(
            parsed(intent="update", date=None, time=None, recurrence=rule()),
            "早上九點",
            context=context,
        )
        self.assertEqual(RecurrencePeriod.WEEKLY, result.recurrence.period)
        self.assertEqual((0,), result.recurrence.weekdays)
        self.assertEqual(datetime(2026, 10, 26, 9, tzinfo=NOW.tzinfo), result.remind_at)


class SemanticValidationTest(unittest.TestCase):
    def test_separate_dates_title_and_absolute_time(self) -> None:
        result = validate_result(parsed(), NOW, [PERSON])
        self.assertEqual("後天要交報告", result.title)
        self.assertEqual(datetime(2026, 10, 23, 15, tzinfo=NOW.tzinfo), result.remind_at)
        self.assertEqual((), result.missing_fields)

    def test_relative_is_calculated_locally(self) -> None:
        result = validate_result(parsed(date=None, time=None, relative_minutes=30), NOW, [PERSON])
        self.assertEqual(NOW + timedelta(minutes=30), result.remind_at)

    def test_relative_minutes_are_elapsed_time_across_dst(self) -> None:
        now = datetime(2027, 3, 14, 1, 45, tzinfo=ZoneInfo("America/New_York"))
        result = validate_result(parsed(date=None, time=None, relative_minutes=30), now, [PERSON])
        self.assertEqual(1800, result.remind_at.timestamp() - now.timestamp())
        self.assertEqual((3, 15), (result.remind_at.hour, result.remind_at.minute))

    def test_missing_past_ambiguous_and_unknown_participant_require_followup(self) -> None:
        cases = [
            (parsed(time=None, question="那天幾點？"), "time"),
            (parsed(date="2026-10-21"), "time"),
            (parsed(question="是否只限週一到週五？"), "clarification"),
            (parsed(participant_indices=[], question="請提供小明的 @username。"), "participants"),
        ]
        for data, missing in cases:
            with self.subTest(missing=missing):
                self.assertIn(missing, validate_result(data, NOW, [PERSON]).missing_fields)

    def test_schema_and_value_validation_reject_hallucinated_fields(self) -> None:
        cases = [
            parsed(date="2026-02-30"),
            parsed(time="25:00"),
            parsed(relative_minutes=True),
            parsed(relative_minutes=1),
            parsed(participant_indices=[100]),
            parsed(participant_indices=[True]),
            parsed(user_id=999),
            parsed(title="x" * 1001),
            parsed(recurrence=rule(hour=24)),
            parsed(recurrence=rule(period="weekly", weekdays=[7])),
            parsed(recurrence=rule(period="interval", interval_seconds=60)),
            parsed(recurrence=rule(period="interval", interval_seconds=601)),
            parsed(recurrence=rule(period="yearly", year_month=2, year_day=30)),
        ]
        for data in cases:
            with self.subTest(data=data), self.assertRaises((ValueError, TypeError)):
                validate_result(data, NOW, [PERSON])

    def test_supported_recurrence_uses_shared_next_fire(self) -> None:
        for recurrence in (
            rule(),
            rule(period="weekly", weekdays=[0, 2, 4]),
            rule(period="monthly", month_days=[1, 18, 25]),
            rule(period="yearly", year_month=12, year_day=25),
            rule(period="interval", hour=None, minute=None, interval_seconds=900),
        ):
            with self.subTest(rule=recurrence):
                result = validate_result(
                    parsed(date=None, time=None, recurrence=recurrence), NOW, [PERSON]
                )
                self.assertGreater(result.remind_at, NOW)
                self.assertEqual((), result.missing_fields)

    def test_dst_invalid_or_ambiguous_time_is_not_silently_accepted(self) -> None:
        now = NOW.astimezone(ZoneInfo("America/New_York"))
        for day in ("2027-03-14", "2027-11-07"):
            clock = "02:30" if day == "2027-03-14" else "01:30"
            result = validate_result(parsed(date=day, time=clock), now, [PERSON])
            self.assertIn("time", result.missing_fields)

    def test_recurring_start_checks_dst_without_explicit_date(self) -> None:
        zone = ZoneInfo("America/New_York")
        for now, hour in (
            (datetime(2027, 3, 14, tzinfo=zone), 2),
            (datetime(2027, 11, 7, tzinfo=zone), 1),
        ):
            with self.subTest(now=now):
                result = validate_result(
                    parsed(date=None, time=None, recurrence=rule(hour=hour, minute=30)),
                    now,
                    [PERSON],
                )
                self.assertIn("time", result.missing_fields)
                self.assertIn("日光節約時間", result.raw["question"])

    def test_semantic_parser_converts_bad_output_to_safe_error(self) -> None:
        class Gateway:
            def generate(self, *args):
                return GeneratedResponse(parsed(date="wrong"), "test", "test-mini")

        with self.assertRaises(AiUnavailable):
            SemanticParser(Gateway()).parse("明天下午提醒我", 7, NOW, [PERSON])
