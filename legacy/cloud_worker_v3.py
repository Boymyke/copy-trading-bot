import asyncio
import json
import os
import threading

import cloud_worker_v2 as base

# Fast, self-healing runtime for the copier.
# The broker/MetaApi websocket can still experience network interruptions, but this
# worker will detect them quickly, tear down the stale SDK client, and establish a
# completely fresh connection instead of remaining stuck on a dead subscription.

POLL_INTERVAL = max(0.05, float(os.getenv("COPY_POLL_INTERVAL", "0.10")))
DISCONNECT_GRACE = max(0.25, float(os.getenv("DISCONNECT_GRACE_SECONDS", "1.0")))
RECONNECT_DELAY = max(0.25, float(os.getenv("RECONNECT_DELAY_SECONDS", "1.0")))

base.STATE.setdefault("reconnectCount", 0)
base.STATE.setdefault("lastReconnectAt", None)
base.STATE.setdefault("streamHealthy", False)


def status_text_fast() -> str:
    s = base.dashboard_state()
    st = s["settings"]
    source_ok = bool(s.get("sourceConnected") and s.get("brokerConnected"))
    target_ok = bool(s.get("targetConnected"))
    engine_ok = source_ok and target_ok

    if st["copy_enabled"] and engine_ok:
        copy_line = "⚡ ACTIVE — waiting for trades"
    elif st["copy_enabled"]:
        copy_line = "🟠 ARMED — reconnecting"
    else:
        copy_line = "⏸ PAUSED"

    return (
        f"🤖 {base.APP_NAME}\n\n"
        f"Source feed: {'🟢 LIVE' if source_ok else '🔴 OFFLINE'}\n"
        f"Target feed: {'🟢 LIVE' if target_ok else ('🟠 not configured' if not s['targetConfigured'] else '🔴 OFFLINE')}\n"
        f"Copy engine: {copy_line}\n"
        f"Lot: {st['lot_size']:.2f}\n"
        f"Open copied trades: {len(s['copiedPositions'])}\n"
        f"Open P/L: ${s['pnl']['open']:.2f}\n"
        f"Today P/L: ${s['pnl']['today']:.2f}\n"
        f"Total P/L: ${s['pnl']['total']:.2f}\n"
        f"Reconnects: {int(s.get('reconnectCount') or 0)}"
    )


# handle_telegram_message() in v2 resolves status_text from its module globals at
# runtime, so replacing it here updates /status without duplicating Telegram code.
base.status_text = status_text_fast


async def prewarm_target(connection) -> None:
    """Subscribe to likely gold symbols so the first copied order does not wait for quotes."""
    terminal = connection.terminal_state
    candidates = []

    for src in [s.strip() for s in os.getenv("COPY_SYMBOLS", "").split(",") if s.strip()]:
        candidates.append(base.target_symbol_for(src))

    if not candidates:
        candidates = ["XAUUSD", "GOLD"]

    seen = set()
    for symbol in candidates:
        if symbol in seen:
            continue
        seen.add(symbol)
        try:
            if terminal.specification(symbol):
                await base.subscribe_symbol(connection, symbol)
                print(f"[target] prewarmed {symbol}", flush=True)
        except Exception as exc:
            print(f"[target] prewarm {symbol} warning: {exc}", flush=True)


async def close_quietly(connection) -> None:
    if connection is None:
        return
    try:
        await asyncio.wait_for(connection.close(), timeout=5)
    except Exception:
        pass


async def robust_trading_loop() -> None:
    token = os.getenv("METAAPI_TOKEN", "").strip()
    source_id = os.getenv("METAAPI_SOURCE_ACCOUNT_ID", "").strip()
    target_id = os.getenv("METAAPI_TARGET_ACCOUNT_ID", "").strip()

    if not token:
        raise RuntimeError("METAAPI_TOKEN is not configured")
    if not source_id:
        raise RuntimeError("METAAPI_SOURCE_ACCOUNT_ID is not configured")

    reconnect_count = 0
    outage_notified = False
    ever_connected = False

    while True:
        source_connection = None
        target_connection = None
        api = None
        try:
            # IMPORTANT: create a brand-new SDK client on every recovery cycle.
            # Reusing the previous client was the reason the old worker could remain
            # stuck for hours on a dead MetaApi websocket subscription.
            api = base.MetaApi(token)

            base.STATE.update({
                "status": "connecting",
                "sourceConnected": False,
                "brokerConnected": False,
                "targetConfigured": bool(target_id),
                "targetConnected": False,
                "streamHealthy": False,
                "reconnectCount": reconnect_count,
                "lastError": None,
                "lastUpdate": base.now_iso(),
            })

            _, source_connection = await base.connect_account(api, source_id, "source")
            source_terminal = source_connection.terminal_state

            if target_id:
                _, target_connection = await base.connect_account(api, target_id, "target")
                target_terminal = target_connection.terminal_state
                await prewarm_target(target_connection)
            else:
                target_terminal = None
                print("[target] METAAPI_TARGET_ACCOUNT_ID not configured; source monitoring only", flush=True)

            source_ok = bool(source_terminal.connected and source_terminal.connected_to_broker)
            target_ok = bool(not target_id or (target_terminal and target_terminal.connected and target_terminal.connected_to_broker))
            if not source_ok or not target_ok:
                raise ConnectionError("initial stream health check failed after synchronization")

            base.STATE.update({
                "status": "connected",
                "sourceConnected": True,
                "brokerConnected": True,
                "targetConnected": bool(target_id),
                "streamHealthy": True,
                "reconnectCount": reconnect_count,
                "lastReconnectAt": base.now_iso() if reconnect_count else base.STATE.get("lastReconnectAt"),
                "lastError": None,
                "lastUpdate": base.now_iso(),
            })

            if outage_notified:
                await base.tg_send("🟢 COPY FEED RESTORED\n\nSource and target are live again. The copier is watching for trades.")
                outage_notified = False
            elif not ever_connected:
                await base.tg_send("🟢 Gold Copy Trader cloud worker is online.\nSource and target feeds are live.")
            ever_connected = True

            # Start empty deliberately. Any source position which is currently open
            # but was not previously copied is treated as a catch-up candidate. The
            # DB trade_seen() guard prevents already-copied positions from duplicating.
            previous = {}
            bad_since = None
            loop = asyncio.get_running_loop()

            while True:
                source_ok = bool(source_terminal.connected and source_terminal.connected_to_broker)
                target_ok = bool(not target_id or (target_terminal and target_terminal.connected and target_terminal.connected_to_broker))

                if source_ok and target_ok:
                    bad_since = None
                else:
                    if bad_since is None:
                        bad_since = loop.time()
                    elif loop.time() - bad_since >= DISCONNECT_GRACE:
                        parts = []
                        if not source_ok:
                            parts.append("source")
                        if not target_ok:
                            parts.append("target")
                        raise ConnectionError(f"{' + '.join(parts)} MetaApi stream unhealthy for {DISCONNECT_GRACE:.2f}s")

                positions = source_terminal.positions or []
                current = {str(p.get("id")): p for p in positions if p.get("id") is not None}

                for ticket in current.keys() - previous.keys():
                    p = current[ticket]
                    print("[source trade] OPEN " + json.dumps(base.safe_position(p), default=str), flush=True)
                    if target_connection:
                        try:
                            await base.open_copy(p, target_connection)
                        except Exception as exc:
                            print(f"[copy] source #{ticket} open failed: {exc}", flush=True)
                            await base.tg_send(f"⚠️ COPY FAILED\n\nSource #{ticket} {p.get('symbol')}\n{exc}")

                for ticket in previous.keys() - current.keys():
                    print("[source trade] CLOSED " + json.dumps(base.safe_position(previous[ticket]), default=str), flush=True)

                if target_connection:
                    await base.manage_open_mappings(current, target_connection)

                base.STATE.update({
                    "status": "connected" if source_ok and target_ok else "connection-degraded",
                    "sourceConnected": bool(source_terminal.connected),
                    "brokerConnected": bool(source_terminal.connected_to_broker),
                    "targetConnected": bool(target_id and target_terminal and target_terminal.connected_to_broker),
                    "streamHealthy": bool(source_ok and target_ok),
                    "positionCount": len(current),
                    "positions": [base.safe_position(p) for p in current.values()],
                    "settings": base.get_settings(),
                    "telegramPaired": bool(base.get_auth("telegram_chat_id")),
                    "reconnectCount": reconnect_count,
                    "lastUpdate": base.now_iso(),
                    "lastError": None,
                })

                previous = current
                await asyncio.sleep(POLL_INTERVAL)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reconnect_count += 1
            base.STATE.update({
                "status": "reconnecting",
                "sourceConnected": False,
                "brokerConnected": False,
                "targetConnected": False,
                "streamHealthy": False,
                "reconnectCount": reconnect_count,
                "lastError": str(exc),
                "lastUpdate": base.now_iso(),
            })
            print(f"[watchdog] connection lost: {exc}; forcing fresh MetaApi client in {RECONNECT_DELAY:.2f}s", flush=True)

            if not outage_notified:
                outage_notified = True
                await base.tg_send(
                    "🔴 COPY FEED INTERRUPTED\n\n"
                    "The worker detected a broken MetaApi stream and is forcing a fresh connection automatically.\n"
                    "No action is required from you."
                )

            # Tear down BOTH streams. A partial reconnect leaves the SDK sharing the
            # same stale websocket client, which is exactly what we want to avoid.
            await asyncio.gather(
                close_quietly(target_connection),
                close_quietly(source_connection),
                return_exceptions=True,
            )
            source_connection = None
            target_connection = None
            api = None
            await asyncio.sleep(RECONNECT_DELAY)
        finally:
            await asyncio.gather(
                close_quietly(target_connection),
                close_quietly(source_connection),
                return_exceptions=True,
            )


async def main() -> None:
    base.init_db()
    base.STATE["settings"] = base.get_settings()
    threading.Thread(target=base.start_health_server, daemon=True).start()
    tasks = [asyncio.create_task(robust_trading_loop())]
    if os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
        tasks.append(asyncio.create_task(base.telegram_loop()))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
