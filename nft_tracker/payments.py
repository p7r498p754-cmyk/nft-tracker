"""One-time 30-day access through the official Telegram Stars interface."""
from __future__ import annotations

import logging
import secrets

from .subscriptions import MONTH, PLANS, entitlement

LOG = logging.getLogger(__name__)
ORDER_LIFETIME = 1800

SCHEMA = """
CREATE TABLE IF NOT EXISTS refund_requests(charge TEXT PRIMARY KEY, requested_at INTEGER NOT NULL);
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


def pending_plan(db, uid):
    row = db.execute("""SELECT o.plan FROM star_checkouts c JOIN star_orders o USING(payload)
        WHERE o.uid=? AND c.query_id IS NOT NULL AND c.completed_charge IS NULL LIMIT 1""",
                     (uid,)).fetchone()
    return row[0] if row else None


def pending_message(plan):
    return (f"Ожидается подтверждение оплаты тарифа {plan} от Telegram. "
            "Если Stars уже списались, повторная оплата не нужна. Если доступ не появился, откройте /paysupport.")


def reserve_checkout(db, uid, query, now):
    with db:
        db.execute("BEGIN IMMEDIATE")
        order = _order(db, uid, query)
        query_id = query.get("id")
        if order is None or not isinstance(query_id, str) or not query_id or order[3] is not None:
            return False
        _, expires, existing_query, _ = order
        if expires <= now or entitlement(db, uid, now, confirmed_only=True)[0] != "FREE":
            return False
        if existing_query is not None:
            return existing_query == query_id
        if pending_plan(db, uid) or db.execute("SELECT 1 FROM star_checkouts WHERE query_id=?", (query_id,)).fetchone():
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
        if event.get("is_recurring") or event.get("subscription_expiration_date"):
            raise ValueError("Unexpected recurring payment")
        if db.execute("SELECT 1 FROM star_terms WHERE charge=?", (charge,)).fetchone():
            return None
        if type(paid_at) is not int or not 0 < paid_at <= 253402300799 - MONTH:
            raise ValueError("Invalid payment date")
        db.execute("INSERT INTO star_terms VALUES (?,?,?,?)", (charge, payload, paid_at, paid_at + MONTH))
        if db.execute("SELECT 1 FROM star_refunds WHERE charge=?", (charge,)).fetchone():
            return None
        return plan

def owner_refund(cfg, store, tg, actor, receipt_id, now, *, confirm=False):
    if actor != cfg.owner_chat_id or not cfg.stars_payments or cfg.on_actions:
        return "Возврат недоступен.", None
    if not receipt_id.isascii() or not receipt_id.isdigit() or len(receipt_id) > 16:
        return "Укажите номер из /payments покупателя.", None
    db = store.db
    row = db.execute("""SELECT t.charge,o.uid,o.plan,o.amount,o.payload,r.charge
        FROM star_terms t JOIN star_orders o USING(payload) JOIN star_checkouts c USING(payload)
        LEFT JOIN star_refunds r ON r.charge=t.charge
        WHERE t.rowid=? AND c.completed_charge=t.charge""", (int(receipt_id),)).fetchone()
    if row is None:
        return "Подтверждённая покупка не найдена.", None
    charge, buyer, plan, amount, payload, refunded = row
    if refunded:
        return "Возврат уже подтверждён.", None
    if not confirm:
        return (f"Вернуть {amount} ⭐ за {plan}, квитанция №{receipt_id}? Доступ по покупке прекратится.",
                [[{"text":"Подтвердить возврат", "callback_data":"refund:" + receipt_id}],
                 [{"text":"Отмена", "callback_data":"menu:main"}]])
    with db:
        reserved = db.execute("INSERT OR IGNORE INTO refund_requests VALUES (?,?)", (charge,now)).rowcount
    if not reserved:
        return "Запрос уже зарегистрирован. Проверьте историю Stars перед дальнейшими действиями.", None
    from .telegram import TelegramError
    try:
        ok = tg.call("refundStarPayment", {"user_id": buyer, "telegram_payment_charge_id": charge}, timeout=5)
    except TelegramError:
        ok = False
    if ok is not True:
        return "Подтверждения от Telegram нет. Требуется сверка истории Stars.", None
    settle_receipt(db,buyer,{"telegram_payment_charge_id":charge, "invoice_payload":payload,
                             "currency":"XTR","total_amount":amount},now,refund=True)
    return "Telegram подтвердил возврат.", None
