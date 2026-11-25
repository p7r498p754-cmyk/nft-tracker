import assert from "node:assert/strict";
import { timingSafeEqual } from "node:crypto";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { test } from "node:test";

import { checkDelivery, connectTestBot, handle } from "./stars-staging.mjs";

test("staging webhook validates order, persists one receipt, and revokes refunded access", async () => {
    const sqlite = new DatabaseSync(":memory:");
    sqlite.exec(readFileSync(new URL("./stars-staging.sql", import.meta.url), "utf8"));
    const ledger = {
        prepare(sql) {
            const statement = sqlite.prepare(sql);
            const bind = (...params) => {
                // Map D1's indexed placeholders to the named keys of node:sqlite.
                const named = {};
                for (const match of sql.matchAll(/\?(\d+)/g)) {
                    named[match[0]] = params[Number(match[1]) - 1];
                }
                const args = Object.keys(named).length ? [named] : params;
                return {
                    all: async () => ({ results: statement.all(...args) }),
                    first: async () => statement.get(...args),
                    run: async () => statement.run(...args),
                };
            };
            return { bind, ...bind() };
        },
    };
    const env = { LEDGER: ledger, STAGING_WEBHOOK_SECRET: "fixture-secret", STAGING_FIXTURES: "1" };
    const compare = (left, right) => timingSafeEqual(Buffer.from(left), Buffer.from(right));
    const post = (path, body, secret = env.STAGING_WEBHOOK_SECRET) => handle(
        new Request("https://preview.example" + path, {
            method: "POST", headers: { "X-Telegram-Bot-Api-Secret-Token": secret }, body: JSON.stringify(body),
        }), env, compare);
    const result = async (path, body) => (await post(path, body)).json();

    const health = await handle(new Request("https://preview.example/health"), env, compare);
    assert.equal(health.status, 503);
    assert.equal((await post("/fixtures/order", { uid: 17, plan: "PRO" }, "wrong")).status, 401);
    assert.equal((await post("/telegram", { padding: "x".repeat(17_000) })).status, 400);
    const order = await result("/fixtures/order", { uid: 17, plan: "PRO" });
    const offer = { invoice_payload: order.payload, currency: "XTR", total_amount: 199 };
    const checkout = (id, uid = 17, fields = {}) => result("/telegram",
        { pre_checkout_query: { ...offer, ...fields, id, from: { id: uid } } });
    const stamp = Math.floor(Date.now() / 1000);
    const receipt = (type, charge, fields = {}) => result("/telegram", {
        message: { chat: { type: "private", id: 17 }, from: { id: 17 }, date: stamp,
                   [type]: { ...offer, ...fields, telegram_payment_charge_id: charge } },
    });

    assert.deepEqual(await checkout("wrong-amount", 17, { total_amount: 99 }), { approved: false });
    assert.deepEqual(await checkout("wrong-user", 18), { approved: false });
    assert.deepEqual(await receipt("successful_payment", "charge"), { recorded: false });
    assert.deepEqual(await checkout("query"), { approved: true });
    assert.deepEqual(await checkout("query"), { approved: true });
    assert.deepEqual(await checkout("other-query"), { approved: false });
    assert.deepEqual(await result("/fixtures/access", { uid: 17 }), { plan: "FREE" });
    assert.deepEqual(await receipt("successful_payment", "charge"), { recorded: true });
    assert.deepEqual(await receipt("successful_payment", "charge"), { recorded: false });
    assert.deepEqual(await result("/fixtures/access", { uid: 17 }), { plan: "PRO" });
    assert.deepEqual(await receipt("refunded_payment", "another-charge"), { recorded: false });
    assert.deepEqual(await receipt("refunded_payment", "charge"), { recorded: true });
    assert.deepEqual(await receipt("successful_payment", "charge"), { recorded: false });
    assert.deepEqual(await result("/fixtures/access", { uid: 17 }), { plan: "FREE" });
    const apiMethods = [];
    const telegramEnv = { ...env, STAGING_FIXTURES: "0", TELEGRAM_TEST_BOT_TOKEN: "12345:test-token",
        TELEGRAM_TEST_BOT_USERNAME: "nfttracker_app", STAGING_WEBHOOK_URL: "https://preview.example/telegram" };
    let webhook = "";
    let invoice, menu;
    const fakeApi = async (url, options) => {
        assert.match(url, /^https:\/\/api\.telegram\.org\/bot[0-9]+:[^/]+\/test\//);
        const method = url.split("/").at(-1);
        const payload = JSON.parse(options.body);
        apiMethods.push(method);
        if (method === "setWebhook") webhook = payload.url;
        if (method === "sendMessage") menu = payload;
        if (method === "sendInvoice") invoice = payload;
        return Response.json({ ok: true, result: method === "getMe" ? { username: "nfttracker_app" } :
            method === "getWebhookInfo" ? { url: webhook } : true });
    };
    assert.equal(await connectTestBot({ ...telegramEnv, TELEGRAM_TEST_BOT_USERNAME: "wrong" }, fakeApi), "wrong_test_bot");
    assert.deepEqual(apiMethods, ["getMe"]);
    assert.equal(await connectTestBot(telegramEnv, fakeApi), "ready");
    assert.equal(webhook, telegramEnv.STAGING_WEBHOOK_URL);
    for (const [info, expected] of [
        [{url:webhook, pending_update_count:0}, "configured"],
        [{url:""}, "webhook_mismatch"],
        [{url:webhook, allowed_updates:["message"]}, "updates_missing"],
    ]) {
        await checkDelivery(telegramEnv, async () => Response.json({ok:true,result:info}));
        const check = await handle(new Request("https://preview.example/health"), telegramEnv, compare);
        assert.equal((await check.json()).delivery.status, expected);
        assert.equal(check.status, expected === "configured" ? 200 : 503);
    }
    const deliverText = text => handle(new Request("https://preview.example/telegram", {
        method: "POST", headers: { "X-Telegram-Bot-Api-Secret-Token": env.STAGING_WEBHOOK_SECRET },
        body: JSON.stringify({ message: { chat: { type: "private", id: 18 }, from: { id: 18 }, text } }),
    }), telegramEnv, compare, fakeApi);
    assert.equal((await deliverText("/buy")).status, 204);
    assert.equal(invoice, undefined);
    const buttons = menu.reply_markup.keyboard.flat().map(button => button.text);
    assert.deepEqual(buttons, ["PLUS · 99 ⭐","PRO · 199 ⭐","ULTRA · 399 ⭐"]);
    const response = await deliverText(buttons[0]);
    assert.equal(response.status, 204);
    assert.equal(invoice.currency, "XTR");
    assert.equal(invoice.prices[0].amount, 99);
    assert.equal(invoice.subscription_period, undefined);
    const previewEnv = { ...telegramEnv, STAGING_TELEGRAM_MODE: "ordinary-preview" };
    let checkoutResponse;
    const previewApi = async (url, options) => {
        assert.match(url, /^https:\/\/api\.telegram\.org\/bot[0-9]+:[^/]+\/answerPreCheckoutQuery$/);
        checkoutResponse = JSON.parse(options.body);
        return Response.json({ ok: true, result: true });
    };
    const deliverPreview = body => handle(new Request("https://preview.example/telegram", {
        method: "POST", headers: { "X-Telegram-Bot-Api-Secret-Token": env.STAGING_WEBHOOK_SECRET },
        body: JSON.stringify(body),
    }), previewEnv, compare, previewApi);
    const payment = { invoice_payload: invoice.payload, total_amount: 99, currency: "XTR" };
    assert.equal((await deliverPreview({ pre_checkout_query: { ...payment, id: "ordinary", from: { id: 18 } } })).status, 204);
    assert.equal(checkoutResponse.ok, false);
    assert.equal(sqlite.prepare("SELECT checkout_id FROM orders WHERE payload=?").get(invoice.payload).checkout_id, null);
    assert.equal((await deliverPreview({ message: { chat: { type: "private", id: 18 }, from: { id: 18 }, date: stamp,
        successful_payment: { ...payment, telegram_payment_charge_id: "ordinary-charge" } } })).status, 204);
    assert.equal(sqlite.prepare("SELECT charge_id FROM orders WHERE payload=?").get(invoice.payload).charge_id, null);
    const deliverTest = body => handle(new Request("https://preview.example/telegram", {
        method:"POST", headers:{"X-Telegram-Bot-Api-Secret-Token":env.STAGING_WEBHOOK_SECRET},
        body:JSON.stringify(body)
    }), telegramEnv, compare, fakeApi);
    await deliverText("/subscription");
    assert.match(menu.text,/Выставлен счёт: PLUS/);
    await deliverTest({pre_checkout_query:{...payment,id:"test-check",from:{id:18}}});
    await deliverText("/subscription");
    assert.match(menu.text,/ещё не прислал квитанцию/);
    const issued = apiMethods.filter(m => m === "sendInvoice").length;
    await deliverText("/buy PRO");
    assert.equal(apiMethods.filter(m => m === "sendInvoice").length,issued);
    for (const field of ["successful_payment","refunded_payment"]) {
        const before = apiMethods.filter(method => method === "sendMessage").length;
        const update = {message:{chat:{type:"private",id:18},from:{id:18},date:stamp,
            [field]:{...payment,telegram_payment_charge_id:"test-charge-18"}}};
        await deliverTest(update);
        await deliverTest(update);
        assert.equal(apiMethods.filter(method => method === "sendMessage").length,before+1);
        assert.match(menu.text,field === "successful_payment" ? /PLUS/ : /FREE/);
        await deliverText("/subscription");
        assert.match(menu.text,field === "successful_payment" ? /PLUS до .* UTC/ : /FREE/);
    }
    sqlite.close();
});
