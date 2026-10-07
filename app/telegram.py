"""Telegram control + notification bot (long polling, single paired chat)."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp

from .config import Settings
from .controller import Controller
from .logging_setup import get_logger
from .notifier import Notifier
from .storage import Store

log = get_logger("telegram")

MAX_MESSAGE = 4000


def _ago(ts: Optional[float]) -> str:
    if not ts:
        return "never"
    seconds = max(0, int(time.time() - ts))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


def _money(value: Any) -> str:
    if value is None:
        return "—"
    v = float(value)
    return f"{'+' if v >= 0 else '-'}${abs(v):.2f}"


def _account_line(label: str, acc: Optional[dict]) -> str:
    if not acc:
        return f"{label}: ⚪ not checked yet"
    if acc.get("error"):
        return f"{label}: 🔴 {acc['error'][:200]}"
    icon = "🟢" if acc.get("connected") else "🔴"
    return f"{label}: {icon} {acc.get('connectionStatus') or '?'} ({acc.get('state') or '?'}) {acc.get('name') or ''}".rstrip()


class TelegramBot:
    def __init__(self, settings: Settings, store: Store, controller: Controller, notifier: Notifier):
        self.settings = settings
        self.store = store
        self.controller = controller
        self.notifier = notifier
        self.offset = 0
        self._session: Optional[aiohttp.ClientSession] = None
        self.started = False
        self.username: Optional[str] = None

    # -- transport ----------------------------------------------------------------

    async def _api(self, method: str, data: Optional[dict] = None, timeout: float = 40) -> dict:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/{method}"
        async with self._session.post(url, data=data or {}, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            payload = await resp.json(content_type=None)
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {payload.get('error_code')} {payload.get('description')}")
        return payload

    async def _send_to(self, chat_id: str, text: str) -> None:
        for start in range(0, len(text), MAX_MESSAGE):
            await self._api("sendMessage", {"chat_id": chat_id, "text": text[start : start + MAX_MESSAGE], "disable_web_page_preview": "true"})

    async def send(self, text: str) -> None:
        chat_id = self.store.get("telegram_chat_id")
        if not chat_id:
            return
        try:
            await self._send_to(chat_id, text)
        except Exception as exc:
            log.warning("telegram_send_failed", error=str(exc))

    # -- text builders ---------------------------------------------------------------

    def status_text(self) -> str:
        s = self.controller.state()
        cf = s["copyFactory"]
        mon = s["monitor"]
        accounts = s.get("accounts") or {}
        st = cf.get("status") or {}

        if cf["active"]:
            cf_line = f"🟢 ACTIVE — '{st.get('strategyName')}' → '{st.get('subscriberName')}'"
        elif cf["ready"]:
            cf_line = "🟠 NOT ACTIVE — see warnings below"
        else:
            cf_line = f"🔴 NOT READY — {(cf.get('error') or 'checking…')[:300]}"
        if cf["paused"] is None:
            copy_line = "⚪ unknown (not read yet)"
        else:
            copy_line = "⏸ PAUSED (closeOnly)" if cf["paused"] else "▶️ ON — source open = target open, source close = target close"

        if mon["online"]:
            mon_line = f"🟢 OK — positions read {_ago(mon.get('lastOkAt'))} (every {mon['pollSeconds']:g}s)"
        elif mon.get("rateLimitedUntil"):
            mon_line = f"🟠 rate-limited by MetaApi, resuming in {max(0, int(mon['rateLimitedUntil'] - time.time()))}s"
        else:
            mon_line = f"🔴 retrying ({mon.get('consecutiveFailures')}x) — {(mon.get('lastError') or 'starting…')[:300]}"
        sync = "🟢 in sync" if mon.get("inSync") else f"🟠 {mon.get('syncDetail')}"
        lines = [
            "🤖 Gold Copy Trader",
            "",
            _account_line("Source", accounts.get("source")),
            _account_line("Target", accounts.get("target")),
            f"CopyFactory: {cf_line}",
            f"Copying: {copy_line}",
            f"Monitor: {mon_line}",
            "",
            f"Route: {cf['sourceSymbol']} → {cf['targetSymbol']}  |  Lot: {cf['lot']:g}",
            f"SL/TP copied: {'yes' if st.get('copyStopLoss') else 'no'}/{'yes' if st.get('copyTakeProfit') else 'no'}",
            f"Open: source {len(mon['sourcePositions'])} · target {len(mon['targetPositions'])} · {sync}",
        ]
        warnings = st.get("warnings") or []
        if warnings:
            lines += ["", "⚠️ Needs attention:"] + [f"• {w}" for w in warnings]
        return "\n".join(lines)

    def positions_text(self) -> str:
        mon = self.controller.monitor.state()
        lines = []
        for label, rows in (("Source", mon["sourcePositions"]), ("Target", mon["targetPositions"])):
            lines.append(f"{label}: {len(rows)} open")
            for r in rows[:20]:
                lines.append(f"  {r['side']} {r['symbol']} {r['volume']:g} @ {r['openPrice']} · P/L {_money(r.get('profit'))} · #{r['id']}")
        lines.append("" if mon.get("inSync") else f"\n⚠️ {mon.get('syncDetail')}")
        lines.append(f"(as of {_ago(mon.get('lastOkAt'))})")
        return "\n".join(lines).strip()

    @staticmethod
    def help_text() -> str:
        return (
            "Gold Copy Trader\n\n"
            "/status — accounts, CopyFactory, copying on/off, sync\n"
            "/positions — open source and target trades\n"
            "/pause — CopyFactory stops opening NEW target trades\n"
            "/resume — CopyFactory copies new trades again\n"
            "/help — this list\n\n"
            "MetaApi CopyFactory opens and closes the target trades (XAUUSDm, 0.01). "
            "This bot only monitors and reports."
        )

    # -- command handling -----------------------------------------------------------

    async def handle(self, message: dict) -> None:
        chat_id = str((message.get("chat") or {}).get("id", ""))
        text = str(message.get("text") or "").strip()
        if not chat_id or not text:
            return

        paired = self.store.get("telegram_chat_id")
        if not paired:
            parts = text.split(maxsplit=1)
            if len(parts) == 2 and parts[0].lower() == "/pair":
                expected = self.settings.telegram_pairing_code or self.settings.dashboard_password
                if expected and parts[1].strip() == expected:
                    self.store.set("telegram_chat_id", chat_id)
                    self.store.event("info", "telegram", "Telegram chat paired")
                    await self._send_to(chat_id, "✅ Telegram paired.\n\n" + self.help_text())
                else:
                    log.warning("telegram_pair_rejected", chat_id=chat_id)
                    await self._send_to(chat_id, "❌ Invalid pairing code.")
            return
        if chat_id != paired:
            log.warning("telegram_unpaired_chat_ignored", chat_id=chat_id)
            return

        cmd, *rest = text.split(maxsplit=1)
        cmd = cmd.split("@", 1)[0].lower()
        arg = rest[0].strip() if rest else ""
        log.info("telegram_command", command=cmd, arg=arg)
        try:
            if cmd in ("/start", "/help"):
                reply = self.help_text()
            elif cmd == "/status":
                reply = self.status_text()
            elif cmd == "/positions":
                reply = self.positions_text()
            elif cmd == "/pause":
                await self.controller.set_pause(True)
                reply = "⏸ CopyFactory paused (closeOnly=by-position). New source trades will not copy; already-copied trades still close when the source closes."
            elif cmd == "/resume":
                await self.controller.set_pause(False)
                reply = (
                    f"▶️ CopyFactory resumed. New {self.settings.copy_symbol} source trades will copy to the target as "
                    f"{self.settings.target_symbol} at {self.controller.lot():g} lot."
                )
            else:
                reply = "Unknown command.\n\n" + self.help_text()
        except Exception as exc:
            log.exception("telegram_command_failed", command=cmd)
            reply = f"❌ {exc}"
        await self._send_to(chat_id, reply)

    # -- loops ------------------------------------------------------------------------

    async def run_notifications(self) -> None:
        while True:
            text = await self.notifier.queue.get()
            if not self.settings.telegram_bot_token:
                continue
            await self.send(text)
            await asyncio.sleep(0.05)  # stay far below Telegram's per-chat rate limit

    async def run(self) -> None:
        if not self.settings.telegram_bot_token:
            log.warning("telegram_disabled", reason="TELEGRAM_BOT_TOKEN missing")
            return
        backoff = 5.0
        while True:
            try:
                if not self.started:
                    me = await self._api("getMe", timeout=15)
                    self.username = (me.get("result") or {}).get("username")
                    self.started = True
                    paired = bool(self.store.get("telegram_chat_id"))
                    log.info("telegram_started", username=self.username, paired=paired)
                    self.store.event("info", "telegram", f"Telegram bot @{self.username} started (paired={paired})")
                    if paired:
                        await self.send(
                            f"🚀 Gold Copy Trader restarted at {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC\n"
                            "CopyFactory keeps copying regardless. Send /status for details."
                        )
                response = await self._api(
                    "getUpdates",
                    {
                        "offset": self.offset,
                        "timeout": self.settings.telegram_poll_timeout,
                        "allowed_updates": json.dumps(["message"]),
                    },
                    timeout=self.settings.telegram_poll_timeout + 15,
                )
                backoff = 5.0
                for update in response.get("result", []):
                    self.offset = max(self.offset, int(update.get("update_id", 0)) + 1)
                    if update.get("message"):
                        await self.handle(update["message"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 409 Conflict is expected for a few seconds while Railway swaps deployments.
                log.warning("telegram_poll_failed", error=str(exc), retry_in=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
