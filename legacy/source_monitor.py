from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import MetaTrader5 as mt5

from common import SOURCE_STATE_FILE, atomic_write_json, env_float, env_int


def connect() -> None:
    path = os.getenv("SOURCE_MT5_PATH", "").strip()
    login = env_int("SOURCE_LOGIN", 0)
    password = os.getenv("SOURCE_PASSWORD", "")
    server = os.getenv("SOURCE_SERVER", "")

    kwargs = {}
    if login:
        kwargs["login"] = login
    if password:
        kwargs["password"] = password
    if server:
        kwargs["server"] = server

    ok = mt5.initialize(path=path or None, **kwargs)
    if not ok:
        raise RuntimeError(f"Source MT5 initialize failed: {mt5.last_error()}")

    account = mt5.account_info()
    if account is None:
        raise RuntimeError(f"Source account unavailable: {mt5.last_error()}")
    print(f"[source] connected login={account.login} server={account.server}")


def main() -> None:
    symbols = {s.strip() for s in os.getenv("SOURCE_SYMBOLS", "XAUUSD,GOLD").split(",") if s.strip()}
    poll = max(0.2, env_float("POLL_INTERVAL_SECONDS", 1.0))
    connect()

    try:
        while True:
            positions = mt5.positions_get()
            if positions is None:
                print(f"[source] positions_get failed: {mt5.last_error()}")
                time.sleep(poll)
                continue

            filtered = []
            for p in positions:
                if symbols and p.symbol not in symbols:
                    continue
                filtered.append({
                    "ticket": int(p.ticket),
                    "identifier": int(getattr(p, "identifier", p.ticket)),
                    "symbol": p.symbol,
                    "type": int(p.type),
                    "volume": float(p.volume),
                    "price_open": float(p.price_open),
                    "sl": float(p.sl),
                    "tp": float(p.tp),
                    "time": int(p.time),
                    "magic": int(p.magic),
                    "comment": p.comment,
                })

            payload = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "generated_at_epoch": time.time(),
                "positions": filtered,
            }
            atomic_write_json(SOURCE_STATE_FILE, payload)
            time.sleep(poll)
    except KeyboardInterrupt:
        print("[source] stopped")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
