from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


INITIAL_SCHEMA_SQL = """
create table if not exists reminders (
    id text primary key,
    short_id text not null unique,
    chat_id integer not null,
    chat_type text not null,
    creator_user_id integer not null,
    title text not null,
    remind_at text not null,
    timezone text not null,
    status text not null,
    source_text text not null,
    parse_result text not null,
    created_at text not null,
    updated_at text not null,
    fired_at text
);

create index if not exists idx_reminders_due
    on reminders(status, remind_at);

create index if not exists idx_reminders_chat_status
    on reminders(chat_id, status, remind_at);

create table if not exists reminder_participants (
    id text primary key,
    reminder_id text not null,
    user_id integer,
    username text,
    display_name text not null,
    mention_kind text not null,
    foreign key(reminder_id) references reminders(id)
);

create index if not exists idx_reminder_participants_reminder
    on reminder_participants(reminder_id);

create table if not exists users (
    id integer primary key,
    username text,
    first_name text,
    updated_at text not null
);

create table if not exists chats (
    id integer primary key,
    type text not null,
    title text,
    username text,
    updated_at text not null
);

create table if not exists user_settings (
    user_id integer primary key,
    timezone text not null,
    locale text not null default 'zh-TW',
    created_at text not null,
    updated_at text not null
);

create table if not exists reminder_drafts (
    id text primary key,
    chat_id integer not null,
    chat_type text not null,
    creator_user_id integer not null,
    timezone text not null,
    source_text text not null,
    title text,
    remind_at text,
    participants text not null,
    missing_fields text not null,
    parse_result text not null,
    expires_at text not null,
    unique(chat_id, creator_user_id)
);

create index if not exists idx_reminder_drafts_context
    on reminder_drafts(chat_id, creator_user_id);

create index if not exists idx_reminder_drafts_expires
    on reminder_drafts(expires_at);

create table if not exists edit_sessions (
    chat_id integer not null,
    user_id integer not null,
    short_id text not null,
    field text not null,
    expires_at text not null,
    primary key(chat_id, user_id)
);

create index if not exists idx_edit_sessions_expires
    on edit_sessions(expires_at);
"""

CHAT_SETTINGS_SQL = """
create table if not exists chat_settings (
    chat_id integer primary key,
    natural_language_enabled integer not null default 0,
    updated_by_user_id integer,
    created_at text not null,
    updated_at text not null
);
"""

PROMPT_MESSAGE_ID_SQL = """
alter table reminder_drafts add column prompt_message_id integer;
alter table edit_sessions add column prompt_message_id integer;
"""

RECURRENCE_SQL = """
alter table reminders add column recurrence text;
"""

DRAFT_RECURRENCE_SQL = """
alter table reminder_drafts add column recurrence text;
"""

MIGRATIONS = (
    Migration(version=1, name="initial_schema", sql=INITIAL_SCHEMA_SQL),
    Migration(version=2, name="chat_settings", sql=CHAT_SETTINGS_SQL),
    Migration(version=3, name="prompt_message_id", sql=PROMPT_MESSAGE_ID_SQL),
    Migration(version=4, name="recurrence", sql=RECURRENCE_SQL),
    Migration(version=5, name="draft_recurrence", sql=DRAFT_RECURRENCE_SQL),
    Migration(
        version=6,
        name="personal_ai_keys",
        sql="""
        create table ai_settings (
            user_id integer primary key,
            enabled integer not null default 1
        );
        create table ai_keys (
            id text primary key,
            user_id integer not null,
            encrypted_key blob not null,
            fingerprint text not null,
            position integer not null,
            status text not null default 'ready',
            cooldown_until text,
            unique(user_id, fingerprint)
        );
        create index idx_ai_keys_user on ai_keys(user_id, position);
    """,
    ),
    Migration(
        version=7,
        name="ai_key_providers",
        sql="""
        alter table ai_keys add column provider text not null default 'gemini';
    """,
    ),
    Migration(
        version=8,
        name="ai_edit_context",
        sql="alter table edit_sessions add column ai_context text;",
    ),
    Migration(
        version=9,
        name="ai_key_last_error",
        sql="alter table ai_keys add column last_error text;",
    ),
)


def migrate_sqlite_database(connection: sqlite3.Connection) -> None:
    """依序套用尚未執行的 SQLite schema migrations。"""
    ensure_migration_table(connection)
    applied_versions = load_applied_versions(connection)
    for migration in MIGRATIONS:
        if migration.version in applied_versions:
            continue
        apply_migration(connection, migration)


def ensure_migration_table(connection: sqlite3.Connection) -> None:
    """建立 migration ledger，用來記錄哪些 schema migration 已執行。"""
    connection.execute(
        """
        create table if not exists schema_migrations (
            version integer primary key,
            name text not null,
            applied_at text not null
        )
        """
    )


def load_applied_versions(connection: sqlite3.Connection) -> set[int]:
    """讀取已套用的 migration version，避免重複執行同一版。"""
    rows = connection.execute("select version from schema_migrations").fetchall()
    return {int(row["version"] if isinstance(row, sqlite3.Row) else row[0]) for row in rows}


def apply_migration(connection: sqlite3.Connection, migration: Migration) -> None:
    """Schema 與 ledger 同時成功或回滾，且不隱式提交呼叫端的交易。"""
    connection.execute("savepoint schema_migration")
    try:
        statement = ""
        # executescript 會先 commit，改用 SQLite 自己判斷 statement 邊界。
        for char in migration.sql:
            statement += char
            if char == ";" and sqlite3.complete_statement(statement):
                connection.execute(statement)
                statement = ""
        if statement.strip():
            connection.execute(statement)
        connection.execute(
            """insert into schema_migrations (version, name, applied_at)
            values (?, ?, ?)""",
            (migration.version, migration.name, datetime.now().astimezone().isoformat()),
        )
    except Exception:
        connection.execute("rollback to schema_migration")
        raise
    finally:
        connection.execute("release schema_migration")


def latest_schema_version() -> int:
    """回傳程式碼目前支援的最新 schema version。"""
    return MIGRATIONS[-1].version if MIGRATIONS else 0
