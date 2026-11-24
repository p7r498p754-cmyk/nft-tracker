// Isolated integration preview. This Worker has no credentials for Telegram.
const PRICES = { PLUS: 99, PRO: 199, ULTRA: 399 };
const ACCESS_SECONDS = 30 * 86400;
const MAX_BODY_BYTES = 16_384;

async function authorized(request, secret, compare) {
    const received = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
    if (!secret || !received || received.length > 256) return false;
    const encoder = new TextEncoder();
    const [left, right] = await Promise.all([received, secret].map(value =>
        crypto.subtle.digest("SHA-256", encoder.encode(value))));
    return compare(left, right);
}

async function bodyJson(request) {
    if (!request.body) return null;
    const reader = request.body.getReader();
    const chunks = [];
    let size = 0;
    for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > MAX_BODY_BYTES) {
            await reader.cancel();
            return null;
        }
        chunks.push(value);
    }
    try {
        return JSON.parse(await new Blob(chunks).text());
    } catch {
        return null;
    }
}

function validId(value) {
    return Number.isSafeInteger(value) && value > 0;
}

function textId(value) {
    return typeof value === "string" && value.length > 0 && value.length <= 128;
}

function orderMatch(user, event) {
    return validId(user) && event?.currency === "XTR" &&
        typeof event.invoice_payload === "string" &&
        /^staging-[0-9a-f-]{36}$/.test(event.invoice_payload) &&
        Number.isSafeInteger(event.total_amount) && event.total_amount > 0;
}

async function checkout(db, query, now) {
    const uid = query?.from?.id;
    if (!orderMatch(uid, query) || !textId(query.id)) return { approved: false };
    const { results } = await db.prepare(`
        UPDATE OR IGNORE orders SET checkout_id=?1
        WHERE payload=?2 AND user_id=?3 AND stars=?4 AND charge_id IS NULL
          AND (checkout_id=?1 OR (checkout_id IS NULL AND expires_at>?5
              AND NOT EXISTS (SELECT 1 FROM orders existing
                  WHERE existing.user_id=?3 AND existing.refunded=0
                    AND existing.paid_at>?6)))
        RETURNING plan
    `).bind(query.id, query.invoice_payload, uid, query.total_amount, now, now - ACCESS_SECONDS).all();
    return { approved: results.length === 1 };
}

async function settle(db, message, now) {
    const uid = message?.chat?.id;
    const isRefund = Boolean(message?.refunded_payment);
    const event = isRefund ? message.refunded_payment : message?.successful_payment;
    if (message?.chat?.type !== "private" || !orderMatch(uid, event) ||
        !textId(event.telegram_payment_charge_id) || event.is_recurring ||
        (!isRefund && (message.from?.id !== uid || !validId(message.date) || message.date > now))) {
        return { recorded: false };
    }
    const { results } = await db.prepare(`
        UPDATE OR IGNORE orders
        SET charge_id=?4, paid_at=CASE WHEN ?5=1 THEN paid_at ELSE ?6 END,
            refunded=?5
        WHERE payload=?1 AND user_id=?2 AND stars=?3 AND checkout_id IS NOT NULL
          AND refunded=0 AND ((?5=1 AND (charge_id IS NULL OR charge_id=?4)) OR
                               (?5=0 AND charge_id IS NULL))
        RETURNING plan
    `).bind(event.invoice_payload, uid, event.total_amount, event.telegram_payment_charge_id,
        Number(isRefund), isRefund ? null : message.date).all();
    return { recorded: results.length === 1 };
}

export async function handle(request, env, compare = crypto.subtle.timingSafeEqual.bind(crypto.subtle)) {
    const path = new URL(request.url).pathname;
    const db = env.LEDGER;
    if (request.method === "GET" && path === "/health") {
        try {
            await db.prepare("SELECT payload FROM orders LIMIT 1").first();
            return Response.json({ status: "ok", mode: "staging" });
        } catch {
            return Response.json({ status: "unavailable" }, { status: 503 });
        }
    }
    if (request.method !== "POST" || !["/telegram", "/fixtures/order", "/fixtures/access"].includes(path)) {
        return new Response(null, { status: 404 });
    }
    if (!await authorized(request, env.STAGING_WEBHOOK_SECRET, compare)) {
        return new Response(null, { status: 401 });
    }
    const input = await bodyJson(request);
    if (!input || typeof input !== "object") return new Response(null, { status: 400 });
    const now = Math.floor(Date.now() / 1000);
    try {
        if (path === "/fixtures/order") {
            const { uid, plan } = input;
            if (!validId(uid) || !Object.hasOwn(PRICES, plan)) return new Response(null, { status: 400 });
            const payload = "staging-" + crypto.randomUUID();
            await db.prepare("INSERT INTO orders(payload,user_id,plan,stars,expires_at) VALUES (?,?,?,?,?)")
                .bind(payload, uid, plan, PRICES[plan], now + 1800).run();
            return Response.json({ payload, amount: PRICES[plan] }, { status: 201 });
        }
        if (path === "/fixtures/access") {
            if (!validId(input.uid)) return new Response(null, { status: 400 });
            const row = await db.prepare(`
                SELECT plan,paid_at FROM orders WHERE user_id=? AND refunded=0 AND paid_at>? AND paid_at<=?
                ORDER BY paid_at DESC LIMIT 1
            `).bind(input.uid, now - ACCESS_SECONDS, now).first();
            return Response.json({ plan: row?.plan ?? "FREE" });
        }
        if (input.pre_checkout_query) return Response.json(await checkout(db, input.pre_checkout_query, now));
        return Response.json(await settle(db, input.message, now));
    } catch {
        console.error(JSON.stringify({ event: "staging_ledger_unavailable" }));
        return new Response(null, { status: 503 });
    }
}

export default { fetch: (request, env) => handle(request, env) };
