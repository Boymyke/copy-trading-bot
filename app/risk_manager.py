import asyncio
import math
from typing import Any, Dict, Optional

from metaapi_cloud_sdk import MetaApi

from .config import Settings
from .storage import Store


class RiskManager:
    """Applies the user's independent target-side SL/trailing rules.

    CopyFactory owns trade replication. This service only watches the dedicated
    target account and modifies Gold positions that match the configured fixed lot.
    Stop losses are broker-side once written, so they remain active if this process
    or its monitoring connection temporarily restarts.
    """

    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.api = MetaApi(token=settings.metaapi_token)
        self.connection = None
        self.connected = False
        self.last_error: Optional[str] = None
        self.last_positions: list[dict] = []

    @staticmethod
    def _side(position: Dict[str, Any]) -> str:
        ptype = str(position.get("type", "")).upper()
        return "BUY" if ptype.endswith("BUY") else "SELL"

    @staticmethod
    def _tick_metrics(spec: Dict[str, Any]) -> tuple[float, float]:
        tick_size = float(spec.get("tickSize") or spec.get("point") or 0)
        tick_value = float(spec.get("tickValueLoss") or spec.get("tickValue") or spec.get("tickValueProfit") or 0)
        if tick_size <= 0 or tick_value <= 0:
            raise RuntimeError("Broker specification does not expose tickSize/tickValue")
        return tick_size, tick_value

    def _money_distance(self, spec: Dict[str, Any], volume: float, money: float) -> float:
        tick_size, tick_value = self._tick_metrics(spec)
        return (max(0.0, money) / (max(volume, 1e-12) * tick_value)) * tick_size

    @staticmethod
    def _round_price(spec: Dict[str, Any], price: float) -> float:
        digits = int(spec.get("digits") or 2)
        return round(float(price), digits)

    def _configured(self, key: str, default: float) -> float:
        return float(self.store.get(key, str(default)))

    def _is_managed_position(self, position: Dict[str, Any]) -> bool:
        if str(position.get("symbol")) != self.settings.target_symbol:
            return False
        expected = float(self.store.get("lot_size", str(self.settings.fixed_lot)))
        volume = float(position.get("volume") or 0)
        return math.isclose(volume, expected, rel_tol=0, abs_tol=1e-8)

    async def _connect(self):
        account = await self.api.metatrader_account_api.get_account(self.settings.target_account_id)
        if account.state != "DEPLOYED":
            await account.deploy()
        await account.wait_connected()
        connection = account.get_rpc_connection()
        await connection.connect()
        await connection.wait_synchronized()
        self.connection = connection
        self.connected = True
        self.last_error = None
        self.store.event("info", "risk", "Target RPC risk manager connected")

    async def _disconnect(self):
        self.connected = False
        if self.connection is not None:
            try:
                await self.connection.close()
            except Exception:
                pass
        self.connection = None

    async def _safe_stop(self, position: Dict[str, Any], spec: Dict[str, Any], desired: float) -> float:
        """Clamp stop to the broker's minimum stop distance when the broker exposes it."""
        price = await self.connection.get_symbol_price(self.settings.target_symbol)
        if not price:
            return self._round_price(spec, desired)
        side = self._side(position)
        point = float(spec.get("point") or (10 ** -int(spec.get("digits") or 2)))
        stops_level = float(spec.get("stopsLevel") or spec.get("tradeStopsLevel") or 0)
        min_distance = max(0.0, stops_level * point)
        bid = float(price.get("bid") or 0)
        ask = float(price.get("ask") or 0)
        if side == "BUY" and bid:
            desired = min(desired, bid - min_distance)
        elif side == "SELL" and ask:
            desired = max(desired, ask + min_distance)
        return self._round_price(spec, desired)

    async def _ensure_initial_sl(self, position: Dict[str, Any], spec: Dict[str, Any]) -> float:
        pid = str(position.get("id"))
        side = self._side(position)
        volume = float(position.get("volume") or 0)
        open_price = float(position.get("openPrice") or 0)
        current_sl = float(position.get("stopLoss") or 0)
        risk_money = self._configured("initial_sl_usd", self.settings.initial_sl_usd) * (volume / 0.01)
        distance = self._money_distance(spec, volume, risk_money)
        desired = open_price - distance if side == "BUY" else open_price + distance
        desired = await self._safe_stop(position, spec, desired)

        needs = current_sl <= 0
        if current_sl > 0:
            # Never widen an existing SL. Only improve it.
            needs = desired > current_sl if side == "BUY" else desired < current_sl
        if needs:
            await self.connection.modify_position(pid, desired, None)
            self.store.event("info", "initial-sl", f"Position {pid} SL set to {desired}")
            return desired
        return current_sl

    async def _trail(self, position: Dict[str, Any], spec: Dict[str, Any], current_sl: float) -> float:
        pid = str(position.get("id"))
        side = self._side(position)
        volume = float(position.get("volume") or 0)
        open_price = float(position.get("openPrice") or 0)
        profit = float(position.get("profit") or 0)
        scale = volume / 0.01
        trigger = self._configured("trail_trigger_usd", self.settings.trail_trigger_usd) * scale
        gap = self._configured("trail_gap_usd", self.settings.trail_gap_usd) * scale
        step = self._configured("trail_step_usd", self.settings.trail_step_usd) * scale
        if profit < trigger:
            return current_sl

        locked_money = max(0.0, profit - gap)
        locked_distance = self._money_distance(spec, volume, locked_money)
        desired = open_price + locked_distance if side == "BUY" else open_price - locked_distance
        desired = await self._safe_stop(position, spec, desired)

        if current_sl:
            existing_distance = (current_sl - open_price) if side == "BUY" else (open_price - current_sl)
            tick_size, tick_value = self._tick_metrics(spec)
            existing_locked = max(0.0, (existing_distance / tick_size) * tick_value * volume)
        else:
            existing_locked = 0.0

        improves = (side == "BUY" and desired > current_sl) or (side == "SELL" and (current_sl <= 0 or desired < current_sl))
        if not improves or locked_money < existing_locked + step:
            return current_sl

        await self.connection.modify_position(pid, desired, None)
        self.store.event("info", "trailing-sl", f"Position {pid} SL -> {desired}; locked about ${locked_money:.2f}")
        return desired

    async def cycle(self):
        positions = await self.connection.get_positions()
        managed = [p for p in positions if self._is_managed_position(p)]
        self.last_positions = managed
        open_ids: set[str] = set()

        for position in managed:
            pid = str(position.get("id"))
            open_ids.add(pid)
            spec = await self.connection.get_symbol_specification(str(position.get("symbol")))
            if not spec:
                raise RuntimeError(f"No broker specification for {position.get('symbol')}")
            current_sl = await self._ensure_initial_sl(position, spec)
            # Refresh after a potential SL modification before trailing calculations.
            try:
                fresh = await self.connection.get_position(pid)
                if fresh:
                    position = fresh
                    current_sl = float(position.get("stopLoss") or current_sl)
            except Exception:
                pass
            current_sl = await self._trail(position, spec, current_sl)
            self.store.upsert_position(position, current_sl, current_sl, float(position.get("profit") or 0))

        self.store.close_missing_positions(open_ids)

    async def run(self):
        while True:
            try:
                if self.connection is None:
                    await self._connect()
                await self.cycle()
                self.connected = True
                self.last_error = None
                await asyncio.sleep(max(0.25, self.settings.controller_poll_seconds))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.last_error = str(exc)
                self.store.event("error", "risk", str(exc))
                await self._disconnect()
                # Risk management can reconnect independently without affecting
                # CopyFactory's native source->target replication.
                await asyncio.sleep(2)

    def state(self) -> Dict[str, Any]:
        return {
            "connected": self.connected,
            "lastError": self.last_error,
            "positions": self.last_positions,
            "managedPositions": self.store.open_positions(),
            "initialSlUsd": self._configured("initial_sl_usd", self.settings.initial_sl_usd),
            "trailTriggerUsd": self._configured("trail_trigger_usd", self.settings.trail_trigger_usd),
            "trailGapUsd": self._configured("trail_gap_usd", self.settings.trail_gap_usd),
            "trailStepUsd": self._configured("trail_step_usd", self.settings.trail_step_usd),
        }
