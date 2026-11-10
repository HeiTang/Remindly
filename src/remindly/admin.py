from __future__ import annotations

import json
import os
import secrets
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from authlib.integrations.flask_client import OAuth, OAuthError
from flask import Flask, abort, redirect, render_template, request, session, url_for
from joserfc.errors import JoseError
from requests import RequestException

from remindly.reminders.reports import ATTACHMENT_FIELDS, PARSER_SOURCES, REPORT_REASONS
from remindly.settings import load_dotenv

_ISSUER = "https://oauth.telegram.org"
_PAGE_SIZE = 50
_FILTERS = ("reason", "parser_source", "model", "bot_version")


def error_page(message: str, status: int):
    return render_template("reports.html", screen="error", message=message), status


def create_app() -> Flask:
    load_dotenv()
    client_id = os.getenv("ADMIN_TELEGRAM_CLIENT_ID", "").strip()
    client_secret = os.getenv("ADMIN_TELEGRAM_CLIENT_SECRET", "").strip()
    session_secret = os.getenv("ADMIN_SESSION_SECRET", "").strip()
    redirect_uri = os.getenv("ADMIN_TELEGRAM_REDIRECT_URI", "").strip()
    callback = urlsplit(redirect_uri)
    if (
        not client_id
        or not client_secret
        or len(session_secret) < 32
        or callback.scheme != "https"
        or not callback.hostname
        or callback.username is not None
        or callback.path != "/auth/telegram"
        or callback.query
        or callback.fragment
    ):
        raise RuntimeError("Configure the Telegram login callback and admin secrets first.")

    database = Path(os.getenv("DATABASE_PATH", "data/reminders.db")).resolve().as_uri()
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=session_secret,
        SESSION_COOKIE_NAME="__Host-remindly-admin",
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_REFRESH_EACH_REQUEST=False,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAINTAINER_TELEGRAM_IDS=frozenset(
            int(value)
            for raw in os.getenv("MAINTAINER_TELEGRAM_IDS", "").split(",")
            if (value := raw.strip())
        ),
    )
    oauth = OAuth(app)
    telegram = oauth.register(
        "telegram",
        client_id=client_id,
        client_secret=client_secret,
        authorize_url=f"{_ISSUER}/auth",
        access_token_url=f"{_ISSUER}/token",
        issuer=_ISSUER,
        jwks_uri=f"{_ISSUER}/.well-known/jwks.json",
        id_token_signing_alg_values_supported=["RS256"],
        client_kwargs={
            "scope": "openid profile",
            "code_challenge_method": "S256",
            "token_endpoint_auth_method": "client_secret_basic",
            "default_timeout": 8,
        },
    )

    def can_read(user_id: object) -> bool:
        return type(user_id) is int and user_id in app.config["MAINTAINER_TELEGRAM_IDS"]

    def read_database() -> sqlite3.Connection:
        connection = sqlite3.connect(f"{database}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    @app.before_request
    def require_maintainer():
        if request.endpoint in {None, "login", "start_login", "auth_callback", "static"}:
            return None
        if "telegram_user_id" not in session:
            return redirect(url_for("login"))
        if not can_read(session["telegram_user_id"]):
            session.clear()
            abort(403)
        return None

    @app.after_request
    def protect_response(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self'; script-src 'self'; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @app.errorhandler(sqlite3.OperationalError)
    def database_unavailable(error):
        app.logger.warning("Report database unavailable: %s", type(error).__name__)
        return error_page("回報資料暫時無法讀取。", 503)

    @app.errorhandler(403)
    def access_denied(_error):
        return error_page("此帳號沒有回報查閱權限。", 403)

    @app.get("/login")
    def login():
        return render_template("reports.html", screen="login")

    @app.get("/login/telegram")
    def start_login():
        if not app.config["MAINTAINER_TELEGRAM_IDS"]:
            abort(403)
        session.clear()
        try:
            return telegram.authorize_redirect(redirect_uri)
        except RequestException:
            return error_page("Telegram 登入暫時無法使用。", 502)

    @app.get("/auth/telegram")
    def auth_callback():
        try:
            token = telegram.authorize_access_token(leeway=0)
        except (OAuthError, JoseError, RequestException, ValueError):
            session.clear()
            return error_page("Telegram 登入失敗，請重新登入。", 400)
        user_id = (token.get("userinfo") or {}).get("id")
        session.clear()
        if not can_read(user_id):
            abort(403)
        session.permanent = True
        session["telegram_user_id"] = user_id
        session["logout_token"] = secrets.token_urlsafe(24)
        return redirect(url_for("reports"))

    @app.post("/logout")
    def logout():
        expected = session.get("logout_token", "")
        if not expected or not secrets.compare_digest(
            request.form.get("token", "").encode(), expected.encode()
        ):
            abort(403)
        session.clear()
        return redirect(url_for("login"), code=303)

    @app.get("/")
    def reports():
        filters = {name: request.args.get(name, "") for name in _FILTERS}
        if (
            filters["reason"] not in {"", *REPORT_REASONS}
            or filters["parser_source"] not in {"", *PARSER_SOURCES}
            or any(len(value) > 128 for value in filters.values())
        ):
            abort(400)
        page = request.args.get("page", 1, type=int)
        if not 1 <= page <= 100_000:
            abort(400)
        active = {name: value for name, value in filters.items() if value}
        where = " where " + " and ".join(f"{name} = ?" for name in active) if active else ""
        with closing(read_database()) as connection:
            rows = connection.execute(
                "select draft_id, reported_at, reason, parser_source, provider, model, "
                "bot_version, attachment_json is not null as has_attachment "
                f"from reminder_parse_reports{where} "
                "order by reported_at desc, draft_id desc limit ? offset ?",
                (*active.values(), _PAGE_SIZE + 1, (page - 1) * _PAGE_SIZE),
            ).fetchall()
            models = [
                row[0]
                for row in connection.execute(
                    "select distinct model from reminder_parse_reports "
                    "where model is not null order by model"
                )
            ]
            versions = [
                row[0]
                for row in connection.execute(
                    "select distinct bot_version from reminder_parse_reports order by bot_version"
                )
            ]
        return render_template(
            "reports.html",
            screen="list",
            rows=rows[:_PAGE_SIZE],
            reasons=REPORT_REASONS,
            sources=PARSER_SOURCES,
            models=models,
            versions=versions,
            filters=filters,
            detail_query={**active, **({"page": page} if page > 1 else {})},
            page=page,
            previous=url_for("reports", **active, page=page - 1) if page > 1 else None,
            following=url_for("reports", **active, page=page + 1)
            if len(rows) > _PAGE_SIZE and page < 100_000
            else None,
        )

    @app.get("/reports/<draft_id>")
    def report_detail(draft_id: str):
        with closing(read_database()) as connection:
            row = connection.execute(
                "select * from reminder_parse_reports where draft_id = ?", (draft_id,)
            ).fetchone()
        if row is None:
            abort(404)
        attachment = json.loads(row["attachment_json"]) if row["attachment_json"] else {}
        return render_template(
            "reports.html",
            screen="detail",
            report=row,
            reasons=REPORT_REASONS,
            sources=PARSER_SOURCES,
            list_url=url_for(
                "reports",
                **{
                    name: request.args[name]
                    for name in (*_FILTERS, "page")
                    if request.args.get(name)
                },
            ),
            original_text=attachment.get("source_text"),
            attachment={
                label: attachment[field]
                for field, label in ATTACHMENT_FIELDS.items()
                if field in attachment and field != "source_text"
            },
        )

    return app
