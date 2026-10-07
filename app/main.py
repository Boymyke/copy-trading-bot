"""Entry point: ``python -m app.main`` (Railway start command)."""

from __future__ import annotations

import asyncio
import signal
from typing import Awaitable, Callable

from . import __version__
from .config import load_settings
from .controller import Controller
from .logging_setup import configure_logging, get_logger
from .notifier import Notifier
from .storage import Store
from .telegram import TelegramBot
from .web import WebServer

log = get_logger("main")


async def supervise(name: str, factory: Callable[[], Awaitable[None]]) -> None:
    """Run a long-lived loop forever, restarting it with backoff if it ever exits."""
    delay = 2.0
    while True:
        try:
            log.info("task_start", task=name)
            await factory()
            log.warning("task_exited", task=name, restart_in=delay)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("task_crashed", task=name, error=str(exc), restart_in=delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60.0)


async def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    log.info("startup", version=__version__, settings=settings.redacted())

    missing = [
        name
        for name, value in (
            ("METAAPI_TOKEN", settings.metaapi_token),
            ("METAAPI_SOURCE_ACCOUNT_ID", settings.source_account_id),
            ("METAAPI_TARGET_ACCOUNT_ID", settings.target_account_id),
        )
        if not value
    ]
    if missing:
        # Stay alive (health stays green) but make the problem impossible to miss.
        while True:
            log.error("missing_configuration", missing=missing)
            await asyncio.sleep(60)

    store = Store(settings.data_dir)
    notifier = Notifier()
    controller = Controller(settings, store, notifier)
    telegram = TelegramBot(settings, store, controller, notifier)
    WebServer(controller).start()
    store.event("info", "startup", f"Gold Copy Trader v{__version__} started in DRY RUN mode")

    tasks = [
        asyncio.create_task(supervise("copyfactory-monitor", controller.monitor_copyfactory)),
        asyncio.create_task(supervise("risk-manager", controller.risk.run)),
        asyncio.create_task(supervise("telegram-notifications", telegram.run_notifications)),
    ]
    if settings.telegram_bot_token:
        tasks.append(asyncio.create_task(supervise("telegram-commands", telegram.run)))
    else:
        log.warning("telegram_disabled", reason="TELEGRAM_BOT_TOKEN missing")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover (Windows)
            pass
    await stop.wait()
    log.info("shutdown")
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await controller.rest.close()


if __name__ == "__main__":
    asyncio.run(main())
