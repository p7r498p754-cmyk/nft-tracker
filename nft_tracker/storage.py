"""SQLite state. Single file, no WAL, so it can be cached/restored between runs.

Owner-only mode uses the symbolic owner key. Multi-user deployments require
private storage because recipient identifiers are needed to deliver alerts."""
from __future__ import annotations

import json
import sqlite3
import weakref
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from .beta import SCHEMA as BETA_SCHEMA
from .payments import SCHEMA as PAYMENTS_SCHEMA
from .subscriptions import SCHEMA as SUBSCRIPTION_SCHEMA
from .valuation import HISTORY_DAYS, MarketEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
  user_key TEXT PRIMARY KEY, chat_id INTEGER, active INTEGER NOT NULL DEFAULT 1,
  min_discount INTEGER NOT NULL DEFAULT 30, tier TEXT NOT NULL DEFAULT 'free', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS deliveries (
  user_key TEXT NOT NULL, signal_id TEXT NOT NULL, sent_at TEXT NOT NULL,
  PRIMARY KEY (user_key, signal_id),
  FOREIGN KEY (user_key) REFERENCES users(user_key) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS sales (
  sale_key TEXT PRIMARY KEY, gift_id TEXT NOT NULL, name TEXT NOT NULL, model TEXT NOT NULL,
  backdrop TEXT NOT NULL, symbol TEXT NOT NULL, price TEXT NOT NULL, event_time TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS sales_time ON sales(event_time);
"""


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class User:
    user_key: str
    chat_id: int | None
    active: bool
    min_discount: int
    tier: str


class Storage:
    def __init__(self, path: Path | str, *, initialized=False):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        if initialized:
            # The webhook factory has already prepared this file. Mode rw fails
            # closed if the database disappears between requests.
            self.db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=rw", uri=True)
        else:
            self.db = sqlite3.connect(str(path))
        # sqlite3 on Python 3.13 warns when a connection is garbage-collected
        # without close(). The main program closes explicitly; the finalizer
        # also releases test/exception-path connections safely.
        self._finalizer = weakref.finalize(self, self.db.close)
        self.db.execute("PRAGMA foreign_keys = ON")
        if initialized:
            return
        self.db.executescript(SCHEMA + SUBSCRIPTION_SCHEMA + PAYMENTS_SCHEMA + BETA_SCHEMA)
        # Backfill deduplication and day budgets for databases created by 0.5.x.
        if not self.get("subscription_migration_v1"):
            with self.db:
                self.db.execute("""INSERT OR IGNORE INTO alert_slots(user_key,signal,day)
                    SELECT user_key,signal_id,substr(sent_at,1,10) FROM deliveries""")
                snapshot = self.get_json("price_catalog")
                if snapshot.get("at") and snapshot.get("rows"):
                    self.db.execute("INSERT OR IGNORE INTO price_history VALUES (?,?)",
                                    (snapshot["at"], json.dumps(snapshot, ensure_ascii=False)))
                self.set("subscription_migration_v1","1")

    def close(self) -> None:
        self._finalizer()

    # --- key/value state -------------------------------------------------
    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, value))
        self.db.commit()

    def get_json(self, key: str) -> dict:
        try:
            value = json.loads(self.get(key, "{}") or "{}")
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}

    # --- users -----------------------------------------------------------
    def ensure_user(self, user_key: str, chat_id: int | None, default_discount: int, now: datetime,
                    *, activate: bool = False) -> User:
        self.db.execute("INSERT OR IGNORE INTO users(user_key,chat_id,active,min_discount,tier,created_at) "
                        "VALUES (?,?,1,?, 'free', ?)", (user_key, chat_id, default_discount, iso(now)))
        if activate:
            self.db.execute("UPDATE users SET active=1 WHERE user_key=?", (user_key,))
        self.db.commit()
        return self.user(user_key)  # type: ignore[return-value]

    def user(self, user_key: str) -> User | None:
        r = self.db.execute("SELECT user_key,chat_id,active,min_discount,tier FROM users WHERE user_key=?",
                            (user_key,)).fetchone()
        return User(r[0], r[1], bool(r[2]), r[3], r[4]) if r else None

    def active_users(self) -> list[User]:
        rows = self.db.execute("SELECT user_key,chat_id,active,min_discount,tier FROM users WHERE active=1")
        return [User(r[0], r[1], True, r[3], r[4]) for r in rows]

    def set_active(self, user_key: str, active: bool) -> None:
        self.db.execute("UPDATE users SET active=? WHERE user_key=?", (int(active), user_key))
        self.db.commit()

    def set_min_discount(self, user_key: str, value: int) -> None:
        self.db.execute("UPDATE users SET min_discount=? WHERE user_key=?", (value, user_key))
        self.db.commit()

    def delete_user(self, user_key: str) -> None:
        """Removes the user, their settings and delivery history (FK cascade)."""
        self.db.execute("DELETE FROM state WHERE key=?", ("summary:" + user_key,))
        self.db.execute("DELETE FROM users WHERE user_key=?", (user_key,))
        self.db.commit()

    # --- deliveries ------------------------------------------------------
    def was_sent(self, user_key: str, signal_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM deliveries WHERE user_key=? AND signal_id=?",
                               (user_key, signal_id)).fetchone() is not None

    def mark_sent(self, user_key: str, signal_id: str, now: datetime) -> None:
        self.db.execute("INSERT OR IGNORE INTO deliveries SELECT user_key, ?, ? FROM users WHERE user_key=?",
                        (signal_id, iso(now), user_key))
        self.db.commit()

    # --- sales history ---------------------------------------------------
    def add_sales(self, sales: list[MarketEvent]) -> int:
        added = 0
        for s in sales:
            key = f"{s.gift_id}|{iso(s.event_time)}|{s.price}"
            cur = self.db.execute("INSERT OR IGNORE INTO sales VALUES (?,?,?,?,?,?,?,?)",
                                  (key, s.gift_id, s.name, s.model, s.backdrop, s.symbol, str(s.price), iso(s.event_time)))
            added += cur.rowcount
        self.db.commit()
        return added

    def sales(self) -> list[MarketEvent]:
        out = []
        for gid, name, model, bd, sym, price, at in self.db.execute(
                "SELECT gift_id,name,model,backdrop,symbol,price,event_time FROM sales"):
            out.append(MarketEvent("portals", gid, name, model, bd, sym, Decimal(price),
                                   datetime.fromisoformat(at), "purchase"))
        return out

    def sales_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM sales").fetchone()[0]

    def prune(self, now: datetime) -> None:
        self.db.execute("DELETE FROM alert_slots WHERE day < ?", ((now - timedelta(days=HISTORY_DAYS)).date().isoformat(),))
        self.db.execute("DELETE FROM sales WHERE event_time < ?", (iso(now - timedelta(days=HISTORY_DAYS + 1)),))
        self.db.execute("DELETE FROM deliveries WHERE sent_at < ?", (iso(now - timedelta(days=HISTORY_DAYS)),))
        self.db.commit()
