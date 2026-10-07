from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class Store:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "copyfactory_v4.db"
        self.legacy_path = data_dir / "copier.db"
        self.lock = threading.RLock()
        self._init()
        self._migrate_legacy_chat()

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _db(self):
        conn = sqlite3.connect(self.path, timeout=20, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self.lock, self._db() as conn:
            conn.executescript("""
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
            """)
            conn.commit()

    def _migrate_legacy_chat(self):
        if self.get("telegram_chat_id") or not self.legacy_path.exists():
            return
        try:
            old = sqlite3.connect(self.legacy_path)
            old.row_factory = sqlite3.Row
            row = old.execute("SELECT value FROM auth WHERE key='telegram_chat_id'").fetchone()
            old.close()
            if row and row["value"]:
                self.set("telegram_chat_id", str(row["value"]))
        except Exception:
            pass

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self.lock, self._db() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set(self, key: str, value) -> None:
        with self.lock, self._db() as conn:
            conn.execute(
                "INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (key, str(value), self.now()),
            )
            conn.commit()

    def event(self, level: str, kind: str, message: str) -> None:
        with self.lock, self._db() as conn:
            conn.execute("INSERT INTO events(created_at,level,kind,message) VALUES(?,?,?,?)",
                         (self.now(), level, kind, message[:4000]))
            conn.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 500)")
            conn.commit()

    def recent_events(self, limit: int = 40):
        with self.lock, self._db() as conn:
            rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def upsert_position(self, p: dict, initial_sl: float, current_sl: float, profit: float) -> None:
        pid = str(p.get("id"))
        side = "BUY" if str(p.get("type", "")).upper().endswith("BUY") else "SELL"
        now = self.now()
        with self.lock, self._db() as conn:
            conn.execute("""
              INSERT INTO managed_positions(position_id,symbol,side,volume,open_price,initial_sl,current_sl,last_profit,first_seen,last_seen,status)
              VALUES(?,?,?,?,?,?,?,?,?,?,'open')
              ON CONFLICT(position_id) DO UPDATE SET
                current_sl=excluded.current_sl,last_profit=excluded.last_profit,last_seen=excluded.last_seen,status='open'
            """, (pid, str(p.get("symbol")), side, float(p.get("volume") or 0),
                    float(p.get("openPrice") or 0), float(initial_sl or 0), float(current_sl or 0),
                    float(profit or 0), now, now))
            conn.commit()

    def close_missing_positions(self, open_ids: set[str]) -> None:
        with self.lock, self._db() as conn:
            rows = conn.execute("SELECT position_id FROM managed_positions WHERE status='open'").fetchall()
            for row in rows:
                if str(row["position_id"]) not in open_ids:
                    conn.execute("UPDATE managed_positions SET status='closed',last_seen=? WHERE position_id=?",
                                 (self.now(), str(row["position_id"])))
            conn.commit()

    def open_positions(self):
        with self.lock, self._db() as conn:
            rows = conn.execute("SELECT * FROM managed_positions WHERE status='open' ORDER BY first_seen DESC").fetchall()
        return [dict(r) for r in rows]
