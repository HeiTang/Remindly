from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import unittest
from html import unescape
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

    def test_export_reports_is_authorized_and_omits_reporter_fields(self) -> None:
        path = "/exports/reports.md"
        self.assertEqual(302, self.client.get(path + "?id=draft_ai", base_url=ORIGIN).status_code)
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session["telegram_user_id"] = 123
        with sqlite3.connect(os.environ["DATABASE_PATH"]) as connection:
            connection.execute(
                "update reminder_parse_reports set attachment_json = ? where draft_id = ?",
                (
                    json.dumps(
                        {
                            "source_text": "原句\n```\n# 偽造標題\n```",
                            "title": "測試事項",
                            "timezone": "Asia/Taipei",
                            "parsed_at": "2026-11-10T18:00:00+08:00",
                            "reporter_username": "must-not-export",
                        }
                    ),
                    "draft_ai",
                ),
            )
        export = self.client.get(
            path, query_string={"id": ["draft_rules", "draft_ai", "draft_rules"]}, base_url=ORIGIN
        )
        self.assertEqual(200, export.status_code)
        self.assertEqual("text/markdown; charset=utf-8", export.content_type)
        self.assertIn("-2.md", export.headers["Content-Disposition"])
        self.assertEqual("no-store", export.headers["Cache-Control"])
        self.assertIn("connect-src 'self';", export.headers["Content-Security-Policy"])
        text = export.get_data(as_text=True)
        self.assertLess(text.index("draft_rules"), text.index("draft_ai"))
        self.assertEqual(2, text.count("## 回報 "))
        self.assertIn("Asia/Taipei", text)
        self.assertIn("2026-11-10T18:00:00+08:00", text)
        self.assertIn("2026-11-10T10:00:00+00:00", text)
        self.assertIn("使用者未提供預期結果", text)
        self.assertIn("未提供。", text)
        self.assertIn("````\n原句\n```\n# 偽造標題\n```\n````", text)
        for excluded in ("reporter_demo", "123456789", "must-not-export", "reporter_username"):
            self.assertNotIn(excluded, text)
        for ids, expected in (
            ([], 400),
            ([""], 400),
            (["x" * 129], 400),
            (["draft_ai"] * 51, 400),
            (["draft_ai", "missing"], 404),
            (["' OR 1=1 --"], 404),
        ):
            with self.subTest(ids=ids):
                response = self.client.get(path, query_string={"id": ids}, base_url=ORIGIN)
                self.assertEqual(expected, response.status_code)
                self.assertNotIn("Content-Disposition", response.headers)
        with sqlite3.connect(os.environ["ADMIN_DATABASE_PATH"]) as connection:
            connection.execute("insert into maintainers (user_id, added_by) values (789, 123)")
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session["telegram_user_id"] = 789
        self.assertEqual(200, self.client.get(path + "?id=draft_ai", base_url=ORIGIN).status_code)
        with sqlite3.connect(os.environ["ADMIN_DATABASE_PATH"]) as connection:
            connection.execute("delete from maintainers where user_id = 789")
        self.assertEqual(403, self.client.get(path + "?id=draft_ai", base_url=ORIGIN).status_code)

    def test_expected_result_detail_and_single_batch_markdown(self) -> None:
        expected = "每週六下午三點\n<script>預期內容</script>\n```\n# 假標題"
        with sqlite3.connect(os.environ["DATABASE_PATH"]) as connection:
            connection.execute(
                "update reminder_parse_reports set expected_result=? where draft_id='draft_ai'",
                (expected,),
            )
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session["telegram_user_id"] = 123
        detail = self.client.get("/reports/draft_ai", base_url=ORIGIN).text
        self.assertIn("使用者預期結果", detail)
        self.assertIn("&lt;script&gt;預期內容&lt;/script&gt;", detail)
        self.assertNotIn("<script>預期內容</script>", detail)
        for ids in [["draft_ai"], ["draft_rules", "draft_ai"]]:
            text = self.client.get(
                "/exports/reports.md", query_string={"id": ids}, base_url=ORIGIN
            ).text
            self.assertIn("### 使用者預期結果", text)
            self.assertIn("````\n" + expected + "\n````", text)
            self.assertNotIn("reporter_demo", text)
            self.assertNotIn("123456789", text)
        self.assertIn(
            "使用者未提供預期結果", self.client.get("/reports/draft_rules", base_url=ORIGIN).text
        )

    def test_report_notes_status_filters_and_maintainer_permissions(self) -> None:
        original = hashlib.sha256(Path(os.environ["DATABASE_PATH"]).read_bytes()).hexdigest()
        path = "/reports/draft_ai/manage"
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session["telegram_user_id"] = 123
            session["logout_token"] = "csrf"
        self.assertEqual(
            403, self.client.post(path, data={"status": "resolved"}, base_url=ORIGIN).status_code
        )
        for data in [{"status": "unknown"}, {"status": "open", "note": "字" * 2001}]:
            self.assertEqual(
                400,
                self.client.post(path, data={**data, "token": "csrf"}, base_url=ORIGIN).status_code,
            )
        note = "已修正時區\n</textarea><script>這是資料</script>\n```"
        self.assertEqual(
            303,
            self.client.post(
                path, data={"status": "resolved", "note": note, "token": "csrf"}, base_url=ORIGIN
            ).status_code,
        )
        detail = self.client.get("/reports/draft_ai", base_url=ORIGIN).text
        self.assertIn("&lt;/textarea&gt;&lt;script&gt;這是資料&lt;/script&gt;", detail)
        self.assertNotIn("<script>這是資料</script>", detail)
        listing = self.client.get("/?status=resolved", base_url=ORIGIN).text
        self.assertIn("/reports/draft_ai", listing)
        self.assertNotIn("/reports/draft_rules", listing)
        self.assertEqual(400, self.client.get("/?status=unknown", base_url=ORIGIN).status_code)
        self.assertEqual(400, self.client.get("/?trash=yes", base_url=ORIGIN).status_code)
        exported = self.client.get("/exports/reports.md?id=draft_ai", base_url=ORIGIN).text
        self.assertIn('"處理狀態": "已解決"', exported)
        self.assertIn("````\n" + note + "\n````", exported)
        self.assertNotIn("reporter_demo", exported)
        with sqlite3.connect(os.environ["ADMIN_DATABASE_PATH"]) as connection:
            connection.execute("insert into maintainers(user_id,added_by) values(789,123)")
        client = create_app().test_client()
        with client.session_transaction(base_url=ORIGIN) as session:
            session["telegram_user_id"] = 789
            session["logout_token"] = "maintainer-csrf"
        self.assertIn(
            note.replace("<", "&lt;").replace(">", "&gt;"),
            client.get("/reports/draft_ai", base_url=ORIGIN).text,
        )
        self.assertEqual(
            303,
            client.post(
                path,
                data={"status": "in_progress", "note": "重新檢查", "token": "maintainer-csrf"},
                base_url=ORIGIN,
            ).status_code,
        )
        self.assertEqual(
            404,
            client.post(
                "/reports/missing/manage",
                data={"status": "open", "token": "maintainer-csrf"},
                base_url=ORIGIN,
            ).status_code,
        )
        self.assertEqual(
            403, client.get("/reports/batch?action=delete&id=draft_ai", base_url=ORIGIN).status_code
        )
        self.assertEqual(
            403,
            client.post(
                "/reports/batch",
                data={"action": "delete", "id": "draft_ai", "token": "maintainer-csrf"},
                base_url=ORIGIN,
            ).status_code,
        )
        with sqlite3.connect(os.environ["ADMIN_DATABASE_PATH"]) as connection:
            row = connection.execute(
                "select status,note,updated_by from report_management where draft_id='draft_ai'"
            ).fetchone()
            self.assertEqual(("in_progress", "重新檢查", 789), row)
            connection.execute("delete from maintainers where user_id=789")
        self.assertEqual(
            403,
            client.post(
                path, data={"status": "open", "token": "maintainer-csrf"}, base_url=ORIGIN
            ).status_code,
        )
        self.assertEqual(
            original, hashlib.sha256(Path(os.environ["DATABASE_PATH"]).read_bytes()).hexdigest()
        )

    def test_report_batch_delete_restore_is_confirmed_atomic_and_reversible(self) -> None:
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session["telegram_user_id"] = 123
            session["logout_token"] = "csrf"
        path = "/reports/batch"
        ids = ["draft_ai", "draft_rules"]
        self.client.post(
            "/reports/draft_ai/manage",
            data={"status": "resolved", "note": "保留備註", "token": "csrf"},
            base_url=ORIGIN,
        )
        for selected, action, expected in [
            ([], "delete", 400),
            (["x"] * 51, "delete", 400),
            (["draft_ai", "missing"], "delete", 404),
            (ids, "restore", 409),
            (ids, "purge", 400),
        ]:
            self.assertEqual(
                expected,
                self.client.post(
                    path, data={"id": selected, "action": action, "token": "csrf"}, base_url=ORIGIN
                ).status_code,
            )
        with sqlite3.connect(os.environ["ADMIN_DATABASE_PATH"]) as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "select count(*) from report_management where deleted_at is not null"
                ).fetchone()[0],
            )
        confirm = self.client.get(
            path, query_string={"id": ids, "action": "delete"}, base_url=ORIGIN
        )
        self.assertEqual(200, confirm.status_code)
        self.assertIn("確認刪除", confirm.text)
        self.assertIn("可隨時還原", confirm.text)
        self.assertIn("/reports/draft_ai", self.client.get("/", base_url=ORIGIN).text)
        self.assertEqual(
            403,
            self.client.post(
                path, data={"id": ids, "action": "delete"}, base_url=ORIGIN
            ).status_code,
        )
        self.assertEqual(
            303,
            self.client.post(
                path,
                data={"id": ids + ["draft_ai"], "action": "delete", "token": "csrf"},
                base_url=ORIGIN,
            ).status_code,
        )
        normal = self.client.get("/", base_url=ORIGIN).text
        self.assertNotIn('href="/reports/draft_ai', normal)
        self.assertNotIn('href="/reports/draft_rules', normal)
        trash = self.client.get("/?trash=1&status=resolved", base_url=ORIGIN).text
        self.assertIn("/reports/draft_ai", trash)
        self.assertNotIn('href="/reports/draft_rules', trash)
        self.assertIn("還原回報", trash)
        self.assertNotIn("data-copy-reports", trash)
        self.assertEqual(
            404, self.client.get("/exports/reports.md?id=draft_ai", base_url=ORIGIN).status_code
        )
        self.assertIn("已移到回收區", self.client.get("/reports/draft_ai", base_url=ORIGIN).text)
        self.assertEqual(
            409,
            self.client.post(
                "/reports/draft_ai/manage",
                data={"status": "open", "token": "csrf"},
                base_url=ORIGIN,
            ).status_code,
        )
        self.assertEqual(
            303,
            self.client.post(
                path, data={"id": ids, "action": "restore", "token": "csrf"}, base_url=ORIGIN
            ).status_code,
        )
        self.assertIn(
            'href="/reports/draft_ai', self.client.get("/?status=resolved", base_url=ORIGIN).text
        )
        self.assertIn("保留備註", self.client.get("/reports/draft_ai", base_url=ORIGIN).text)
        with sqlite3.connect(os.environ["DATABASE_PATH"]) as connection:
            self.assertEqual(
                3, connection.execute("select count(*) from reminder_parse_reports").fetchone()[0]
            )

    def test_batch_status_is_atomic_and_available_to_maintainers(self) -> None:
        with sqlite3.connect(os.environ["ADMIN_DATABASE_PATH"]) as connection:
            connection.execute("insert into maintainers(user_id,added_by) values(789,123)")
            connection.execute(
                "insert into report_management(draft_id,note,updated_by) "
                "values('draft_ai','保留備註',123)"
            )
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session["telegram_user_id"] = 789
            session["logout_token"] = "csrf"
        path = "/reports/batch"
        ids = ["draft_ai", "draft_rules"]
        confirm = self.client.get(
            path, query_string={"action": "status", "id": ids}, base_url=ORIGIN
        )
        self.assertEqual(200, confirm.status_code)
        self.assertIn("請選擇狀態", confirm.text)
        self.assertEqual(
            403,
            self.client.post(
                path, data={"action": "status", "id": ids, "status": "resolved"}, base_url=ORIGIN
            ).status_code,
        )
        for selected, status, expected in [
            (ids, "", 400),
            (ids, "unknown", 400),
            (["draft_ai", "missing"], "resolved", 404),
        ]:
            self.assertEqual(
                expected,
                self.client.post(
                    path,
                    data={"action": "status", "id": selected, "status": status, "token": "csrf"},
                    base_url=ORIGIN,
                ).status_code,
            )
        with sqlite3.connect(os.environ["ADMIN_DATABASE_PATH"]) as connection:
            self.assertEqual(
                "open",
                connection.execute(
                    "select status from report_management where draft_id='draft_ai'"
                ).fetchone()[0],
            )
        self.assertEqual(
            303,
            self.client.post(
                path,
                data={"action": "status", "id": ids, "status": "resolved", "token": "csrf"},
                base_url=ORIGIN,
            ).status_code,
        )
        listing = self.client.get("/?status=resolved", base_url=ORIGIN).text
        self.assertIn("/reports/draft_ai", listing)
        self.assertIn("/reports/draft_rules", listing)
        with sqlite3.connect(os.environ["ADMIN_DATABASE_PATH"]) as connection:
            self.assertEqual(
                ("resolved", "保留備註", 789),
                connection.execute(
                    "select status,note,updated_by from report_management where draft_id='draft_ai'"
                ).fetchone(),
            )
            connection.execute(
                "update report_management set deleted_at='2026-11-13' where draft_id='draft_ai'"
            )
        self.assertEqual(
            409,
            self.client.post(
                path,
                data={"action": "status", "id": ids, "status": "open", "token": "csrf"},
                base_url=ORIGIN,
            ).status_code,
        )

    def test_fix_links_validation_upgrade_and_batch_preservation(self) -> None:
        with sqlite3.connect(os.environ["ADMIN_DATABASE_PATH"]) as connection:
            connection.execute("drop table report_management")
            connection.execute(
                "create table report_management (draft_id text primary key, "
                "status text not null default 'open', note text not null default '', "
                "deleted_at text, "
                "updated_by integer not null, updated_at text not null default CURRENT_TIMESTAMP)"
            )
            connection.execute(
                "insert into report_management(draft_id,note,updated_by) "
                "values('draft_ai','舊備註',123)"
            )
        client = create_app().test_client()
        with client.session_transaction(base_url=ORIGIN) as session:
            session["telegram_user_id"] = 123
            session["logout_token"] = "csrf"
        path = "/reports/draft_ai/manage"
        base = {"status": "in_progress", "note": "舊備註", "token": "csrf"}
        for bad in [
            "javascript:alert(1)",
            "https://github.com.evil.test/a/b/pull/1",
            "https://github.com/a/b/issues/1",
            "https://github.com/a/b/pull/1?x=1",
            "http://github.com/a/b/pull/1",
        ]:
            self.assertEqual(
                400, client.post(path, data={**base, "fix_pr": bad}, base_url=ORIGIN).status_code
            )
        self.assertEqual(
            400,
            client.post(path, data={**base, "fix_version": "x" * 81}, base_url=ORIGIN).status_code,
        )
        pr = "https://github.com/HeiTang/Remindly/pull/123"
        self.assertEqual(
            303,
            client.post(
                path, data={**base, "fix_pr": pr, "fix_version": "v0.6.1"}, base_url=ORIGIN
            ).status_code,
        )
        detail = client.get("/reports/draft_ai", base_url=ORIGIN).text
        self.assertIn('href="' + pr + '"', detail)
        self.assertIn('value="v0.6.1"', detail)
        for action, extra in [("status", {"status": "resolved"}), ("delete", {}), ("restore", {})]:
            self.assertEqual(
                303,
                client.post(
                    "/reports/batch",
                    data={"action": action, "id": "draft_ai", "token": "csrf", **extra},
                    base_url=ORIGIN,
                ).status_code,
            )
        exported = client.get("/exports/reports.md?id=draft_ai", base_url=ORIGIN).text
        self.assertIn(pr, exported)
        self.assertIn('"修復版本": "v0.6.1"', exported)
        self.assertIn("舊備註", exported)
        self.assertNotIn("reporter_demo", exported)
        with sqlite3.connect(os.environ["ADMIN_DATABASE_PATH"]) as connection:
            self.assertEqual(
                (pr, "v0.6.1", "resolved"),
                connection.execute(
                    "select fix_pr,fix_version,status from report_management "
                    "where draft_id='draft_ai'"
                ).fetchone(),
            )
        self.assertEqual(303, client.post(path, data=base, base_url=ORIGIN).status_code)
        with sqlite3.connect(os.environ["ADMIN_DATABASE_PATH"]) as connection:
            self.assertEqual(
                ("", ""),
                connection.execute(
                    "select fix_pr,fix_version from report_management where draft_id='draft_ai'"
                ).fetchone(),
            )

    def test_content_search_is_literal_and_preserves_filters_and_pagination(self) -> None:
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session["telegram_user_id"] = 123
        with sqlite3.connect(os.environ["DATABASE_PATH"]) as connection:
            connection.execute(
                "update reminder_parse_reports set attachment_json=?, expected_result=? "
                "where draft_id='draft_ai'",
                (
                    json.dumps({"source_text": "Test %_ 原句文字", "title": "解析事項"}),
                    "每週六下午三點",
                ),
            )
        with sqlite3.connect(os.environ["ADMIN_DATABASE_PATH"]) as connection:
            connection.execute(
                "insert into report_management(draft_id,status,note,updated_by) "
                "values('draft_ai','resolved','修復時區問題',123)"
            )
        for query in ["test", "%_", "原句文字", "解析事項", "每週六", "時區問題"]:
            with self.subTest(query=query):
                page = self.client.get(
                    "/", query_string={"q": query, "status": "resolved"}, base_url=ORIGIN
                )
                self.assertEqual(200, page.status_code)
                self.assertIn("/reports/draft_ai", page.text)
                self.assertNotIn("/reports/draft_rules", page.text)
                self.assertIn("q=", page.text)
        for query in ["source_text", "' OR 1=1 --"]:
            self.assertNotIn(
                'href="/reports/draft_ai',
                self.client.get("/", query_string={"q": query}, base_url=ORIGIN).text,
            )
        escaped = self.client.get("/", query_string={"q": "<script>"}, base_url=ORIGIN).text
        self.assertIn('value="&lt;script&gt;"', escaped)
        self.assertNotIn('value="<script>"', escaped)
        self.assertEqual(
            400, self.client.get("/", query_string={"q": "字" * 201}, base_url=ORIGIN).status_code
        )
        detail = self.client.get(
            "/reports/draft_ai",
            query_string={"q": "時區問題", "status": "resolved"},
            base_url=ORIGIN,
        ).text
        self.assertIn("q=", detail)
        self.assertIn("status=resolved", detail)
        with sqlite3.connect(os.environ["ADMIN_DATABASE_PATH"]) as connection:
            connection.execute(
                "update report_management set deleted_at='2026-11-13' where draft_id='draft_ai'"
            )
        self.assertNotIn(
            'href="/reports/draft_ai',
            self.client.get("/", query_string={"q": "時區問題"}, base_url=ORIGIN).text,
        )
        self.assertIn(
            "/reports/draft_ai",
            self.client.get(
                "/", query_string={"q": "時區問題", "trash": "1"}, base_url=ORIGIN
            ).text,
        )
        with sqlite3.connect(os.environ["DATABASE_PATH"]) as connection:
            connection.executemany(
                "insert into reminder_parse_reports "
                "(draft_id,reported_at,reason,parser_source,bot_version,expected_result) "
                "values(?, '2026-11-13', 'time', 'rules', '0.6.0', '分頁關鍵字')",
                [(f"page_{index}",) for index in range(55)],
            )
        page = self.client.get("/", query_string={"q": "分頁關鍵字"}, base_url=ORIGIN)
        next_url = unescape(re.search(r'href="([^"\n]+)">下一頁', page.text).group(1))
        self.assertEqual(["分頁關鍵字"], parse_qs(urlsplit(next_url).query)["q"])
        second = self.client.get(next_url, base_url=ORIGIN)
        self.assertIn("本頁 5 筆", second.text)

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
