// Developer features: run against the dev server, with keys in FREE_KEY / PRO_KEY / BUSINESS_KEY.
// Starter calls use a fresh key, because the shared FREE_KEY (a Starter key) is limited to 60 calls a minute.
// OAuth tests need OAUTH_ENABLED=1 in .dev.vars; they skip themselves when it is off.
import assert from "node:assert/strict";
import { createHash, randomBytes } from "node:crypto";
import test from "node:test";

const BASE = process.env.BASE ?? "http://localhost:8788";
const PRO = process.env.PRO_KEY;
const get = (path, key) => fetch(BASE + path, { headers: key ? { authorization: `Bearer ${key}` } : {} });
const rpc = (token, method, params, id = 1) =>
  fetch(BASE + "/mcp", { method: "POST", headers: { authorization: `Bearer ${token}`, "content-type": "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", id, method, params }) });

async function signIn() {
  const email = `dev${Date.now()}${Math.floor(Math.random() * 1e6)}@example.com`;
  const res = await fetch(BASE + "/login", { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded", origin: BASE }, body: `email=${encodeURIComponent(email)}` });
  const link = (await res.text()).match(/href="([^"]*\/auth\/verify\?token=[^"]+)"/)[1].replace(/&amp;/g, "&");
  const verify = await fetch(link, { redirect: "manual" });
  return { email, link, verify, cookie: verify.headers.get("set-cookie").split(";")[0] };
}
async function freshKey() {
  const { cookie } = await signIn();
  const html = await (await fetch(BASE + "/dashboard/key", { method: "POST", headers: { cookie, origin: BASE } })).text();
  return html.match(/<pre>(jsk_[^<]+)<\/pre>/)[1];
}

const FREE = await freshKey();

test("X-Data-As-Of is on data responses, not on errors or usage", async () => {
  const r = await get("/v1/large-holdings?limit=1", PRO);
  assert.match(r.headers.get("x-data-as-of"), /^\d{4}-\d{2}-\d{2}$/);
  assert.match((await get("/v1/companies/7203/financials", FREE)).headers.get("x-data-as-of"), /^\d{4}-\d{2}-\d{2}$/);
  assert.equal((await get("/v1/companies/0000", FREE)).headers.get("x-data-as-of"), null);
  assert.equal((await get("/v1/usage", FREE)).headers.get("x-data-as-of"), null);
});

test("format=csv: list endpoints only, flattened, with source credit", async () => {
  const r = await get("/v1/companies?limit=3&format=csv", FREE);
  assert.equal(r.status, 200);
  assert.match(r.headers.get("content-type"), /text\/csv/);
  assert.match(r.headers.get("x-attribution"), /EDINET/);
  assert.ok(r.headers.get("x-next-cursor"));
  assert.ok(r.headers.get("x-data-as-of"));
  const lines = (await r.text()).trim().split("\r\n");
  assert.equal(lines.length, 4);
  assert.match(lines[0], /^ticker,/);
  assert.equal((await get("/v1/companies/7203?format=csv", FREE)).status, 400);
  assert.equal((await get("/v1/companies?format=xml", FREE)).status, 400);
  assert.equal((await get("/v1/webhooks?format=csv", FREE)).status, 400);
  const nested = await (await get("/v1/events?limit=5&format=csv", PRO)).text();
  assert.ok(!nested.split("\r\n")[0].includes("[object"));
  assert.ok(!nested.includes("[object Object]"));
});

test("CSV guard neutralises formulas but leaves numbers alone", async () => {
  const { csvCell, toCsv, flatten } = await loadFormat();
  for (const bad of ["=1+1", "+cmd", "-2+3", "@SUM(A1)", "\t=1", " =1", "＝1", "\r=1"]) assert.ok(csvCell(bad).replace(/^"/, "").startsWith("'"), JSON.stringify(bad));
  assert.equal(csvCell(-5.5), "-5.5");
  assert.equal(csvCell("Toyota"), "Toyota");
  assert.equal(csvCell('a,"b"'), '"a,""b"""');
  assert.equal(csvCell(null), "");
  assert.deepEqual([...flatten({ a: { b: 1 }, c: [1, 2] }).entries()], [["a.b", 1], ["c", "[1,2]"]]);
  assert.equal(toCsv([{ "=x": 1 }]), "'=x\r\n1\r\n");
});
// format.ts has only type imports from hono, so Node can load it directly when type stripping exists.
async function loadFormat() {
  try { return await import("../src/format.ts"); } catch { return localFormat; }
}
const localFormat = await (async () => {
  const { readFileSync, writeFileSync, mkdtempSync } = await import("node:fs");
  const { tmpdir } = await import("node:os");
  const { join } = await import("node:path");
  const ts = (await import("typescript")).default;
  const out = ts.transpileModule(readFileSync(new URL("../src/format.ts", import.meta.url), "utf8"), { compilerOptions: { module: "ESNext", target: "ES2022" } }).outputText;
  const file = join(mkdtempSync(join(tmpdir(), "jf-")), "format.mjs");
  writeFileSync(file, out);
  return import(file);
})();

test("per-minute limit: 429 with Retry-After, and refused calls are not billed", async () => {
  const key = await freshKey();
  const first = await get("/v1/usage", key);
  assert.equal(first.headers.get("x-ratelimit-limit-minute"), "60");
  assert.equal(first.headers.get("ratelimit-limit"), "20000", "RateLimit-* stay the monthly quota");
  // Stay inside one fixed window, or the counter resets mid-test.
  const toBoundary = 60_000 - (Date.now() % 60_000);
  if (toBoundary < 15_000) await new Promise((r) => setTimeout(r, toBoundary + 200));
  const rs = await Promise.all(Array.from({ length: 70 }, () => get("/v1/status_none", key)));
  const limited = rs.filter((r) => r.status === 429);
  assert.ok(limited.length >= 1 && limited.length <= 70, `limited ${limited.length}`);
  const retry = Number(limited[0].headers.get("retry-after"));
  assert.ok(retry >= 1 && retry <= 60, "retry within the minute, not next month");
  assert.equal(limited[0].headers.get("x-ratelimit-remaining-minute"), "0");
});

test("batch: 50 at most, one request per ticker, reports what was not found", async () => {
  const key = await freshKey();
  const before = (await (await get("/v1/usage", key)).json()).requests_used;
  const res = await get("/v1/batch/companies?tickers=7203,6758,9999,7203", key);
  const body = await res.json();
  assert.deepEqual(body.data.map((d) => d.ticker), ["7203", "6758"]);
  assert.deepEqual(body.not_found, ["9999"]);
  assert.equal((await (await get("/v1/usage", key)).json()).requests_used, before + 4, "3 tickers + the usage call");
  const fin = await (await get("/v1/batch/financials?tickers=3930", key)).json();
  assert.equal(fin.data[0].key_metrics[0].revenue, 3647171000);
  assert.equal(fin.data[0].statements, undefined);
  const many = Array.from({ length: 51 }, (_, i) => String(1000 + i)).join(",");
  assert.equal((await get(`/v1/batch/companies?tickers=${many}`, key)).status, 400);
  assert.equal((await get("/v1/batch/companies?tickers=7203';DROP", key)).status, 400);
});

test("usage, company list paging, sectors, EDINET-code search", async () => {
  const u = await (await get("/v1/usage", PRO)).json();
  assert.equal(u.plan, "pro");
  assert.equal(u.per_minute_limit, 600);
  assert.ok(u.requests_used >= 1 && u.resets_at);
  const a = await (await get("/v1/companies?limit=2", FREE)).json();
  const b = await (await get(`/v1/companies?limit=2&cursor=${a.next_cursor}`, FREE)).json();
  assert.ok(b.data[0].ticker > a.data[1].ticker);
  const s = await (await get("/v1/sectors", FREE)).json();
  assert.ok(s.data.some((x) => x.slug === "banks" && x.sector_ja === "銀行業" && x.company_count > 50));
  const found = await (await get("/v1/companies/search?q=E02144", FREE)).json();
  assert.equal(found.data[0].ticker, "7203");
});

test("llms files come from OpenAPI and omit indicator methods", async () => {
  const short = await (await get("/llms.txt")).text();
  const full = await (await get("/llms-full.txt")).text();
  assert.match(short, /GET \/v1\/screener/);
  assert.match(full, /### GET \/v1\/companies\/\{ticker\}\/peers/);
  for (const text of [short, full]) assert.doesNotMatch(text, /firm-level|EPU|percentile-based|formula|with inputs and percentiles/i);
});

test("screener: on every plan, allow-listed, parameterized, no valuation words", async () => {
  assert.equal((await get("/v1/screener", FREE)).status, 200);
  for (const bad of ["min_price=1", "sort=revenue;DROP TABLE users", "min_roe=abc", "sector=nowhere", "min_roe);DROP TABLE users;--=1", "cursor=1'--", "min_stated_rating=great"]) {
    assert.equal((await get(`/v1/screener?${bad}`, PRO)).status, 400, bad);
  }
  const r = await (await get("/v1/screener?sector=transportation-equipment&min_roe=10&min_equity_ratio=30&sort=revenue&limit=5", PRO)).json();
  assert.ok(r.data.length > 0);
  for (const d of r.data) { assert.ok(d.roe >= 10 && d.equity_ratio >= 30); assert.equal(d.sector, "transportation-equipment"); }
  assert.ok(r.data[0].revenue >= r.data.at(-1).revenue);
  const next = await (await get(`/v1/screener?sector=transportation-equipment&min_roe=10&min_equity_ratio=30&sort=revenue&limit=5&cursor=${r.next_cursor}`, PRO)).json();
  assert.ok(next.data[0].revenue <= r.data.at(-1).revenue);
  assert.ok(!next.data.some((d) => r.data.some((e) => e.ticker === d.ticker)));
  const rated = await (await get("/v1/screener?min_stated_rating=AA-&sort=revenue&limit=5", PRO)).json();
  assert.ok(rated.data.every((d) => d.stated_rating && d.stated_rating_agency && d.stated_rating_date));
  assert.doesNotMatch(JSON.stringify(Object.keys(r)) + (await (await get("/llms-full.txt")).text()), /\b(cheap|undervalued|bargain)\b/i);
  assert.equal(JSON.stringify(r).includes('"price"'), false);
});

test("screener read cost: bounded, repeat query served from cache", async () => {
  const worst = `/v1/screener?min_cross_held_pct=99.5&limit=20&max_roe=${1000 + Math.floor(Math.random() * 1e6)}`;
  const one = await (await get(worst, PRO)).json();
  assert.ok(one.meta.rows_read <= 4000, `read ${one.meta.rows_read}`);
  const two = await (await get(worst, PRO)).json();
  assert.equal(two.meta.cached, true);
  assert.equal(two.meta.rows_read, 0);
  const top = await (await get("/v1/screener?sort=mean_percentile&limit=5&max_roe=99999", PRO)).json();
  assert.ok(top.meta.rows_read <= 200 || top.meta.cached, `read ${top.meta.rows_read}`);
});

test("peers: same sector, nearest revenue; benchmarks: ordered quartiles", async () => {
  const p = await (await get("/v1/companies/7201/peers?limit=5", PRO)).json();
  assert.equal(p.data.length, 5);
  assert.ok(p.data.every((d) => d.sector_ja === p.company.sector_ja && d.ticker !== "7201"));
  const free = await (await get("/v1/companies/7201/peers?limit=50", FREE)).json();
  assert.ok(free.data.length <= 50);
  assert.equal((await get("/v1/companies/0000/peers", FREE)).status, 404);
  const b = await (await get("/v1/sectors/banks/benchmarks", FREE)).json();
  const roe = b.metrics.roe;
  assert.ok(roe.n >= 5 && roe.q1 <= roe.median && roe.median <= roe.q3);
  assert.equal((await get("/v1/sectors/nowhere/benchmarks", FREE)).status, 404);
});

test("MCP: new tools work and keep plan rules", async () => {
  const names = (await (await rpc(PRO, "tools/list")).json()).result.tools.map((t) => t.name);
  for (const n of ["screen_companies", "get_company_peers", "get_sector_benchmarks", "list_sectors", "list_companies", "get_companies_batch", "get_financials_batch", "get_my_usage"]) assert.ok(names.includes(n), n);
  const call = async (key, name, args) => (await (await rpc(key, "tools/call", { name, arguments: args })).json()).result;
  const s = await call(PRO, "screen_companies", { filters: { min_roe: 20 }, sector: "banks,services", limit: 3 });
  assert.equal(s.isError, false);
  assert.ok(JSON.parse(s.content[0].text).data.every((d) => d.roe >= 20));
  assert.equal((await call(FREE, "screen_companies", {})).isError, false, "screener on every plan");
  assert.equal((await call(PRO, "screen_companies", { filters: { "1=1;--": 1 } })).isError, true);
  assert.equal((await call(PRO, "get_companies_batch", { tickers: ["7203", "6758"] })).isError, false);
  assert.equal(JSON.parse((await call(PRO, "get_my_usage", {})).content[0].text).plan, "pro");
  assert.equal(JSON.parse((await call(PRO, "get_company_peers", { ticker: "7201", limit: 2 })).content[0].text).data.length, 2);
});

// ---------- OAuth ----------

const oauthOn = (await fetch(BASE + "/.well-known/oauth-authorization-server")).status === 200;
const form = (o) => new URLSearchParams(o).toString();
const b64 = (b) => b.toString("base64url");
const REDIRECT = "https://claude.ai/api/mcp/auth_callback";
const RESOURCE = `${BASE}/mcp`;
const post = (path, body, headers = {}) => fetch(BASE + path, { method: "POST", redirect: "manual", headers: { "content-type": "application/x-www-form-urlencoded", ...headers }, body });
const register = async (uris = [REDIRECT], ip = randomBytes(4).toString("hex")) =>
  fetch(BASE + "/oauth/register", { method: "POST", headers: { "content-type": "application/json", "cf-connecting-ip": ip }, body: JSON.stringify({ client_name: "Test <b>client</b>", redirect_uris: uris }) });

function pkce() {
  const verifier = b64(randomBytes(32));
  return { verifier, challenge: b64(createHash("sha256").update(verifier).digest()) };
}
const authzFields = (clientId, challenge, extra = {}) => ({ client_id: clientId, redirect_uri: REDIRECT, response_type: "code", code_challenge: challenge, code_challenge_method: "S256", scope: "mcp:read", state: "st&ate", resource: RESOURCE, ...extra });

async function approve(cookie, fields, decision = "allow") {
  const res = await post("/oauth/authorize", form({ ...fields, decision }), { cookie, origin: BASE });
  assert.equal(res.status, 200);
  const href = (await res.text()).match(/<a href="([^"]+)">Continue/)[1].replace(/&amp;/g, "&");
  return new URL(href);
}
async function fullGrant() {
  const { cookie } = await signIn();
  const { client_id } = await (await register()).json();
  const { verifier, challenge } = pkce();
  const back = await approve(cookie, authzFields(client_id, challenge));
  const code = back.searchParams.get("code");
  const tokenBody = { grant_type: "authorization_code", code, client_id, redirect_uri: REDIRECT, code_verifier: verifier, resource: RESOURCE };
  const tokens = await (await post("/oauth/token", form(tokenBody))).json();
  return { cookie, client_id, verifier, challenge, back, code, tokenBody, tokens };
}

test("OAuth discovery follows the MCP spec", { skip: !oauthOn }, async () => {
  const challenge = await fetch(BASE + "/mcp", { method: "POST", headers: { "content-type": "application/json" }, body: "{}" });
  assert.equal(challenge.status, 401);
  const www = challenge.headers.get("www-authenticate");
  assert.match(www, /resource_metadata="[^"]+\/\.well-known\/oauth-protected-resource\/mcp"/);
  assert.match(www, /scope="mcp:read"/);
  const prm = await (await fetch(www.match(/resource_metadata="([^"]+)"/)[1])).json();
  assert.equal(prm.resource, RESOURCE);
  const as = await (await fetch(BASE + "/.well-known/oauth-authorization-server")).json();
  assert.equal(as.issuer, prm.authorization_servers[0]);
  assert.deepEqual(as.code_challenge_methods_supported, ["S256"]);
  assert.equal(as.authorization_response_iss_parameter_supported, true);
  assert.deepEqual(as.token_endpoint_auth_methods_supported, ["none"]);
  assert.ok(!("client_id_metadata_document_supported" in as), "CIMD is not claimed");
});

test("OAuth registration rejects unsafe redirect URIs", { skip: !oauthOn }, async () => {
  for (const bad of ["http://evil.example/cb", "https://a.example/cb#x", "https://u:p@a.example/cb", "https://*.example/cb", "javascript:alert(1)", "cursor://cb", "not a url"]) {
    assert.equal((await register([bad])).status, 400, bad);
  }
  const ok = await register(["http://localhost:3118/callback", REDIRECT]);
  assert.equal(ok.status, 201);
  const body = await ok.json();
  assert.equal(body.token_endpoint_auth_method, "none");
  assert.equal(body.client_secret, undefined);
});

test("OAuth registration is throttled per IP", { skip: !oauthOn }, async () => {
  const ip = "ip-" + randomBytes(4).toString("hex");
  let status = 0;
  for (let i = 0; i < 61; i++) status = (await register([REDIRECT], ip)).status;
  assert.equal(status, 429);
});

test("OAuth never redirects to an unregistered address", { skip: !oauthOn }, async () => {
  const { client_id } = await (await register()).json();
  const { challenge } = pkce();
  const tries = [
    authzFields(client_id, challenge, { redirect_uri: "https://evil.example/cb" }),
    authzFields(client_id, challenge, { redirect_uri: REDIRECT + "/" }),
    authzFields(client_id, challenge, { redirect_uri: REDIRECT + "?x=1" }),
    authzFields("jfc_unknown", challenge),
  ];
  for (const t of tries) {
    const r = await fetch(`${BASE}/oauth/authorize?${form(t)}`, { redirect: "manual" });
    assert.equal(r.status, 400);
    assert.equal(r.headers.get("location"), null);
    assert.doesNotMatch(await r.text(), /evil\.example/);
  }
  // A loopback client may change port, and nothing else.
  const lb = await (await register(["http://localhost:3118/callback"])).json();
  const portOk = await fetch(`${BASE}/oauth/authorize?${form(authzFields(lb.client_id, challenge, { redirect_uri: "http://localhost:5555/callback" }))}`, { redirect: "manual" });
  assert.equal(portOk.status, 200);
  const pathBad = await fetch(`${BASE}/oauth/authorize?${form(authzFields(lb.client_id, challenge, { redirect_uri: "http://localhost:5555/other" }))}`, { redirect: "manual" });
  assert.equal(pathBad.status, 400);
});

test("OAuth errors after validation return to the client with iss and state; PKCE is mandatory", { skip: !oauthOn }, async () => {
  const { client_id } = await (await register()).json();
  for (const [extra, error] of [[{ code_challenge_method: "plain" }, "invalid_request"], [{ code_challenge: "short" }, "invalid_request"], [{ scope: "admin" }, "invalid_scope"], [{ resource: "https://other.example/mcp" }, "invalid_target"], [{ response_type: "token" }, "unsupported_response_type"]]) {
    const r = await fetch(`${BASE}/oauth/authorize?${form(authzFields(client_id, pkce().challenge, extra))}`, { redirect: "manual" });
    assert.equal(r.status, 302, JSON.stringify(extra));
    const u = new URL(r.headers.get("location"));
    assert.equal(u.origin + u.pathname, REDIRECT);
    assert.equal(u.searchParams.get("error"), error);
    assert.equal(u.searchParams.get("state"), "st&ate");
    assert.ok(u.searchParams.get("iss"));
  }
});

test("OAuth sign-in resumes the request; the client name is escaped; cross-site approval is refused", { skip: !oauthOn }, async () => {
  const { client_id } = await (await register()).json();
  const fields = authzFields(client_id, pkce().challenge);
  const url = `${BASE}/oauth/authorize?${form(fields)}`;
  const out = await fetch(url, { redirect: "manual" });
  assert.equal(out.status, 200);
  assert.match(await out.clone().text(), /Sign in/);
  const pending = out.headers.get("set-cookie").split(";")[0];
  // Sign in with the pending cookie present, as the same browser would.
  const email = `resume${Date.now()}@example.com`;
  const login = await post("/login", `email=${encodeURIComponent(email)}`, { origin: BASE });
  const link = (await login.text()).match(/href="([^"]*\/auth\/verify\?token=[^"]+)"/)[1].replace(/&amp;/g, "&");
  const verify = await fetch(link, { redirect: "manual", headers: { cookie: pending } });
  const loc = verify.headers.get("location");
  assert.ok(loc.startsWith("/oauth/authorize?"), loc);
  const cookie = verify.headers.get("set-cookie").split(";")[0];
  const consent = await fetch(BASE + loc, { headers: { cookie } });
  const html = await consent.text();
  assert.match(html, /Test &lt;b&gt;client&lt;\/b&gt;/);
  assert.match(html, /claude\.ai/);
  assert.match(html, new RegExp(email.replace(".", "\\.")));
  assert.equal((await post("/oauth/authorize", form({ ...fields, decision: "allow" }), { cookie, origin: "https://evil.example" })).status, 403);
  assert.equal((await post("/oauth/authorize", form({ ...fields, decision: "allow" }), { cookie })).status, 403, "Origin is required");
  const denied = await approve(cookie, fields, "deny");
  assert.equal(denied.searchParams.get("error"), "access_denied");
  // A tampered pending cookie still lands on the fixed path.
  const v2 = await signInWithCookie("x=1&client_id=abc&next=https://evil.example");
  assert.ok(v2.startsWith("/oauth/authorize?") && !v2.includes("evil"), v2);
});
async function signInWithCookie(pendingValue) {
  const login = await post("/login", `email=t${Date.now()}x@example.com`, { origin: BASE });
  const link = (await login.text()).match(/href="([^"]*\/auth\/verify\?token=[^"]+)"/)[1].replace(/&amp;/g, "&");
  return (await fetch(link, { redirect: "manual", headers: { cookie: `jf_oauth_pending=${encodeURIComponent(pendingValue)}` } })).headers.get("location");
}

test("OAuth full flow: token works on MCP only, no secrets stored in clear", { skip: !oauthOn }, async () => {
  const g = await fullGrant();
  assert.equal(g.back.origin + g.back.pathname, REDIRECT);
  assert.equal(g.back.searchParams.get("state"), "st&ate");
  assert.ok(g.back.searchParams.get("iss"));
  assert.equal(g.tokens.token_type, "Bearer");
  assert.equal(g.tokens.expires_in, 3600);
  const init = await (await rpc(g.tokens.access_token, "initialize", {})).json();
  assert.equal(init.result.serverInfo.name, "japanstake");
  assert.equal(JSON.parse((await (await rpc(g.tokens.access_token, "tools/call", { name: "get_my_usage", arguments: {} })).json()).result.content[0].text).plan, "starter");
  assert.equal((await get("/v1/companies/7203", g.tokens.access_token)).status, 401, "not a REST credential");
  const bad = await rpc("jfo_wrong", "ping");
  assert.equal(bad.status, 401);
  assert.match(bad.headers.get("www-authenticate"), /invalid_token/);
});

test("OAuth codes: one use, right verifier, client and redirect; replay revokes", { skip: !oauthOn }, async () => {
  const g = await fullGrant();
  const replay = await post("/oauth/token", form(g.tokenBody));
  assert.equal(replay.status, 400);
  assert.equal((await replay.json()).error, "invalid_grant");
  assert.equal((await rpc(g.tokens.access_token, "ping")).status, 401, "tokens from a replayed code are withdrawn");

  const { cookie } = await signIn();
  const { client_id } = await (await register()).json();
  const other = await (await register()).json();
  const attempt = async (mutate) => {
    const { verifier, challenge } = pkce();
    const code = (await approve(cookie, authzFields(client_id, challenge))).searchParams.get("code");
    const r = await post("/oauth/token", form(mutate({ grant_type: "authorization_code", code, client_id, redirect_uri: REDIRECT, code_verifier: verifier, resource: RESOURCE })));
    return [r.status, (await r.json()).error];
  };
  assert.deepEqual(await attempt((b) => ({ ...b, code_verifier: b64(randomBytes(32)) })), [400, "invalid_grant"]);
  assert.deepEqual(await attempt((b) => ({ ...b, client_id: other.client_id })), [400, "invalid_grant"]);
  assert.deepEqual(await attempt((b) => ({ ...b, redirect_uri: REDIRECT + "/x" })), [400, "invalid_grant"]);
  assert.deepEqual(await attempt((b) => ({ ...b, resource: "https://other.example/mcp" })), [400, "invalid_target"]);
  const noVerifier = await attempt(({ code_verifier, ...b }) => b);
  assert.deepEqual(noVerifier, [400, "invalid_request"]);
});

test("OAuth refresh rotates; reuse revokes the grant; revoke and withdrawal work", { skip: !oauthOn }, async () => {
  const g = await fullGrant();
  const refresh = (t, client_id = g.client_id) => post("/oauth/token", form({ grant_type: "refresh_token", refresh_token: t, client_id }));
  const r2 = await (await refresh(g.tokens.refresh_token)).json();
  assert.ok(r2.refresh_token && r2.refresh_token !== g.tokens.refresh_token);
  assert.equal((await rpc(r2.access_token, "ping")).status, 200);
  const reuse = await refresh(g.tokens.refresh_token);
  assert.equal((await reuse.json()).error, "invalid_grant");
  assert.equal((await rpc(r2.access_token, "ping")).status, 401, "reuse withdraws the family");
  assert.equal((await (await refresh(r2.refresh_token)).json()).error, "invalid_grant");

  const h = await fullGrant();
  assert.equal((await post("/oauth/revoke", form({ token: h.tokens.refresh_token, client_id: h.client_id }))).status, 200);
  assert.equal((await rpc(h.tokens.access_token, "ping")).status, 401);
  assert.equal((await post("/oauth/revoke", form({ token: "nonsense" }))).status, 200);

  const k = await fullGrant();
  const page = await (await fetch(BASE + "/oauth/connections", { headers: { cookie: k.cookie } })).text();
  const family = page.match(/name="family" value="([^"]+)"/)[1];
  assert.equal((await post("/oauth/connections/revoke", form({ family }), { cookie: k.cookie, origin: "https://evil.example" })).status, 403);
  assert.equal((await post("/oauth/connections/revoke", form({ family }), { cookie: k.cookie, origin: BASE })).status, 303);
  assert.equal((await rpc(k.tokens.access_token, "ping")).status, 401);
});

test("OAuth token and API key share one account's monthly count", { skip: !oauthOn }, async () => {
  const g = await fullGrant();
  const html = await (await fetch(BASE + "/dashboard/key", { method: "POST", headers: { cookie: g.cookie, origin: BASE } })).text();
  const key = html.match(/<pre>(jsk_[^<]+)<\/pre>/)[1];
  const before = (await (await get("/v1/usage", key)).json()).requests_used;
  await rpc(g.tokens.access_token, "ping");
  const after = (await (await get("/v1/usage", key)).json()).requests_used;
  assert.equal(after, before + 2);
});
