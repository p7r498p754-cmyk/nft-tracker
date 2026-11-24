"""One-time 30-day access through the official Telegram Stars interface."""
from __future__ import annotations

import logging
import secrets

from .subscriptions import MONTH, PLANS, entitlement

LOG = logging.getLogger(__name__)
ORDER_LIFETIME = 1800

SCHEMA = """
CREATE TABLE IF NOT EXISTS star_checkouts (
    payload TEXT PRIMARY KEY REFERENCES star_orders(payload),
    expires INTEGER NOT NULL,
    query_id TEXT UNIQUE,
    completed_charge TEXT UNIQUE);
"""


def create_invoice(db, tg, uid, plan, now):
    amount = PLANS[plan].price
    payload = "stars-" + secrets.token_urlsafe(24)
    with db:
        db.execute("INSERT INTO star_orders VALUES (?,?,?,?)", (payload, uid, plan, amount))
        db.execute("INSERT INTO star_checkouts(payload,expires) VALUES (?,?)", (payload, now + ORDER_LIFETIME))
    return tg.call("sendInvoice", {
        "chat_id": uid, "title": f"NFT Tracker {plan}", "description": "Доступ на 30 дней. Разовый платёж.",
        "provider_token": "", "currency": "XTR", "payload": payload,
        "prices": [{"label": plan, "amount": amount}], "start_parameter": "tariffs",
    }, timeout=5)


def _order(db, uid, event):
    payload, amount = event.get("invoice_payload"), event.get("total_amount")
    if (not isinstance(payload, str) or not payload.startswith("stars-") or len(payload) > 128
            or type(uid) is not int or uid <= 0 or type(amount) is not int or event.get("currency") != "XTR"):
        return None
    return db.execute("""SELECT o.plan,c.expires,c.query_id,c.completed_charge FROM star_orders o
        JOIN star_checkouts c USING(payload) WHERE o.payload=? AND o.uid=? AND o.amount=?""",
                      (payload, uid, amount)).fetchone()


def reserve_checkout(db, uid, query, now):
    with db:
        db.execute("BEGIN IMMEDIATE")
        order = _order(db, uid, query)
        query_id = query.get("id")
        if order is None or not isinstance(query_id, str) or not query_id or order[3] is not None:
            return False
        _, expires, existing_query, _ = order
        if existing_query is not None:
            return existing_query == query_id
        if expires <= now or entitlement(db, uid, now)[0] != "FREE":
            return False
        outstanding = db.execute("""SELECT 1 FROM star_checkouts c JOIN star_orders o USING(payload)
            WHERE o.uid=? AND c.query_id IS NOT NULL AND c.completed_charge IS NULL""", (uid,)).fetchone()
        if outstanding or db.execute("SELECT 1 FROM star_checkouts WHERE query_id=?", (query_id,)).fetchone():
            return False
        db.execute("UPDATE star_checkouts SET query_id=? WHERE payload=?", (query_id, query["invoice_payload"]))
        return True


def answer_checkout(cfg, store, tg, query, now):
    query_id = query.get("id")
    if not isinstance(query_id, str) or not query_id:
        return
    uid = (query.get("from") or {}).get("id")
    ok = (cfg.stars_payments and cfg.public_users and not cfg.on_actions
          and not (uid == cfg.owner_chat_id and cfg.owner_lifetime_ultra)
          and reserve_checkout(store.db, uid, query, now))
    response = {"pre_checkout_query_id": query_id, "ok": bool(ok)}
    if not ok:
        response["error_message"] = "Счёт больше недоступен или есть незавершённая оплата. Используйте /paysupport."
    tg.call("answerPreCheckoutQuery", response, timeout=4)


def settle_receipt(db, uid, event, paid_at, *, refund=False):
    """Apply a Telegram receipt or refund once. Return its plan for a new successful receipt."""
    charge = event.get("telegram_payment_charge_id")
    if not isinstance(charge, str) or not charge:
        return None
    with db:
        db.execute("BEGIN IMMEDIATE")
        order = _order(db, uid, event)
        if order is None:
            return None
        plan, _, query_id, completed = order
        if query_id is None or (completed is not None and completed != charge):
            return None
        payload = event["invoice_payload"]
        for table in ("star_terms", "star_refunds"):
            existing = db.execute(f"SELECT payload FROM {table} WHERE charge=?", (charge,)).fetchone()
            if existing and existing[0] != payload:
                return None
        db.execute("UPDATE star_checkouts SET completed_charge=? WHERE payload=?", (charge, payload))
        if refund:
            db.execute("INSERT OR IGNORE INTO star_refunds VALUES (?,?)", (charge, payload))
            return None
        if completed is not None or event.get("is_recurring") or event.get("subscription_expiration_date"):
            if completed is None:
                raise ValueError("Unexpected recurring payment")
            return None
        if type(paid_at) is not int or not 0 < paid_at <= 253402300799 - MONTH:
            raise ValueError("Invalid payment date")
        db.execute("INSERT INTO star_terms VALUES (?,?,?,?)", (charge, payload, paid_at, paid_at + MONTH))
        return plan
