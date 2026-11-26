// Only an authenticated internal caller may exercise the release candidate.
// Before cutover, the caller supplies the private owner identity via a binding.
async function readPortals(url,key,stage,load) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(),8000);
    try {
        // Never forward partner credentials to a redirect destination.
        const response = await load(url,{redirect:"manual",signal:controller.signal,
            headers:{Accept:"application/json",Authorization:"partners " + key.trim()}});
        if (!response.ok) return {status:stage+"_http",code:response.status};
        try {
            return {status:"ok",data:await response.json()};
        } catch {
            return {status:stage+(controller.signal.aborted?"_timeout":"_invalid_json")};
        }
    } catch {
        return {status:stage+(controller.signal.aborted?"_timeout":"_transport")};
    } finally {
        clearTimeout(timer);
    }
}

export async function recentListings(key, now, load=fetch) {
    if (!key) return {status:"key_missing"};
    const url = new URL("https://portal-market.com/partners/market/actions/");
    for (const kind of ["listing","price_update"]) url.searchParams.append("action_types",kind);
    for (const [k,v] of Object.entries({limit:"20",offset:"0",sort_by:"created_at desc"})) url.searchParams.set(k,v);
    const response = await readPortals(url,key,"upstream",load);
    if (response.status !== "ok") return response;
    const actions = response.data?.actions;
    if (!Array.isArray(actions) || actions.length>20) return {status:"invalid_response"};
    const listings = [];
    for (const event of actions) {
        if (!["listing","price_update"].includes(event?.type)) continue;
        const at = Date.parse(event.created_at);
        if (!Number.isFinite(at) || typeof event.nft?.id !== "string" || !event.nft.id ||
            typeof event.nft?.name !== "string" || !event.nft.name.trim() ||
            !/^[0-9]+(?:[.][0-9]+)?$/.test(String(event.amount)) || !(Number(event.amount)>0)) return {status:"invalid_response"};
        if (at<=now && now-at<=45*60*1000) listings.push({id:event.nft.id,name:event.nft.name,price:String(event.amount),at});
    }
    const latest = new Map();
    for (const item of listings) {
        if (!latest.has(item.id) || latest.get(item.id).at < item.at) latest.set(item.id,item);
    }
    if (!latest.size) return {status:"ok",listings:[],observed:0};
    await new Promise(resolve => setTimeout(resolve,1100));
    const search = new URL("https://portal-market.com/partners/nfts/search");
    for (const id of latest.keys()) search.searchParams.append("ids",id);
    search.searchParams.set("status","listed");
    search.searchParams.set("limit",String(latest.size));
    const checked = await readPortals(search,key,"verification",load);
    if (checked.status !== "ok") return checked;
    const results = checked.data?.results;
    if (!Array.isArray(results)) return {status:"invalid_verification"};
    const verified = new Map();
    const decimal = value => String(value).replace(/(\.\d*?)0+$/, "$1").replace(/\.$/,"");
    for (const row of results) {
        const item = latest.get(row?.id);
        if (item && row.status==="listed" && row.name===item.name && decimal(row.price)===decimal(item.price))
            verified.set(item.id,item);
    }
    return {status:"ok",listings:[...verified.values()],observed:latest.size};
}

const worker = {
    async scheduled(event, env) {
        let market;
        try { market = await recentListings(env.PORTALS_API_KEY,event.scheduledTime); }
        catch { market = {status:"request_failed"}; }
        await env.DB.prepare("INSERT OR REPLACE INTO state(key,value) VALUES (?,?)")
            .bind("rc_portals_attempt",JSON.stringify({at:event.scheduledTime,status:market.status,code:market.code})).run();
        if (market.status === "ok") await env.DB.prepare("INSERT OR REPLACE INTO state(key,value) VALUES (?,?)")
            .bind("rc_portals_page", JSON.stringify({at:event.scheduledTime,listings:market.listings,observed:market.observed})).run();
        const probe = name => worker.fetch(new Request("https://internal/command?name="+name, {
            method:"POST", headers:{"X-NFT-Internal-Token":env.INTERNAL_TOKEN}
        }),env);
        const result = {at:event.scheduledTime, status:null, prices:null, rows:0};
        try {
            const status = await probe("status");
            result.status = status.status;
            const prices = await probe("prices");
            result.prices = prices.status;
            if (prices.ok) result.rows = (await prices.json()).rows.length;
        } finally {
            await env.DB.prepare("INSERT OR REPLACE INTO state(key,value) VALUES (?,?)")
                .bind("rc_route_health", JSON.stringify(result)).run();
        }
    },
    async fetch(request, env) {
        if (request.method !== "POST" || new URL(request.url).pathname !== "/command")
            return new Response(null, {status:404});
        const token = request.headers.get("X-NFT-Internal-Token") || "";
        if (!env.INTERNAL_TOKEN || token.length > 256 || !crypto.subtle.timingSafeEqual(
            await crypto.subtle.digest("SHA-256", new TextEncoder().encode(token)),
            await crypto.subtle.digest("SHA-256", new TextEncoder().encode(env.INTERNAL_TOKEN))))
            return new Response(null, {status:403});
        const cmd = new URL(request.url).searchParams.get("name");
        if (cmd === "prices") {
            const row = await env.CATALOG.prepare("SELECT at,snapshot FROM catalog ORDER BY at DESC LIMIT 1").first();
            if (!row) return Response.json({error:"catalog_pending"},{status:503});
            const rows = JSON.parse(row.snapshot).sort((a,b)=>a.name.localeCompare(b.name,"en")).slice(0,5);
            return Response.json({source:"StarPrice", at:row.at, stale:Date.now()/1000-row.at>=432000, rows});
        }
        if (!["start","stop","status"].includes(cmd)) return new Response(null,{status:400});
        // Symbolic owner key matches the Python service and avoids storing a chat ID.
        if (cmd === "start") await env.DB.prepare(
            "INSERT INTO users(user_key,created_at) VALUES ('owner',?) ON CONFLICT(user_key) DO UPDATE SET active=1"
        ).bind(new Date().toISOString()).run();
        if (cmd === "stop") await env.DB.prepare("UPDATE users SET active=0 WHERE user_key='owner'").run();
        const row = await env.DB.prepare("SELECT active,min_discount FROM users WHERE user_key='owner'").first();
        return Response.json({monitoring_enabled:row?.active===1,discount_percent:row?.min_discount??30});
    }
};

export default worker;
