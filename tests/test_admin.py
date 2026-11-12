from __future__ import annotations

import json
import os
import sqlite3
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
                    "ADMIN_TELEGRAM_IDS": "123, 456",
                    "ADMIN_DATABASE_PATH": str(directory / "access.db"),
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
        self.app.config["ADMIN_TELEGRAM_IDS"] = frozenset({456})
        self.assertEqual(403, self.client.get("/reports/draft_ai", base_url=ORIGIN).status_code)
        self.assertEqual(302, self.client.get("/", base_url=ORIGIN).status_code)

    def test_manage_maintainers_and_revoke_active_session(self) -> None:
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session["telegram_user_id"] = 123
            session["logout_token"] = "csrf"
        self.assertIn("人員權限", self.client.get("/maintainers", base_url=ORIGIN).text)
        self.assertEqual(
            403,
            self.client.post("/maintainers", data={"user_id": "789"}, base_url=ORIGIN).status_code,
        )
        for value in ("", "@someone", "0", "-1", "1.5", "１２３", str(2**63), "123"):
            self.assertEqual(
                400,
                self.client.post(
                    "/maintainers", data={"user_id": value, "token": "csrf"}, base_url=ORIGIN
                ).status_code,
            )
        for _ in range(2):
            self.assertEqual(
                303,
                self.client.post(
                    "/maintainers", data={"user_id": "789", "token": "csrf"}, base_url=ORIGIN
                ).status_code,
            )
        restarted_app = create_app()
        restarted_app.testing = True
        viewer = restarted_app.test_client()
        with viewer.session_transaction(base_url=ORIGIN) as session:
            session["telegram_user_id"] = 789
            session["logout_token"] = "viewer-csrf"
        self.assertEqual(200, viewer.get("/", base_url=ORIGIN).status_code)
        self.assertNotIn('href="/maintainers"', viewer.get("/", base_url=ORIGIN).text)
        self.assertEqual(403, viewer.get("/maintainers", base_url=ORIGIN).status_code)
        self.assertEqual(
            403,
            viewer.post(
                "/maintainers", data={"user_id": "999", "token": "viewer-csrf"}, base_url=ORIGIN
            ).status_code,
        )
        self.assertEqual(
            403,
            viewer.post(
                "/maintainers/789/remove", data={"token": "viewer-csrf"}, base_url=ORIGIN
            ).status_code,
        )
        self.assertEqual(
            403, self.client.post("/maintainers/789/remove", base_url=ORIGIN).status_code
        )
        self.assertEqual(
            400,
            self.client.post(
                "/maintainers/123/remove", data={"token": "csrf"}, base_url=ORIGIN
            ).status_code,
        )
        self.assertEqual(
            303,
            self.client.post(
                "/maintainers/789/remove", data={"token": "csrf"}, base_url=ORIGIN
            ).status_code,
        )
        self.assertEqual(403, viewer.get("/reports/draft_ai", base_url=ORIGIN).status_code)
        self.assertEqual(302, viewer.get("/", base_url=ORIGIN).status_code)
        self.assertEqual(200, self.client.get("/", base_url=ORIGIN).status_code)

    def test_member_notes_profiles_and_existing_database_upgrade(self) -> None:
        access_path = os.environ["ADMIN_DATABASE_PATH"]
        with sqlite3.connect(access_path) as connection:
            connection.execute("drop table maintainers")
            connection.execute(
                "create table maintainers (user_id integer primary key, "
                "added_by integer not null, added_at text not null default CURRENT_TIMESTAMP)"
            )
            connection.execute("insert into maintainers (user_id, added_by) values (789, 123)")
        upgraded = create_app()
        upgraded.testing = True
        client = upgraded.test_client()
        with client.session_transaction(base_url=ORIGIN) as session:
            session["telegram_user_id"] = 123
            session["logout_token"] = "csrf"
        self.assertIn("未命名帳號", client.get("/maintainers", base_url=ORIGIN).text)
        note = "<script>小橘</script>"
        self.assertEqual(
            403,
            client.post("/maintainers/789/note", data={"note": note}, base_url=ORIGIN).status_code,
        )
        self.assertEqual(
            400,
            client.post(
                "/maintainers/789/note", data={"note": "x" * 81, "token": "csrf"}, base_url=ORIGIN
            ).status_code,
        )
        self.assertEqual(
            303,
            client.post(
                "/maintainers/789/note", data={"note": note, "token": "csrf"}, base_url=ORIGIN
            ).status_code,
        )
        page = client.get("/maintainers", base_url=ORIGIN).text
        self.assertIn("&lt;script&gt;小橘&lt;/script&gt;", page)
        self.assertNotIn(note, page)
        self.assertNotIn("尚未登入", page)
        self.assertNotIn("曾登入", page)
        self.assertIn('class="identity-pill">ID <code>789</code>', page)
        with sqlite3.connect(access_path) as connection:
            connection.execute(
                "insert into user_profiles (user_id, display_name, username) "
                "values (789, 'Telegram 名稱', 'orange')"
            )
        self.assertIn("Telegram 名稱", client.get("/maintainers", base_url=ORIGIN).text)
        self.assertIn("@orange", client.get("/maintainers", base_url=ORIGIN).text)
        self.assertEqual(
            303,
            client.post(
                "/maintainers",
                data={"user_id": "890", "note": "共同維護者", "token": "csrf"},
                base_url=ORIGIN,
            ).status_code,
        )
        self.assertIn("共同維護者", client.get("/maintainers", base_url=ORIGIN).text)
        with client.session_transaction(base_url=ORIGIN) as session:
            session["telegram_user_id"] = 789
        self.assertEqual(
            403,
            client.post(
                "/maintainers/890/note", data={"note": "越權", "token": "csrf"}, base_url=ORIGIN
            ).status_code,
        )

    def test_oidc_validates_signed_claims_and_applies_allowlist(self) -> None:
        key = RSAKey.generate_key(parameters={"kid": "test"})
        for user_id, audience, wrong_nonce, expected, name, username in (
            (123, "987", False, 302, "小橘", "orange"),
            ("123", "987", False, 302, "小橘", "orange"),
            (123, "987", False, 302, "小橘改名", None),
            (999, "987", False, 403, "未授權", "denied"),
            ("999", "987", False, 403, "未授權", "denied"),
            ("+123", "987", False, 403, "格式錯誤", "denied"),
            ("123.0", "987", False, 403, "格式錯誤", "denied"),
            ("１２３", "987", False, 403, "格式錯誤", "denied"),
            (str(2**63), "987", False, 403, "超出範圍", "denied"),
            ("1" * 100, "987", False, 403, "超出範圍", "denied"),
            (True, "987", False, 403, "格式錯誤", "denied"),
            (None, "987", False, 403, "缺少 ID", "denied"),
            (123, "wrong-audience", False, 400, "不可覆寫", "bad"),
            (123, "987", True, 400, "不可覆寫", "bad"),
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
                        "name": name,
                        "preferred_username": username,
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
                with sqlite3.connect(os.environ["ADMIN_DATABASE_PATH"]) as connection:
                    profile = connection.execute(
                        "select display_name, username from user_profiles where user_id = ?",
                        (user_id,),
                    ).fetchone()
                    profiles = connection.execute(
                        "select user_id, display_name, username from user_profiles"
                    ).fetchall()
                if expected == 302:
                    self.assertEqual((name, username), profile)
                    page = self.client.get("/maintainers", base_url=ORIGIN).text
                    self.assertIn(name, page)
                    if username:
                        self.assertIn("@" + username, page)
                    else:
                        self.assertNotIn("@orange", page)
                else:
                    self.assertEqual([(123, "小橘改名", None)], profiles)

        self.assertEqual(
            400,
            self.client.get("/auth/telegram?code=simulated&state=bad", base_url=ORIGIN).status_code,
        )
