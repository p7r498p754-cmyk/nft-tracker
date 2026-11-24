// Private D1 binding DB. This worker never receives a Telegram or Portals token.
export function normalize(data, now) {
  if (!Array.isArray(data?.gifts) || !data.gifts.length ||
      (data.count !== undefined && data.count !== data.gifts.length)) throw Error("catalog_shape");
  const seen = new Set();
  let offers = 0;
  const rows = data.gifts.map(g => {
    if (!g?.slug || seen.has(g.slug) || typeof g.name !== "string" || !Array.isArray(g.offers)) throw Error("catalog_row");
    seen.add(g.slug);
    const prices = {};
    for (const o of g.offers) {
      const currency = {telegram:"STARS", fragment:"GRAM", portals:"GRAM"}[o?.market];
      const age = now - Date.parse(o?.updated_at) / 1000;
      if (currency && o.currency === currency && age >= 0 && age <= 10800 &&
          ["string","number"].includes(typeof o.price) && Number.isFinite(Number(o.price)) && Number(o.price)>0) {
        prices[o.market] = {value:String(o.price), currency, at:o.updated_at};
        offers++;
      }
    }
    return {name:g.name, slug:g.slug, prices};
  });
  if (!offers) throw Error("no_recent_prices");
  return rows;
}

export async function tick(db, now, load=fetch) {
  const lease = await db.prepare("UPDATE monitor SET lease_until=?,last_attempt=? WHERE id=1 AND lease_until<=? AND coalesce(last_attempt,-1)<?").bind(now+90, now, now, now).run();
  if (!lease.meta.changes) return;
  try {
    const latest = await db.prepare("SELECT max(at) AS at FROM catalog").first();
    const writes = [];
    if (!latest.at || now-latest.at >= 432000) {
      const r = await load("https://starprice.net/api/v1/gifts",{signal:AbortSignal.timeout(10000)});
      if (!r.ok) throw Error("upstream_http");
      const rows = normalize(await r.json(), now);
      writes.push(db.prepare("INSERT INTO catalog VALUES (?,?,?)").bind(now,rows.length,JSON.stringify(rows)));
    }
    writes.push(db.prepare("UPDATE monitor SET gap_seconds=?-last_success,last_success=?,lease_until=0,last_error=NULL WHERE id=1 AND last_attempt=?").bind(now,now,now));
    await db.batch(writes);
  } catch (e) {
    const code = ["upstream_http","catalog_shape","catalog_row","no_recent_prices"].includes(e.message)?e.message:"refresh_error";
    await db.prepare("UPDATE monitor SET last_error=?,lease_until=0 WHERE id=1 AND last_attempt=?").bind(code,now).run();
    throw Error(code);
  }
}
export default {
  async scheduled(event,env) { await tick(env.DB,Math.floor(event.scheduledTime/1000)); },
  async fetch(request,env) {
    const path = new URL(request.url).pathname;
    if (request.method === "GET" && path === "/preview") {
      const record = await env.DB.prepare("SELECT at,snapshot FROM catalog ORDER BY at DESC LIMIT 1").first();
      if (!record) return Response.json({error:"catalog_pending"}, {status:503});
      const rows = JSON.parse(record.snapshot).sort((a,b) => a.name.toLowerCase() < b.name.toLowerCase() ? -1 : Number(a.name.toLowerCase() > b.name.toLowerCase()))
        .slice(0,5).map(({name, prices}) => ({name,prices:Object.fromEntries(Object.entries(prices).map(([market,offer])=>[market,offer.value]))}));
      return Response.json({at:new Date(record.at*1000).toISOString(), rows}, {headers:{"Cache-Control":"public, max-age=60"}});
    }
    if (request.method!=="GET" || path!=="/health") return new Response(null,{status:404});
    const state=await env.DB.prepare("SELECT last_success,gap_seconds,last_error,(SELECT max(at) FROM catalog) AS catalog_at FROM monitor WHERE id=1").first();
    const now=Date.now()/1000;
    const ok=!!state?.last_success && !state.last_error && now-state.last_success<900 && now-state.catalog_at<432900;
    return Response.json(state,{status:ok?200:503,headers:{"Cache-Control":"no-store"}});
  }
};
