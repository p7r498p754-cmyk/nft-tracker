import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {DatabaseSync} from "node:sqlite";
import test from "node:test";
import worker, {tick} from "./worker.mjs";

test("cron retains the last good snapshot, retries failures and deduplicates a tick", async t => {
  const sqlite = new DatabaseSync(":memory:");
  t.after(() => sqlite.close());
  sqlite.exec(readFileSync(new URL("./schema.sql", import.meta.url), "utf8"));
  const db = {
    prepare(sql) {
      const query = sqlite.prepare(sql);
      const bind = (...args) => ({
        run: () => ({meta: query.run(...args)}),
        first: () => query.get(...args)
      });
      return {...bind(), bind};
    },
    batch(statements) {
      sqlite.exec("BEGIN");
      try {
        for (const s of statements) s.run();
        sqlite.exec("COMMIT");
      } catch (e) {
        sqlite.exec("ROLLBACK");
        throw e;
      }
    }
  };
  let now = Math.floor(Date.now()/1000);
  let calls = 0;
  const load = async () => {
    calls++;
    return Response.json({count:1,gifts:[{name:"Test",slug:"test",offers:[
      {market:"telegram",currency:"STARS",price:10,updated_at:new Date(now*1000).toISOString()}
    ]}]});
  };
  const health = () => worker.fetch(new Request("https://local/health"), {DB:db});
  assert.equal((await health()).status,503);
  await tick(db,now,load);
  await tick(db,now,load);
  await tick(db,now+300,load);
  assert.equal(calls,1);
  assert.equal((await (await health()).json()).gap_seconds,300);
  now += 432000;
  await assert.rejects(tick(db,now,async () => new Response(null,{status:503})), /upstream_http/);
  assert.equal((await health()).status,503);
  assert.equal(sqlite.prepare("SELECT count(*) AS n FROM catalog").get().n,1);
  now += 300;
  await tick(db,now,load);
  assert.equal(sqlite.prepare("SELECT count(*) AS n FROM catalog").get().n,2);
});
