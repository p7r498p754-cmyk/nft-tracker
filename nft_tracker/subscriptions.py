"""Offline settlement model. No method here can contact Telegram or move Stars."""
import secrets
from dataclasses import dataclass

MONTH = 2592000


@dataclass(frozen=True)
class Plan:
    price: int
    alerts: int
    favorites: int
    level: int


PLANS = {name: Plan(*values) for name, values in (
    ("FREE", (0, 3, 1, 0)), ("PLUS", (99, 15, 10, 1)),
    ("PRO", (199, 100, 50, 2)), ("ULTRA", (399, 300, 200, 3)))}

SCHEMA = """
CREATE TABLE IF NOT EXISTS star_orders (
    payload TEXT PRIMARY KEY, uid INTEGER NOT NULL, plan TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK(amount > 0));
CREATE TABLE IF NOT EXISTS star_terms (
    charge TEXT PRIMARY KEY, payload TEXT NOT NULL REFERENCES star_orders(payload),
    starts INTEGER NOT NULL, ends INTEGER NOT NULL CHECK(ends > starts));
CREATE INDEX IF NOT EXISTS order_owner ON star_orders(uid);
CREATE TABLE IF NOT EXISTS star_refunds (
    charge TEXT PRIMARY KEY, payload TEXT NOT NULL REFERENCES star_orders(payload));
CREATE TABLE IF NOT EXISTS test_gift_offers (
    payload TEXT PRIMARY KEY REFERENCES star_orders(payload), gift_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS test_gift_claims (
    payload TEXT PRIMARY KEY REFERENCES test_gift_offers(payload),
    charge TEXT NOT NULL UNIQUE REFERENCES star_terms(charge),
    approved_at INTEGER);
CREATE TABLE IF NOT EXISTS price_history (
    at TEXT PRIMARY KEY, snapshot TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS preferences (
    user_key TEXT PRIMARY KEY REFERENCES users(user_key) ON DELETE CASCADE, settings TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS star_operations (
    operation TEXT NOT NULL, direction TEXT NOT NULL, at INTEGER NOT NULL, category TEXT NOT NULL,
    amount TEXT NOT NULL, PRIMARY KEY(operation,direction));
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY, at INTEGER NOT NULL, action TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alert_slots (
    user_key TEXT NOT NULL, signal TEXT NOT NULL, day TEXT NOT NULL,
    PRIMARY KEY(user_key,signal));
"""


def entitlement(db, uid, now, *, confirmed_only=False):
    row = db.execute("""SELECT o.plan,t.ends FROM star_terms t JOIN star_orders o USING(payload)
        LEFT JOIN star_refunds r ON r.charge=t.charge WHERE o.uid=? AND r.charge IS NULL
        AND t.starts<=? AND t.ends>? AND (?=0 OR EXISTS (SELECT 1 FROM star_checkouts c
        WHERE c.payload=t.payload AND c.completed_charge=t.charge))
        ORDER BY t.ends DESC LIMIT 1""", (uid, now, now, int(confirmed_only))).fetchone()
    return row or ("FREE", None)


def test_order(db, uid, plan, *, gift_id=None):
    if type(uid) is not int or uid <= 0 or plan not in PLANS or not PLANS[plan].price:
        raise ValueError("Invalid sandbox order")
    if gift_id is not None and (not isinstance(gift_id, str) or not gift_id.startswith("test-") or len(gift_id) <= 5):
        raise ValueError("Invalid sandbox gift")
    token = "test-" + secrets.token_urlsafe(24)
    with db:
        db.execute("INSERT INTO star_orders VALUES (?,?,?,?)", (token, uid, plan, PLANS[plan].price))
        if gift_id is not None:
            db.execute("INSERT INTO test_gift_offers VALUES (?,?)", (token, gift_id))
    return token


def invoice_spec(db, token):
    """Fields for createInvoiceLink, for offline validation only."""
    row = db.execute("SELECT plan,amount FROM star_orders WHERE payload=?", (token,)).fetchone()
    if not row or not token.startswith("test-"):
        raise ValueError("Unknown order")
    name, price = row
    return dict(title="NFT Tracker " + name, description="Доступ на 30 дней", payload=token,
                provider_token="", currency="XTR", subscription_period=MONTH,
                prices=[{"label": name, "amount": price}])


def validate_test_checkout(db, query, now):
    uid = (query.get("from") or {}).get("id")
    token = query.get("invoice_payload")
    spec = invoice_spec(db, token)
    row = db.execute("SELECT uid FROM star_orders WHERE payload=?", (token,)).fetchone()
    if (type(uid) is not int or row != (uid,) or query.get("currency") != "XTR"
            or type(query.get("total_amount")) is not int or query["total_amount"] != spec["prices"][0]["amount"]
            or entitlement(db, uid, now)[0] != "FREE"):
        raise ValueError("Checkout declined")


def settle_test_event(db, uid, event, *, refund=False):
    """Trusted offline adapter, never registered as a Telegram message handler."""
    token, charge = event.get("invoice_payload"), event.get("telegram_payment_charge_id")
    if not isinstance(token, str) or not token.startswith("test-") or not isinstance(charge, str) or not charge:
        raise ValueError("Invalid payment identifiers")
    with db:
        db.execute("BEGIN IMMEDIATE")
        order = db.execute("SELECT uid,amount FROM star_orders WHERE payload=?", (token,)).fetchone()
        amount = event.get("total_amount")
        if type(uid) is not int or type(amount) is not int or order != (uid, amount) or event.get("currency") != "XTR":
            raise ValueError("Order mismatch")
        previous = db.execute("SELECT payload,ends FROM star_terms WHERE charge=?", (charge,)).fetchone()
        reversal = db.execute("SELECT payload FROM star_refunds WHERE charge=?", (charge,)).fetchone()
        if (previous and previous[0] != token) or (reversal and reversal[0] != token):
            raise ValueError("Charge mismatch")
        if refund:
            db.execute("INSERT OR IGNORE INTO star_refunds VALUES (?,?)", (charge, token))
            return
        end = event.get("subscription_expiration_date")
        if type(end) is not int or end <= MONTH or end > 253402300799:
            raise ValueError("Invalid term")
        if previous:
            if previous != (token, end):
                raise ValueError("Conflicting replay")
            return
        start = end - MONTH
        collision = db.execute("""SELECT 1 FROM star_terms t JOIN star_orders o USING(payload)
            LEFT JOIN star_refunds r ON r.charge=t.charge WHERE o.uid=? AND r.charge IS NULL
            AND t.starts<? AND t.ends>?""", (uid, end, start)).fetchone()
        if collision and not reversal:
            raise ValueError("Overlapping subscription")
        db.execute("INSERT INTO star_terms VALUES (?,?,?,?)", (charge, token, start, end))

        if event.get("is_recurring") is True and event.get("is_first_recurring") is True and not reversal:
            db.execute("""INSERT OR IGNORE INTO test_gift_claims(payload, charge)
                        SELECT payload, ? FROM test_gift_offers WHERE payload=?""", (charge, token))


def approve_test_gift(db, payload, *, actor, owner, now):
    """Simulate the owner's confirmation within the offline payment ledger."""
    if type(owner) is not int or owner <= 0 or type(actor) is not int or actor != owner:
        raise PermissionError("Owner confirmation required")
    with db:
        db.execute("BEGIN IMMEDIATE")
        return bool(db.execute("""UPDATE test_gift_claims SET approved_at=?
            WHERE payload=? AND approved_at IS NULL AND NOT EXISTS (
                SELECT 1 FROM star_refunds WHERE charge=test_gift_claims.charge
            )""", (now, payload)).rowcount)


def reserve_alert(db, user, signal, day, limit):
    with db:
        db.execute("BEGIN IMMEDIATE")
        count = db.execute("SELECT count(*) FROM alert_slots WHERE user_key=? AND day=?", (user, day)).fetchone()[0]
        if count >= limit:
            return False
        return bool(db.execute("INSERT OR IGNORE INTO alert_slots VALUES (?,?,?)", (user, signal, day)).rowcount)


def apply_test_message(db, message):
    """Consume a payment service message supplied by the offline Telegram test fixture."""
    chat = message.get("chat") or {}
    uid = chat.get("id")
    if chat.get("type") != "private" or type(uid) is not int or uid <= 0:
        raise ValueError("Invalid private payment message")
    fields = [k for k in ("successful_payment", "refunded_payment") if k in message]
    if len(fields) != 1 or not isinstance(message[fields[0]], dict):
        raise ValueError("Expected one settlement event")
    settle_test_event(db, uid, message[fields[0]], refund=fields[0] == "refunded_payment")
