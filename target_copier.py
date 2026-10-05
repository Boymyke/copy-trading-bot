from __future__ import annotations

import os
import time
from typing import Dict, Optional

import MetaTrader5 as mt5

from common import (
    COPY_MAP_FILE,
    SOURCE_STATE_FILE,
    atomic_write_json,
    env_bool,
    env_float,
    env_int,
    normalize_volume,
    parse_symbol_map,
    read_json,
)

POSITION_TYPE_BUY = 0
POSITION_TYPE_SELL = 1


def connect() -> None:
    path = os.getenv("TARGET_MT5_PATH", "").strip()
    login = env_int("TARGET_LOGIN", 0)
    password = os.getenv("TARGET_PASSWORD", "")
    server = os.getenv("TARGET_SERVER", "")

    kwargs = {}
    if login:
        kwargs["login"] = login
    if password:
        kwargs["password"] = password
    if server:
        kwargs["server"] = server

    ok = mt5.initialize(path=path or None, **kwargs)
    if not ok:
        raise RuntimeError(f"Target MT5 initialize failed: {mt5.last_error()}")

    account = mt5.account_info()
    if account is None:
        raise RuntimeError(f"Target account unavailable: {mt5.last_error()}")
    print(f"[target] connected login={account.login} server={account.server}")


def ensure_symbol(symbol: str):
    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"Target symbol {symbol!r} not found")
    if not info.visible and not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"Could not select target symbol {symbol!r}")
    return info


def target_position(ticket: int):
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return None
    return positions[0]


def lot_size(source_position: dict, symbol: str, sl_points: float) -> float:
    info = ensure_symbol(symbol)
    mode = os.getenv("LOT_MODE", "fixed").strip().lower()

    if mode == "source_multiplier":
        raw = float(source_position["volume"]) * env_float("LOT_MULTIPLIER", 1.0)
    elif mode == "risk_percent":
        account = mt5.account_info()
        if account is None:
            raise RuntimeError("Target account_info unavailable")
        if sl_points <= 0:
            raise RuntimeError("SL_POINTS must be > 0 for risk_percent mode")
        risk_cash = float(account.equity) * (env_float("RISK_PERCENT", 1.0) / 100.0)
        if info.trade_tick_size <= 0 or info.trade_tick_value <= 0:
            raise RuntimeError(f"Invalid tick data for {symbol}; cannot calculate risk-based volume")
        price_distance = sl_points * info.point
        ticks_to_sl = price_distance / info.trade_tick_size
        loss_per_lot = ticks_to_sl * info.trade_tick_value
        if loss_per_lot <= 0:
            raise RuntimeError(f"Invalid calculated loss-per-lot for {symbol}")
        raw = risk_cash / loss_per_lot
    else:
        raw = env_float("FIXED_LOT", 0.01)

    return normalize_volume(raw, info.volume_min, info.volume_max, info.volume_step)


def build_protection(order_type: int, price: float, point: float, sl_points: float, tp_points: float):
    sl = 0.0
    tp = 0.0
    if order_type == mt5.ORDER_TYPE_BUY:
        if sl_points > 0:
            sl = price - (sl_points * point)
        if tp_points > 0:
            tp = price + (tp_points * point)
    else:
        if sl_points > 0:
            sl = price + (sl_points * point)
        if tp_points > 0:
            tp = price - (tp_points * point)
    return sl, tp


def open_copy(source_position: dict, target_symbol: str, dry_run: bool) -> Optional[int]:
    info = ensure_symbol(target_symbol)
    tick = mt5.symbol_info_tick(target_symbol)
    if tick is None:
        raise RuntimeError(f"No tick available for {target_symbol}")

    source_type = int(source_position["type"])
    if source_type == POSITION_TYPE_BUY:
        order_type = mt5.ORDER_TYPE_BUY
        price = tick.ask
    elif source_type == POSITION_TYPE_SELL:
        order_type = mt5.ORDER_TYPE_SELL
        price = tick.bid
    else:
        print(f"[target] ignoring unsupported source type={source_type}")
        return None

    sl_points = env_float("SL_POINTS", 0)
    tp_points = env_float("TP_POINTS", 0)
    volume = lot_size(source_position, target_symbol, sl_points)
    sl, tp = build_protection(order_type, price, info.point, sl_points, tp_points)
    comment = f"{os.getenv('COMMENT_PREFIX', 'FerrnCopier')}:{source_position['ticket']}"

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": target_symbol,
        "volume": volume,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": env_int("DEVIATION_POINTS", 30),
        "magic": env_int("MAGIC_NUMBER", 26051001),
        "comment": comment[:31],
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    if dry_run:
        print(f"[DRY RUN] would open {request}")
        return None

    result = mt5.order_send(request)
    if result is None:
        raise RuntimeError(f"order_send returned None: {mt5.last_error()}")
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        raise RuntimeError(f"Open failed retcode={result.retcode} comment={result.comment}")

    position_ticket = int(result.order)
    found = mt5.positions_get(symbol=target_symbol) or []
    candidates = [p for p in found if p.magic == request["magic"] and p.comment == request["comment"]]
    if candidates:
        position_ticket = int(max(candidates, key=lambda p: p.time_msc).ticket)

    print(f"[target] opened source={source_position['ticket']} -> target={position_ticket} {target_symbol} {volume}")
    return position_ticket


def close_position(position, dry_run: bool) -> bool:
    tick = mt5.symbol_info_tick(position.symbol)
    if tick is None:
        raise RuntimeError(f"No tick available for {position.symbol}")

    if position.type == POSITION_TYPE_BUY:
        order_type = mt5.ORDER_TYPE_SELL
        price = tick.bid
    else:
        order_type = mt5.ORDER_TYPE_BUY
        price = tick.ask

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "position": int(position.ticket),
        "symbol": position.symbol,
        "volume": float(position.volume),
        "type": order_type,
        "price": price,
        "deviation": env_int("DEVIATION_POINTS", 30),
        "magic": env_int("MAGIC_NUMBER", 26051001),
        "comment": f"{os.getenv('COMMENT_PREFIX', 'FerrnCopier')}:close"[:31],
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    if dry_run:
        print(f"[DRY RUN] would close {request}")
        return True

    result = mt5.order_send(request)
    if result is None:
        raise RuntimeError(f"close order_send returned None: {mt5.last_error()}")
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        raise RuntimeError(f"Close failed retcode={result.retcode} comment={result.comment}")
    print(f"[target] closed target={position.ticket}")
    return True


def main() -> None:
    poll = max(0.2, env_float("POLL_INTERVAL_SECONDS", 1.0))
    max_age = max(1.0, env_float("SOURCE_STATE_MAX_AGE_SECONDS", 5.0))
    dry_run = env_bool("DRY_RUN", True)
    symbol_map = parse_symbol_map(os.getenv("TARGET_SYMBOL_MAP", "XAUUSD:XAUUSD,GOLD:GOLD"))
    connect()

    state = read_json(COPY_MAP_FILE, {"trades": {}})
    trades: Dict[str, dict] = state.setdefault("trades", {})

    try:
        while True:
            snapshot = read_json(SOURCE_STATE_FILE, {})
            generated = float(snapshot.get("generated_at_epoch", 0) or 0)
            if generated <= 0 or (time.time() - generated) > max_age:
                print("[target] source snapshot missing/stale; no trading action")
                time.sleep(poll)
                continue

            source_positions = {str(p["ticket"]): p for p in snapshot.get("positions", [])}

            for source_ticket, record in list(trades.items()):
                source_still_open = source_ticket in source_positions
                target_ticket = record.get("target_ticket")
                status = record.get("status", "active")

                if status == "active" and target_ticket:
                    target = target_position(int(target_ticket))
                    if source_still_open:
                        if target is None:
                            record["status"] = "target_closed_early"
                            record["target_ticket"] = None
                            print(f"[target] target closed early for source={source_ticket}; re-entry suppressed")
                    else:
                        if target is not None:
                            close_position(target, dry_run)
                        del trades[source_ticket]
                        print(f"[target] source={source_ticket} closed; lifecycle completed")

                elif not source_still_open:
                    del trades[source_ticket]

            for source_ticket, source_position in source_positions.items():
                if source_ticket in trades:
                    continue
                target_symbol = symbol_map.get(source_position["symbol"])
                if not target_symbol:
                    print(f"[target] no mapping for source symbol={source_position['symbol']}; skipped")
                    trades[source_ticket] = {"status": "ignored_unmapped", "target_ticket": None}
                    continue

                target_ticket = open_copy(source_position, target_symbol, dry_run)
                if dry_run:
                    continue
                trades[source_ticket] = {
                    "status": "active",
                    "target_ticket": target_ticket,
                    "source_symbol": source_position["symbol"],
                    "target_symbol": target_symbol,
                }

            atomic_write_json(COPY_MAP_FILE, state)
            time.sleep(poll)
    except KeyboardInterrupt:
        print("[target] stopped")
    finally:
        atomic_write_json(COPY_MAP_FILE, state)
        mt5.shutdown()


if __name__ == "__main__":
    main()
