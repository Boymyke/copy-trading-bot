"""Low-frequency mirror monitor (read-only).

MetaApi CopyFactory does all execution: it opens the target ``XAUUSDm`` trade
when a source ``XAUUSD.f`` trade opens and closes it when the source closes.
This module never sends a trade. Once every ``MONITOR_POLL_SECONDS`` (default
60 s) it:

1. reads open positions on the source and the target account (2 REST calls),
2. reports opens/closes on both sides and the final P/L of closed target trades,
3. raises an alert if the source and target stay out of sync (e.g. a source
   trade with no copied target trade) for two polls in a row,
4. reads the CopyFactory subscriber user log (separate CopyFactory host, not
   the trading-API credit budget) and reports CopyFactory warnings/errors.

On HTTP 429 it waits for MetaApi's recommended retry time instead of retrying,
so it can never exhaust the trading-API credit budget again.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .config import Settings
from .logging_setup import get_logger
from .metaapi_rest import MetaApiRestClient, MetaApiRestError
from .notifier import Notifier
from .storage import Store

log = get_logger("monitor")

FAILURE_NOTIFY_AFTER = 3
FAILURE_RENOTIFY_SECONDS = 1800.0
MAX_RATE_LIMIT_WAIT = 1800.0
STATE_KEY = "mirror_state_v1"


def _side(position: dict) -> str:
    return "BUY" if str(position.get("type", "")).upper().endswith("BUY") else "SELL"


def _money(value: Any) -> str:
    if value is None:
        return "—"
    v = float(value)
    return f"{'+' if v >= 0 else '-'}${abs(v):.2f}"


def _parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _summary(p: dict) -> dict:
    return {
        "id": str(p.get("id")),
        "symbol": p.get("symbol"),
        "side": _side(p),
        "volume": float(p.get("volume") or 0),
        "openPrice": p.get("openPrice"),
        "currentPrice": p.get("currentPrice"),
        "profit": p.get("profit"),
        "stopLoss": p.get("stopLoss"),
        "takeProfit": p.get("takeProfit"),
        "time": str(p.get("time") or ""),
        "comment": p.get("comment"),
        "clientId": p.get("clientId"),
    }


class MirrorMonitor:
    def __init__(self, settings: Settings, store: Store, rest: MetaApiRestClient, notifier: Notifier, copyfactory: Any):
        self.settings = settings
        self.store = store
        self.rest = rest
        self.notifier = notifier
        self.copyfactory = copyfactory  # CopyFactoryService (for user log)

        self.online = False
        self.last_ok_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self.consecutive_failures = 0
        self.total_failures = 0
        self.rate_limited_until: Optional[float] = None
        self._failure_notified_at: Optional[float] = None

        self.source: dict[str, dict] = {}
        self.target: dict[str, dict] = {}
        self.other_target_positions = 0
        self.out_of_sync_polls = 0
        self.sync_alerted = False
        self.sync_detail: Optional[str] = None
        self._bad_volume_alerted: set[str] = set()
        self._user_log_since: datetime = datetime.now(timezone.utc) - timedelta(minutes=10)
        self._user_log_seen: set[str] = set()
        self._loaded = self._load_state()

    # -- persistence ----------------------------------------------------------------

    def _load_state(self) -> bool:
        raw = self.store.get(STATE_KEY)
        if not raw:
            return False
        try:
            data = json.loads(raw)
            self.source = data.get("source") or {}
            self.target = data.get("target") or {}
            return True
        except (ValueError, TypeError):
            return False

    def _save_state(self) -> None:
        self.store.set(STATE_KEY, json.dumps({"source": self.source, "target": self.target}))

    # -- polling --------------------------------------------------------------------

    async def cycle(self) -> None:
        s = self.settings
        source_positions = await self.rest.get_positions(s.source_account_id)
        target_positions = await self.rest.get_positions(s.target_account_id)

        new_source = {str(p.get("id")): _summary(p) for p in source_positions if p.get("symbol") == s.copy_symbol}
        new_target = {str(p.get("id")): _summary(p) for p in target_positions if p.get("symbol") == s.target_symbol}
        self.other_target_positions = sum(1 for p in target_positions if p.get("symbol") != s.target_symbol)

        if not self._loaded:
            # First run after a deploy: adopt what is open without re-announcing it.
            self._loaded = True
            self.source, self.target = new_source, new_target
            self._save_state()
            self.store.event(
                "info",
                "monitor",
                f"Mirror monitor started: {len(new_source)} open {s.copy_symbol} on source, "
                f"{len(new_target)} open {s.target_symbol} on target",
            )
        else:
            old_source, self.source = self.source, new_source
            self._diff("source", old_source, new_source)
            old_target, self.target = self.target, new_target
            self._diff("target", old_target, new_target)
            self._save_state()

        self._check_volumes()
        self._check_sync()
        await self._read_user_log()

    def _diff(self, side: str, old: dict, new: dict) -> None:
        s = self.settings
        for pid, p in new.items():
            if pid in old:
                continue
            if side == "source":
                self.store.event("info", "source-open", f"Source opened {p['side']} {p['symbol']} {p['volume']:g} @ {p['openPrice']} (#{pid})", **p)
                self.notifier.notify(
                    f"📥 Source opened {p['side']} {p['symbol']} {p['volume']:g} @ {p['openPrice']}\n#{pid}\n"
                    f"CopyFactory should open {s.target_symbol} {s.fixed_lot:g} on the target.",
                    kind="source-open",
                )
            else:
                lag = self._copy_lag(p)
                self.store.event("info", "target-open", f"Target copied {p['side']} {p['symbol']} {p['volume']:g} @ {p['openPrice']} (#{pid})", lag_seconds=lag, **p)
                self.notifier.notify(
                    f"✅ Target trade opened by CopyFactory\n{p['side']} {p['symbol']} {p['volume']:g} @ {p['openPrice']}\n#{pid}"
                    + (f"\nCopied ~{lag:.1f}s after source" if lag is not None else ""),
                    kind="target-open",
                )
        for pid, p in old.items():
            if pid in new:
                continue
            if side == "source":
                self.store.event("info", "source-close", f"Source closed {p['side']} {p['symbol']} {p['volume']:g} (#{pid})", **p)
                self.notifier.notify(
                    f"📤 Source closed {p['side']} {p['symbol']} {p['volume']:g}\n#{pid}\n"
                    f"CopyFactory should close the matching {s.target_symbol} trade.",
                    kind="source-close",
                )
            else:
                asyncio.create_task(self._report_target_close(p), name=f"target-close-{pid}")

    def _copy_lag(self, target: dict) -> Optional[float]:
        t_open = _parse_time(target.get("time"))
        if not t_open:
            return None
        candidates = [
            _parse_time(p.get("time")) for p in self.source.values() if p.get("side") == target.get("side")
        ]
        diffs = [(t_open - c).total_seconds() for c in candidates if c and (t_open - c).total_seconds() >= -5]
        return round(min(diffs), 1) if diffs else None

    async def _report_target_close(self, p: dict) -> None:
        pid = p["id"]
        final: Optional[float] = None
        source = "last floating P/L"
        try:
            deals = await self.rest.get_deals_by_position(self.settings.target_account_id, pid)
            if any(str(d.get("entryType", "")).endswith(("OUT", "OUT_BY", "INOUT")) for d in deals):
                final = round(sum(float(d.get("profit") or 0) + float(d.get("swap") or 0) + float(d.get("commission") or 0) for d in deals), 2)
                source = "deal history"
        except MetaApiRestError as exc:
            log.warning("deal_history_failed", position_id=pid, error=str(exc))
        if final is None and p.get("profit") is not None:
            final = float(p["profit"])
        self.store.event("info", "target-close", f"Target closed {p['side']} {p['symbol']} {p['volume']:g} (#{pid}) P/L {_money(final)}", final_profit=final, source=source)
        self.notifier.notify(
            f"✅ Target trade closed by CopyFactory\n{p['side']} {p['symbol']} {p['volume']:g} #{pid}\n"
            f"Entry {p['openPrice']}\nFinal P/L: {_money(final)} ({source})",
            kind="target-close",
        )

    def _check_volumes(self) -> None:
        for pid, p in self.target.items():
            if pid in self._bad_volume_alerted:
                continue
            if not math.isclose(p["volume"], self.settings.fixed_lot, abs_tol=1e-8):
                self._bad_volume_alerted.add(pid)
                self.store.event("warning", "lot-mismatch", f"Target position #{pid} is {p['volume']:g} lot, expected {self.settings.fixed_lot:g}")
                self.notifier.notify(f"⚠️ Target position #{pid} is {p['volume']:g} lot (expected {self.settings.fixed_lot:g}).", kind="lot-mismatch")
        self._bad_volume_alerted &= set(self.target)

    def _check_sync(self) -> None:
        def counts(d: dict) -> dict:
            out = {"BUY": 0, "SELL": 0}
            for p in d.values():
                out[p["side"]] += 1
            return out

        src, tgt = counts(self.source), counts(self.target)
        if src == tgt:
            if self.sync_alerted:
                self.store.event("info", "sync", "Source and target are back in sync")
                self.notifier.notify("🟢 Source and target are back in sync.", kind="sync")
            self.out_of_sync_polls = 0
            self.sync_alerted = False
            self.sync_detail = None
            return
        self.out_of_sync_polls += 1
        self.sync_detail = f"source BUY {src['BUY']}/SELL {src['SELL']} vs target BUY {tgt['BUY']}/SELL {tgt['SELL']}"
        if self.out_of_sync_polls >= 2 and not self.sync_alerted:
            self.sync_alerted = True
            self.store.event("warning", "sync", f"Out of sync for {self.out_of_sync_polls} polls: {self.sync_detail}")
            self.notifier.notify(
                f"⚠️ Source and target out of sync\n{self.sync_detail}\n"
                "Check CopyFactory (/status) — the bot does not open or close trades itself.",
                kind="sync",
            )

    async def _read_user_log(self) -> None:
        since = self._user_log_since
        now = datetime.now(timezone.utc)
        try:
            records = await self.copyfactory.get_user_log(since)
        except Exception as exc:  # informational only
            log.warning("user_log_failed", error=f"{type(exc).__name__}: {exc}")
            return
        self._user_log_since = now - timedelta(seconds=5)
        for r in reversed(records or []):
            key = f"{r.get('time')}|{r.get('positionId')}|{r.get('message')}"
            if key in self._user_log_seen:
                continue
            self._user_log_seen.add(key)
            level = str(r.get("level") or "INFO").upper()
            fields = {k: r.get(k) for k in ("symbol", "positionId", "side", "type", "openPrice", "strategyName")}
            log.info("copyfactory_user_log", cf_level=level, cf_message=r.get("message"), time=r.get("time"), **fields)
            if level in ("WARN", "ERROR"):
                self.store.event("warning" if level == "WARN" else "error", "copyfactory-log", str(r.get("message"))[:500])
                self.notifier.notify(f"{'⚠️' if level == 'WARN' else '🔴'} CopyFactory {level}: {r.get('message')}", kind="copyfactory-log")
        if len(self._user_log_seen) > 2000:
            self._user_log_seen = set(list(self._user_log_seen)[-500:])

    # -- loop -------------------------------------------------------------------------

    async def run(self) -> None:
        log.info("monitor_start", poll_seconds=self.settings.monitor_poll_seconds, execution="MetaApi CopyFactory")
        while True:
            try:
                await self.cycle()
                self._on_success()
                await asyncio.sleep(self.settings.monitor_poll_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await asyncio.sleep(self._on_failure(exc))

    def _on_success(self) -> None:
        if self.consecutive_failures >= FAILURE_NOTIFY_AFTER:
            self.store.event("info", "monitor", f"Monitoring recovered after {self.consecutive_failures} failed polls")
            if self._failure_notified_at is not None:
                self.notifier.notify("🟢 Monitoring recovered.", kind="monitor-recovered")
        elif self.last_ok_at is None:
            self.store.event("info", "monitor", "Mirror monitoring online")
        self.online = True
        self.last_ok_at = time.time()
        self.last_error = None
        self.rate_limited_until = None
        self.consecutive_failures = 0
        self._failure_notified_at = None

    def _on_failure(self, exc: Exception) -> float:
        self.consecutive_failures += 1
        self.total_failures += 1
        self.last_error = f"{type(exc).__name__}: {exc}"
        outage = self.consecutive_failures >= FAILURE_NOTIFY_AFTER
        if outage or self.last_ok_at is None:
            self.online = False
        is_rest = isinstance(exc, MetaApiRestError)
        delay = min(300.0, self.settings.monitor_poll_seconds * (2 ** min(self.consecutive_failures - 1, 3)))
        if is_rest and exc.status == 429:
            wait = exc.retry_after if exc.retry_after else 600.0
            delay = max(delay, min(wait, MAX_RATE_LIMIT_WAIT))
            self.rate_limited_until = time.time() + delay
        elif not is_rest:
            log.exception("monitor_cycle_crashed", error=self.last_error)
        (log.error if outage else log.warning)(
            "monitor_poll_failed",
            error=self.last_error,
            endpoint=getattr(exc, "endpoint", None),
            status=getattr(exc, "status", None),
            consecutive_failures=self.consecutive_failures,
            retry_in=round(delay),
        )
        if self.consecutive_failures == FAILURE_NOTIFY_AFTER or (is_rest and exc.status == 429 and self.consecutive_failures == 1):
            self.store.event("error", "monitor", f"Monitoring poll failing ({self.consecutive_failures}x): {exc}")
        now = time.time()
        if outage and (self._failure_notified_at is None or now - self._failure_notified_at >= FAILURE_RENOTIFY_SECONDS):
            self._failure_notified_at = now
            self.notifier.notify(
                f"🔴 Monitoring cannot read positions ({self.consecutive_failures}x): {exc}\n"
                "CopyFactory keeps copying independently. Retrying automatically.",
                kind="monitor-failure",
            )
        return delay

    def state(self) -> dict[str, Any]:
        stale_after = self.settings.monitor_poll_seconds * 3 + 60
        healthy = bool(self.online and self.last_ok_at and time.time() - self.last_ok_at < stale_after)
        return {
            "online": healthy,
            "pollSeconds": self.settings.monitor_poll_seconds,
            "lastOkAt": self.last_ok_at,
            "lastError": self.last_error,
            "consecutiveFailures": self.consecutive_failures,
            "totalFailures": self.total_failures,
            "rateLimitedUntil": self.rate_limited_until,
            "restRequests": self.rest.request_count,
            "restErrors": self.rest.error_count,
            "sourcePositions": list(self.source.values()),
            "targetPositions": list(self.target.values()),
            "otherTargetPositions": self.other_target_positions,
            "inSync": self.out_of_sync_polls == 0,
            "syncDetail": self.sync_detail,
        }
