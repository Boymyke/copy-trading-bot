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
        risk = s["risk"]
        accounts = s.get("accounts") or {}
        cf_status = cf.get("status") or {}

        if cf["ready"]:
            cf_line = f"🟢 READY — '{cf_status.get('strategyName')}' adopted"
        else:
            cf_line = f"🔴 NOT READY — {(cf.get('error') or 'checking…')[:300]}"
        if cf["paused"] is None:
            copy_line = "⚪ unknown (CopyFactory not read yet)"
        else:
            copy_line = ("⏸ PAUSED (closeOnly)" if cf["paused"] else "▶️ RESUMED — copying new trades")
            if not cf.get("pausedIsLive"):
                copy_line += " (last known)"

        if risk["online"]:
            risk_line = f"🟢 ONLINE — REST read {_ago(risk.get('lastCycleOkAt'))}"
        else:
            risk_line = f"🔴 RETRYING ({risk.get('consecutiveFailures')}x) — {(risk.get('lastError') or 'starting…')[:300]}"
        price = risk.get("price") or {}
        spec = risk.get("spec")
        lines = [
            "🤖 Gold Copy Trader v5",
            "🟡 MODE: DRY RUN — no orders or SL changes are ever sent",
            "",
            _account_line("Source", accounts.get("source")),
            _account_line("Target", accounts.get("target")),
            f"CopyFactory: {cf_line}",
            f"Copying: {copy_line}",
            f"Risk manager: {risk_line}",
            "",
            f"Route: {cf['sourceSymbol']} → {cf['targetSymbol']}  |  Lot: {cf['lot']:g}",
            f"{risk['symbol']} spec: " + (f"digits {spec['digits']}, tick {spec['tickSize']:g}, stops {spec['stopsLevel']:g} pts, freeze {spec['freezeLevel']:g} pts" if spec else "not read yet"),
            f"Bid/Ask: {price.get('bid', '—')}/{price.get('ask', '—')}",
            f"Initial SL ${risk['initialSlUsd']:.2f} · Trail at ${risk['trailTriggerUsd']:.2f} · gap ${risk['trailGapUsd']:.2f} · step ${risk['trailStepUsd']:.2f} (per 0.01)",
            f"Managed open: {len(risk['managedPositions'])}  |  All target positions: {risk['targetPositionsTotal']}",
        ]
        warnings = cf_status.get("warnings") or []
        if warnings:
            lines += ["", "⚠️ Config warnings:"] + [f"• {w}" for w in warnings]
        return "\n".join(lines)

    def positions_text(self) -> str:
        risk = self.controller.risk.state()
        rows = risk["managedPositions"]
        lines: list[str] = []
        if not rows:
            lines.append("No managed target positions are open.")
        else:
            lines.append("📈 Managed target positions (DRY RUN)")
            for r in rows[:20]:
                lines.append(
                    f"\n{r['side']} {r['symbol']} {float(r['volume']):g} #{r['position_id']}\n"
                    f"Entry {r['open_price']} | Bid/Ask {r.get('last_bid')}/{r.get('last_ask')}\n"
                    f"Floating P/L {_money(r.get('broker_profit'))}\n"
                    f"Simulated SL {r.get('simulated_sl')} ({'trailing, locks ' + _money(r.get('locked_money')) if r.get('trail_active') else 'initial'})"
                    + (f"\n⚠️ Simulated SL hit at {r.get('sl_hit_price')} (≈{_money(r.get('sl_hit_money'))})" if r.get("sl_hit_at") else "")
                )
        ignored = risk.get("ignoredPositions") or []
        if ignored:
            lines.append("\nIgnored (not managed):")
            lines += [f"• #{i['id']} {i['symbol']} {i['volume']}: {i['why']}" for i in ignored[:10]]
        return "\n".join(lines)

    @staticmethod
    def help_text() -> str:
        return (
            "Gold Copy Trader v5 — DRY RUN\n\n"
            "/status — source, target, CopyFactory, risk manager, pause state\n"
            "/positions — managed target trades with simulated SL\n"
            "/pause — CopyFactory stops opening NEW copied trades (closeOnly)\n"
            "/resume — CopyFactory copies new trades again\n"
            "/lot 0.01 — CopyFactory fixed volume for NEW trades\n"
            "/setsl 0.60 — simulated initial SL $ per 0.01\n"
            "/settrigger 0.50 — simulated trailing starts at this profit per 0.01\n"
            "/setgap 0.20 — simulated trailing gap $ per 0.01\n"
            "/setstep 0.10 — minimum simulated SL improvement per 0.01\n"
            "/help — this list\n\n"
            "The risk manager only calculates and reports. It never sends orders or SL changes."
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
                reply = "⏸ CopyFactory paused (closeOnly=by-position). Already-copied trades still close when the source closes."
            elif cmd == "/resume":
                await self.controller.set_pause(False)
                reply = (
                    f"▶️ CopyFactory resumed. New {self.settings.copy_symbol} source trades will copy to the target as "
                    f"{self.settings.target_symbol}. Risk manager stays DRY RUN."
                )
            elif cmd == "/lot":
                value = float(arg)
                where = await self.controller.set_lot(value)
                reply = f"✅ CopyFactory fixed volume set to {value:g} (changed on the {where}; nothing else modified)."
            elif cmd in ("/setsl", "/settrigger", "/setgap", "/setstep"):
                value = float(arg)
                key, label = {
                    "/setsl": ("initial_sl_usd", "Simulated initial SL"),
                    "/settrigger": ("trail_trigger_usd", "Simulated trail trigger"),
                    "/setgap": ("trail_gap_usd", "Simulated trailing gap"),
                    "/setstep": ("trail_step_usd", "Simulated trailing step"),
                }[cmd]
                self.controller.set_risk(key, value)
                reply = f"✅ {label} set to ${value:g} per 0.01 lot (applies to new calculations)."
            else:
                reply = "Unknown command.\n\n" + self.help_text()
        except ValueError:
            reply = f"❌ Expected a number, e.g. {cmd} 0.01" if cmd in ("/lot", "/setsl", "/settrigger", "/setgap", "/setstep") else "❌ Invalid value"
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
                            f"🚀 Gold Copy Trader v5 started at {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC\n"
                            "🟡 DRY RUN — the risk manager only calculates and reports.\nSend /status for details."
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
