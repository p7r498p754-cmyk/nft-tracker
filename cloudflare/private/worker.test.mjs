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
    assert.deepEqual(JSON.parse(sql.prepare("SELECT value FROM state WHERE key='rc_route_health'").get().value),{at:1,status:200,prices:null,rows:0});
});

test("only matching live listings survive verification", async () => {
    const {recentListings} = await import("./worker.mjs");
    const now = Date.now();
    const actions = ["same","sold","changed"].map(id=>({type:"listing",created_at:new Date(now).toISOString(),
        nft:{id,name:"Gift"},amount:"1.00"}));
    const rows = actions.map(({nft})=>({...nft,status:nft.id==="sold"?"sold":"listed",price:nft.id==="changed"?"1.01":"1"}));
    const result = await recentListings("fixture",now,async url=>Response.json(
        url.pathname.endsWith("/actions/") ? {actions} : {results:rows}));
    assert.deepEqual(result.listings.map(row=>row.id),["same"]);
});
