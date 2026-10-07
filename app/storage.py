"""Persistent SQLite storage under DATA_DIR (``/data`` on Railway).

Tables:
- ``kv``                key/value settings (pause state, lot, risk settings, Telegram chat)
- ``events``            last 500 operator-facing events (also mirrored to stdout logs)
- ``tracked_positions`` dry-run risk state per managed target position
- ``managed_positions`` legacy v4 table, kept untouched for history
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .logging_setup import get_logger

log = get_logger("store")

_TRACKED_COLUMNS = (
    "position_id", "symbol", "side", "volume", "open_price", "open_time", "magic", "client_id",
    "comment", "reason", "simulated_initial_sl", "simulated_sl", "trail_active", "locked_money",
    "floating_money", "broker_profit", "broker_sl", "last_bid", "last_ask", "sl_hit_at",
    "sl_hit_price", "sl_hit_money", "status", "first_seen", "last_seen", "closed_at",
    "final_profit", "missing_polls", "notes",
)


class Store:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "copyfactory_v4.db"
        self.legacy_path = data_dir / "copier.db"
        self.lock = threading.RLock()
        self._init()
        self._migrate_legacy_chat()

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=20, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self.lock, self._db() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS kv (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS managed_positions (
                  position_id TEXT PRIMARY KEY,
                  symbol TEXT NOT NULL,
                  side TEXT NOT NULL,
                  volume REAL NOT NULL,
                  open_price REAL NOT NULL,
                  initial_sl REAL,
                  current_sl REAL,
                  last_profit REAL DEFAULT 0,
                  first_seen TEXT NOT NULL,
                  last_seen TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'open'
                );
                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  created_at TEXT NOT NULL,
                  level TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  message TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tracked_positions (
                  position_id TEXT PRIMARY KEY,
                  symbol TEXT NOT NULL,
                  side TEXT NOT NULL,
                  volume REAL NOT NULL,
                  open_price REAL NOT NULL,
                  open_time TEXT,
                  magic TEXT,
                  client_id TEXT,
                  comment TEXT,
                  reason TEXT,
                  simulated_initial_sl REAL,
                  simulated_sl REAL,
                  trail_active INTEGER NOT NULL DEFAULT 0,
                  locked_money REAL,
                  floating_money REAL,
                  broker_profit REAL,
                  broker_sl REAL,
                  last_bid REAL,
                  last_ask REAL,
                  sl_hit_at TEXT,
                  sl_hit_price REAL,
                  sl_hit_money REAL,
                  status TEXT NOT NULL DEFAULT 'open',
                  first_seen TEXT NOT NULL,
                  last_seen TEXT NOT NULL,
                  closed_at TEXT,
                  final_profit REAL,
                  missing_polls INTEGER NOT NULL DEFAULT 0,
                  notes TEXT
                );
                CREATE INDEX IF NOT EXISTS tracked_status ON tracked_positions(status);
                """
            )
            conn.commit()

    def _migrate_legacy_chat(self) -> None:
        if self.get("telegram_chat_id") or not self.legacy_path.exists():
            return
        try:
            old = sqlite3.connect(self.legacy_path)
            old.row_factory = sqlite3.Row
            row = old.execute("SELECT value FROM auth WHERE key='telegram_chat_id'").fetchone()
            old.close()
            if row and row["value"]:
                self.set("telegram_chat_id", str(row["value"]))
        except Exception as exc:  # legacy DB is optional
            log.warning("legacy_chat_migration_failed", error=str(exc))

    # -- key/value --------------------------------------------------------------

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self.lock, self._db() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def get_float(self, key: str, default: float) -> float:
        try:
            return float(self.get(key, str(default)))
        except (TypeError, ValueError):
            return default

    def set(self, key: str, value: Any) -> None:
        with self.lock, self._db() as conn:
            conn.execute(
                "INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (key, str(value), self.now()),
            )
            conn.commit()

    # -- events -----------------------------------------------------------------

    def event(self, level: str, kind: str, message: str, **fields: Any) -> None:
        """Record an operator-facing event and mirror it to structured stdout logs."""
        getattr(log, level if level in ("info", "warning", "error") else "info")(
            "event", kind=kind, message=message, **fields
        )
        with self.lock, self._db() as conn:
            conn.execute(
                "INSERT INTO events(created_at,level,kind,message) VALUES(?,?,?,?)",
                (self.now(), level, kind, message[:4000]),
            )
            conn.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 500)")
            conn.commit()

    def recent_events(self, limit: int = 40) -> list[dict]:
        with self.lock, self._db() as conn:
            rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # -- tracked positions ----------------------------------------------------------

    def get_tracked(self, position_id: str) -> Optional[dict]:
        with self.lock, self._db() as conn:
            row = conn.execute("SELECT * FROM tracked_positions WHERE position_id=?", (str(position_id),)).fetchone()
        return dict(row) if row else None

    def open_tracked(self) -> list[dict]:
        with self.lock, self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM tracked_positions WHERE status='open' ORDER BY first_seen DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_closed_tracked(self, limit: int = 10) -> list[dict]:
        with self.lock, self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM tracked_positions WHERE status='closed' ORDER BY closed_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def save_tracked(self, row: dict) -> None:
        unknown = set(row) - set(_TRACKED_COLUMNS)
        if unknown:
            raise KeyError(f"unknown tracked_positions columns: {sorted(unknown)}")
        cols = list(row)
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "position_id")
        with self.lock, self._db() as conn:
            conn.execute(
                f"INSERT INTO tracked_positions({','.join(cols)}) VALUES({placeholders}) "
                f"ON CONFLICT(position_id) DO UPDATE SET {updates}",
                [row[c] for c in cols],
            )
            conn.commit()
