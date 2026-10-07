"""Dry-run target risk manager (REST polling, no trade requests).

What it does every ``CONTROLLER_POLL_SECONDS``:

1. Reads the target account's open positions over MetaApi REST.
2. Classifies each one: only ``TARGET_SYMBOL`` positions at the CopyFactory
   lot that were not opened manually are "managed". Everything else is
   reported once as ignored and never touched.
3. For a newly detected managed position it computes the simulated initial SL
   ($ risk per 0.01 lot) using the broker's tick size, tick value, digits,
   stops level and freeze level, and reports the POSITION_MODIFY it *would* send.
4. On every cycle it recomputes the simulated trailing SL and reports each move
   (never widening it), plus whether the simulated SL would have been hit.
5. When a managed position disappears for ``CLOSE_CONFIRM_POLLS`` consecutive
   successful reads, it is marked closed and its final P/L is read from deal history.

There is no code path here (or in ``metaapi_rest``) that sends a trade request.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any, Callable, Optional

from . import risk_math as rm
from .config import Settings
from .logging_setup import get_logger
from .metaapi_rest import MetaApiRestClient, MetaApiRestError
from .notifier import Notifier
from .storage import Store

log = get_logger("risk")

PRICE_IDLE_REFRESH_SECONDS = 30.0
ACCOUNT_INFO_REFRESH_SECONDS = 60.0
FAILURE_NOTIFY_AFTER = 3
FAILURE_RENOTIFY_SECONDS = 900.0


def _fmt(value: Optional[float], digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _money(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{'+' if value >= 0 else '-'}${abs(value):.2f}"


class RiskManager:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        rest: MetaApiRestClient,
        notifier: Notifier,
        expected_lot: Callable[[], float],
    ):
        self.settings = settings
        self.store = store
        self.rest = rest
        self.notifier = notifier
        self.expected_lot = expected_lot
        self.account_id = settings.target_account_id
        self.symbol = settings.target_symbol

        self.online = False
        self.started_at = time.time()
        self.last_cycle_ok_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self.consecutive_failures = 0
        self.total_failures = 0
        self._failure_notified_at: Optional[float] = None

        self.spec: Optional[dict] = None
        self.rules: Optional[rm.SymbolRules] = None
        self._spec_fetched_at = 0.0
        self.price: Optional[dict] = None
        self._price_fetched_at = 0.0
        self.account_info: Optional[dict] = None
        self._account_task: Optional[asyncio.Task] = None
        self._account_info_fetched_at = 0.0

        self.target_positions_total = 0
        self.ignored: dict[str, dict] = {}
        self._last_pnl_notify: dict[str, float] = {}
        self._closing: set[str] = set()

    # -- configuration helpers ------------------------------------------------------

    def _cfg(self, key: str, default: float) -> float:
        return self.store.get_float(key, default)

    @property
    def initial_sl_usd(self) -> float:
        return self._cfg("initial_sl_usd", self.settings.initial_sl_usd)

    @property
    def trail_trigger_usd(self) -> float:
        return self._cfg("trail_trigger_usd", self.settings.trail_trigger_usd)

    @property
    def trail_gap_usd(self) -> float:
        return self._cfg("trail_gap_usd", self.settings.trail_gap_usd)

    @property
    def trail_step_usd(self) -> float:
        return self._cfg("trail_step_usd", self.settings.trail_step_usd)

    # -- REST reads -------------------------------------------------------------------

    async def _refresh_spec(self, force: bool = False) -> None:
        if not force and self.rules and time.time() - self._spec_fetched_at < self.settings.spec_cache_seconds:
            return
        spec = await self.rest.get_symbol_specification(self.account_id, self.symbol)
        rules = rm.SymbolRules.from_spec(spec)
        first = self.rules is None
        self.spec, self.rules, self._spec_fetched_at = spec, rules, time.time()
        if first:
            self.store.event(
                "info",
                "spec",
                f"{self.symbol} specification loaded: digits={rules.digits} tickSize={rules.tick_size:g} "
                f"point={rules.point:g} stopsLevel={rules.stops_level_points:g} freezeLevel={rules.freeze_level_points:g}",
                symbol=self.symbol,
                digits=rules.digits,
                tick_size=rules.tick_size,
                point=rules.point,
                stops_level=rules.stops_level_points,
                freeze_level=rules.freeze_level_points,
                contract_size=spec.get("contractSize"),
                min_volume=spec.get("minVolume"),
                volume_step=spec.get("volumeStep"),
                trade_mode=spec.get("tradeMode"),
            )

    async def _refresh_price(self, force: bool = False) -> dict:
        if force or not self.price or time.time() - self._price_fetched_at >= PRICE_IDLE_REFRESH_SECONDS:
            first = self.price is None
            price = await self.rest.get_symbol_price(self.account_id, self.symbol)
            if not price.get("bid") or not price.get("ask"):
                raise RuntimeError(f"price for {self.symbol} has no bid/ask: {price}")
            self.price, self._price_fetched_at = price, time.time()
            if first:
                log.info(
                    "price_ok",
                    symbol=self.symbol,
                    bid=price.get("bid"),
                    ask=price.get("ask"),
                    loss_tick_value=price.get("lossTickValue"),
                    profit_tick_value=price.get("profitTickValue"),
                )
        return self.price

    async def _refresh_account_info(self) -> None:
        if time.time() - self._account_info_fetched_at < ACCOUNT_INFO_REFRESH_SECONDS:
            return
        self._account_info_fetched_at = time.time()  # do not hammer on failure
        try:
            info = await self.rest.get_account_information(self.account_id)
        except MetaApiRestError as exc:
            log.warning("account_info_failed", error=str(exc))
            return
        first = self.account_info is None
        self.account_info = info
        if first:
            log.info(
                "account_info_ok",
                broker=info.get("broker"),
                server=info.get("server"),
                currency=info.get("currency"),
                balance=info.get("balance"),
                equity=info.get("equity"),
                trade_allowed=info.get("tradeAllowed"),
            )

    # -- classification --------------------------------------------------------------

    def _ignore_reason(self, position: dict) -> Optional[str]:
        pid = str(position.get("id"))
        tracked = self.store.get_tracked(pid)
        if tracked and tracked.get("status") == "open":
            return None  # already managed: keep managing even if the lot setting changed
        if str(position.get("symbol")) != self.symbol:
            return f"symbol {position.get('symbol')} is not {self.symbol}"
        expected = self.expected_lot()
        volume = float(position.get("volume") or 0)
        if not math.isclose(volume, expected, rel_tol=0, abs_tol=1e-8):
            return f"volume {volume:g} is not the CopyFactory lot {expected:g}"
        reason = str(position.get("reason") or "")
        if reason and reason in self.settings.managed_exclude_reasons:
            return f"opened manually ({reason})"
        return None

    def _note_ignored(self, position: dict, why: str) -> None:
        pid = str(position.get("id"))
        if pid in self.ignored:
            self.ignored[pid]["last_seen"] = time.time()
            return
        self.ignored[pid] = {
            "id": pid,
            "symbol": position.get("symbol"),
            "type": position.get("type"),
            "volume": position.get("volume"),
            "why": why,
            "last_seen": time.time(),
        }
        self.store.event(
            "info",
            "ignored-position",
            f"Not managing position {pid} {position.get('symbol')} {position.get('volume')}: {why}",
            position_id=pid,
            magic=position.get("magic"),
            client_id=position.get("clientId"),
            comment=position.get("comment"),
            reason=position.get("reason"),
        )

    # -- main cycle -----------------------------------------------------------------

    async def cycle(self) -> None:
        positions = await self.rest.get_positions(self.account_id)
        self.target_positions_total = len(positions)

        # Always prove the symbol is readable, even when flat.
        await self._refresh_spec()
        # Account info is informational only: never let a slow read delay SL maths.
        if self._account_task is None or self._account_task.done():
            self._account_task = asyncio.create_task(self._refresh_account_info(), name="account-info")

        managed: list[dict] = []
        seen_ids: set[str] = set()
        for position in positions:
            pid = str(position.get("id"))
            seen_ids.add(pid)
            why = self._ignore_reason(position)
            if why:
                self._note_ignored(position, why)
            else:
                managed.append(position)
        for stale in [pid for pid in self.ignored if pid not in seen_ids]:
            self.ignored.pop(stale, None)

        price = await self._refresh_price(force=bool(managed))
        managed_ids = set()
        for position in managed:
            managed_ids.add(str(position.get("id")))
            await self._process(position, price)

        await self._detect_closes(managed_ids)

    async def _process(self, position: dict, price: dict) -> None:
        assert self.rules is not None
        rules = self.rules
        pid = str(position.get("id"))
        side = rm.side_of(position)
        volume = float(position.get("volume") or 0)
        open_price = float(position.get("openPrice") or 0)
        bid = float(price.get("bid") or 0)
        ask = float(price.get("ask") or 0)
        fallback_tv = float(position.get("currentTickValue") or 0)
        tv_loss = float(price.get("lossTickValue") or fallback_tv)
        tv_profit = float(price.get("profitTickValue") or fallback_tv)
        if tv_loss <= 0 or tv_profit <= 0:
            raise RuntimeError(f"no tick value available for {self.symbol} (price={price}, position={pid})")
        broker_profit = float(position.get("profit") or 0)
        broker_sl = float(position.get("stopLoss") or 0) or None
        now = self.store.now()

        row = self.store.get_tracked(pid)
        if row is None:
            plan = rm.plan_initial_sl(
                side=side,
                open_price=open_price,
                volume=volume,
                risk_per_base_lot=self.initial_sl_usd,
                rules=rules,
                tick_value_loss=tv_loss,
                bid=bid,
                ask=ask,
            )
            row = {
                "position_id": pid,
                "symbol": str(position.get("symbol")),
                "side": side,
                "volume": volume,
                "open_price": open_price,
                "open_time": str(position.get("time") or ""),
                "magic": str(position.get("magic") or ""),
                "client_id": str(position.get("clientId") or ""),
                "comment": str(position.get("comment") or ""),
                "reason": str(position.get("reason") or ""),
                "simulated_initial_sl": plan.stop_loss,
                "simulated_sl": plan.stop_loss,
                "trail_active": 0,
                "locked_money": plan.money_at_sl,
                "status": "open",
                "first_seen": now,
                "missing_polls": 0,
                "notes": ",".join(plan.notes),
            }
            self._log_intended_modify(pid, plan.stop_loss, "initial_sl", notes=plan.notes)
            self.store.event(
                "info",
                "trade-detected",
                f"Managed {side} {self.symbol} {volume:g} @ {open_price} detected; simulated SL {plan.stop_loss}",
                position_id=pid,
                side=side,
                volume=volume,
                entry=open_price,
                simulated_sl=plan.stop_loss,
                risk_money=plan.money_at_sl,
                notes=plan.notes,
                broker_sl=broker_sl,
                magic=position.get("magic"),
                client_id=position.get("clientId"),
                comment=position.get("comment"),
                reason=position.get("reason"),
            )
            note_line = f"\nNotes: {', '.join(plan.notes)}" if plan.notes else ""
            self.notifier.notify(
                "🟡 DRY RUN — target trade detected\n\n"
                f"{side} {self.symbol} {volume:g} lot\n"
                f"Entry: {open_price}\n"
                f"Position #{pid}\n"
                f"Simulated initial SL: {_fmt(plan.stop_loss, rules.digits)} (risk {_money(plan.money_at_sl)})\n"
                f"Broker SL on position: {_fmt(broker_sl, rules.digits) if broker_sl else 'none'}\n"
                f"Floating P/L: {_money(broker_profit)}"
                f"{note_line}\n\n"
                "No order was sent. This is what the live manager would set.",
                kind="trade-detected",
            )
            self._last_pnl_notify[pid] = time.time()

        current_sl = row.get("simulated_sl")
        trail = rm.plan_trailing(
            side=side,
            open_price=open_price,
            volume=volume,
            current_sl=current_sl,
            rules=rules,
            tick_value_profit=tv_profit,
            bid=bid,
            ask=ask,
            trigger_per_base_lot=self.trail_trigger_usd,
            gap_per_base_lot=self.trail_gap_usd,
            step_per_base_lot=self.trail_step_usd,
        )
        if trail.move:
            new_sl = rm.tighter(side, current_sl, trail.stop_loss)  # never widen
            if new_sl != current_sl:
                row["simulated_sl"] = new_sl
                row["trail_active"] = 1
                row["locked_money"] = trail.locked_money
                self._log_intended_modify(pid, new_sl, "trailing_sl", previous=current_sl, notes=trail.notes)
                self.store.event(
                    "info",
                    "trailing-sl",
                    f"Position {pid} simulated SL {current_sl} -> {new_sl} (locks {_money(trail.locked_money)})",
                    position_id=pid,
                    previous_sl=current_sl,
                    new_sl=new_sl,
                    locked_money=trail.locked_money,
                    floating_money=round(trail.floating_money, 2),
                    bid=bid,
                    ask=ask,
                )
                self.notifier.notify(
                    "🟡 DRY RUN — simulated trailing SL moved\n\n"
                    f"{side} {self.symbol} {volume:g} #{pid}\n"
                    f"SL: {_fmt(current_sl, rules.digits)} → {_fmt(new_sl, rules.digits)}\n"
                    f"Locks in: {_money(trail.locked_money)}\n"
                    f"Floating P/L: {_money(broker_profit)} (price-based {_money(trail.floating_money)})\n"
                    f"Bid/Ask: {bid}/{ask}",
                    kind="trailing-sl",
                )

        sim_sl = row.get("simulated_sl")
        if sim_sl and not row.get("sl_hit_at") and rm.sl_breached(side, sim_sl, bid, ask):
            hit_money = rm.money_at_price(side, open_price, sim_sl, volume, tv_profit if row.get("trail_active") else tv_loss, rules.tick_size)
            row["sl_hit_at"] = now
            row["sl_hit_price"] = rm.close_price(side, bid, ask)
            row["sl_hit_money"] = round(hit_money, 2)
            self.store.event(
                "warning",
                "simulated-sl-hit",
                f"Position {pid}: simulated SL {sim_sl} would have been hit (≈{_money(hit_money)}); real position still open",
                position_id=pid,
                simulated_sl=sim_sl,
                bid=bid,
                ask=ask,
            )
            self.notifier.notify(
                "🔶 DRY RUN — simulated SL would have been hit\n\n"
                f"{side} {self.symbol} {volume:g} #{pid}\n"
                f"Simulated SL: {_fmt(sim_sl, rules.digits)}  |  Bid/Ask: {bid}/{ask}\n"
                f"Simulated exit P/L: ≈{_money(hit_money)}\n"
                f"Real floating P/L: {_money(broker_profit)}\n\n"
                "The real position is still open because nothing is sent in dry-run.",
                kind="simulated-sl-hit",
            )

        row.update(
            {
                "floating_money": round(trail.floating_money, 2),
                "broker_profit": broker_profit,
                "broker_sl": broker_sl,
                "last_bid": bid,
                "last_ask": ask,
                "last_seen": now,
                "missing_polls": 0,
                "status": "open",
            }
        )
        self.store.save_tracked(row)

        interval = self.settings.pnl_update_seconds
        if interval > 0 and time.time() - self._last_pnl_notify.get(pid, 0) >= interval:
            self._last_pnl_notify[pid] = time.time()
            self.notifier.notify(
                f"📊 {side} {self.symbol} {volume:g} #{pid}\n"
                f"Floating P/L: {_money(broker_profit)}  |  Bid/Ask: {bid}/{ask}\n"
                f"Simulated SL: {_fmt(row.get('simulated_sl'), rules.digits)} "
                f"({'trailing' if row.get('trail_active') else 'initial'}; trail starts at {_money(self.trail_trigger_usd * rm.scale_for(volume))})",
                kind="pnl",
            )

    def _log_intended_modify(self, position_id: str, stop_loss: Optional[float], purpose: str, **fields: Any) -> None:
        log.info(
            "dry_run_intended_action",
            dry_run=True,
            action="POSITION_MODIFY",
            purpose=purpose,
            account_id=self.account_id,
            position_id=position_id,
            stop_loss=stop_loss,
            take_profit=None,
            sent=False,
            **fields,
        )

    async def _detect_closes(self, managed_ids: set[str]) -> None:
        for row in self.store.open_tracked():
            pid = row["position_id"]
            if pid in managed_ids or pid in self._closing:
                continue
            missing = int(row.get("missing_polls") or 0) + 1
            if missing < self.settings.close_confirm_polls:
                self.store.save_tracked({"position_id": pid, **_required(row), "missing_polls": missing})
                continue
            self._closing.add(pid)
            closed_row = {**row, "status": "closed", "closed_at": self.store.now(), "missing_polls": missing}
            self.store.save_tracked(closed_row)
            self._last_pnl_notify.pop(pid, None)
            asyncio.create_task(self._finalize_close(closed_row), name=f"finalize-{pid}")

    async def _finalize_close(self, row: dict) -> None:
        pid = row["position_id"]
        final: Optional[float] = None
        source = "last floating P/L (deal history unavailable)"
        try:
            for attempt in range(6):
                try:
                    deals = await self.rest.get_deals_by_position(self.account_id, pid)
                except MetaApiRestError as exc:
                    log.warning("deal_history_failed", position_id=pid, attempt=attempt + 1, error=str(exc))
                    deals = []
                exits = [d for d in deals if str(d.get("entryType", "")).endswith(("OUT", "OUT_BY", "INOUT"))]
                if exits:
                    final = round(
                        sum(float(d.get("profit") or 0) + float(d.get("swap") or 0) + float(d.get("commission") or 0) for d in deals),
                        2,
                    )
                    source = "deal history (profit + swap + commission)"
                    break
                await asyncio.sleep(5)
            if final is None and row.get("broker_profit") is not None:
                final = float(row["broker_profit"])
            self.store.save_tracked({**_required(row), "position_id": pid, "final_profit": final})
            sim = ""
            if row.get("sl_hit_at"):
                sim = f"\nSimulated SL would have exited at ≈{_money(row.get('sl_hit_money'))}"
            elif row.get("trail_active"):
                sim = f"\nSimulated trailing SL was locking {_money(row.get('locked_money'))}"
            self.store.event(
                "info",
                "trade-closed",
                f"Managed position {pid} closed; final P/L {_money(final)} from {source}",
                position_id=pid,
                final_profit=final,
                source=source,
            )
            self.notifier.notify(
                "✅ Target trade closed\n\n"
                f"{row['side']} {row['symbol']} {float(row['volume']):g} #{pid}\n"
                f"Entry: {row['open_price']}\n"
                f"Final P/L: {_money(final)}\n"
                f"Source: {source}"
                f"{sim}",
                kind="trade-closed",
            )
        finally:
            self._closing.discard(pid)

    # -- loop -----------------------------------------------------------------------

    async def run(self) -> None:
        log.info(
            "risk_manager_start",
            dry_run=True,
            mode="REST polling",
            base_url=self.rest.base_url,
            account_id=self.account_id,
            symbol=self.symbol,
            poll_seconds=self.settings.controller_poll_seconds,
        )
        self.store.event("info", "risk", f"Dry-run REST risk manager started for {self.symbol}")
        while True:
            started = time.monotonic()
            try:
                await self.cycle()
                self._on_success()
                delay = self.settings.controller_poll_seconds - (time.monotonic() - started)
                await asyncio.sleep(max(0.05, delay))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # retry forever
                delay = self._on_failure(exc)
                await asyncio.sleep(delay)

    def _on_success(self) -> None:
        recovered = self.consecutive_failures >= FAILURE_NOTIFY_AFTER and self._failure_notified_at is not None
        if self.last_cycle_ok_at is None:
            self.store.event("info", "risk", "Target REST monitoring online")
        elif self.consecutive_failures >= FAILURE_NOTIFY_AFTER:
            self.store.event("info", "risk", f"Target REST monitoring back online after {self.consecutive_failures} failed reads")
        self.online = True
        self.last_cycle_ok_at = time.time()
        self.last_error = None
        if recovered:
            self.notifier.notify(
                f"🟢 Target REST monitoring recovered after {self.consecutive_failures} failed reads.",
                kind="rest-recovered",
            )
        self.consecutive_failures = 0
        self._failure_notified_at = None

    def _on_failure(self, exc: Exception) -> float:
        self.consecutive_failures += 1
        self.total_failures += 1
        self.last_error = str(exc)
        # A single 504 from MetaApi ("not connected to broker yet") is common and
        # clears on the next poll; only a run of failures counts as an outage.
        outage = self.consecutive_failures >= FAILURE_NOTIFY_AFTER
        if outage or self.last_cycle_ok_at is None:
            self.online = False
        is_rest = isinstance(exc, MetaApiRestError)
        if is_rest:
            (log.error if outage else log.warning)(
                "risk_cycle_failed", error=str(exc), endpoint=exc.endpoint, status=exc.status,
                consecutive_failures=self.consecutive_failures, outage=outage,
            )
        else:
            log.exception("risk_cycle_crashed", error=str(exc), consecutive_failures=self.consecutive_failures)
        if (not is_rest and self.consecutive_failures == 1) or self.consecutive_failures == FAILURE_NOTIFY_AFTER or self.consecutive_failures % 50 == 0:
            self.store.event("error", "risk", f"Risk cycle failed ({self.consecutive_failures}x): {exc}")
        now = time.time()
        should_notify = self.consecutive_failures >= FAILURE_NOTIFY_AFTER and (
            self._failure_notified_at is None or now - self._failure_notified_at >= FAILURE_RENOTIFY_SECONDS
        )
        if should_notify:
            self._failure_notified_at = now
            self.notifier.notify(
                "🔴 Target REST/API failure\n\n"
                f"{exc}\n\n"
                f"Failed reads in a row: {self.consecutive_failures}. Retrying automatically.",
                kind="rest-failure",
            )
        backoff = min(30.0, 0.5 * (2 ** min(self.consecutive_failures, 6)))
        if is_rest and exc.retry_after:
            backoff = max(backoff, min(exc.retry_after, 120.0))
        return backoff

    # -- reporting --------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        stale_after = max(10.0, self.settings.controller_poll_seconds * 6 + self.settings.rest_timeout_seconds)
        healthy = bool(self.online and self.last_cycle_ok_at and time.time() - self.last_cycle_ok_at < stale_after)
        rules = self.rules
        return {
            "dryRun": True,
            "mode": "REST polling (no RPC/WebSocket)",
            "online": healthy,
            "lastCycleOkAt": self.last_cycle_ok_at,
            "lastError": self.last_error,
            "consecutiveFailures": self.consecutive_failures,
            "totalFailures": self.total_failures,
            "restBaseUrl": self.rest.base_url,
            "restRequests": self.rest.request_count,
            "restErrors": self.rest.error_count,
            "symbol": self.symbol,
            "spec": None
            if not rules
            else {
                "digits": rules.digits,
                "tickSize": rules.tick_size,
                "point": rules.point,
                "stopsLevel": rules.stops_level_points,
                "freezeLevel": rules.freeze_level_points,
                "contractSize": (self.spec or {}).get("contractSize"),
            },
            "price": None
            if not self.price
            else {k: self.price.get(k) for k in ("bid", "ask", "lossTickValue", "profitTickValue", "time")},
            "account": None
            if not self.account_info
            else {k: self.account_info.get(k) for k in ("broker", "server", "currency", "balance", "equity", "margin", "freeMargin")},
            "targetPositionsTotal": self.target_positions_total,
            "managedPositions": self.store.open_tracked(),
            "recentClosed": self.store.recent_closed_tracked(5),
            "ignoredPositions": list(self.ignored.values()),
            "initialSlUsd": self.initial_sl_usd,
            "trailTriggerUsd": self.trail_trigger_usd,
            "trailGapUsd": self.trail_gap_usd,
            "trailStepUsd": self.trail_step_usd,
        }


def _required(row: dict) -> dict:
    """Columns that are NOT NULL in tracked_positions, needed for upserts."""
    return {k: row[k] for k in ("symbol", "side", "volume", "open_price", "first_seen", "last_seen") if k in row}
