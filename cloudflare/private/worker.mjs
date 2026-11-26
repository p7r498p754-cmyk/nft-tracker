// Only an authenticated internal caller may exercise the release candidate.
// Before cutover, the caller supplies the private owner identity via a binding.
const worker = {
    async scheduled(event, env) {
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
