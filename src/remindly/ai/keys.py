from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from cryptography.fernet import Fernet, InvalidToken

from remindly.ai.contracts import AiUnavailable
from remindly.storage.sqlite import ReminderRepository


@dataclass(frozen=True)
class ApiKey:
    id: str
    provider: str
    fingerprint: str
    position: int
    status: str
    cooldown_until: str | None
    encrypted: bytes = field(repr=False)
    last_error: str | None = None


class ApiKeyStore:
    def __init__(self, repository: ReminderRepository, encryption_key: str) -> None:
        self._repository = repository
        self._cipher = Fernet(encryption_key.encode()) if encryption_key else None

    @property
    def configured(self) -> bool:
        return self._cipher is not None

    def enabled(self, user_id: int) -> bool:
        if not self.configured:
            return False
        with self._repository.connect() as connection:
            row = connection.execute(
                "select enabled from ai_settings where user_id = ?", (user_id,)
            ).fetchone()
            has_key = connection.execute(
                "select 1 from ai_keys where user_id = ? limit 1", (user_id,)
            ).fetchone()
        return bool(row and row["enabled"] and has_key)

    def set_enabled(self, user_id: int, enabled: bool) -> None:
        if enabled and (not self.configured or not self.list(user_id)):
            raise ValueError("請先設定至少一組 API Key。")
        with self._repository.connect() as connection:
            connection.execute(
                """insert into ai_settings(user_id, enabled) values (?, ?)
                on conflict(user_id) do update set enabled = excluded.enabled""",
                (user_id, int(enabled)),
            )

    def list(self, user_id: int) -> list[ApiKey]:
        with self._repository.connect() as connection:
            rows = connection.execute(
                "select * from ai_keys where user_id = ? order by position, id", (user_id,)
            ).fetchall()
        return [
            ApiKey(
                id=row["id"],
                provider=row["provider"],
                fingerprint=row["fingerprint"],
                position=row["position"],
                status=row["status"],
                cooldown_until=row["cooldown_until"],
                encrypted=bytes(row["encrypted_key"]),
                last_error=row["last_error"],
            )
            for row in rows
        ]

    def add(self, user_id: int, key: str, *, provider: str) -> str:
        if self._cipher is None:
            raise ValueError("伺服器尚未設定 AI_ENCRYPTION_KEY，暫時無法保存 Key。")
        if not 1 <= len(key) <= 4096 or any(not 33 <= ord(c) <= 126 for c in key):
            raise ValueError("API Key 格式不正確，不可包含空白或控制字元。")
        fingerprint = hashlib.sha256(key.encode()).hexdigest()[:12]
        key_id = secrets.token_hex(8)
        with self._repository.connect() as connection:
            rows = connection.execute(
                "select fingerprint, position from ai_keys where user_id = ?", (user_id,)
            ).fetchall()
            if any(row["fingerprint"] == fingerprint for row in rows):
                raise ValueError("這組 Key 已經設定過了。")
            if len(rows) >= 10:
                raise ValueError("最多保存 10 組 Key，請先刪除不使用的 Key。")
            position = max((row["position"] for row in rows), default=0) + 1
            connection.execute(
                """insert into ai_keys
                (id, user_id, encrypted_key, fingerprint, position, status, provider)
                values (?, ?, ?, ?, ?, 'unchecked', ?)""",
                (
                    key_id,
                    user_id,
                    self._cipher.encrypt(key.encode()),
                    fingerprint,
                    position,
                    provider,
                ),
            )
            connection.execute(
                """insert into ai_settings(user_id, enabled) values (?, 1)
                on conflict(user_id) do nothing""",
                (user_id,),
            )
        return key_id

    def delete(self, user_id: int, key_id: str) -> bool:
        with self._repository.connect() as connection:
            result = connection.execute(
                "delete from ai_keys where user_id = ? and id = ?", (user_id, key_id)
            )
        return result.rowcount == 1

    def move(self, user_id: int, key_id: str, position: int) -> None:
        keys = self.list(user_id)
        selected = next((key for key in keys if key.id == key_id), None)
        if selected is None or not 1 <= position <= len(keys):
            raise ValueError("Key ID 或順位不正確。")
        keys.remove(selected)
        keys.insert(position - 1, selected)
        with self._repository.connect() as connection:
            connection.executemany(
                "update ai_keys set position = ? where user_id = ? and id = ?",
                [(index, user_id, key.id) for index, key in enumerate(keys, 1)],
            )

    def decrypt(self, key: ApiKey) -> str:
        if self._cipher is None:
            raise AiUnavailable("Bot 伺服器尚未設定 AI 憑證加密，請聯絡 Bot 維運者。")
        try:
            return self._cipher.decrypt(key.encrypted).decode()
        except InvalidToken:
            raise AiUnavailable("AI 憑證無法解密，請確認加密設定或重新新增 Key。") from None

    def mark(
        self,
        user_id: int,
        key_id: str,
        status: str,
        seconds: int = 0,
        *,
        last_error: str | None = None,
    ) -> None:
        until = (datetime.now().astimezone() + timedelta(seconds=seconds)).isoformat()
        with self._repository.connect() as connection:
            connection.execute(
                """update ai_keys set status = ?, cooldown_until = ?, last_error = ?
                where user_id = ? and id = ?""",
                (status, until if seconds else None, last_error, user_id, key_id),
            )

    def available(self, user_id: int) -> list[ApiKey]:
        now = datetime.now().astimezone()
        return [
            key
            for key in self.list(user_id)
            if key.status != "invalid"
            and (key.cooldown_until is None or datetime.fromisoformat(key.cooldown_until) <= now)
        ]
