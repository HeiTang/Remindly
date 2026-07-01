from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Settings:
    token: str
    bot_username: str | None
    database_path: Path
    default_timezone: str
    poll_timeout_seconds: int
    scheduler_interval_seconds: int
    draft_ttl_minutes: int
    confirming_ttl_minutes: int
    log_level: str

    @property
    def default_zone(self) -> ZoneInfo:
        return ZoneInfo(self.default_timezone)


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_settings() -> Settings:
    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN. Copy .env.example to .env and fill it in.")

    bot_username = os.getenv("BOT_USERNAME", "").strip().lstrip("@") or None

    return Settings(
        token=token,
        bot_username=bot_username,
        database_path=Path(os.getenv("DATABASE_PATH", "data/reminders.db")),
        default_timezone=os.getenv("DEFAULT_TIMEZONE", "Asia/Taipei"),
        poll_timeout_seconds=int(os.getenv("POLL_TIMEOUT_SECONDS", "25")),
        scheduler_interval_seconds=int(os.getenv("SCHEDULER_INTERVAL_SECONDS", "10")),
        draft_ttl_minutes=int(os.getenv("DRAFT_TTL_MINUTES", "10")),
        confirming_ttl_minutes=int(os.getenv("CONFIRMING_TTL_MINUTES", "15")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )
