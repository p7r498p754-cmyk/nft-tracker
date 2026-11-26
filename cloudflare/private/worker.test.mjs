import assert from "node:assert/strict";
import { timingSafeEqual } from "node:crypto";
import test from "node:test";
import { DatabaseSync } from "node:sqlite";
import worker from "./worker.mjs";

test("private controls preserve settings and failed catalog reads record partial health", async t => {
    Object.defineProperty(crypto.subtle, "timingSafeEqual", {value:timingSafeEqual, configurable:true});
    t.after(() => delete crypto.subtle.timingSafeEqual);
    const sql = new DatabaseSync(":memory:");
    t.after(() => sql.close());
    sql.exec("CREATE TABLE users(user_key TEXT PRIMARY KEY,created_at TEXT,active INTEGER DEFAULT 1,min_discount INTEGER DEFAULT 30); CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT);");
    const env = { INTERNAL_TOKEN:"fixture", DB:{prepare(query) {
        const stmt = sql.prepare(query);
        return {first:()=>stmt.get(), bind:(...args)=>({run:()=>stmt.run(...args)}),run:()=>stmt.run()};
    }}, CATALOG:{prepare(){throw Error("catalog unavailable");}} };
    const call = (name,token) => worker.fetch(new Request("https://local/command?name="+name,{
        method:"POST",headers:{"X-NFT-Internal-Token":token}
    }), env);
    assert.equal((await call("start","wrong")).status,403);
    assert.equal((await (await call("start","fixture")).json()).monitoring_enabled,true);
    sql.exec("UPDATE users SET min_discount=40");
    await call("stop","fixture");
    assert.deepEqual(await (await call("status","fixture")).json(),{monitoring_enabled:false,discount_percent:40});
    await assert.rejects(worker.scheduled({scheduledTime:1},env));
    assert.deepEqual(JSON.parse(sql.prepare("SELECT value FROM state").get().value),{at:1,status:200,prices:null,rows:0});
});
