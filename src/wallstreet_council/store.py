"""SQLite store: council sessions, every message said in them, and paper positions."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path(os.environ.get("COUNCIL_DB", Path.home() / ".wallstreet-council" / "council.db"))
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, created REAL, status TEXT, budget REAL, horizon TEXT,
  seats TEXT, verdict TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, ts REAL, phase TEXT,
  speaker TEXT, model TEXT, kind TEXT, text TEXT, data TEXT
);
CREATE INDEX IF NOT EXISTS events_session ON events(session, seq);
CREATE TABLE IF NOT EXISTS usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, day TEXT, provider TEXT, model TEXT, ok INTEGER
);
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, ticker TEXT, dollars REAL,
  entry_price REAL, shares REAL, opened REAL, target_date TEXT, status TEXT,
  exit_price REAL, closed REAL
);
"""


def conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    return c


def create_session(sid: str, budget: float, horizon: str, seats: list[dict], status: str = "running") -> None:
    with _lock, conn() as c:
        c.execute("INSERT OR IGNORE INTO sessions VALUES (?,?,?,?,?,?,?,?)",
                  (sid, time.time(), status, budget, horizon, json.dumps(seats), None, None))


def finish_session(sid: str, status: str, verdict: dict | None = None, error: str | None = None) -> None:
    with _lock, conn() as c:
        c.execute("UPDATE sessions SET status=?, verdict=?, error=? WHERE id=?",
                  (status, json.dumps(verdict) if verdict else None, error, sid))


def add_event(sid: str, phase: str, speaker: str, kind: str, text: str, model: str = "",
              data: dict | None = None) -> int:
    with _lock, conn() as c:
        cur = c.execute("INSERT INTO events (session, ts, phase, speaker, model, kind, text, data) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (sid, time.time(), phase, speaker, model, kind, text, json.dumps(data) if data else None))
        return cur.lastrowid


def events(sid: str | None = None, since: int = 0, limit: int = 500) -> list[dict]:
    with conn() as c:
        if sid:
            rows = c.execute("SELECT * FROM events WHERE session=? AND seq>? ORDER BY seq LIMIT ?",
                             (sid, since, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (since, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["data"] = json.loads(d["data"]) if d["data"] else None
        out.append(d)
    return out


def sessions(limit: int = 20) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM sessions ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["seats"] = json.loads(d["seats"] or "[]")
        d["verdict"] = json.loads(d["verdict"]) if d["verdict"] else None
        out.append(d)
    return out


def session(sid: str) -> dict | None:
    for s in sessions(1000):
        if s["id"] == sid:
            return s
    return None


def open_position(sid: str, ticker: str, dollars: float, price: float, target_date: str) -> int:
    with _lock, conn() as c:
        cur = c.execute("INSERT INTO positions (session, ticker, dollars, entry_price, shares, opened, target_date, "
                        "status) VALUES (?,?,?,?,?,?,?,?)",
                        (sid, ticker, dollars, price, dollars / price, time.time(), target_date, "open"))
        return cur.lastrowid


def close_position(pid: int, price: float) -> None:
    with _lock, conn() as c:
        c.execute("UPDATE positions SET status='closed', exit_price=?, closed=? WHERE id=?", (price, time.time(), pid))


def positions(status: str | None = None) -> list[dict]:
    with conn() as c:
        q = "SELECT * FROM positions" + (" WHERE status=?" if status else "") + " ORDER BY id DESC"
        return [dict(r) for r in c.execute(q, (status,) if status else ()).fetchall()]


def _et_day() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")


def record_usage(provider: str, model: str, ok: bool) -> None:
    with _lock, conn() as c:
        c.execute("INSERT INTO usage (ts, day, provider, model, ok) VALUES (?,?,?,?,?)",
                  (time.time(), _et_day(), provider, model, int(ok)))


def usage_today(provider: str) -> int:
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM usage WHERE day=? AND provider=?", (_et_day(), provider)).fetchone()[0]


def last_events(sid: str, n: int = 12) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM events WHERE session=? ORDER BY seq DESC LIMIT ?", (sid, n)).fetchall()
    return [dict(r) for r in reversed(rows)]


def fail_stale_sessions() -> None:
    """Councils left 'running' by a killed process."""
    with _lock, conn() as c:
        c.execute("UPDATE sessions SET status='failed', error='process stopped' "
                  "WHERE status='running' AND created < ?", (time.time() - 3600,))
