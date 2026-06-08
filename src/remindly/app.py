from __future__ import annotations

import logging
import signal
from types import FrameType

from remindly.bot.router import BotRouter
from remindly.reminders.parser import ReminderParser
from remindly.reminders.renderer import ReminderRenderer
from remindly.reminders.scheduler import ReminderScheduler, start_scheduler_thread
from remindly.reminders.service import ReminderService
from remindly.settings import load_settings
from remindly.storage.session_stores import SqliteDraftStore, SqliteEditSessionStore
from remindly.storage.sqlite import ReminderRepository
from remindly.telegram.client import BotCommand, TelegramClient

LOGGER = logging.getLogger(__name__)


COMMANDS = [
    BotCommand("start", "開始使用"),
    BotCommand("help", "查看說明"),
    BotCommand("remind", "建立提醒"),
    BotCommand("list", "列出提醒"),
    BotCommand("cancel", "取消提醒"),
    BotCommand("timezone", "設定時區"),
    BotCommand("groupmode", "群組自然語言模式"),
]


def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        level=settings.log_level,
    )

    repository = ReminderRepository(settings.database_path)
    repository.migrate()

    client = TelegramClient(settings.token)
    renderer = ReminderRenderer()
    parser = ReminderParser(settings.default_timezone)
    draft_store = SqliteDraftStore(settings.draft_ttl_minutes, repository)
    edit_store = SqliteEditSessionStore(settings.draft_ttl_minutes, repository)
    reminder_service = ReminderService(
        repository=repository,
        parser=parser,
        draft_store=draft_store,
        edit_store=edit_store,
        default_timezone=settings.default_timezone,
    )
    router = BotRouter(
        client=client,
        reminder_service=reminder_service,
        renderer=renderer,
        bot_username=settings.bot_username,
        default_timezone=settings.default_timezone,
    )
    scheduler = ReminderScheduler(
        repository=repository,
        client=client,
        renderer=renderer,
        timezone=settings.default_timezone,
        interval_seconds=settings.scheduler_interval_seconds,
    )

    client.delete_webhook()
    client.set_my_commands(COMMANDS)
    scheduler_thread, stop_event = start_scheduler_thread(scheduler)

    def stop(_signum: int, _frame: FrameType | None) -> None:
        LOGGER.info("Stopping bot")
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    LOGGER.info("Starting long polling")
    offset: int | None = None
    try:
        while not stop_event.is_set():
            updates = client.get_updates(offset, settings.poll_timeout_seconds)
            for update in updates:
                offset = update.id + 1
                try:
                    router.handle_update(update)
                except Exception:
                    LOGGER.exception("Failed to handle update %s", update.id)
    finally:
        stop_event.set()
        scheduler_thread.join(timeout=5)
