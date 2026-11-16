"""Entry point: ``python -m goldbot`` (Railway start command).

Railway variables
-----------------
METAAPI_TOKEN        MetaApi API token (required to trade)
TELEGRAM_BOT_TOKEN   Telegram bot token from @BotFather (required for messages)
METAAPI_ACCOUNT_ID   optional - found from EXNESS_LOGIN when missing
EXNESS_LOGIN         MT5 login of the trading account (default 224085839)
TELEGRAM_CHAT_ID     optional - otherwise send "/pair <PAIR_CODE>" to the bot once
PAIR_CODE            one-time code for /pair
SYMBOL               default XAUUSDm
DATA_DIR             where state.json lives (default /data when it exists)
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

import aiohttp

from . import __version__
from .bot import HELP, Bot, State, env, env_float
from .metaapi import ApiError, MetaApi, TradeRejected

log = logging.getLogger("goldbot")


class Telegram:
    def __init__(self, token: str, state: State):
        self.token, self.state = token, state
        self._session: Optional[aiohttp.ClientSession] = None

    async def _api(self, method: str, data: dict, timeout: float = 40) -> dict:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        async with self._session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            payload = await resp.json(content_type=None)
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram {method}: {payload.get('error_code')} {payload.get('description')}")
        return payload

    async def send(self, text: str, chat_id: str = "") -> None:
        chat_id = chat_id or str(self.state["chat_id"] or "")
        log.info("message: %s", text.replace("\n", " | "))
        if not (self.token and chat_id):
            return
        try:
            for start in range(0, len(text), 4000):
                await self._api("sendMessage", {"chat_id": chat_id, "text": text[start:start + 4000], "disable_web_page_preview": "true"})
        except asyncio.CancelledError:
            raise
        except Exception as exc:      # a Telegram outage must never stop the robot
            log.warning("telegram send failed: %s", exc)

    async def run(self, bot: Bot) -> None:
        pair_code = env("PAIR_CODE")
        try:
            await self._api("deleteWebhook", {"drop_pending_updates": "false"})
        except Exception as exc:
            log.warning("deleteWebhook: %s", exc)
        while True:
            try:
                updates = await self._api("getUpdates", {"offset": self.state["tg_offset"], "timeout": 25, "allowed_updates": '["message"]'})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("getUpdates: %s", exc)
                await asyncio.sleep(10)
                continue
            for update in updates.get("result", []):
                self.state["tg_offset"] = update["update_id"] + 1
                msg = update.get("message") or {}
                text, chat = (msg.get("text") or "").strip(), str((msg.get("chat") or {}).get("id") or "")
                if not text.startswith("/") or not chat:
                    continue
                try:
                    if chat != str(self.state["chat_id"] or ""):
                        parts = text.split()
                        if parts[0].lower().startswith("/pair") and pair_code and len(parts) > 1 and hmac.compare_digest(parts[1], pair_code):
                            self.state["chat_id"] = chat
                            self.state.save()
                            await self.send("✅ Paired. This chat now controls the robot.\n\n" + HELP + "\n\n" + await bot.status())
                        else:
                            await self.send("This bot is private. Send /pair <code> to connect it.", chat)
                        continue
                    async with bot.lock:
                        reply = await bot.command(text)
                    await self.send(reply)
                except (ApiError, TradeRejected) as exc:
                    await self.send(f"That did not work: {exc}")
                except Exception as exc:
                    log.exception("command failed")
                    await self.send(f"That did not work: {type(exc).__name__}")
            self.state.save()


async def trade_loop(bot: Bot) -> None:
    while True:
        try:
            async with bot.lock:
                await bot.tick()
            bot.errors = 0
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except ApiError as exc:
            bot.errors += 1
            log.warning("api: %s", exc)
            if exc.status == 429:
                await bot.tell_once("429", "⏳ MetaApi rate limit reached - pausing briefly. The broker stop stays in place.", 3600)
            if bot.errors >= 8 and bot.ready:      # stop guessing: re-check the connection and say what is wrong
                bot.ready, bot.next_connect, bot.problem = False, 0.0, str(exc)
            await asyncio.sleep(min(max(exc.retry_after or 10.0, 10.0), 900.0))
        except Exception as exc:
            log.exception("tick failed")
            await bot.tell_once("crash", f"⚠️ Robot error: {type(exc).__name__}: {exc}. Still running.", 1800)
            await asyncio.sleep(15)


async def main() -> None:
    logging.basicConfig(level=env("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stdout)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    data_dir = Path(env("DATA_DIR") or ("/data" if os.path.isdir("/data") else "./data"))
    state = State(data_dir / "goldbot_state.json")
    if env("TELEGRAM_CHAT_ID") and not state["chat_id"]:
        state["chat_id"] = env("TELEGRAM_CHAT_ID")
    telegram = Telegram(env("TELEGRAM_BOT_TOKEN"), state)
    token = env("METAAPI_TOKEN")
    api = MetaApi(token, env("METAAPI_ACCOUNT_ID") or env("METAAPI_TARGET_ACCOUNT_ID")) if token else None
    bot = Bot(
        api, state, telegram.send, time.time,
        symbol=env("SYMBOL", "XAUUSDm"),
        poll_open=max(20.0, env_float("POLL_OPEN_SECONDS", 30.0)),
        max_spread=env_float("MAX_SPREAD", 0.80),
        max_lot=env_float("MAX_LOT", 0.50),
    )
    log.info("GoldBot %s starting: mode=%s telegram=%s chat=%s metaapi=%s", __version__, bot.mode(),
             "set" if telegram.token else "missing", "paired" if state["chat_id"] else "not paired", "set" if token else "missing")
    await telegram.send(f"🤖 GoldBot v{__version__} started on Railway. Mode: {bot.mode()}.")
    tasks = [asyncio.create_task(trade_loop(bot))]
    if telegram.token:
        tasks.append(asyncio.create_task(telegram.run(bot)))
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    if api:
        await api.close()


if __name__ == "__main__":
    asyncio.run(main())
