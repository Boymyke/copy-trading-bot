import asyncio
import base64
import json
import os
import sqlite3
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from metaapi_cloud_sdk import MetaApi

APP_NAME = "Gold Copy Trader"
MAGIC = int(os.getenv("MAGIC_NUMBER", "26051001"))
DATA_DIR = Path(os.getenv("DATA_DIR", "/data" if os.path.isdir("/data") else "."))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "copier.db"
DASHBOARD_FILE = Path(__file__).resolve().parent / "dashboard.html"
DB_LOCK = threading.RLock()

STATE: Dict[str, Any] = {
    "status": "starting",
    "sourceConnected": False,
    "brokerConnected": False,
    "targetConfigured": False,
    "targetConnected": False,
    "telegramPaired": False,
    "positionCount": 0,
    "positions": [],
    "copiedPositions": [],
    "recentClosed": [],
    "pnl": {"open": 0.0, "today": 0.0, "total": 0.0},
    "settings": {},
    "lastUpdate": None,
    "lastError": None,
}

DEFAULT_SETTINGS = {
    "lot_size": "0.01",
    "copy_enabled": "0",
    "initial_sl_per_001": "5.0",
    "trail_trigger_per_001": "5.0",
    "trailing_gap_per_001": "2.0",
    "trailing_step_per_001": "0.50",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with DB_LOCK, db() as conn:
        conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS copied_trades (
            source_ticket TEXT PRIMARY KEY,
            source_symbol TEXT,
            target_symbol TEXT,
            side TEXT,
            volume REAL,
            target_position_id TEXT,
            open_price REAL,
            stop_loss REAL,
            last_profit REAL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'open',
            close_reason TEXT,
            realized_pnl REAL DEFAULT 0,
            opened_at TEXT NOT NULL,
            closed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_copied_status ON copied_trades(status);
        CREATE TABLE IF NOT EXISTS auth (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """)
        ts = now_iso()
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES(?,?,?)", (key, value, ts))
        conn.commit()


def get_settings() -> Dict[str, Any]:
    with DB_LOCK, db() as conn:
        rows = conn.execute("SELECT key,value FROM settings").fetchall()
    raw = {r["key"]: r["value"] for r in rows}
    return {
        "lot_size": float(raw.get("lot_size", "0.01")),
        "copy_enabled": raw.get("copy_enabled", "0") == "1",
        "initial_sl_per_001": float(raw.get("initial_sl_per_001", "5")),
        "trail_trigger_per_001": float(raw.get("trail_trigger_per_001", "5")),
        "trailing_gap_per_001": float(raw.get("trailing_gap_per_001", "2")),
        "trailing_step_per_001": float(raw.get("trailing_step_per_001", "0.5")),
    }


def set_setting(key: str, value: Any) -> None:
    if key not in DEFAULT_SETTINGS:
        raise ValueError("unknown setting")
    with DB_LOCK, db() as conn:
        conn.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (key, str(value), now_iso()),
        )
        conn.commit()


def get_auth(key: str) -> Optional[str]:
    with DB_LOCK, db() as conn:
        row = conn.execute("SELECT value FROM auth WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_auth(key: str, value: str) -> None:
    with DB_LOCK, db() as conn:
        conn.execute(
            "INSERT INTO auth(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (key, value, now_iso()),
        )
        conn.commit()


def load_open_mappings() -> Dict[str, Dict[str, Any]]:
    with DB_LOCK, db() as conn:
        rows = conn.execute("SELECT * FROM copied_trades WHERE status='open'").fetchall()
    return {str(r["source_ticket"]): dict(r) for r in rows}


def trade_seen(source_ticket: str) -> bool:
    with DB_LOCK, db() as conn:
        row = conn.execute("SELECT 1 FROM copied_trades WHERE source_ticket=? LIMIT 1", (source_ticket,)).fetchone()
    return bool(row)


def insert_mapping(source: Dict[str, Any], target_symbol: str, volume: float, target_position_id: str, open_price: float, stop_loss: float) -> None:
    side = "BUY" if str(source.get("type", "")).upper().endswith("BUY") else "SELL"
    with DB_LOCK, db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO copied_trades(
                source_ticket,source_symbol,target_symbol,side,volume,target_position_id,
                open_price,stop_loss,last_profit,status,opened_at
            ) VALUES(?,?,?,?,?,?,?,?,?,'open',?)""",
            (
                str(source.get("id")), str(source.get("symbol")), target_symbol, side, volume,
                str(target_position_id), float(open_price or 0), float(stop_loss or 0), 0.0, now_iso(),
            ),
        )
        conn.commit()


def update_mapping_live(source_ticket: str, *, stop_loss: Optional[float] = None, last_profit: Optional[float] = None, target_position_id: Optional[str] = None, open_price: Optional[float] = None) -> None:
    fields, values = [], []
    for name, value in (("stop_loss", stop_loss), ("last_profit", last_profit), ("target_position_id", target_position_id), ("open_price", open_price)):
        if value is not None:
            fields.append(f"{name}=?")
            values.append(value)
    if not fields:
        return
    values.append(source_ticket)
    with DB_LOCK, db() as conn:
        conn.execute(f"UPDATE copied_trades SET {','.join(fields)} WHERE source_ticket=?", values)
        conn.commit()


def mark_closed(source_ticket: str, reason: str, pnl: float) -> None:
    with DB_LOCK, db() as conn:
        conn.execute(
            "UPDATE copied_trades SET status='closed',close_reason=?,realized_pnl=?,closed_at=? WHERE source_ticket=? AND status='open'",
            (reason, float(pnl or 0), now_iso(), source_ticket),
        )
        conn.commit()


def safe_position(position: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("id", "symbol", "type", "volume", "openPrice", "currentPrice", "stopLoss", "takeProfit", "profit", "magic", "comment")
    return {key: position.get(key) for key in keys if key in position}


def scaled_amount(per_001: float, volume: float) -> float:
    return max(0.0, per_001) * (max(volume, 0.0) / 0.01)


def symbol_map() -> Dict[str, str]:
    result: Dict[str, str] = {}
    raw = os.getenv("TARGET_SYMBOL_MAP", "").strip()
    for part in raw.split(","):
        if ":" in part:
            src, tgt = part.split(":", 1)
            result[src.strip()] = tgt.strip()
    return result


def target_symbol_for(source_symbol: str) -> str:
    return symbol_map().get(source_symbol, source_symbol)


def should_copy_symbol(symbol: str) -> bool:
    configured = [s.strip() for s in os.getenv("COPY_SYMBOLS", "").split(",") if s.strip()]
    if configured:
        return symbol in configured
    upper = symbol.upper()
    return "XAU" in upper or "GOLD" in upper


def side_of(position: Dict[str, Any]) -> str:
    ptype = str(position.get("type", "")).upper()
    return "BUY" if ptype.endswith("BUY") or ptype == "POSITION_TYPE_BUY" else "SELL"


def normalize_volume(spec: Dict[str, Any], volume: float) -> float:
    minimum = float(spec.get("minVolume") or spec.get("volumeMin") or 0.01)
    maximum = float(spec.get("maxVolume") or spec.get("volumeMax") or 100.0)
    step = float(spec.get("volumeStep") or 0.01)
    volume = min(max(volume, minimum), maximum)
    steps = round((volume - minimum) / step)
    value = minimum + steps * step
    decimals = max(0, len((f"{step:.8f}".rstrip("0").split(".") + [""])[1]))
    return round(value, decimals)


def tick_metrics(spec: Dict[str, Any], side: str) -> Tuple[float, float]:
    tick_size = float(spec.get("tickSize") or spec.get("point") or 0)
    tick_value = float(spec.get("tickValueLoss") or spec.get("tickValue") or spec.get("tickValueProfit") or 0)
    if tick_size <= 0 or tick_value <= 0:
        raise RuntimeError("Broker symbol specification does not expose tickSize/tickValue")
    return tick_size, tick_value


def price_distance_for_money(spec: Dict[str, Any], volume: float, money: float, side: str) -> float:
    tick_size, tick_value = tick_metrics(spec, side)
    if volume <= 0:
        raise ValueError("volume must be positive")
    return (max(money, 0.0) / (volume * tick_value)) * tick_size


def round_price(spec: Dict[str, Any], price: float) -> float:
    return round(float(price), int(spec.get("digits") or 2))


def dashboard_state() -> Dict[str, Any]:
    settings = get_settings()
    with DB_LOCK, db() as conn:
        open_rows = [dict(r) for r in conn.execute("SELECT * FROM copied_trades WHERE status='open' ORDER BY opened_at DESC").fetchall()]
        recent = [dict(r) for r in conn.execute("SELECT * FROM copied_trades WHERE status='closed' ORDER BY closed_at DESC LIMIT 30").fetchall()]
        total = conn.execute("SELECT COALESCE(SUM(realized_pnl),0) v FROM copied_trades WHERE status='closed'").fetchone()["v"]
        today = conn.execute("SELECT COALESCE(SUM(realized_pnl),0) v FROM copied_trades WHERE status='closed' AND date(closed_at)=date('now')").fetchone()["v"]
    open_pnl = sum(float(r.get("last_profit") or 0) for r in open_rows)
    payload = dict(STATE)
    payload["settings"] = settings
    payload["telegramPaired"] = bool(get_auth("telegram_chat_id"))
    payload["copiedPositions"] = open_rows
    payload["recentClosed"] = recent
    payload["pnl"] = {"open": open_pnl, "today": float(today or 0), "total": float(total or 0)}
    return payload


def basic_auth_ok(headers) -> bool:
    password = os.getenv("DASHBOARD_PASSWORD", "").strip()
    if not password:
        return False
    auth = headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
        user, supplied = decoded.split(":", 1)
        return user == "trader" and supplied == password
    except Exception:
        return False


class HealthHandler(BaseHTTPRequestHandler):
    def _json(self, code: int, payload: Dict[str, Any]):
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self) -> bool:
        if basic_auth_ok(self.headers):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Gold Copy Trader"')
        self.end_headers()
        return False

    def do_GET(self):
        if self.path == "/health":
            self._json(200 if STATE["status"] != "fatal" else 503, {
                "status": STATE["status"], "sourceConnected": STATE["sourceConnected"],
                "targetConfigured": STATE["targetConfigured"], "targetConnected": STATE["targetConnected"],
                "lastError": STATE["lastError"],
            })
            return
        if not self._auth():
            return
        if self.path.startswith("/api/state"):
            self._json(200, dashboard_state())
            return
        if self.path == "/" or self.path.startswith("/?"):
            if not DASHBOARD_FILE.exists():
                self.send_response(404); self.end_headers(); return
            body = DASHBOARD_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404); self.end_headers()

    def log_message(self, *_args):
        return


def start_health_server() -> None:
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"[web] listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()


def telegram_request(method: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return {"ok": False, "description": "TELEGRAM_BOT_TOKEN not configured"}
    url = f"https://api.telegram.org/bot{token}/{method}"
    encoded = urllib.parse.urlencode(data or {}).encode("utf-8")
    req = urllib.request.Request(url, data=encoded, method="POST")
    with urllib.request.urlopen(req, timeout=40) as response:
        return json.loads(response.read().decode("utf-8"))


async def tg_call(method: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return await asyncio.to_thread(telegram_request, method, data)


async def tg_send(text: str) -> None:
    chat_id = get_auth("telegram_chat_id")
    if not chat_id or not os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
        return
    try:
        await tg_call("sendMessage", {"chat_id": chat_id, "text": text})
    except Exception as exc:
        print(f"[telegram] send failed: {exc}", flush=True)


def status_text() -> str:
    s = dashboard_state(); st = s["settings"]
    return (
        f"🤖 {APP_NAME}\n\n"
        f"Source: {'🟢 connected' if s['sourceConnected'] and s['brokerConnected'] else '🔴 disconnected'}\n"
        f"Target: {'🟢 connected' if s['targetConnected'] else ('🟠 not configured' if not s['targetConfigured'] else '🔴 disconnected')}\n"
        f"Copying: {'🟢 ON' if st['copy_enabled'] else '⏸ PAUSED'}\n"
        f"Lot: {st['lot_size']:.2f}\nOpen copied trades: {len(s['copiedPositions'])}\n"
        f"Open P/L: ${s['pnl']['open']:.2f}\nToday P/L: ${s['pnl']['today']:.2f}\nTotal P/L: ${s['pnl']['total']:.2f}"
    )


def help_text() -> str:
    return (
        "Gold Copy Trader commands\n\n"
        "/status — system status\n/lot 0.10 — set persistent lot size\n/pause — stop NEW copied entries\n"
        "/resume — allow new copied entries\n/positions — show open copied trades\n/pnl — show open/today/total P&L\n"
        "/setsl 5 — initial SL dollars per 0.01 lot\n/settrigger 5 — profit dollars per 0.01 before trailing starts\n"
        "/setgap 2 — dollars per 0.01 kept behind current profit\n/setstep 0.50 — minimum locked-profit improvement per 0.01 before SL moves again\n"
        "/help — show this help\n\nChanging lot/settings affects NEW trades. Existing trades keep their opening lot but use the latest trailing rules."
    )


async def handle_telegram_message(message: Dict[str, Any]) -> None:
    chat = message.get("chat") or {}; chat_id = str(chat.get("id", "")); text = str(message.get("text") or "").strip()
    if not chat_id or not text:
        return
    paired = get_auth("telegram_chat_id")
    if not paired:
        parts = text.split(maxsplit=1)
        if len(parts) == 2 and parts[0].lower() == "/pair":
            expected = os.getenv("DASHBOARD_PASSWORD", "").strip()
            if expected and parts[1].strip() == expected:
                set_auth("telegram_chat_id", chat_id)
                await tg_call("sendMessage", {"chat_id": chat_id, "text": "✅ Telegram paired.\n\n" + help_text()})
            else:
                await tg_call("sendMessage", {"chat_id": chat_id, "text": "❌ Invalid pairing code."})
        else:
            await tg_call("sendMessage", {"chat_id": chat_id, "text": "This bot is not paired yet. Use /pair YOUR_PAIRING_CODE."})
        return
    if chat_id != paired:
        return
    cmd, *rest = text.split(maxsplit=1); cmd = cmd.split("@", 1)[0].lower(); arg = rest[0].strip() if rest else ""
    try:
        if cmd in ("/start", "/help"):
            reply = help_text()
        elif cmd == "/status":
            reply = status_text()
        elif cmd == "/lot":
            value = float(arg)
            if value <= 0 or value > 100: raise ValueError("lot must be greater than 0 and at most 100")
            set_setting("lot_size", f"{value:.8f}".rstrip("0").rstrip("."))
            reply = f"✅ Default lot size set to {value:g}. It will stay this way until you change it again."
        elif cmd == "/pause":
            set_setting("copy_enabled", "0")
            reply = "⏸ New copying paused. Existing copied positions will still be managed and source-close synchronization remains active."
        elif cmd == "/resume":
            if not os.getenv("METAAPI_TARGET_ACCOUNT_ID", "").strip(): reply = "⚠️ Target account is not configured yet, so copying cannot be resumed."
            else:
                set_setting("copy_enabled", "1"); reply = "▶️ Copying enabled. New eligible source trades will now be copied."
        elif cmd in ("/setsl", "/settrigger", "/setgap", "/setstep"):
            value = float(arg)
            if value < 0 or value > 100000: raise ValueError("value must be between 0 and 100000")
            key = {"/setsl": "initial_sl_per_001", "/settrigger": "trail_trigger_per_001", "/setgap": "trailing_gap_per_001", "/setstep": "trailing_step_per_001"}[cmd]
            set_setting(key, value)
            label = {"/setsl": "Initial SL", "/settrigger": "Trail trigger", "/setgap": "Trailing gap", "/setstep": "Trailing step"}[cmd]
            reply = f"✅ {label} set to ${value:g} per 0.01 lot."
        elif cmd == "/pnl":
            p = dashboard_state()["pnl"]; reply = f"💰 P/L\n\nOpen: ${p['open']:.2f}\nToday: ${p['today']:.2f}\nTotal: ${p['total']:.2f}"
        elif cmd == "/positions":
            rows = dashboard_state()["copiedPositions"]
            if not rows: reply = "No copied positions are currently open."
            else:
                lines = ["📈 Open copied positions"]
                for r in rows[:20]:
                    lines.append(f"\n{r['side']} {r['target_symbol']} {float(r['volume'] or 0):g} lot\nSource #{r['source_ticket']} → Target #{r['target_position_id']}\nSL: {float(r['stop_loss'] or 0):.2f} | P/L: ${float(r['last_profit'] or 0):.2f}")
                reply = "\n".join(lines)
        else:
            reply = "Unknown command.\n\n" + help_text()
    except Exception as exc:
        reply = f"❌ {exc}"
    await tg_call("sendMessage", {"chat_id": chat_id, "text": reply})


async def telegram_loop() -> None:
    if not os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
        print("[telegram] TELEGRAM_BOT_TOKEN is not configured; Telegram control disabled", flush=True); return
    offset = 0; print("[telegram] polling enabled", flush=True)
    while True:
        try:
            response = await tg_call("getUpdates", {"offset": offset, "timeout": 25, "allowed_updates": json.dumps(["message"])})
            if not response.get("ok"): raise RuntimeError(response.get("description") or "Telegram API error")
            for update in response.get("result", []):
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                if update.get("message"): await handle_telegram_message(update["message"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[telegram] error: {exc}", flush=True); await asyncio.sleep(5)


async def connect_account(api: MetaApi, account_id: str, label: str):
    account = await api.metatrader_account_api.get_account(account_id)
    if account.state != "DEPLOYED":
        print(f"[{label}] deploying...", flush=True); await account.deploy()
    print(f"[{label}] waiting for broker connection...", flush=True); await account.wait_connected()
    connection = account.get_streaming_connection(); await connection.connect()
    print(f"[{label}] synchronizing terminal state...", flush=True); await connection.wait_synchronized({"timeoutInSeconds": 600})
    print(f"[{label}] CONNECTED", flush=True); return account, connection


async def subscribe_symbol(connection, symbol: str) -> None:
    try: await connection.subscribe_to_market_data(symbol)
    except Exception as exc: print(f"[target] market data subscription for {symbol} warning: {exc}", flush=True)


async def find_target_position(terminal_state, *, position_id: Optional[str], comment: str, symbol: str, side: str, volume: float) -> Optional[Dict[str, Any]]:
    for _ in range(30):
        positions = terminal_state.positions or []
        if position_id:
            for p in positions:
                if str(p.get("id")) == str(position_id): return p
        candidates = [p for p in positions if p.get("symbol") == symbol and side_of(p) == side and abs(float(p.get("volume") or 0) - volume) < 1e-8 and (str(p.get("comment") or "") == comment or int(p.get("magic") or 0) == MAGIC)]
        if candidates: return candidates[-1]
        await asyncio.sleep(0.2)
    return None


async def realized_pnl(connection, position_id: str, fallback: float) -> float:
    await asyncio.sleep(0.8)
    try:
        deals = connection.history_storage.get_deals_by_position(str(position_id)) or []
        if deals:
            total = 0.0
            for deal in deals:
                total += float(deal.get("profit") or 0) + float(deal.get("commission") or 0) + float(deal.get("swap") or 0) + float(deal.get("fee") or 0)
            return total
    except Exception: pass
    return float(fallback or 0)


async def open_copy(source: Dict[str, Any], target_connection) -> None:
    source_ticket = str(source.get("id"))
    if trade_seen(source_ticket): return
    settings = get_settings()
    if not settings["copy_enabled"]: return
    source_symbol = str(source.get("symbol"))
    if not should_copy_symbol(source_symbol): return
    target_symbol = target_symbol_for(source_symbol); terminal = target_connection.terminal_state; spec = terminal.specification(target_symbol)
    if not spec: raise RuntimeError(f"Target symbol {target_symbol} is unavailable")
    await subscribe_symbol(target_connection, target_symbol)
    side = side_of(source); volume = normalize_volume(spec, settings["lot_size"]); price = terminal.price(target_symbol)
    if not price: await asyncio.sleep(0.5); price = terminal.price(target_symbol)
    if not price: raise RuntimeError(f"No target quote available for {target_symbol}")
    entry = float(price.get("ask") if side == "BUY" else price.get("bid")); risk_money = scaled_amount(settings["initial_sl_per_001"], volume)
    sl_distance = price_distance_for_money(spec, volume, risk_money, side); stop_loss = round_price(spec, entry - sl_distance if side == "BUY" else entry + sl_distance)
    comment = f"FERRN:{source_ticket}"[:31]; options = {"comment": comment, "magic": MAGIC, "clientId": f"FERRN_{source_ticket}"[:32]}
    print(f"[copy] opening {side} {target_symbol} {volume} lot for source #{source_ticket}", flush=True)
    if side == "BUY": result = await target_connection.create_market_buy_order(target_symbol, volume, stop_loss, None, options)
    else: result = await target_connection.create_market_sell_order(target_symbol, volume, stop_loss, None, options)
    position_id = str(result.get("positionId") or result.get("position_id") or "")
    target_position = await find_target_position(terminal, position_id=position_id or None, comment=comment, symbol=target_symbol, side=side, volume=volume)
    if not target_position: raise RuntimeError(f"Target order succeeded but position could not be identified: {result}")
    position_id = str(target_position.get("id")); actual_open = float(target_position.get("openPrice") or entry); actual_sl = float(target_position.get("stopLoss") or stop_loss)
    insert_mapping(source, target_symbol, volume, position_id, actual_open, actual_sl)
    await tg_send(f"🔵 TRADE COPIED\n\n{side} {target_symbol}\nLot: {volume:g}\nSource: #{source_ticket}\nTarget: #{position_id}\nSL: {actual_sl:.2f}\nTP: none")


async def manage_open_mappings(source_current: Dict[str, Dict[str, Any]], target_connection) -> None:
    terminal = target_connection.terminal_state; target_positions = {str(p.get("id")): p for p in (terminal.positions or []) if p.get("id") is not None}; settings = get_settings()
    for source_ticket, mapping in load_open_mappings().items():
        target_id = str(mapping.get("target_position_id") or ""); target = target_positions.get(target_id)
        if source_ticket not in source_current:
            fallback = float(mapping.get("last_profit") or 0)
            if target:
                fallback = float(target.get("profit") or fallback)
                try:
                    await target_connection.close_position(target_id, {"comment": "source closed"}); reason = "source_closed"
                except Exception as exc:
                    print(f"[copy] close failed for target #{target_id}: {exc}", flush=True); continue
            else: reason = "target_already_closed"
            pnl = await realized_pnl(target_connection, target_id, fallback); mark_closed(source_ticket, reason, pnl)
            await tg_send(f"✅ COPIED TRADE CLOSED\n\n{mapping['side']} {mapping['target_symbol']} {float(mapping['volume']):g} lot\nReason: {'Source closed' if reason == 'source_closed' else 'Target SL/exit'}\nP/L: ${pnl:.2f}")
            continue
        if not target:
            pnl = await realized_pnl(target_connection, target_id, float(mapping.get("last_profit") or 0)); mark_closed(source_ticket, "target_exit", pnl)
            await tg_send(f"🟠 TARGET TRADE ENDED\n\n{mapping['side']} {mapping['target_symbol']}\nSource #{source_ticket} is still open. This source trade will NOT be copied again.\nP/L: ${pnl:.2f}")
            continue
        profit = float(target.get("profit") or 0); current_sl = float(target.get("stopLoss") or mapping.get("stop_loss") or 0); update_mapping_live(source_ticket, last_profit=profit, stop_loss=current_sl)
        trigger = scaled_amount(settings["trail_trigger_per_001"], float(mapping["volume"])); gap = scaled_amount(settings["trailing_gap_per_001"], float(mapping["volume"])); step = scaled_amount(settings["trailing_step_per_001"], float(mapping["volume"]))
        if profit < trigger: continue
        desired_locked_profit = max(0.0, profit - gap); target_symbol = str(mapping["target_symbol"]); spec = terminal.specification(target_symbol)
        if not spec: continue
        open_price = float(target.get("openPrice") or mapping.get("open_price") or 0); side = str(mapping["side"]); locked_distance = price_distance_for_money(spec, float(mapping["volume"]), desired_locked_profit, side)
        desired_sl = round_price(spec, open_price + locked_distance if side == "BUY" else open_price - locked_distance)
        if current_sl:
            existing_dist = (current_sl - open_price) if side == "BUY" else (open_price - current_sl); tick_size, tick_value = tick_metrics(spec, side); existing_locked = max(0.0, (existing_dist / tick_size) * tick_value * float(mapping["volume"]))
        else: existing_locked = 0.0
        improves = (side == "BUY" and (not current_sl or desired_sl > current_sl)) or (side == "SELL" and (not current_sl or desired_sl < current_sl))
        if not improves or desired_locked_profit < existing_locked + step: continue
        try:
            await target_connection.modify_position(target_id, desired_sl, None); update_mapping_live(source_ticket, stop_loss=desired_sl, last_profit=profit)
            print(f"[trail] target #{target_id} SL -> {desired_sl} locking ~${desired_locked_profit:.2f}", flush=True)
        except Exception as exc: print(f"[trail] target #{target_id} SL update failed: {exc}", flush=True)


async def trading_loop() -> None:
    token = os.getenv("METAAPI_TOKEN", "").strip(); source_id = os.getenv("METAAPI_SOURCE_ACCOUNT_ID", "").strip(); target_id = os.getenv("METAAPI_TARGET_ACCOUNT_ID", "").strip()
    if not token: raise RuntimeError("METAAPI_TOKEN is not configured")
    if not source_id: raise RuntimeError("METAAPI_SOURCE_ACCOUNT_ID is not configured")
    api = MetaApi(token); retry_seconds = 5
    while True:
        source_connection = target_connection = None
        try:
            STATE.update({"status": "connecting", "sourceConnected": False, "brokerConnected": False, "targetConfigured": bool(target_id), "targetConnected": False, "lastError": None, "lastUpdate": now_iso()})
            _, source_connection = await connect_account(api, source_id, "source"); source_terminal = source_connection.terminal_state
            STATE.update({"sourceConnected": bool(source_terminal.connected), "brokerConnected": bool(source_terminal.connected_to_broker)})
            if target_id:
                _, target_connection = await connect_account(api, target_id, "target"); STATE["targetConnected"] = bool(target_connection.terminal_state.connected_to_broker)
            else: print("[target] METAAPI_TARGET_ACCOUNT_ID not configured; source monitoring only", flush=True)
            STATE["status"] = "connected"; await tg_send("🟢 Gold Copy Trader cloud worker is online." + ("" if target_id else "\nTarget account is not configured yet.")); previous: Dict[str, Dict[str, Any]] = {}
            while True:
                positions = source_terminal.positions or []; current = {str(p.get("id")): p for p in positions if p.get("id") is not None}
                for ticket in current.keys() - previous.keys():
                    p = current[ticket]; print("[source trade] OPEN " + json.dumps(safe_position(p), default=str), flush=True)
                    if target_connection:
                        try: await open_copy(p, target_connection)
                        except Exception as exc:
                            print(f"[copy] source #{ticket} open failed: {exc}", flush=True); await tg_send(f"⚠️ COPY FAILED\n\nSource #{ticket} {p.get('symbol')}\n{exc}")
                for ticket in previous.keys() - current.keys(): print("[source trade] CLOSED " + json.dumps(safe_position(previous[ticket]), default=str), flush=True)
                if target_connection:
                    await manage_open_mappings(current, target_connection); STATE["targetConnected"] = bool(target_connection.terminal_state.connected_to_broker)
                STATE.update({"status": "connected" if source_terminal.connected_to_broker else "broker-disconnected", "sourceConnected": bool(source_terminal.connected), "brokerConnected": bool(source_terminal.connected_to_broker), "positionCount": len(current), "positions": [safe_position(p) for p in current.values()], "settings": get_settings(), "telegramPaired": bool(get_auth("telegram_chat_id")), "lastUpdate": now_iso(), "lastError": None})
                previous = current; await asyncio.sleep(0.5)
        except asyncio.CancelledError: raise
        except Exception as exc:
            STATE.update({"status": "reconnecting", "sourceConnected": False, "brokerConnected": False, "targetConnected": False, "lastError": str(exc), "lastUpdate": now_iso()}); print(f"[worker] error: {exc}", flush=True); await asyncio.sleep(retry_seconds)
        finally:
            for connection in (target_connection, source_connection):
                if connection is not None:
                    try: await connection.close()
                    except Exception: pass


async def main() -> None:
    init_db(); STATE["settings"] = get_settings(); threading.Thread(target=start_health_server, daemon=True).start(); tasks = [asyncio.create_task(trading_loop())]
    if os.getenv("TELEGRAM_BOT_TOKEN", "").strip(): tasks.append(asyncio.create_task(telegram_loop()))
    await asyncio.gather(*tasks)


if __name__ == "__main__": asyncio.run(main())
