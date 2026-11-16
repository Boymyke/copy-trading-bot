"""The robot: watches 5-minute gold bars, opens and manages trades, reports to Telegram.

Two modes, switched from Telegram with ``/live on`` and ``/live off``:

* practice (default) - no orders are sent. Signals are followed on paper with
  live prices and reported exactly like real trades.
* live - real market orders on the MetaApi account, each with a stop loss held
  by the broker, so a trade stays protected even if this process stops.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from . import __version__, strategy as S
from .metaapi import ApiError, MetaApi, TradeRejected

log = logging.getLogger("goldbot")

COMMENT = "GB6H"
LAGOS = timezone(timedelta(hours=1))


def env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def env_float(name: str, default: float) -> float:
    try:
        return float(env(name) or default)
    except ValueError:
        return default


class State:
    """Small JSON file under DATA_DIR. Survives restarts when a volume is mounted there."""

    DEFAULTS: dict[str, Any] = {
        "chat_id": "",
        "live": False,
        "fixed_lot": 0.0,
        "per": 40.0,
        "goal": 200.0,
        "goal_told": False,
        "day": "",
        "count": 0,
        "pos": None,
        "trades": [],
        "sent": {},
        "tg_offset": 0,
    }

    def __init__(self, path: Path):
        self.path = path
        self.d: dict[str, Any] = json.loads(json.dumps(self.DEFAULTS))
        try:
            self.d.update(json.loads(path.read_text()))
        except (OSError, ValueError):
            pass

    def __getitem__(self, key: str) -> Any:
        return self.d[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.d[key] = value

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.d))
            tmp.replace(self.path)
        except OSError as exc:  # no volume: keep running from memory
            log.warning("state not saved: %s", exc)


class Bot:
    def __init__(
        self,
        api: Optional[MetaApi],
        state: State,
        send: Callable[[str], Any],
        clock: Callable[[], float] = time.time,
        symbol: str = "XAUUSDm",
        poll_open: float = 30.0,
        max_spread: float = 0.80,
        max_lot: float = 0.50,
    ):
        self.api, self.state, self.send, self.clock = api, state, send, clock
        self.symbol, self.poll_open, self.max_spread, self.max_lot = symbol, poll_open, max_spread, max_lot
        self.ready = False            # MetaApi account resolved and reachable
        self.problem = ""             # last connection problem, shown in /status
        self.last_bar = 0
        self.next_manage = 0.0
        self.next_connect = 0.0
        self.misses = 0
        self.balance: Optional[float] = None
        self.lock = asyncio.Lock()    # one of: a tick, or a Telegram command
        self.wait = 1.0               # pause multiplier (0 in tests)
        self.errors = 0               # consecutive API failures
        self._told: dict[str, float] = {}

    # -- helpers ------------------------------------------------------------------------

    async def tell(self, text: str) -> None:
        try:
            result = self.send(text)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # Telegram must never stop trading
            log.warning("telegram send failed: %s", exc)

    async def tell_once(self, key: str, text: str, every: float = 1800.0) -> None:
        now = self.clock()
        if now - self._told.get(key, 0.0) >= every:
            self._told[key] = now
            await self.tell(text)

    def mode(self) -> str:
        return "LIVE" if self.state["live"] else "PRACTICE"

    def _roll_day(self, now: float) -> None:
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        if self.state["day"] != day:
            self.state["day"], self.state["count"] = day, 0
            self.state["sent"] = {k: v for k, v in self.state["sent"].items() if v == day}
            self.state.save()

    # -- connection ------------------------------------------------------------------------

    async def connect(self) -> bool:
        """Find the account on MetaApi and check it is deployed and connected to the broker."""
        self.ready = False
        if self.api is None:
            self.problem = "METAAPI_TOKEN is not set in Railway"
            return False
        try:
            if not self.api.account_id:
                login = env("EXNESS_LOGIN", "224085839")
                found = [a for a in await self.api.accounts() if str(a.get("login")) == login]
                if not found:
                    self.problem = f"MetaApi has no account with login {login}"
                    return False
                self.api.account_id = str(found[0].get("_id"))
            acc = await self.api.account()
        except ApiError as exc:
            self.problem = (
                "MetaApi refused the token (check METAAPI_TOKEN)" if exc.status in (401, 403) else f"MetaApi not reachable: {exc}"
            )
            return False
        if acc.get("state") != "DEPLOYED":
            self.problem = f"the account is switched off on MetaApi ({acc.get('state')}). Send /deploy to switch it on"
            return False
        if acc.get("connectionStatus") != "CONNECTED":
            self.problem = f"MetaApi is not connected to Exness yet ({acc.get('connectionStatus')})"
            return False
        self.problem, self.ready = "", True
        return True

    async def recover(self) -> None:
        """After a restart in live mode: pick the open robot trade back up from the broker."""
        if not (self.ready and self.state["live"]):
            return
        positions = await self.api.positions()
        mine = [p for p in positions if p.get("symbol") == self.symbol and str(p.get("comment") or "").startswith(COMMENT)]
        pos = self.state["pos"]
        if pos and pos.get("live") and not any(str(p.get("id")) == str(pos["id"]) for p in mine):
            await self._closed_at_broker(pos)
        elif mine and not (pos and pos.get("live")):
            p = mine[0]
            side = 1 if str(p.get("type", "")).endswith("BUY") else -1
            entry = float(p["openPrice"])
            stop = float(p.get("stopLoss") or 0) or S.initial_stop(side, entry)
            self.state["pos"] = {
                "live": True, "id": str(p["id"]), "side": side, "entry": entry, "stop": stop,
                "best": S.best_from_stop(side, entry, stop), "lot": float(p.get("volume") or 0.01),
                "opened": S_parse(p.get("time")) or self.clock(),
            }
            self.state.save()
            await self.tell(f"♻️ Restarted - picked the open {'BUY' if side > 0 else 'SELL'} at {entry:.2f} back up.")

    # -- main step (called about once a second) -------------------------------------------------

    async def tick(self) -> None:
        now = self.clock()
        self._roll_day(now)
        if not self.ready:
            if now >= self.next_connect:
                self.next_connect = now + 120
                if await self.connect():
                    await self.recover()
                    await self.tell(f"🟢 Connected to Exness through MetaApi. Mode: {self.mode()}.")
                else:
                    await self.tell_once("connect", f"🔴 Not trading: {self.problem}.", every=6 * 3600)
            return
        await self._scheduled(now)
        if self.state["pos"]:
            if now >= self.next_manage:
                self.next_manage = now + self.poll_open
                await self.manage()
            return
        bar = int(now // 300) * 300 - 300          # start of the bar that has just closed
        if bar == self.last_bar or now < bar + 300 + 3:
            return
        weekday = datetime.fromtimestamp(bar, timezone.utc).weekday()
        if weekday >= 5 or not S.in_session(bar) or self.state["count"] >= S.MAX_TRADES_PER_DAY:
            self.last_bar = bar
            return
        if await self.check_signal(bar) or now > bar + 300 + 90:
            self.last_bar = bar

    async def check_signal(self, bar: int) -> bool:
        """Returns False when the bar is not available from the broker yet (try again shortly)."""
        candles = await self.api.candles(self.symbol, "5m", limit=S.LOOKBACK + 8)
        idx = next((i for i, c in enumerate(candles) if c["t"] == bar), None)
        if idx is None:
            if candles and abs(candles[-1]["t"] - bar) > 3600:
                await self.tell_once("clock", "⚠️ Broker candles are more than an hour away from the clock - no trades until that clears (market closed?).", 6 * 3600)
                return True
            return False
        before = candles[max(0, idx - S.LOOKBACK):idx]
        side = S.breakout_signal([c["h"] for c in before], [c["l"] for c in before], candles[idx]["c"])
        if side:
            await self.open_trade(side)
        return True

    # -- opening ------------------------------------------------------------------------------------

    async def open_trade(self, side: int) -> None:
        name = "BUY" if side > 0 else "SELL"
        quote = await self.api.price(self.symbol)
        spread = quote["ask"] - quote["bid"]
        if spread > self.max_spread:
            await self.tell(f"⏭ {name} signal skipped: spread ${spread:.2f} is too wide right now.")
            return
        live = bool(self.state["live"])
        info = await self.api.info()
        self.balance = float(info.get("balance") or 0)
        equity = float(info.get("equity") or self.balance)
        lot = S.lot_size(self.balance, self.state["fixed_lot"] or None, self.state["per"], max_lot=self.max_lot)
        risk = S.money(S.STOP_DISTANCE, lot)
        if live and equity < risk + 0.5:
            await self.tell_once(
                "funds", f"⛔ {name} signal not taken: the account has ${equity:.2f} and one stop-out at {lot:.2f} lot costs ${risk:.2f}.", 6 * 3600
            )
            return
        entry = quote["ask"] if side > 0 else quote["bid"]
        pos = {"live": live, "id": "", "side": side, "entry": entry, "stop": S.initial_stop(side, entry), "best": 0.0, "lot": lot, "opened": self.clock()}
        if live:
            try:
                result = await self.api.market_order(self.symbol, side, lot, pos["stop"], COMMENT)
            except TradeRejected as exc:
                await self.tell(f"❌ {name} order rejected by the broker: {exc.text}")
                return
            pos["id"] = str(result.get("positionId") or result.get("orderId") or "")
            await self._sync_fill(pos)
        self.state["pos"] = pos
        self.state["count"] += 1
        self.state.save()
        self.next_manage, self.misses = self.clock() + self.poll_open, 0
        await self.tell(
            f"{'🟢' if side > 0 else '🔴'} {name} {lot:.2f} {self.symbol} at {pos['entry']:.2f}  [{self.mode()}]\n"
            f"Stop {pos['stop']:.2f} (-${risk:.2f}). Trails $2 behind once $2 ahead.\n"
            f"Trade {self.state['count']} of {S.MAX_TRADES_PER_DAY} today."
        )

    async def _sync_fill(self, pos: dict) -> None:
        """Read the real fill price and put the stop exactly $6 from it."""
        for _ in range(4):
            await asyncio.sleep(1.5 * self.wait)
            try:
                found = [p for p in await self.api.positions() if str(p.get("id")) == pos["id"] or (
                    not pos["id"] and p.get("symbol") == self.symbol and str(p.get("comment") or "").startswith(COMMENT))]
            except ApiError:
                continue
            if found:
                p = found[0]
                pos["id"], pos["entry"] = str(p["id"]), float(p["openPrice"])
                pos["stop"] = S.initial_stop(pos["side"], pos["entry"])
                if abs(float(p.get("stopLoss") or 0) - pos["stop"]) > 0.05:
                    try:
                        await self.api.modify_stop(pos["id"], pos["stop"])
                    except (ApiError, TradeRejected) as exc:
                        log.warning("stop not re-centred: %s", exc)
                        pos["stop"] = float(p.get("stopLoss") or pos["stop"])
                return

    # -- managing -----------------------------------------------------------------------------------------

    async def manage(self) -> None:
        pos = self.state["pos"]
        side = pos["side"]
        if pos["live"]:
            current = next((p for p in await self.api.positions() if str(p.get("id")) == pos["id"]), None)
            if current is None:
                self.misses += 1
                if self.misses >= 2:      # two reads in a row: the broker closed it (stop hit)
                    await self._closed_at_broker(pos)
                return
            self.misses = 0
            price = float(current["currentPrice"])
        else:
            quote = await self.api.price(self.symbol)
            price = quote["bid"] if side > 0 else quote["ask"]
            if S.stop_hit(side, pos["stop"], price):
                # a broker stop fills at about the stop price, so practice does the same
                await self._finish(pos, S.money((pos["stop"] - pos["entry"]) * side, pos["lot"]), pos["stop"], "stop")
                return
        old_stop = pos["stop"]
        pos["best"], pos["stop"] = S.trail(side, pos["entry"], pos["stop"], pos["best"], price)
        expired = self.clock() - pos["opened"] > S.MAX_HOLD_MINUTES * 60
        if pos["live"]:
            if expired:
                await self._close_live(pos, "time limit (4 hours)")
                return
            if (pos["stop"] - old_stop) * side >= 0.05:
                try:
                    await self.api.modify_stop(pos["id"], pos["stop"])
                except TradeRejected as exc:
                    if S.stop_hit(side, pos["stop"], price):
                        # price is already through the new stop: take what is there
                        await self._close_live(pos, "trailing stop")
                        return
                    pos["stop"] = old_stop
                    await self.tell_once("modify", f"⚠️ The broker refused to move the stop ({exc.text}). It stays at {old_stop:.2f}.", 600)
                except ApiError:
                    pos["stop"] = old_stop      # try again on the next read
        elif expired:
            await self._finish(pos, S.money((price - pos["entry"]) * side, pos["lot"]), price, "time limit (4 hours)")
            return
        self.state.save()

    async def _close_live(self, pos: dict, reason: str) -> None:
        try:
            await self.api.close_position(pos["id"])
        except TradeRejected as exc:
            await self.tell_once("close", f"⚠️ Could not close the trade ({exc.text}). The broker stop at {pos['stop']:.2f} is still in place.", 600)
            return
        await self._closed_at_broker(pos, reason)

    async def _closed_at_broker(self, pos: dict, reason: str = "stop") -> None:
        profit, exit_price = None, None
        for _ in range(5):
            try:
                deals = await self.api.deals_for_position(pos["id"])
            except ApiError:
                deals = []
            outs = [d for d in deals if d.get("entryType") == "DEAL_ENTRY_OUT"]
            if outs:
                profit = sum(float(d.get(k) or 0) for d in deals for k in ("profit", "commission", "swap"))
                exit_price = float(outs[-1].get("price") or 0)
                break
            await asyncio.sleep(3 * self.wait)
        if profit is None:      # history not available yet: report from the last known stop
            exit_price = pos["stop"]
            profit = S.money((exit_price - pos["entry"]) * pos["side"], pos["lot"])
        await self._finish(pos, profit, exit_price, reason)

    async def _finish(self, pos: dict, profit: float, exit_price: float, reason: str) -> None:
        if pos["live"]:
            try:
                self.balance = float((await self.api.info()).get("balance") or 0)
            except ApiError:
                pass
        trade = {
            "t": int(self.clock()), "side": pos["side"], "lot": pos["lot"], "entry": round(pos["entry"], 2),
            "exit": round(exit_price, 2), "pnl": round(profit, 2), "live": pos["live"], "why": reason,
        }
        self.state["trades"] = (self.state["trades"] + [trade])[-300:]
        self.state["pos"] = None
        self.state.save()
        mins = int((self.clock() - pos["opened"]) / 60)
        text = (
            f"{'✅' if profit > 0 else '❌' if profit < 0 else '➖'} {'BUY' if pos['side'] > 0 else 'SELL'} closed at {exit_price:.2f} "
            f"({reason}, {mins} min)  [{'LIVE' if pos['live'] else 'PRACTICE'}]\n"
            f"Result: {'+' if profit >= 0 else '-'}${abs(profit):.2f}"
        )
        if pos["live"] and self.balance is not None:
            text += f"\nBalance: ${self.balance:.2f} (goal ${self.state['goal']:.0f})"
            if self.balance >= self.state["goal"] and not self.state["goal_told"]:
                self.state["goal_told"] = True
                self.state.save()
                text += f"\n🏁 Goal of ${self.state['goal']:.0f} reached. The robot keeps running; send /live off to stop it."
        await self.tell(text)

    # -- scheduled messages ----------------------------------------------------------------------------------

    async def _scheduled(self, now: float) -> None:
        dt = datetime.fromtimestamp(now, timezone.utc)
        if dt.weekday() >= 5:
            return
        day, sent = self.state["day"], self.state["sent"]
        if dt.hour == 11 and dt.minute >= 50 and sent.get("ready") != day:
            sent["ready"] = day
            self.state.save()
            await self.tell("☀️ Ready for today's session (1-6 pm Lagos).\n" + await self.status())
        if dt.hour == 21 and dt.minute >= 15 and sent.get("summary") != day and not self.state["pos"]:
            sent["summary"] = day
            self.state.save()
            today = [t for t in self.state["trades"] if datetime.fromtimestamp(t["t"], timezone.utc).strftime("%Y-%m-%d") == day]
            if today:
                total = sum(t["pnl"] for t in today)
                wins = sum(1 for t in today if t["pnl"] > 0)
                await self.tell(f"📊 Today: {len(today)} trade(s), {wins} won, result {'+' if total >= 0 else '-'}${abs(total):.2f}.\n" + await self.status())
            else:
                await self.tell("📊 Today: no breakout signal, no trades.")

    # -- Telegram commands -----------------------------------------------------------------------------------------

    async def status(self) -> str:
        lines = [f"Mode: {self.mode()}" + ("" if self.state["live"] else " (no real orders - send /live on to trade the account)")]
        if not self.ready:
            lines.append(f"Connection: 🔴 {self.problem or 'starting'}")
        else:
            try:
                info = await self.api.info()
                self.balance = float(info.get("balance") or 0)
                lines.append(f"Balance: ${self.balance:.2f}, equity ${float(info.get('equity') or 0):.2f} (goal ${self.state['goal']:.0f})")
            except ApiError as exc:
                lines.append(f"Balance: not available ({exc.message})")
        lot = S.lot_size(self.balance or 0, self.state["fixed_lot"] or None, self.state["per"], max_lot=self.max_lot)
        how = "fixed" if self.state["fixed_lot"] else f"auto: 0.01 per ${self.state['per']:.0f}"
        lines.append(f"Lot: {lot:.2f} ({how}) - a stop-out costs ${S.money(S.STOP_DISTANCE, lot):.2f}")
        pos = self.state["pos"]
        if pos:
            lines.append(f"Open: {'BUY' if pos['side'] > 0 else 'SELL'} {pos['lot']:.2f} at {pos['entry']:.2f}, stop {pos['stop']:.2f}")
        else:
            lines.append(f"Open: none. Trades today: {self.state['count']} of {S.MAX_TRADES_PER_DAY}")
        for label, live in (("Live", True), ("Practice", False)):
            done = [t for t in self.state["trades"] if t["live"] == live]
            if done:
                wins = sum(1 for t in done if t["pnl"] > 0)
                lines.append(f"{label} so far: {len(done)} trades, {100 * wins // len(done)}% won, {sum(t['pnl'] for t in done):+.2f} $")
        return "\n".join(lines)

    async def command(self, text: str) -> str:
        parts = text.strip().split()
        cmd, arg = parts[0].lower().split("@")[0], (parts[1].lower() if len(parts) > 1 else "")
        st = self.state
        if cmd in ("/start", "/help"):
            return HELP
        if cmd == "/status":
            return await self.status()
        if cmd == "/live":
            if arg == "on":
                st["live"] = True
                st.save()
                return (
                    "🟢 LIVE trading is ON. The robot will now place real orders on the Exness account.\n"
                    f"Each stop-out costs ${S.money(S.STOP_DISTANCE, 0.01):.2f} per 0.01 lot. Send /live off to stop new trades."
                )
            if arg == "off":
                st["live"] = False
                st.save()
                extra = " The open trade keeps its stop and is still managed." if st["pos"] and st["pos"]["live"] else ""
                return "⏸ LIVE trading is OFF. Signals are followed on paper only." + extra
            return f"Mode is {self.mode()}. Use /live on or /live off."
        if cmd == "/lot":
            if arg == "auto":
                st["fixed_lot"] = 0.0
            else:
                try:
                    value = round(float(arg), 2)
                except ValueError:
                    return "Use /lot 0.02 or /lot auto."
                if not 0.01 <= value <= self.max_lot:
                    return f"Lot must be between 0.01 and {self.max_lot:.2f}."
                st["fixed_lot"] = value
            st.save()
            return "Lot updated (applies to the next trade).\n" + await self.status()
        if cmd == "/per":
            try:
                value = float(arg)
            except ValueError:
                return "Use /per 40 (dollars of balance for each 0.01 lot)."
            if value < 12:
                return "That is below $12 per 0.01 lot: one stop-out would take more than half the account. Not changed."
            st["per"], st["fixed_lot"] = value, 0.0
            st.save()
            return "Lot scaling updated.\n" + await self.status()
        if cmd == "/goal":
            try:
                st["goal"], st["goal_told"] = float(arg), False
            except ValueError:
                return "Use /goal 200."
            st.save()
            return f"Goal set to ${st['goal']:.0f}."
        if cmd == "/close":
            pos = st["pos"]
            if not pos:
                return "No open trade."
            if pos["live"]:
                await self._close_live(pos, "closed from Telegram")
            else:
                quote = await self.api.price(self.symbol)
                price = quote["bid"] if pos["side"] > 0 else quote["ask"]
                await self._finish(pos, S.money((price - pos["entry"]) * pos["side"], pos["lot"]), price, "closed from Telegram")
            return "Done."
        if cmd == "/trades":
            rows = st["trades"][-10:]
            if not rows:
                return "No trades yet."
            return "\n".join(
                f"{datetime.fromtimestamp(t['t'], LAGOS):%d %b %H:%M} {'BUY ' if t['side'] > 0 else 'SELL'} {t['lot']:.2f} "
                f"{t['entry']:.2f}→{t['exit']:.2f} {t['pnl']:+.2f}$ {'' if t['live'] else '(practice)'}"
                for t in rows
            )
        if cmd == "/deploy":
            if self.api is None or not self.api.account_id:
                return f"Not possible yet: {self.problem}."
            try:
                await self.api.deploy()
            except ApiError as exc:
                return f"MetaApi did not accept it: {exc.message}"
            self.next_connect = 0.0
            return "Asked MetaApi to switch the account on. It takes a few minutes; I will message when connected."
        return "Unknown command. Send /help."


HELP = (
    f"GoldBot v{__version__} - 6-hour breakout on gold, 1-6 pm Lagos, max 2 trades a day.\n\n"
    "/status - mode, balance, lot, open trade\n"
    "/live on - trade the real account\n"
    "/live off - practice only (no orders)\n"
    "/lot 0.02 - fixed lot · /lot auto - scale with balance\n"
    "/per 40 - balance needed for each 0.01 lot\n"
    "/goal 200 - target balance\n"
    "/trades - last 10 trades\n"
    "/close - close the open trade now\n"
    "/deploy - switch the account on at MetaApi"
)


def S_parse(value: Any) -> Optional[float]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None
