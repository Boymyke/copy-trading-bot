"""Pure stop-loss / trailing-stop calculations.

No I/O lives here, so every rule can be unit-tested. All money amounts are in
account currency. MetaApi tick values are per 1.0 lot, i.e.::

    profit = direction * (close_price - open_price) / tick_size * tick_value * volume

Rounding is always done in the *conservative* direction:
- an initial SL is rounded towards the entry, so risk never exceeds the target;
- a trailing SL is rounded towards the entry, so the gap is never smaller than configured.
Only the broker's minimum stop distance (``stopsLevel``) can push an initial SL
further away, and that is reported explicitly in the plan notes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

BASE_LOT = 0.01
_EPS = 1e-9


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    tick_size: float
    digits: int
    point: float
    stops_level_points: float = 0.0
    freeze_level_points: float = 0.0

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> "SymbolRules":
        digits = int(spec.get("digits") if spec.get("digits") is not None else 2)
        point = float(spec.get("point") or 10 ** -digits)
        tick_size = float(spec.get("tickSize") or point)
        if tick_size <= 0 or point <= 0:
            raise ValueError(f"invalid specification for {spec.get('symbol')}: tickSize={tick_size} point={point}")
        return cls(
            symbol=str(spec.get("symbol") or ""),
            tick_size=tick_size,
            digits=digits,
            point=point,
            stops_level_points=float(spec.get("stopsLevel") or 0),
            freeze_level_points=float(spec.get("freezeLevel") or 0),
        )

    @property
    def min_stop_distance(self) -> float:
        return self.stops_level_points * self.point

    @property
    def freeze_distance(self) -> float:
        return self.freeze_level_points * self.point

    def round_price(self, price: float) -> float:
        return round(price, self.digits)

    def floor_tick(self, price: float) -> float:
        return self.round_price(math.floor(price / self.tick_size + _EPS) * self.tick_size)

    def ceil_tick(self, price: float) -> float:
        return self.round_price(math.ceil(price / self.tick_size - _EPS) * self.tick_size)


def direction(side: str) -> int:
    return 1 if side == "BUY" else -1


def side_of(position: dict[str, Any]) -> str:
    return "BUY" if str(position.get("type", "")).upper().endswith("BUY") else "SELL"


def money_to_distance(money: float, volume: float, tick_value: float, tick_size: float) -> float:
    if volume <= 0 or tick_value <= 0 or tick_size <= 0:
        raise ValueError(f"cannot convert money to distance: volume={volume} tick_value={tick_value} tick_size={tick_size}")
    return max(0.0, money) / (volume * tick_value) * tick_size


def distance_to_money(distance: float, volume: float, tick_value: float, tick_size: float) -> float:
    return distance / tick_size * tick_value * volume


def money_at_price(side: str, open_price: float, price: float, volume: float, tick_value: float, tick_size: float) -> float:
    """P/L (excluding swap/commission) if the position were closed at ``price``."""
    return distance_to_money(direction(side) * (price - open_price), volume, tick_value, tick_size)


def close_price(side: str, bid: float, ask: float) -> float:
    """The price a position of ``side`` would close at."""
    return bid if side == "BUY" else ask


def scale_for(volume: float) -> float:
    return volume / BASE_LOT


@dataclass
class SlPlan:
    stop_loss: Optional[float]
    money_at_sl: Optional[float]
    notes: list[str] = field(default_factory=list)


@dataclass
class TrailPlan:
    move: bool
    stop_loss: Optional[float]
    floating_money: float
    locked_money: Optional[float]
    reason: str
    notes: list[str] = field(default_factory=list)


def plan_initial_sl(
    *,
    side: str,
    open_price: float,
    volume: float,
    risk_per_base_lot: float,
    rules: SymbolRules,
    tick_value_loss: float,
    bid: float,
    ask: float,
) -> SlPlan:
    notes: list[str] = []
    risk_money = risk_per_base_lot * scale_for(volume)
    distance = money_to_distance(risk_money, volume, tick_value_loss, rules.tick_size)
    if side == "BUY":
        stop = rules.ceil_tick(open_price - distance)
        limit = bid - rules.min_stop_distance
        if bid and stop > limit:
            if bid <= stop:
                notes.append("price_already_at_or_beyond_sl")
            stop = rules.floor_tick(limit)
            notes.append(f"clamped_to_stops_level({rules.stops_level_points:g}pts)")
    else:
        stop = rules.floor_tick(open_price + distance)
        limit = ask + rules.min_stop_distance
        if ask and stop < limit:
            if ask >= stop:
                notes.append("price_already_at_or_beyond_sl")
            stop = rules.ceil_tick(limit)
            notes.append(f"clamped_to_stops_level({rules.stops_level_points:g}pts)")
    money = money_at_price(side, open_price, stop, volume, tick_value_loss, rules.tick_size)
    return SlPlan(stop_loss=stop, money_at_sl=round(money, 2), notes=notes)


def plan_trailing(
    *,
    side: str,
    open_price: float,
    volume: float,
    current_sl: Optional[float],
    rules: SymbolRules,
    tick_value_profit: float,
    bid: float,
    ask: float,
    trigger_per_base_lot: float,
    gap_per_base_lot: float,
    step_per_base_lot: float,
) -> TrailPlan:
    scale = scale_for(volume)
    exit_price = close_price(side, bid, ask)
    floating = money_at_price(side, open_price, exit_price, volume, tick_value_profit, rules.tick_size)
    trigger = trigger_per_base_lot * scale
    gap = gap_per_base_lot * scale
    step = step_per_base_lot * scale

    if floating + _EPS < trigger:
        return TrailPlan(False, current_sl, floating, None, "below_trigger")

    target_lock = max(0.0, floating - gap)
    distance = money_to_distance(target_lock, volume, tick_value_profit, rules.tick_size)
    notes: list[str] = []
    if side == "BUY":
        candidate = rules.floor_tick(open_price + distance)
        limit = bid - rules.min_stop_distance
        if candidate > limit:
            candidate = rules.floor_tick(limit)
            notes.append(f"clamped_to_stops_level({rules.stops_level_points:g}pts)")
    else:
        candidate = rules.ceil_tick(open_price - distance)
        limit = ask + rules.min_stop_distance
        if candidate < limit:
            candidate = rules.ceil_tick(limit)
            notes.append(f"clamped_to_stops_level({rules.stops_level_points:g}pts)")

    locked = money_at_price(side, open_price, candidate, volume, tick_value_profit, rules.tick_size)

    if current_sl:
        improves = candidate > current_sl + _EPS if side == "BUY" else candidate < current_sl - _EPS
        if not improves:
            return TrailPlan(False, current_sl, floating, locked, "would_not_tighten", notes)
        current_locked = money_at_price(side, open_price, current_sl, volume, tick_value_profit, rules.tick_size)
        if locked + _EPS < current_locked + step:
            return TrailPlan(False, current_sl, floating, locked, "below_step", notes)
        if rules.freeze_distance > 0 and abs(exit_price - current_sl) < rules.freeze_distance:
            return TrailPlan(False, current_sl, floating, locked, "inside_freeze_level", notes)

    return TrailPlan(True, candidate, floating, round(locked, 2), "trail", notes)


def tighter(side: str, a: Optional[float], b: Optional[float]) -> Optional[float]:
    """Return whichever SL is closer to/through price (never the wider one)."""
    if not a:
        return b
    if not b:
        return a
    return max(a, b) if side == "BUY" else min(a, b)


def sl_breached(side: str, stop_loss: Optional[float], bid: float, ask: float) -> bool:
    if not stop_loss:
        return False
    return bid <= stop_loss if side == "BUY" else ask >= stop_loss
