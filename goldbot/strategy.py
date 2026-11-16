"""Pure trading rules. No network, no clock - everything here is unit-tested.

The rules are the ones back-tested on Exness XAUUSDm one-minute bars
(June - mid November 2026):

* Signal  : a 5-minute bar closes above the highest high (buy) or below the
            lowest low (sell) of the 72 five-minute bars before it (6 hours).
* Session : the signal bar starts between 12:00 and 16:55 UTC (1-6 pm Lagos).
* Entry   : at market as soon as the bar has closed, at most 2 trades a day,
            one position at a time.
* Exit    : stop $6 from the entry. Once the trade has been $2 in profit the
            stop follows $2 behind the best price reached. Closed at market
            after 240 minutes if neither happened.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

LOOKBACK = 72
STOP_DISTANCE = 6.0
TRAIL_START = 2.0
TRAIL_DISTANCE = 2.0
MAX_HOLD_MINUTES = 240
MAX_TRADES_PER_DAY = 2
SESSION_START_HOUR = 12
SESSION_END_HOUR = 17


def breakout_signal(highs: Sequence[float], lows: Sequence[float], close: float, lookback: int = LOOKBACK) -> int:
    """+1 buy, -1 sell, 0 nothing.

    ``highs`` / ``lows`` are the bars *before* the signal bar, oldest first;
    only the last ``lookback`` are used. ``close`` is the signal bar's close.
    """
    if len(highs) < lookback or len(lows) < lookback:
        return 0
    if close > max(highs[-lookback:]):
        return 1
    if close < min(lows[-lookback:]):
        return -1
    return 0


def in_session(bar_start_epoch: int, start_hour: int = SESSION_START_HOUR, end_hour: int = SESSION_END_HOUR) -> bool:
    hour = (int(bar_start_epoch) // 3600) % 24
    return start_hour <= hour < end_hour


def initial_stop(side: int, entry: float, distance: float = STOP_DISTANCE) -> float:
    return entry - side * distance


def stop_hit(side: int, stop: float, price: float) -> bool:
    """``price`` is the closing side of the quote: bid for a buy, ask for a sell."""
    return price <= stop if side > 0 else price >= stop


def trail(
    side: int,
    entry: float,
    stop: float,
    best: float,
    price: float,
    start: float = TRAIL_START,
    distance: float = TRAIL_DISTANCE,
) -> tuple[float, float]:
    """Update the best favourable move and the stop from a new closing-side price.

    Returns ``(best, stop)``. The stop only ever moves in the trade's favour.
    """
    best = max(best, (price - entry) * side)
    if best >= start:
        wanted = entry + side * (best - distance)
        stop = max(stop, wanted) if side > 0 else min(stop, wanted)
    return best, stop


def best_from_stop(side: int, entry: float, stop: Optional[float], distance: float = TRAIL_DISTANCE) -> float:
    """Recover the best favourable move from a stop that has already been trailed (after a restart)."""
    if stop is None or stop <= 0:
        return 0.0
    locked = (stop - entry) * side
    # A trailed stop is never behind the entry (it starts at +$2 with $2 distance).
    return locked + distance if locked >= -0.01 else 0.0


def lot_size(
    balance: float,
    fixed: Optional[float] = None,
    balance_per_step: float = 40.0,
    step: float = 0.01,
    max_lot: float = 0.50,
) -> float:
    """0.01 lot per ``balance_per_step`` dollars, never below 0.01, unless a fixed lot is set."""
    if fixed and fixed > 0:
        lot = fixed
    else:
        lot = math.floor(max(balance, 0.0) / max(balance_per_step, 1.0)) * step
    lot = min(max(lot, step), max_lot)
    return round(round(lot / step) * step, 2)


def money(price_move: float, lot: float, contract_size: float = 100.0) -> float:
    """Dollars for a price move on XAUUSD: 1 lot = 100 oz, so 0.01 lot = $1 per $1."""
    return price_move * lot * contract_size
