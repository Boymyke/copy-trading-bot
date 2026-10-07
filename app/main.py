import asyncio

from .config import load_settings
from .controller import Controller
from .storage import Store
from .telegram import TelegramBot
from .web import WebServer


async def main():
    settings = load_settings()
    missing = []
    if not settings.metaapi_token:
        missing.append("METAAPI_TOKEN")
    if not settings.source_account_id:
        missing.append("METAAPI_SOURCE_ACCOUNT_ID")
    if not settings.target_account_id:
        missing.append("METAAPI_TARGET_ACCOUNT_ID")
    if missing:
        raise RuntimeError("Missing required configuration: " + ", ".join(missing))

    store = Store(settings.data_dir)
    controller = Controller(settings, store)
    telegram = TelegramBot(settings, store, controller)
    WebServer(controller).start()

    store.event("info", "startup", "Gold Copy Trader v4 controller started")

    tasks = [
        asyncio.create_task(controller.bootstrap_copyfactory(), name="copyfactory-bootstrap"),
        asyncio.create_task(controller.risk.run(), name="risk-manager"),
    ]
    if settings.telegram_bot_token:
        tasks.append(asyncio.create_task(telegram.run(), name="telegram"))

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
