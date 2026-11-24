import assert from "node:assert/strict";
import { timingSafeEqual } from "node:crypto";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { test } from "node:test";

import { handle } from "./stars-staging.mjs";

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
            return { bind, first: bind().first };
        },
    };
    const env = { LEDGER: ledger, STAGING_WEBHOOK_SECRET: "fixture-secret" };
    const compare = (left, right) => timingSafeEqual(Buffer.from(left), Buffer.from(right));
    const post = (path, body, secret = env.STAGING_WEBHOOK_SECRET) => handle(
        new Request("https://preview.example" + path, {
            method: "POST", headers: { "X-Telegram-Bot-Api-Secret-Token": secret }, body: JSON.stringify(body),
        }), env, compare);
    const result = async (path, body) => (await post(path, body)).json();

    const health = await handle(new Request("https://preview.example/health"), env, compare);
    assert.deepEqual(await health.json(), { status: "ok", mode: "staging" });
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
    sqlite.close();
});
