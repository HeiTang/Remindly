from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from joserfc import jwt
from joserfc.jwk import RSAKey
from requests import Response

from remindly.admin import create_app
from remindly.storage.sqlite import ReminderRepository

ORIGIN = "https://reports.example.test"


class AdminTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        repository = ReminderRepository(directory / "reports.db")
        repository.migrate()
        with repository.connect() as connection:
            connection.executemany(
                """insert into reminder_parse_reports
                (draft_id, reported_at, reason, parser_source,
                 provider, model, bot_version, attachment_json, reporter_user_id, reporter_username)
                values (?, '2026-11-10T10:00:00+00:00', ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        "draft_ai",
                        "time",
                        "ai",
                        "groq",
                        "test-model",
                        "1.0",
                        json.dumps({"source_text": "<script>alert(1)</script>"}),
                        123456789,
                        "reporter_demo",
                    ),
                    ("draft_rules", "title", "rules", None, None, "2.0", None, None, None),
                    ("draft_id_only", "other", "rules", None, None, "2.0", None, 123456790, None),
                ],
            )
        self.enterContext(patch("remindly.admin.load_dotenv"))
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "DATABASE_PATH": str(directory / "reports.db"),
                    "MAINTAINER_TELEGRAM_IDS": "123, 456",
                    "ADMIN_TELEGRAM_CLIENT_ID": "987",
                    "ADMIN_TELEGRAM_CLIENT_SECRET": "test-secret",
                    "ADMIN_TELEGRAM_REDIRECT_URI": f"{ORIGIN}/auth/telegram",
                    "ADMIN_SESSION_SECRET": "s" * 40,
                },
                clear=True,
            )
        )
        self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()

    def test_authenticated_list_filters_detail_and_revocation(self) -> None:
        self.assertEqual(302, self.client.get("/reports/draft_ai", base_url=ORIGIN).status_code)
        self.assertEqual(200, self.client.get("/login", base_url=ORIGIN).status_code)
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session["telegram_user_id"] = 123
        listing = self.client.get(
            "/?reason=time&parser_source=ai&model=test-model&bot_version=1.0", base_url=ORIGIN
        )
        self.assertEqual(200, listing.status_code)
        self.assertIn(b"/reports/draft_ai", listing.data)
        self.assertNotIn(b"/reports/draft_rules", listing.data)
        self.assertNotIn(b"alert(1)", listing.data)
        self.assertNotIn(b"reporter_demo", listing.data)
        detail = self.client.get("/reports/draft_ai?reason=time&model=test-model", base_url=ORIGIN)
        self.assertIn(b'href="/?reason=time&amp;model=test-model"', detail.data)
        self.assertIn(b"&lt;script&gt;alert(1)&lt;/script&gt;", detail.data)
        self.assertIn(b"<code>123456789</code>", detail.data)
        self.assertIn(b"<code>@reporter_demo</code>", detail.data)
        legacy = self.client.get("/reports/draft_rules", base_url=ORIGIN)
        self.assertIn("這筆回報未記錄回報者。", legacy.get_data(as_text=True))
        id_only = self.client.get("/reports/draft_id_only", base_url=ORIGIN)
        self.assertIn("未設定", id_only.get_data(as_text=True))
        self.assertEqual("no-store", detail.headers["Cache-Control"])
        self.assertEqual(404, self.client.get("/reports/missing", base_url=ORIGIN).status_code)
        self.app.config["MAINTAINER_TELEGRAM_IDS"] = frozenset({456})
        self.assertEqual(403, self.client.get("/reports/draft_ai", base_url=ORIGIN).status_code)
        self.assertEqual(302, self.client.get("/", base_url=ORIGIN).status_code)

    def test_oidc_validates_signed_claims_and_applies_allowlist(self) -> None:
        key = RSAKey.generate_key(parameters={"kid": "test"})
        for user_id, audience, wrong_nonce, expected in (
            (123, "987", False, 302),
            (999, "987", False, 403),
            (123, "wrong-audience", False, 400),
            (123, "987", True, 400),
        ):
            with self.subTest(user_id=user_id, audience=audience, wrong_nonce=wrong_nonce):
                start = self.client.get("/login/telegram", base_url=ORIGIN)
                params = parse_qs(urlsplit(start.location).query)
                self.assertEqual(["S256"], params["code_challenge_method"])
                self.assertEqual(["openid profile"], params["scope"])
                now = int(time.time())
                id_token = jwt.encode(
                    {"alg": "RS256", "kid": "test"},
                    {
                        "iss": "https://oauth.telegram.org",
                        "sub": "opaque-oidc-subject",
                        "aud": audience,
                        "iat": now,
                        "exp": now + 300,
                        "nonce": "wrong" if wrong_nonce else params["nonce"][0],
                        "id": user_id,
                    },
                    key,
                )
                with patch("authlib.integrations.requests_client.OAuth2Session.send") as send:

                    def fake_telegram(request, *, signed_token=id_token, **_kwargs):
                        response = Response()
                        response.status_code = 200
                        response.url = request.url
                        if request.url.endswith("/token"):
                            self.assertIn("code_verifier=", request.body)
                            data = {
                                "id_token": signed_token,
                                "access_token": "simulated",
                                "token_type": "Bearer",
                            }
                        else:
                            self.assertTrue(request.url.endswith("/.well-known/jwks.json"))
                            data = {"keys": [key.as_dict()]}
                        response._content = json.dumps(data).encode()
                        return response

                    send.side_effect = fake_telegram
                    callback = self.client.get(
                        "/auth/telegram",
                        query_string={"code": "simulated", "state": params["state"][0]},
                        base_url=ORIGIN,
                    )
                self.assertEqual(expected, callback.status_code)
                with self.client.session_transaction(base_url=ORIGIN) as session:
                    self.assertEqual(
                        123 if expected == 302 else None, session.get("telegram_user_id")
                    )
                    self.assertNotIn("id_token", session)

        self.assertEqual(
            400,
            self.client.get("/auth/telegram?code=simulated&state=bad", base_url=ORIGIN).status_code,
        )
