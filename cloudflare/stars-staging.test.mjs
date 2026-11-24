import assert from "node:assert/strict";
import { timingSafeEqual } from "node:crypto";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { test } from "node:test";

import { connectTestBot, handle } from "./stars-staging.mjs";

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
    assert.deepEqual(await health.json(), { status: "ok", mode: "staging", telegram: "pending" });
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
    let invoice;
    const fakeApi = async (url, options) => {
        assert.match(url, /^https:\/\/api\.telegram\.org\/bot[0-9]+:[^/]+\/test\//);
        const method = url.split("/").at(-1);
        const payload = JSON.parse(options.body);
        apiMethods.push(method);
        if (method === "setWebhook") webhook = payload.url;
        if (method === "sendInvoice") invoice = payload;
        return Response.json({ ok: true, result: method === "getMe" ? { username: "nfttracker_app" } :
            method === "getWebhookInfo" ? { url: webhook } : true });
    };
    assert.equal(await connectTestBot({ ...telegramEnv, TELEGRAM_TEST_BOT_USERNAME: "wrong" }, fakeApi), "wrong_test_bot");
    assert.deepEqual(apiMethods, ["getMe"]);
    assert.equal(await connectTestBot(telegramEnv, fakeApi), "ready");
    assert.equal(webhook, telegramEnv.STAGING_WEBHOOK_URL);
    const response = await handle(new Request("https://preview.example/telegram", {
        method: "POST", headers: { "X-Telegram-Bot-Api-Secret-Token": env.STAGING_WEBHOOK_SECRET },
        body: JSON.stringify({ message: { chat: { type: "private", id: 18 }, from: { id: 18 }, text: "/buy PLUS" } }),
    }), telegramEnv, compare, fakeApi);
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
    sqlite.close();
});
