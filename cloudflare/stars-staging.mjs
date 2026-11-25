// Isolated preview. The normal Telegram API can only show invoices and reject checkout.
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

async function createOrder(db, uid, plan, now) {
    const payload = "staging-" + crypto.randomUUID();
    const amount = PRICES[plan];
    await db.prepare("INSERT INTO orders(payload,user_id,plan,stars,expires_at) VALUES (?,?,?,?,?)")
        .bind(payload, uid, plan, amount, now + 1800).run();
    return { payload, amount };
}

async function currentPlan(db, uid, now) {
    const row = await db.prepare(`
        SELECT plan,paid_at FROM orders WHERE user_id=? AND refunded=0 AND paid_at>? AND paid_at<=?
        ORDER BY paid_at DESC LIMIT 1
    `).bind(uid, now - ACCESS_SECONDS, now).first();
    return row?.plan ?? "FREE";
}

function ordinaryPreview(env) {
    return env.STAGING_TELEGRAM_MODE === "ordinary-preview";
}

async function telegram(env, method, payload = {}, send = fetch) {
    const token = env.TELEGRAM_TEST_BOT_TOKEN;
    if (!/^[0-9]+:[A-Za-z0-9_-]+$/.test(token)) throw new Error("invalid_test_token");
    const preview = ordinaryPreview(env);
    const path = preview ? "" : "/test";
    if (preview && method === "answerPreCheckoutQuery" && payload.ok !== false) {
        throw new Error("ordinary_preview_requires_rejection");
    }
    const response = await send(`https://api.telegram.org/bot${token}${path}/${method}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload), signal: AbortSignal.timeout(4000),
    });
    const data = await response.json();
    if (!response.ok || data.ok !== true) {
        const error = new Error("test_api_rejected");
        error.status = response.status;
        throw error;
    }
    return data.result;
}

export async function connectTestBot(env, send = fetch) {
    const token = env.TELEGRAM_TEST_BOT_TOKEN;
    if (!token || !env.STAGING_WEBHOOK_SECRET || !env.STAGING_WEBHOOK_URL) return "waiting_for_token";
    const mode = ordinaryPreview(env) ? "ordinary-preview" : "test";
    const ready = await env.LEDGER.prepare("SELECT status FROM staging_webhook_mode WHERE mode=?").bind(mode).first();
    if (ready?.status === "ready") return "ready";
    try {
        let bot;
        try {
            bot = await telegram(env, "getMe", {}, send);
        } catch (error) {
            if (error.status === 401 || error.status === 404) return "invalid_test_token";
            throw error;
        }
        if (!bot.is_bot && env.TEST_DC || (env.TEST_DC ? !["nfttrecker_test_bot","nfttrecker_beta_test_bot"].includes(bot.username?.toLowerCase()) : bot.username?.toLowerCase() !== env.TELEGRAM_TEST_BOT_USERNAME)) return "wrong_test_bot";
        const existing = await telegram(env, "getWebhookInfo", {}, send);
        if (existing.url && existing.url !== env.STAGING_WEBHOOK_URL) return "other_webhook";
        if (await telegram(env, "setWebhook", {
            url: env.STAGING_WEBHOOK_URL, secret_token: env.STAGING_WEBHOOK_SECRET, max_connections: 1,
            allowed_updates: ["message", "pre_checkout_query"], drop_pending_updates: false,
        }, send) !== true) return "test_api_rejected";
        const webhook = await telegram(env, "getWebhookInfo", {}, send);
        if (webhook.url !== env.STAGING_WEBHOOK_URL) return "test_api_rejected";
        await env.LEDGER.prepare("INSERT OR REPLACE INTO staging_webhook_mode VALUES (?,'ready')").bind(mode).run();
        return "ready";
    } catch {
        return "test_api_rejected";
    }
}

async function testBotUpdate(env, update, now, send) {
    const call = (method, fields) => telegram(env, method, fields, send);
    if (update.pre_checkout_query) {
        const q = update.pre_checkout_query;
        if (!textId(q.id)) return;
        const preview = ordinaryPreview(env);
        const approved = preview ? false : (await checkout(env.LEDGER, q, now)).approved;
        await call("answerPreCheckoutQuery", { pre_checkout_query_id: q.id, ok: approved,
            ...(approved ? {} : { error_message: preview ? "Проверка завершена. Этот счёт нельзя оплатить, Stars не списаны."
                : "Тестовый счёт недоступен. Попробуйте /subscription." }) });
        return;
    }
    const m = update.message;
    const uid = m?.chat?.id;
    if (m?.chat?.type !== "private" || !validId(uid)) return;
    if (m.successful_payment || m.refunded_payment) {
        // The ledger itself is the source of truth, so replay cannot send another message or grant.
        if (!ordinaryPreview(env)) await settle(env.LEDGER, m, now);
        return;
    }
    if (m.from?.id !== uid || typeof m.text !== "string") return;
    const labels = Object.entries(PRICES).map(([name, amount]) => `${name} · ${amount} ⭐`);
    const choice = labels.indexOf(m.text.trim());
    const input = choice < 0 ? m.text.trim() : "/buy " + Object.keys(PRICES)[choice];
    const [raw, requested] = input.split(/\s+/, 2);
    const command = raw.split("@", 1)[0].toLowerCase();
    if (command === "/buy" && Object.hasOwn(PRICES, requested?.toUpperCase())) {
        const plan = requested.toUpperCase();
        if (await currentPlan(env.LEDGER, uid, now) !== "FREE") {
            await call("sendMessage", { chat_id: uid, text: "У вас уже есть тестовый доступ. /subscription" });
            return;
        }
        const { payload, amount } = await createOrder(env.LEDGER, uid, plan, now);
        await call("sendInvoice", {
            chat_id: uid, title: `NFT Tracker DEMO · ${plan}`,
            description: ordinaryPreview(env) ? "Демонстрационный счёт. Оплата отключена. Не пополняйте баланс для проверки."
                : "Тестовый доступ на 30 дней",
            payload, provider_token: "", currency: "XTR", prices: [{ label: plan, amount }],
            start_parameter: "staging",
        });
        return;
    }
    const text = ordinaryPreview(env)
        ? "NFT Tracker · демо без списаний. Для просмотра счёта отправьте /buy PLUS, /buy PRO или /buy ULTRA."
        : `NFT Tracker · тестовая среда. Ваш тариф: ${await currentPlan(env.LEDGER, uid, now)}.\nПроверка оплаты: /buy PLUS, /buy PRO или /buy ULTRA.\nПосле оплаты: /subscription.`;
    await call("sendMessage", { chat_id: uid, text, reply_markup: {
        keyboard: [labels.map(label => ({ text: label }))], resize_keyboard: true, one_time_keyboard: true
    } });
}

export async function handle(request, env, compare = crypto.subtle.timingSafeEqual.bind(crypto.subtle), send = fetch) {
    const path = new URL(request.url).pathname;
    const db = env.LEDGER;
    if (request.method === "GET" && path === "/health") {
        try {
            await db.prepare("SELECT payload FROM orders LIMIT 1").first();
            const mode = ordinaryPreview(env) ? "ordinary-preview" : "test";
            const connection = await db.prepare("SELECT status FROM staging_webhook_mode WHERE mode=?").bind(mode).first();
            return Response.json({ status: "ok", mode: "staging", telegram: connection?.status ?? "pending" });
        } catch {
            return Response.json({ status: "unavailable" }, { status: 503 });
        }
    }
    if (request.method === "POST" && path === "/setup") {
        if (!await authorized(request, env.STAGING_SETUP_SECRET, compare)) return new Response(null, { status: 404 });
        const status = await connectTestBot(env, send);
        if (status !== "ready") return Response.json({ telegram: status });
        const bot = await telegram(env, "getMe", {}, send);
        const webhook = await telegram(env, "getWebhookInfo", {}, send);
        return Response.json({ telegram: status, username: bot.username,
            webhookMatches: webhook.url === env.STAGING_WEBHOOK_URL,
            pending: webhook.pending_update_count, lastError: webhook.last_error_message,
            lastErrorAt: webhook.last_error_date, allowedUpdates: webhook.allowed_updates });
    }
    if (request.method !== "POST" || !["/telegram", "/fixtures/order", "/fixtures/access"].includes(path)) {
        return new Response(null, { status: 404 });
    }
    if (path !== "/telegram" && env.STAGING_FIXTURES !== "1") return new Response(null, { status: 404 });
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
            return Response.json(await createOrder(db, uid, plan, now), { status: 201 });
        }
        if (path === "/fixtures/access") {
            if (!validId(input.uid)) return new Response(null, { status: 400 });
            return Response.json({ plan: await currentPlan(db, input.uid, now) });
        }
        if (env.TELEGRAM_TEST_BOT_TOKEN) {
            console.log(JSON.stringify({ event: "telegram_webhook_received",
                kind: input.pre_checkout_query ? "checkout" : input.message ? "message" : "other" }));
            await testBotUpdate(env, input, now, send);
            return new Response(null, { status: 204 });
        }
        if (env.STAGING_FIXTURES !== "1") return new Response(null, { status: 503 });
        if (input.pre_checkout_query) return Response.json(await checkout(db, input.pre_checkout_query, now));
        return Response.json(await settle(db, input.message, now));
    } catch {
        console.error(JSON.stringify({ event: "staging_ledger_unavailable" }));
        return new Response(null, { status: 503 });
    }
}

function testEnvironment(env) {
    return {LEDGER: env.TEST_LEDGER, TELEGRAM_TEST_BOT_TOKEN: env.TEST_TELEGRAM_BOT_TOKEN,
        STAGING_WEBHOOK_SECRET: env.TEST_DC_SECRET, STAGING_SETUP_SECRET: env.TEST_SETUP_SECRET,
        STAGING_WEBHOOK_URL: env.STAGING_WEBHOOK_URL.replace(/\/telegram$/, "/test/telegram"), TEST_DC: true};
}

export default {
    fetch(request, env) {
        const url = new URL(request.url);
        if (!url.pathname.startsWith("/test/")) return handle(request,env);
        url.pathname = url.pathname.slice(5);
        return handle(new Request(url,request), testEnvironment(env));
    },
    async scheduled(event, env) {
        if (env.TEST_LEDGER) await connectTestBot(testEnvironment(env));
        const status = await connectTestBot(env);
        if (status !== "ready" && status !== "waiting_for_token") {
            console.warn(JSON.stringify({ event: "test_bot_setup", status }));
        }
    },
};
