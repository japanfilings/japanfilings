// Run against `wrangler dev --test-scheduled --local-upstream localhost:8788` with data loaded (the upstream flag keeps links local now that a custom domain is routed):
//   BASE=http://localhost:8788 FREE_KEY=... PRO_KEY=... BUSINESS_KEY=... npm test
import assert from "node:assert/strict";
import test from "node:test";
import { existsSync, readFileSync } from "node:fs";

// Names that must never appear, from the private word lists (kept out of the repository). Without them the check is skipped.
const LEX = new URL("../../japanfilings/private/lexicon.json", import.meta.url);
const WITHHELD = existsSync(LEX) ? new RegExp(JSON.parse(readFileSync(LEX, "utf8")).withheld.join("|")) : /(?!)/;

const BASE = process.env.BASE ?? "http://localhost:8788";

// Opening a sign-in link shows a button (mail scanners must not use it up); the button POSTs the token.
async function verifyLink(link, headers = {}) {
  const u = new URL(link);
  const page = await fetch(link, { headers });
  if (!(await page.text()).includes('action="/auth/verify"')) throw new Error("no sign-in button on " + link);
  return fetch(u.origin + "/auth/verify", { method: "POST", redirect: "manual", headers: { ...headers, origin: u.origin, "content-type": "application/x-www-form-urlencoded" }, body: new URLSearchParams({ token: u.searchParams.get("token") }) });
}
const get = (path, key) => fetch(BASE + path, { headers: key ? { authorization: `Bearer ${key}` } : {} });
const mcp = (key, body) =>
  fetch(BASE + "/mcp", { method: "POST", headers: { authorization: `Bearer ${key}`, "content-type": "application/json" }, body: JSON.stringify(body) });

test("status needs no key", async () => {
  const r = await get("/v1/status");
  assert.equal(r.status, 200);
  assert.equal((await r.json()).ok, true);
});

test("data endpoints reject missing and wrong keys", async () => {
  assert.equal((await get("/v1/companies/7203")).status, 401);
  assert.equal((await get("/v1/companies/7203", "jf_wrong")).status, 401);
});

test("company profile has English name and symbols", async () => {
  const r = await get("/v1/companies/4042", process.env.FREE_KEY);
  assert.equal(r.status, 200);
  assert.ok(Number(r.headers.get("x-quota-used")) >= 1);
  const c = await r.json();
  assert.equal(c.name_en, "TOSOH CORPORATION");
  assert.equal(c.yahoo_symbol, "4042.T");
  assert.match(c.attribution, /EDINET/);
});

test("search finds Toyota by English name", async () => {
  const { data } = await (await get("/v1/companies/search?q=toyota%20motor", process.env.FREE_KEY)).json();
  assert.ok(data.some((d) => d.ticker === "7203"));
});

test("starter plan: investor lookup and full history are included", async () => {
  assert.equal((await get("/v1/large-holdings?filer=E24872", process.env.FREE_KEY)).status, 200, "Starter has every feature");
  const body = await (await get("/v1/large-holdings?since=2000-01-01", process.env.FREE_KEY)).json();
  assert.equal(body.history_limited_to, null);
});

test("pro plan: investor lookup returns the filing", async () => {
  const body = await (await get("/v1/large-holdings?filer=E24872", process.env.PRO_KEY)).json();
  assert.equal(body.history_limited_to, null);
  const rec = body.data.find((d) => d.doc_id === "S100Z9GY");
  assert.equal(rec.holding_ratio_pct, 6.99);
  assert.equal(rec.issuer_name_en, "TOSOH CORPORATION");
});

test("MCP: initialize, list tools, call a tool", async () => {
  const init = await (await mcp(process.env.PRO_KEY, { jsonrpc: "2.0", id: 1, method: "initialize", params: {} })).json();
  assert.equal(init.result.serverInfo.name, "japanstake");
  assert.equal((await mcp(process.env.PRO_KEY, { jsonrpc: "2.0", method: "notifications/initialized" })).status, 202);
  const list = await (await mcp(process.env.PRO_KEY, { jsonrpc: "2.0", id: 2, method: "tools/list" })).json();
  const toolNames = list.result.tools.map((t) => t.name);
  for (const n of ["search_companies", "get_company", "get_financials", "get_company_details", "get_investor", "get_us_fund_holdings", "get_large_holdings", "get_ownership_events", "get_cross_holdings", "get_disclosed_trades", "get_indicators", "sort_companies_by_metric", "get_earnings_releases", "get_company_brief", "get_disclosed_credit_ratings", "get_share_buybacks", "get_governance_filings", "get_cross_holding_trades", "get_policy_uncertainty_index", "get_cross_holding_unwinding"]) assert.ok(toolNames.includes(n), n);
  const call = await (await mcp(process.env.PRO_KEY, { jsonrpc: "2.0", id: 3, method: "tools/call", params: { name: "get_company", arguments: { ticker: "648A" } } })).json();
  assert.match(call.result.content[0].text, /LECRE Inc\./);
});

test("MCP rejects a missing key", async () => {
  const r = await fetch(BASE + "/mcp", { method: "POST", headers: { "content-type": "application/json" }, body: "{}" });
  assert.equal(r.status, 401);
});

test("financials and details come from the annual report", async () => {
  const fin = await (await get("/v1/companies/3930/financials", process.env.FREE_KEY)).json();
  assert.equal(fin.fiscal_year_end, "2026-07-31");
  assert.equal(fin.key_metrics[0].revenue, 3647171000);
  assert.ok(Object.keys(fin.statements.balance_sheet.current).length > 20);
  const det = await (await get("/v1/companies/3930/details", process.env.FREE_KEY)).json();
  assert.equal(det.english_name, "Hatena Co., Ltd.");
  assert.ok(det.officers.length > 0 && det.major_shareholders.length > 0);
  assert.equal((await get("/v1/companies/0000/financials", process.env.FREE_KEY)).status, 404);
});

test("investor lookup is Pro-only and returns positions", async () => {
  assert.equal((await get("/v1/investors/E24872", process.env.FREE_KEY)).status, 200, "Starter has every feature");
  const inv = await (await get("/v1/investors/E24872", process.env.PRO_KEY)).json();
  assert.ok(inv.latest_positions.some((p) => p.ticker === "4042"));
});

test("website pages render", async () => {
  for (const path of ["/", "/pricing", "/docs", "/companies/3930", "/login"]) {
    const r = await get(path);
    assert.equal(r.status, 200, path);
    assert.match(await r.text(), /<title>/, path);
  }
  assert.equal((await get("/companies/0000")).status, 404);
});

test("sign up by email link, create a key, use it", async () => {
  const email = `t${Date.now()}@example.com`;
  const login = await fetch(BASE + "/login", {
    method: "POST", headers: { "content-type": "application/x-www-form-urlencoded", origin: BASE }, body: `email=${encodeURIComponent(email)}`,
  });
  const link = (await login.text()).match(/href="([^"]*\/auth\/verify\?token=[^"]+)"/)?.[1]?.replace(/&amp;/g, "&");
  assert.ok(link, "dev sign-in link shown");
  assert.equal((await fetch(link)).status, 200, "opening the link does not use it");
  const verify = await verifyLink(link);
  assert.equal(verify.status, 302);
  const cookie = verify.headers.get("set-cookie").split(";")[0];
  assert.equal((await verifyLink(link)).status, 400, "link is single-use");
  const dash = await fetch(BASE + "/dashboard", { headers: { cookie } });
  assert.match(await dash.text(), new RegExp(email.replace(".", "\\.")));
  const created = await fetch(BASE + "/dashboard/key", { method: "POST", headers: { cookie, origin: BASE } });
  const key = (await created.text()).match(/<pre>(jsk_[^<]+)<\/pre>/)?.[1];
  assert.ok(key, "new key shown once");
  assert.equal((await get("/v1/companies/4042", key)).status, 200);
  const crossSite = await fetch(BASE + "/dashboard/key", { method: "POST", headers: { cookie, origin: "https://evil.example" } });
  assert.equal(crossSite.status, 403);
  const rotated = await (await fetch(BASE + "/dashboard/key", { method: "POST", headers: { cookie, origin: BASE } })).text();
  assert.ok(rotated.includes("jsk_"));
  assert.equal((await get("/v1/companies/4042", key)).status, 401, "old key revoked after rotation");
});

test("U.S. fund holdings: Business only, Toyota has funds", async () => {
  assert.equal((await get("/v1/companies/7203/us-funds", process.env.FREE_KEY)).status, 200, "U.S. funds are on every plan");
  const r = await (await get("/v1/companies/7203/us-funds", process.env.BUSINESS_KEY)).json();
  assert.ok(r.fund_count > 100);
  assert.ok(r.top_funds.length > 0 && r.top_funds.length <= 25);
  assert.ok(r.top_funds[0].value_usd >= r.top_funds.at(-1).value_usd, "sorted by value");
  assert.match(r.attribution, /N-PORT/);
  const page = await (await get("/companies/7203")).text();
  assert.match(page, /U\.S\. funds holding 7203/);
});

test("ownership events: English summaries, type filter, full history on Starter", async () => {
  const free = await (await get("/v1/events?since=2000-01-01", process.env.FREE_KEY)).json();
  assert.equal(free.history_limited_to, null);
  const pro = await (await get("/v1/events?ticker=4042&type=stake_increase", process.env.PRO_KEY)).json();
  const ev = pro.data.find((e) => e.doc_id === "S100Z9GY");
  assert.equal(ev.summary_en, "Silchester International Investors LLP raised holdings in TOSOH CORPORATION (4042) to 6.99% from 5.98%.");
  assert.ok(pro.data.every((e) => e.type === "stake_increase" && e.issuer_ticker === "4042"));
  const activist = await (await get("/v1/events?type=stated_may_make_proposals&limit=5", process.env.PRO_KEY)).json();
  assert.ok(activist.data.length > 0 && activist.data.every((e) => e.stated_may_make_proposals === true));
});

test("cross-holdings: Pro only, Toyota holds KDDI and the link is mutual", async () => {
  assert.equal((await get("/v1/companies/7203/cross-holdings", process.env.FREE_KEY)).status, 200, "Starter has every feature");
  const xh = await (await get("/v1/companies/7203/cross-holdings", process.env.PRO_KEY)).json();
  const kddi = xh.holds.find((h) => h.ticker === "9433");
  assert.equal(kddi.held_back_as_stated, true);
  assert.equal(kddi.book_value_jpy, 989627000000);
  assert.ok(xh.summary.listed_sale_amount_jpy > 0);
  assert.ok(xh.held_by.length > 0);
  const unwind = await (await get("/v1/cross-holdings/unwinding?limit=10", process.env.PRO_KEY)).json();
  const sales = unwind.data.map((d) => d.listed_sale_amount_jpy);
  assert.deepEqual(sales, [...sales].sort((a, b) => b - a));
  assert.match(await (await get("/companies/7203")).text(), /Cross-shareholdings/);
});

test("disclosed trades: Pro only, Silchester's off-market buys of Tosoh with prices", async () => {
  assert.equal((await get("/v1/companies/4042/trades", process.env.FREE_KEY)).status, 200, "Starter has every feature");
  const tr = await (await get("/v1/companies/4042/trades", process.env.PRO_KEY)).json();
  const row = tr.data.find((r) => r.trade_date === "2026-10-21" && r.venue === "off_market");
  assert.equal(row.unit_price_jpy, 2579);
  assert.equal(row.side, "acquisition");
  const silchester = tr.by_holder.find((h) => h.filer_edinet_code === "E24872");
  assert.ok(silchester.net_shares > 0 && silchester.disclosed_low_jpy <= silchester.disclosed_vwap_jpy);
  assert.equal("last_disclosed_price" in tr, false);
  assert.match(tr.notes, /not market prices/);
  const inv = await (await get("/v1/investors/E24872/trades?limit=5", process.env.PRO_KEY)).json();
  assert.ok(inv.data.length === 5 && inv.data.every((r) => r.filer_edinet_code === "E24872"));
  assert.match(await (await get("/companies/4042")).text(), /Trades by 5% holders/);
});

test("indicators: percentile facts, counts and activity with no score; sorted lists Pro only; monthly index public", async () => {
  const ind = await (await get("/v1/companies/6857/indicators", process.env.FREE_KEY)).json();
  assert.equal("financial_strength" in ind, false);
  assert.equal("ownership_pressure" in ind, false);
  assert.ok(ind.financial_percentiles.mean_percentile > 0 && ind.financial_percentiles.metrics.roe.percentile >= 0);
  assert.match(ind.financial_percentiles.note, /not a rating/);
  assert.match(ind.results_wording_balance.note, /proprietary text method/);
  assert.match(ind.policy_uncertainty.note, /proprietary text method/);
  assert.doesNotMatch(JSON.stringify(ind), /formula|word list|MD&A|per 100|sentence/i);
  assert.doesNotMatch(JSON.stringify(ind), WITHHELD);
  assert.ok(ind.policy_uncertainty.percentile >= 0 && "trade" in ind.policy_uncertainty.policy_uncertainty_by_area);
  assert.ok("new_5pct_holders_180d" in ind.ownership_activity && !("score" in ind.ownership_activity));
  assert.equal((await get("/v1/indicators/sorted?metric=roe", process.env.FREE_KEY)).status, 200, "Starter has every feature");
  assert.equal((await get("/v1/indicators/sorted?metric=nope", process.env.PRO_KEY)).status, 400);
  assert.equal((await get("/v1/indicators/ranking?by=roe", process.env.PRO_KEY)).status, 404);
  const rank = (await (await get("/v1/indicators/sorted?metric=equity_ratio&sector=banks&limit=5", process.env.PRO_KEY)).json()).data.map((d) => d.value);
  assert.deepEqual(rank, [...rank].sort((a, b) => b - a));
  const idx = await (await get("/v1/indicators/policy-uncertainty", process.env.FREE_KEY)).json();
  assert.ok(idx.data.find((m) => m.month === "2026-06").reports > 1000);
  assert.ok("mean_policy_uncertainty" in idx.data[0]);
  assert.doesNotMatch(JSON.stringify(idx), /per_100_sentences|sentence|formula/i);
  assert.doesNotMatch(JSON.stringify(idx), WITHHELD);
  assert.match(await (await get("/indicators")).text(), /policy-uncertainty index/);
});

test("period results come only from EDINET filings; company-site materials are gone", async () => {
  assert.equal((await get("/v1/companies/7269/calls", process.env.PRO_KEY)).status, 404);
  const rel = await (await get("/v1/companies/7203/earnings-releases", process.env.FREE_KEY)).json();
  assert.ok(rel.data.length >= 1, "Starter gets the results history");
  assert.ok(rel.data.every((r) => r.source === "edinet_half_year" && /edinet/.test(r.source_url)));
  assert.doesNotMatch(rel.notes + rel.attribution, /website|robots\.txt|決算短信|company site/i);
  const all = await (await get("/v1/earnings-releases?limit=50", process.env.FREE_KEY)).json();
  assert.ok(all.data.length > 0 && all.data.every((r) => r.source === "edinet_half_year"));
  const page = await (await get("/companies/7269")).text();
  assert.doesNotMatch(page, /Company materials|company's own site/);
});

test("webhooks: on every plan, and local destinations are rejected", async () => {
  const post = (key, body) => fetch(BASE + "/v1/webhooks", { method: "POST", headers: { authorization: `Bearer ${key}`, "content-type": "application/json" }, body: JSON.stringify(body) });
  assert.equal((await post(process.env.FREE_KEY, { url: "http://example.com/x" })).status, 400, "Starter reaches validation, not a plan error");
  assert.equal((await post(process.env.PRO_KEY, { url: "http://example.com/x" })).status, 400, "https required");
  for (const url of ["http://localhost/hook", "https://127.0.0.1/hook", "https://[::1]/hook", "https://metadata.google.internal/hook"])
    assert.equal((await post(process.env.PRO_KEY, { url })).status, 400, url);
});

test("macro statistics are gone: routes 404, nothing beside indicators", async () => {
  for (const path of ["/v1/macro/series", "/v1/companies/7203/macro-brief"]) assert.equal((await get(path, process.env.PRO_KEY)).status, 404, path);
  assert.equal((await get("/macro")).status, 404);
  const ind = await (await get("/v1/companies/7203/indicators", process.env.FREE_KEY)).json();
  assert.equal("industry_sentiment" in ind, false);
  for (const name of ["get_macro_series", "get_company_macro_brief", "get_earnings_briefings"]) {
    const r = await (await mcp(process.env.PRO_KEY, { jsonrpc: "2.0", id: 9, method: "tools/call", params: { name, arguments: { ticker: "7203" } } })).json();
    assert.ok(r.error || r.result?.isError, name);
  }
});

test("company brief: from filings only, dated, no evaluation", async () => {
  assert.equal((await get("/v1/companies/7203/brief", process.env.FREE_KEY)).status, 200, "Starter has every feature");
  const b = await (await get("/v1/companies/7203/brief", process.env.PRO_KEY)).json();
  assert.equal(b.ticker, "7203");
  assert.ok(b.rate_lens.extra_annual_interest_jpy > 0 && b.rate_lens.shock_bp === 50);
  assert.ok(Array.isArray(b.ownership_events_180d) && b.indicators.financial_percentiles);
  assert.equal("macro" in b || "credit" in b || "rate_lens_context" in b, false);
  assert.doesNotMatch(JSON.stringify(b), /call rate|lending rate/i);
  assert.doesNotMatch(JSON.stringify(b), WITHHELD);
  const bank = await (await get("/v1/companies/8306/brief", process.env.PRO_KEY)).json();
  assert.ok(bank.rate_lens == null);
  const t = await (await mcp(process.env.PRO_KEY, { jsonrpc: "2.0", id: 4, method: "tools/call", params: { name: "get_company_brief", arguments: { ticker: "7203" } } })).json();
  assert.match(t.result.content[0].text, /"rate_lens"/);
});

test("credit ratings are shown as stated in filings", async () => {
  const r = await (await get("/v1/companies/3382/credit-ratings", process.env.PRO_KEY)).json();
  assert.ok(r.latest_stated["R&I"].rating && r.data[0].source_url.includes("edinet"));
  assert.match(r.notes, /We do not rate anything/);
  assert.equal((await get("/v1/credit-ratings", process.env.FREE_KEY)).status, 200, "Starter has every feature");
});

// Fetch a public page, retrying only failures that say nothing about the link itself:
// network errors/timeouts, 429, and 5xx (local D1 under a concurrent writer intermittently
// throws "Failed to get session commit token" / SQLITE_BUSY -> 500). A 4xx such as 404 is
// returned at once and never retried; a 5xx that persists across all attempts is returned.
const PAGE_TIMEOUT_MS = 15_000;
const PAGE_ATTEMPTS = 4;
async function getPage(path) {
  let last;
  for (let attempt = 1; attempt <= PAGE_ATTEMPTS; attempt++) {
    try {
      const r = await fetch(BASE + path, { redirect: "manual", signal: AbortSignal.timeout(PAGE_TIMEOUT_MS) });
      const body = await r.text();
      last = { status: r.status, body, error: null, attempts: attempt };
      if (r.status !== 429 && r.status < 500) return last;
    } catch (e) {
      last = { status: 0, body: "", error: `${e.name}: ${e.cause?.code ?? e.message}`, attempts: attempt };
    }
    if (attempt < PAGE_ATTEMPTS) await new Promise((res) => setTimeout(res, 250 * 2 ** (attempt - 1)));
  }
  return last;
}
// Run fn over items with at most `limit` in flight, preserving result order.
async function mapLimit(items, limit, fn) {
  const out = new Array(items.length);
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (next < items.length) { const i = next++; out[i] = await fn(items[i]); }
  }));
  return out;
}
const describeFailure = (path, r) => `${path} -> ${r.error ?? `HTTP ${r.status}`} after ${r.attempts} attempt(s)${r.body ? `: ${r.body.slice(0, 80).replace(/\s+/g, " ")}` : ""}`;

test("every public page renders with a title and description, and has no dead internal links", { timeout: 300_000 }, async () => {
  const pages = ["/", "/pricing", "/docs", "/indicators", "/events", "/cross-holdings", "/search?q=toyota", "/sources", "/terms", "/privacy", "/companies/7203", "/companies/7269", "/companies/8306", "/investors/E24872", "/login"];
  const links = new Set();
  const pageProblems = [];
  const fetched = await mapLimit(pages, 4, getPage);
  pages.forEach((path, i) => {
    const r = fetched[i];
    if (r.status !== 200) return pageProblems.push(describeFailure(path, r));
    if (!/<title>[^<]+<\/title>/.test(r.body)) pageProblems.push(`${path}: missing <title>`);
    if (!/<meta name="description" content="[^"]{20,}"/.test(r.body)) pageProblems.push(`${path}: missing or short meta description`);
    for (const m of r.body.matchAll(/href="(\/[^"#]*)/g)) links.add(m[1].replace(/&amp;/g, "&"));
  });
  assert.deepEqual(pageProblems, [], `public page problems:\n  ${pageProblems.join("\n  ")}`);
  assert.ok(links.size > 0, "no internal links found");

  const list = [...links].sort();
  const results = await mapLimit(list, 4, getPage);
  const dead = list.flatMap((l, i) => (results[i].status >= 200 && results[i].status < 400 ? [] : [describeFailure(l, results[i])]));
  assert.deepEqual(dead, [], `${dead.length} of ${list.length} internal links are dead:\n  ${dead.join("\n  ")}`);
});

test("pages avoid dropped features and banned wording; signup wording is honest", async () => {
  for (const path of ["/", "/pricing", "/docs", "/indicators", "/sources", "/terms", "/privacy", "/companies/7203", "/companies/7269", "/llms.txt", "/llms-full.txt", "/openapi.json", "/login"]) {
    const text = (await (await get(path)).text()).replace(/<style>[\s\S]*?<\/style>/, "");
    assert.doesNotMatch(text, /audio|speech recognition|whisper/i, path);
    assert.doesNotMatch(text, /testimonial|trusted by|\d+(\.\d+)?% accura/i, path);
    assert.doesNotMatch(text, /strong buy|undervalued|overvalued|price target/i, path);
    assert.doesNotMatch(text, /call rate|lending rate|macro|robots\.txt|own websites?|IR sites?|決算短信|transcripts?/i, path);
    assert.doesNotMatch(text, WITHHELD, path);
  }
  assert.match(await (await get("/pricing")).text(), /Billing is not connected/);
  assert.doesNotMatch(await (await get("/terms")).text(), /merchant of record/);
});

test("docs cover every OpenAPI path and every MCP tool", async () => {
  const docs = await (await get("/docs")).text();
  const spec = await (await get("/openapi.json")).json();
  for (const path of Object.keys(spec.paths)) assert.ok(docs.includes(path), path);
  const list = await (await mcp(process.env.PRO_KEY, { jsonrpc: "2.0", id: 2, method: "tools/list" })).json();
  for (const tool of list.result.tools) assert.ok(docs.includes(`<code>${tool.name}</code>`), tool.name);
  for (const anchor of ["start", "auth", "errors", "limits", "mcp", "webhooks", "verify", "brief"]) assert.ok(docs.includes(`id="${anchor}"`), anchor);
  assert.match(docs, /mcp-remote/);
  assert.match(docs, /\.cursor\/mcp\.json/);
});

test("sitemap and robots list the launch pages", async () => {
  const map = await (await get("/sitemap.xml")).text();
  for (const u of ["/terms", "/companies/7203", "/investors/E24872"]) assert.ok(map.includes(`${u}</loc>`), u);
  assert.ok(!map.includes("/macro</loc>"));
  assert.match(await (await get("/robots.txt")).text(), /Sitemap: /);
  assert.equal((await get("/definitely-not-here")).status, 404);
});

test("buybacks, governance and cross-holding trades", async () => {
  assert.equal((await get("/v1/cross-holdings/trades", process.env.FREE_KEY)).status, 200, "Starter has every feature");
  assert.equal((await get("/v1/governance", process.env.FREE_KEY)).status, 200, "Starter has every feature");
  const x = await (await get("/v1/cross-holdings/trades?limit=5", process.env.PRO_KEY)).json();
  assert.ok(x.data.length > 0 && x.data[0].filer_ticker && x.data[0].issuer_ticker);
  const b = await (await get("/v1/companies/7203/buybacks", process.env.PRO_KEY)).json();
  assert.ok(Array.isArray(b.data));
  const g = await (await get("/v1/companies/7203/governance", process.env.PRO_KEY)).json();
  assert.ok(Array.isArray(g.data));
});

test("contact form: validates, ignores bots, refuses other origins, and never shows an email address", async () => {
  const post = (body, headers = {}) => fetch(`${BASE}/contact`, { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded", ...headers }, body: new URLSearchParams(body) });
  const page = await (await fetch(`${BASE}/contact`)).text();
  assert.match(page, /<form method="post" action="\/contact"/);
  assert.doesNotMatch(page, /mailto:/);
  assert.equal((await post({ kind: "other", message: "hello there, a test", email: "not-an-email" })).status, 400);
  assert.equal((await post({ kind: "nope", message: "hello there, a test", email: "a@b.test" })).status, 400);
  assert.equal((await post({ kind: "other", message: "short", email: "a@b.test" })).status, 400);
  assert.equal((await post({ kind: "other", message: "hello there, a test", email: "a@b.test" }, { origin: "https://elsewhere.test" })).status, 403);
  const bot = await post({ kind: "other", message: "hello there, a test", email: "a@b.test", website: "spam" });
  assert.equal(bot.status, 200);
  assert.match(await bot.text(), /received your request/);
});

test("voting results never carry candidate or officer names", async () => {
  const v = await (await get("/v1/voting?limit=500", process.env.PRO_KEY)).json();
  assert.ok(v.data.length > 0);
  assert.ok(v.data.every((r) => !("candidate_ja" in r)));
  assert.ok(v.data.every((r) => !/候補者\S{1,10}氏|選任\s+\S{1,4}\s+\S{1,4}$/.test(r.proposal_title_ja ?? "")));
});

test("sign-in links are limited per network as well as per address", async () => {
  const ip = `203.0.113.${Math.floor(Math.random() * 250)}`;
  const send = (i) => fetch(BASE + "/login", { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded", "cf-connecting-ip": ip }, body: new URLSearchParams({ email: `net${i}-${Date.now()}@example.test` }) }).then((r) => r.text());
  for (let i = 0; i < 10; i++) assert.doesNotMatch(await send(i), /already sent several links/);
  assert.match(await send(10), /already sent several links/);
});
