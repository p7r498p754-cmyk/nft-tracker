"""Private beta scheduling and access. All state belongs in the private SQLite database."""
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
import json

from .valuation import MarketEvent

MINUTES = {"FREE": 30, "PLUS": 15, "PRO": 5, "ULTRA": 1}
SCHEMA = """
CREATE TABLE IF NOT EXISTS beta_invites(uid INTEGER PRIMARY KEY, plan TEXT NOT NULL, ends INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS beta_ticks(user_key TEXT PRIMARY KEY REFERENCES users ON DELETE CASCADE, at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS beta_feed(id TEXT PRIMARY KEY, at REAL NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS beta_feedback(uid INTEGER PRIMARY KEY REFERENCES beta_invites ON DELETE CASCADE, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS public_rate(uid INTEGER PRIMARY KEY, bucket INTEGER NOT NULL, hits INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS public_rate_bucket ON public_rate(bucket);
CREATE TABLE IF NOT EXISTS beta_rate(uid INTEGER PRIMARY KEY REFERENCES beta_invites ON DELETE CASCADE, bucket INTEGER, hits INTEGER);
"""


def allowed(cfg, store, uid):
    if uid == cfg.owner_chat_id:
        return True
    if cfg.closed_beta:
        return bool(store.db.execute("SELECT 1 FROM beta_invites WHERE uid=?", (uid,)).fetchone())
    return cfg.public_users


def invite(store, uid, plan, ends):
    if uid <= 0 or plan not in MINUTES:
        return False
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        if not store.db.execute("SELECT 1 FROM beta_invites WHERE uid=?", (uid,)).fetchone():
            if store.db.execute("SELECT count(*) FROM beta_invites").fetchone()[0] >= 20:
                return False
        store.db.execute("INSERT INTO beta_invites VALUES (?,?,?) ON CONFLICT(uid) DO UPDATE SET plan=excluded.plan,ends=excluded.ends",
                         (uid, plan, ends))
    return True


def admit_request(store, uid, stamp):
    with store.db:
        row = store.db.execute("""INSERT INTO beta_rate VALUES (?,?,1) ON CONFLICT(uid) DO UPDATE SET
            hits=CASE WHEN bucket=excluded.bucket THEN hits+1 ELSE 1 END,bucket=excluded.bucket
            RETURNING hits""", (uid, stamp // 60)).fetchone()
    return row[0] <= 20


def admit_public_request(store, uid, stamp):
    bucket = stamp // 60
    with store.db:
        store.db.execute("DELETE FROM public_rate WHERE bucket<?", (bucket - 1,))
        hits, = store.db.execute("""INSERT INTO public_rate VALUES (?,?,1) ON CONFLICT(uid) DO UPDATE SET
            hits=CASE WHEN bucket=excluded.bucket THEN min(hits+1,21) ELSE 1 END,bucket=excluded.bucket
            RETURNING hits""", (uid, bucket)).fetchone()
    return hits <= 20


def feed(store, events, cutoff):
    with store.db:
        for ev in sorted(events, key=lambda item: item.event_time):
            payload = asdict(ev)
            payload.update(price=str(ev.price), event_time=ev.event_time.isoformat())
            store.db.execute("""INSERT INTO beta_feed VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET
                at=excluded.at,payload=excluded.payload WHERE excluded.at>=at""",
                (ev.gift_id, ev.event_time.timestamp(), json.dumps(payload)))
        store.db.execute("DELETE FROM beta_feed WHERE at<?", (cutoff.timestamp(),))
    result = []
    for raw, in store.db.execute("SELECT payload FROM beta_feed"):
        data = json.loads(raw)
        data.update(price=Decimal(data["price"]), event_time=datetime.fromisoformat(data["event_time"]))
        result.append(MarketEvent(**data))
    return result


def due(store, user, plan, stamp):
    row = store.db.execute("SELECT at FROM beta_ticks WHERE user_key=?", (user.user_key,)).fetchone()
    return row is None or stamp - row[0] >= MINUTES[plan] * 60
