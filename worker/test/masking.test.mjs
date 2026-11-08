// No planted personal name may leave the service by any route. Runs the real Worker on real SQLite with the real migrations,
// and takes its route list from /openapi.json and tools/list, so a new endpoint is covered without touching this file.
import assert from "node:assert/strict";
import { createHash, createHmac } from "node:crypto";
import { mkdtempSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { build } from "esbuild";

const here = dirname(fileURLToPath(import.meta.url));
const out = join(mkdtempSync(join(tmpdir(), "jf-mask-")), "w.mjs");
await build({ entryPoints: [join(here, "../src/index.ts")], bundle: true, format: "esm", outfile: out, platform: "neutral", mainFields: ["module", "main"], logLevel: "error", loader: { ".json": "json" } });
const worker = (await import(pathToFileURL(out))).default;

const EXCLUDED_JA = "試験 個人太郎", EXCLUDED_EN = "Taro TESTPERSON", JOINT = "試験 共同次郎", NEW_FILER = "未分類 三郎", SHAREHOLDER = "試験 大株主", OFFEROR = "試験 買付四郎";
// Names in fields that must be dropped, and names inside free text that only the serve-time pattern rules can find.
const OFFICER = "役員 名前", REP = "加々 試験勉", CANDIDATE = "試験 候補男", NOTE = "試験花子", PROPOSAL = "試験正子", GOV = "試験康史", TOB = "試験けん牛", PURPOSE = "試験亨介", SAMA = "試験高規", JOINT_IN_CELL = "松本 試験祥";
const NAMES = [EXCLUDED_JA, EXCLUDED_EN, JOINT, NEW_FILER, SHAREHOLDER, OFFEROR, OFFICER, REP, CANDIDATE, NOTE, PROPOSAL, GOV, TOB, PURPOSE, SAMA, JOINT_IN_CELL];
const leaks = (text) => NAMES.filter((n) => text.includes(n) || text.includes(n.replace(/\s/g, "")));
const CODE = "E99999", ORG = "E88888", UNCLASSIFIED = "E77777";

function fakeD1() {
  const s = new DatabaseSync(":memory:");
  for (const f of readdirSync(join(here, "../migrations")).sort()) s.exec(readFileSync(join(here, "../migrations", f), "utf8"));
  const stmt = (sql, args = []) => {
    const numbered = /\?\d/.test(sql);
    const q = sql.replace(/\?(\d+)/g, ":p$1");
    const clean = (v) => (v === undefined ? null : v);
    const params = () => (numbered ? [Object.fromEntries(args.map((v, i) => [`p${i + 1}`, clean(v)]).filter(([k]) => new RegExp(`:${k}\\b`).test(q)))] : args.map(clean));
    return {
      sql,
      bind: (...a) => stmt(sql, a),
      first: async () => s.prepare(q).get(...params()) ?? null,
      all: async () => ({ results: s.prepare(q).all(...params()), meta: {} }),
      run: async () => ({ meta: { changes: Number(s.prepare(q).run(...params()).changes) } }),
    };
  };
  return { raw: s, prepare: (sql) => stmt(sql), batch: async (list) => Promise.all(list.map((x) => (/^\s*(WITH|SELECT)/i.test(x.sql) && !/INSERT/i.test(x.sql) ? x.all() : x.run()))) };
}

const db = fakeD1();
function ins(table, row) {
  const cols = db.raw.prepare(`PRAGMA table_info(${table})`).all().map((c) => c.name);
  const keys = Object.keys(row).filter((k) => cols.includes(k));
  assert.equal(keys.length, Object.keys(row).length, `${table}: ${Object.keys(row).filter((k) => !cols.includes(k))}`);
  db.raw.prepare(`INSERT INTO ${table} (${keys.join(",")}) VALUES (${keys.map(() => "?").join(",")})`).run(...keys.map((k) => (row[k] !== null && typeof row[k] === "object" ? JSON.stringify(row[k]) : row[k])));
}
const TODAY = new Date().toISOString().slice(0, 10);
const issuer = { issuer_ticker: "7203", issuer_name_en: "TOYOTA MOTOR CORPORATION", issuer_name_ja: "トヨタ自動車株式会社" };

ins("issuers", { edinet_code: "E02144", ticker: "7203", name_ja: "トヨタ自動車株式会社", name_en: "TOYOTA MOTOR CORPORATION", listed: 1, industry_ja: "輸送用機器" });
ins("issuers", { edinet_code: "E03395", ticker: "2742", name_ja: "株式会社ハローズ", name_en: "HALOWS CO.,LTD.", listed: 1, industry_ja: "小売業" });
ins("investor_excluded_filers", { edinet_code: CODE });
const filerRec = (doc, code, ja, en, holders, extra = {}) => ({ doc_id: doc, submit_date: TODAY, filer_edinet_code: code, filer_name_ja: ja, filer_name_en: en, filer_name_en_filing: en, ...issuer, holding_ratio_pct: 6, holders, change_reason_ja: `${ja}の保有割合が増加`, purpose_tags: [], ...extra });
const person = [{ name: `${EXCLUDED_JA} (${EXCLUDED_EN})`, holder_type: "individual", shares: 1, address: "東京都" }];
const lh = (rec) => ins("large_holdings", { doc_id: rec.doc_id, submit_date: rec.submit_date, issuer_ticker: rec.issuer_ticker, filer_edinet_code: rec.filer_edinet_code, record: rec });
lh(filerRec("DOC1", CODE, EXCLUDED_JA, EXCLUDED_EN, person, { trades: [{ holder: EXCLUDED_JA, note_ja: `${EXCLUDED_JA}へ譲渡` }] }));
lh(filerRec("DOC2", ORG, "試験アセット株式会社", "Test Asset Co., Ltd.", [{ name: "試験アセット株式会社", holder_type: "corporation" }, { name: JOINT, holder_type: null }]));
lh(filerRec("DOC3", UNCLASSIFIED, NEW_FILER, null, [{ name: NEW_FILER, holder_type: null }]));
const event = (doc, code, ja, en, type) => ins("ownership_events", { event_id: `${doc}:${type}`, type, submit_date: TODAY, doc_id: doc, issuer_ticker: "7203", filer_edinet_code: code, ingested_at: "2099-01-01T00:00:00.000Z",
  record: { type, event_id: `${doc}:${type}`, doc_id: doc, submit_date: TODAY, ...issuer, filer_edinet_code: code, filer_name_ja: ja, filer_name_en: en, activist: type === "activist_purpose", summary_en: `${en ?? ja} states it may make proposals to the management of TOYOTA (7203) (first filing).`, source_url: "https://example.test/x.pdf" } });
event("DOC1", CODE, EXCLUDED_JA, EXCLUDED_EN, "activist_purpose");
event("DOC3", UNCLASSIFIED, NEW_FILER, null, "new_stake");
event("DOC2", ORG, "試験アセット株式会社", "Test Asset Co., Ltd.", "activist_purpose");
const leakedIssuer = "株式会社ハローズ 代表取締役 甲藤 利行";
lh(filerRec("S100Z6DM", ORG, "試験アセット株式会社", "Test Asset Co., Ltd.", [], { issuer_ticker: "2742", issuer_name_en: "HALOWS CO.,LTD.", issuer_name_ja: leakedIssuer }));
ins("ownership_events", { event_id: "S100Z6DM:stake_increase", type: "stake_increase", submit_date: TODAY, doc_id: "S100Z6DM", issuer_ticker: "2742", filer_edinet_code: ORG, ingested_at: "2099-01-02T00:00:00.000Z",
  record: { type: "stake_increase", event_id: "S100Z6DM:stake_increase", doc_id: "S100Z6DM", submit_date: TODAY, issuer_ticker: "2742", issuer_name_en: "HALOWS CO.,LTD.", issuer_name_ja: leakedIssuer, filer_edinet_code: ORG, filer_name_ja: "試験アセット株式会社", filer_name_en: "Test Asset Co., Ltd.", activist: false, summary_en: "Test Asset raised holdings in HALOWS (2742)." } });
const trade = (doc, row, code, holder) => ins("holder_trades", { doc_id: doc, row_no: row, trade_date: TODAY, submit_date: TODAY, issuer_ticker: "7203", filer_edinet_code: code, holder, shares: 100, pct_of_outstanding: 0.1, venue: "off_market", side: "acquisition", unit_price_jpy: 1000, kind: "trade", note_ja: `${holder}との取引`, security_ja: "普通株式" });
trade("DOC1", 1, CODE, EXCLUDED_JA);
trade("DOC2", 1, ORG, JOINT);
trade("DOC2", 2, ORG, "試験アセット株式会社");
const filing = (doc, code, name) => ins("filings", { doc_id: doc, submit_datetime: `${TODAY} 10:00`, edinet_code: code, ticker: "7203", filer_name_ja: name, doc_type_code: "350", description_ja: `大量保有報告書 ${name}`, withdrawn: 0 });
for (const [d, c, n] of [["DOC1", CODE, EXCLUDED_JA], ["DOC2", ORG, "試験アセット株式会社"], ["DOC3", UNCLASSIFIED, NEW_FILER]]) {
  filing(d, c, n);
  ins("filing_feed", { doc_id: d, first_seen_at: "2099-01-01T00:00:00.000Z", day: TODAY, withdrawn: 0 });
}
ins("tender_offers", { doc_id: "DOC4", doc_type_code: "240", parse_status: "parsed", source_url: "https://example.test/4", ticker: "7203", submit_datetime: `${TODAY} 10:00`, record: { doc_id: "DOC4", ticker: "7203", edinet_code: CODE, filer_name_ja: EXCLUDED_JA, offeror_name_ja: OFFEROR, target_name_ja: "トヨタ自動車株式会社", purpose_quote_ja: `${EXCLUDED_JA}は買付を行う。`.padEnd(300, "あ"), document_kind: "registration", submit_date: TODAY } });
ins("annual_reports", { doc_id: "DOC5", ticker: "7203", fiscal_year_end: "2026-03-31", submit_date: TODAY, record: { doc_id: "DOC5", ticker: "7203", fiscal_year_end: "2026-03-31", key_metrics: [], statements: {}, company: { representative: `代表取締役社長　社長執行役員　${REP}`, officers: [{ title: "取締役", name: OFFICER, shares: 123 }], major_shareholders: [{ name: SHAREHOLDER, shares: 5, ratio_pct: 5 }, { name: "日本マスタートラスト信託銀行㈱", shares: 9, ratio_pct: 9 }] } } });

ins("voting_results", { doc_id: "DOC6", seq: 1, ticker: "7203", edinet_code: "E02144", submit_date: TODAY, proposal_no: "1", proposal_title_ja: `(社外)取締役${PROPOSAL}解任の件`, candidate_ja: CANDIDATE, result: "rejected", source_url: "https://example.test/6" });
ins("voting_results", { doc_id: "DOC6", seq: 2, ticker: "7203", edinet_code: "E02144", submit_date: TODAY, proposal_no: "2", proposal_title_ja: `取締役選任 ${CANDIDATE}`, candidate_ja: CANDIDATE, result: "approved", source_url: "https://example.test/6" });
ins("governance_events", { doc_id: "DOC7", type: "internal_control", ticker: "7203", edinet_code: "E02144", submit_date: TODAY, record: { doc_id: "DOC7", type: "internal_control", ticker: "7203", submit_date: TODAY, quote_ja: `当社取締役CFO兼CIO${GOV}は、評価した。`, fields: {} } });
ins("holder_trades", { doc_id: "DOC2", row_no: 3, trade_date: TODAY, submit_date: TODAY, issuer_ticker: "7203", filer_edinet_code: ORG, holder: "試験アセット株式会社", shares: 100, pct_of_outstanding: 0.1, venue: "off_market", side: "lending", unit_price_jpy: null, kind: "lending", note_ja: `${NOTE}氏借株の返還、${SAMA}様株券貸借`, security_ja: "普通株式" });
ins("holder_trades", { doc_id: "DOC2", row_no: 4, trade_date: TODAY, submit_date: TODAY, issuer_ticker: "7203", filer_edinet_code: ORG, holder: `試験アセット株式会社、${JOINT_IN_CELL}`, shares: 100, pct_of_outstanding: 0.1, venue: "on_market", side: "acquisition", unit_price_jpy: null, kind: "trade", note_ja: null, security_ja: "普通株式" });
lh(filerRec("DOC8", ORG, "試験アセット株式会社", "Test Asset Co., Ltd.", [{ name: "試験アセット株式会社", holder_type: "corporation" }], { purpose_ja: `発行者の代表取締役社長である${PURPOSE}氏との業務提携`, submit_date: TODAY }));
ins("tender_offers", { doc_id: "DOC9", doc_type_code: "240", parse_status: "parsed", source_url: "https://example.test/9", ticker: "7203", submit_datetime: `${TODAY} 11:00`, record: { doc_id: "DOC9", ticker: "7203", edinet_code: ORG, filer_name_ja: "試験アセット株式会社", offeror_name_ja: "試験アセット株式会社", target_name_ja: "トヨタ自動車株式会社", purpose_quote_ja: `筆頭株主の${TOB}氏が応募する。`, document_kind: "registration", submit_date: TODAY } });
ins("users", { id: "u1", email: "fixture@example.test", plan: "business", created_at: "2026-01-01" });
const KEY = "jf_masking_fixture";
ins("api_keys", { key_hash: createHash("sha256").update(KEY).digest("hex"), key_prefix: "jf_mask", plan: "business", user_id: "u1", created_at: "2026-01-01" });
const FREE_KEY = "jf_masking_free";
ins("api_keys", { key_hash: createHash("sha256").update(FREE_KEY).digest("hex"), key_prefix: "jf_free", plan: "starter", user_id: "u1", created_at: "2026-01-01" });

const env = { DB: db };
const ctx = { waitUntil: (p) => pending.push(p), passThroughOnException() {} };
let pending = [];
// The per-minute limit would otherwise turn later checks into empty 429s.
const call = (path, init = {}) => (db.raw.exec("DELETE FROM usage"), worker.fetch(new Request("http://t.test" + path, { ...init, headers: { authorization: `Bearer ${KEY}`, ...(init.headers ?? {}) } }), env, ctx));
const fill = (p, code) => p.replace("{ticker}", "7203").replace("{code}", code).replace("{id}", "g1").replace("{sector}", "transportation-equipment");

test("every OpenAPI route, as JSON and CSV, with filters on the excluded filer", async () => {
  const spec = await (await call("/openapi.json")).json();
  const paths = Object.keys(spec.paths);
  assert.ok(paths.length > 40, "route list looks wrong");
  let checked = 0;
  for (const p of paths) {
    if (!spec.paths[p].get) continue;
    for (const code of [CODE, ORG, UNCLASSIFIED]) {
      for (const qs of ["", `?ticker=7203&filer=${code}&since=2000-01-01&limit=50`, "?ticker=7203&limit=50&format=csv", "?tickers=7203&q=7203&metric=roe"]) {
        const r = await call(fill(p, code) + qs);
        assert.ok(r.status < 500 && r.status !== 429, `${p}${qs}: ${r.status}`);
        assert.deepEqual(leaks(await r.text()), [], `${p}${qs}`);
        checked++;
      }
    }
  }
  assert.ok(checked > 400);
});

test("pages", async () => {
  for (const p of ["/", "/events", "/events?type=stated_may_make_proposals", "/companies/7203", `/investors/${CODE}`, `/investors/${UNCLASSIFIED}`, `/investors/${ORG}`, "/cross-holdings", "/indicators", "/status", "/sitemap.xml", "/llms-full.txt", "/search?q=7203", "/investor-groups/g1"]) {
    const r = await call(p);
    assert.ok(r.status < 500, `${p}: ${r.status}`);
    const text = await r.text();
    assert.deepEqual(leaks(text), [], p);
    assert.equal(text.includes(`/investors/${CODE}`) && p !== `/investors/${CODE}`, false, p);
  }
});

test("every MCP tool", async () => {
  const rpc = async (method, params) => (await call("/mcp", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }) })).json();
  const { result } = await rpc("tools/list");
  assert.ok(result.tools.length > 30);
  for (const tool of result.tools) for (const code of [CODE, UNCLASSIFIED]) {
    const res = await rpc("tools/call", { name: tool.name, arguments: { ticker: "7203", tickers: ["7203"], query: "7203", filer: code, filer_edinet_code: code, group_id: "g1", sector: "transportation-equipment", metric: "roe", since: "2000-01-01", limit: 50 } });
    assert.deepEqual(leaks(JSON.stringify(res)), [], tool.name);
  }
});

test("filter arguments do not accept an excluded filer", async () => {
  for (const p of [`/v1/events?filer=${CODE}`, `/v1/large-holdings?filer=${CODE}`, `/v1/events?filer=${UNCLASSIFIED}`, `/v1/large-holdings?filer=${UNCLASSIFIED}`]) {
    assert.deepEqual((await (await call(p)).json()).data, [], p);
  }
  assert.equal((await call(`/v1/investors/${CODE}`)).status, 404);
  assert.equal((await call(`/v1/investors/${CODE}/trades`)).status, 404);
  assert.deepEqual((await (await call(`/v1/investors/${UNCLASSIFIED}/trades`)).json()).data, []);
});

test("people are masked, not lost, and organisations keep their names", async () => {
  const ev = (await (await call("/v1/events?ticker=7203")).json()).data;
  const person = ev.find((e) => e.doc_id === "DOC1");
  assert.equal(person.filer_edinet_code, null);
  assert.equal(person.filer_name_ja, "individual filer");
  assert.match(person.summary_en, /^individual filer states it may make proposals/);
  assert.equal(person.type, "stated_may_make_proposals");
  const org = ev.find((e) => e.doc_id === "DOC2");
  assert.equal(org.filer_edinet_code, ORG);
  assert.match(org.summary_en, /^Test Asset/);
  const det = await (await call("/v1/companies/7203/details")).json();
  assert.equal(det.major_shareholders[0].name, "individual holder");
  assert.match(det.major_shareholders[1].name, /信託銀行/);
  assert.deepEqual(det.officers[0], { title: "取締役" });
  assert.equal(det.representative, undefined);
  assert.equal(det.representative_title, "代表取締役社長 社長執行役員");
  const vote = (await (await call("/v1/companies/7203/voting")).json()).data;
  assert.ok(vote.every((v) => !("candidate_ja" in v)));
  assert.ok(vote.some((v) => v.proposal_title_ja === "(社外)取締役[氏名省略]解任の件"));
  const trn = (await (await call("/v1/companies/7203/trades")).json()).data;
  assert.ok(trn.some((r) => r.note_ja === "[氏名省略]氏借株の返還、[氏名省略]様株券貸借"));
  assert.ok(trn.some((r) => r.holder === "試験アセット株式会社、individual holder"));
  const offer = (await (await call("/v1/companies/7203/tender-offers")).json()).data[0];
  assert.ok(offer.purpose_quote_ja.length <= 200);
  const tr = await (await call("/v1/companies/7203/trades")).json();
  assert.ok(tr.data.some((r) => r.holder === "試験アセット株式会社"));
  assert.ok(tr.data.some((r) => r.holder === "individual filer" && r.note_ja === null));
});

test("issuer names come from the listed-company master in JSON, CSV and MCP", async () => {
  for (const path of ["/v1/events?ticker=2742", "/v1/large-holdings?ticker=2742"]) {
    const body = await (await call(path)).text();
    assert.match(body, /株式会社ハローズ/);
    assert.doesNotMatch(body, /代表取締役|甲藤\s+利行/);
    const csv = await (await call(path + "&format=csv")).text();
    assert.match(csv, /株式会社ハローズ/);
    assert.doesNotMatch(csv, /代表取締役|甲藤\s+利行/);
  }
  const rpc = await (await call("/mcp", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "get_ownership_events", arguments: { ticker: "2742", limit: 10 } } }) })).text();
  assert.match(rpc, /株式会社ハローズ/);
  assert.doesNotMatch(rpc, /代表取締役|甲藤\s+利行/);
});

test("Starter company timeline includes disclosed trades, without naming individuals", async () => {
  const free = await (await call("/v1/companies/7203/timeline?since=2000-01-01&limit=50", { headers: { authorization: `Bearer ${FREE_KEY}` } })).json();
  assert.doesNotMatch(free.notes, /excluded/);
  assert.match(free.notes, /Individual filers are not named/);
  const paid = await (await call("/v1/companies/7203/timeline?since=2000-01-01&limit=50")).json();
  assert.ok(paid.data.some((x) => x.type === "disclosed_trades"));
});

test("webhook registration rejects local, private, CGNAT and unresolvable DNS", async () => {
  const postHook = (url) => call("/v1/webhooks", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ url }) });
  for (const url of ["http://localhost/hook", "https://127.0.0.1/hook", "https://[::1]/hook", "https://metadata.google.internal/hook"]) assert.equal((await postHook(url)).status, 400, url);
  const real = globalThis.fetch;
  try {
    for (const address of ["10.0.0.1", "172.16.0.1", "192.168.0.1", "169.254.169.254", "100.64.0.1", "fc00::1", "fe80::1", "::ffff:7f00:1", "::ffff:a9fe:a9fe"]) {
      globalThis.fetch = async () => Response.json({ Status: 0, Answer: [{ type: address.includes(":") ? 28 : 1, data: address }] });
      assert.equal((await postHook("https://blocked.example/hook")).status, 400, address);
    }
    globalThis.fetch = async () => Response.json({ Status: 0, Answer: [] });
    assert.equal((await postHook("https://missing.example/hook")).status, 400);
  } finally { globalThis.fetch = real; }
});

test("webhook delivery rechecks DNS and treats redirects as failures", async () => {
  ins("webhooks", { id: "wh_rebind", owner: "u1", url: "https://rebind.example/x", secret: "s", created_at: "2000-01-01", cursor_at: "2000-01-01", enabled: 0 });
  ins("webhooks", { id: "wh_redirect", owner: "u1", url: "https://redirect.example/x", secret: "s", created_at: "2000-01-01", cursor_at: "2000-01-01", enabled: 0 });
  const real = globalThis.fetch;
  let posts = 0;
  try {
    globalThis.fetch = async (u) => {
      if (String(u).startsWith("https://cloudflare-dns.com/")) return Response.json({ Status: 0, Answer: [{ type: 1, data: "127.0.0.1" }] });
      posts++;
      return new Response("ok");
    };
    const rebinding = await (await call("/v1/webhooks/wh_rebind/test", { method: "POST" })).json();
    assert.equal(rebinding.ok, false);
    assert.equal(posts, 0);
    globalThis.fetch = async (u, init) => {
      if (String(u).startsWith("https://cloudflare-dns.com/")) return Response.json({ Status: 0, Answer: [{ type: 1, data: "93.184.216.34" }] });
      posts++;
      assert.equal(init.redirect, "manual");
      return new Response(null, { status: 302, headers: { location: "http://127.0.0.1/" } });
    };
    const redirect = await (await call("/v1/webhooks/wh_redirect/test", { method: "POST" })).json();
    assert.deepEqual(redirect, { status: 302, ok: false });
    assert.equal(posts, 1);
  } finally { globalThis.fetch = real; }
});

test("webhooks: cron delivery and test send", async () => {
  ins("webhooks", { id: "wh_t", owner: "u1", url: "https://hook.example.test/x", secret: "s", types: ["filing", "new_stake", "stake_increase", "stated_may_make_proposals"], tickers: null, created_at: "2000-01-01T00:00:00.000Z", cursor_at: "2000-01-01T00:00:00.000Z" });
  const sent = [];
  const real = globalThis.fetch;
  globalThis.fetch = async (u, init) => {
    if (String(u).startsWith("https://cloudflare-dns.com/")) return Response.json({ Status: 0, Answer: String(u).includes("type=A%20") ? [] : [{ type: 1, data: "93.184.216.34" }] });
    sent.push({ body: String(init?.body ?? ""), redirect: init?.redirect, signature: init?.headers?.["x-japanstake-signature"] });
    return new Response("ok");
  };
  try {
    pending = [];
    await worker.scheduled({ scheduledTime: Date.now(), cron: "*/5 * * * *" }, env, ctx);
    await Promise.all(pending);
    const t = await call("/v1/webhooks/wh_t/test", { method: "POST" });
    assert.equal(t.status, 200);
  } finally {
    globalThis.fetch = real;
  }
  const all = sent.map((x) => x.body).join("\n");
  assert.ok(all.includes("DOC1") && all.includes("DOC3"), "fixture events must really be sent");
  assert.ok(all.includes("S100Z6DM") && !/代表取締役|甲藤\s+利行/.test(all));
  assert.ok(sent.every((x) => x.redirect === "manual"));
  for (const x of sent) {
    const [t, v1] = x.signature.split(",").map((p) => p.split("=")[1]);
    assert.equal(v1, createHmac("sha256", "s").update(`${t}.${x.body}`).digest("hex"));
  }
  assert.deepEqual(leaks(all), []);
});

test("legal page and contact switches", async () => {
  assert.equal((await call("/legal")).status, 404);
  assert.doesNotMatch(await (await call("/sitemap.xml")).text(), /\/legal/);
  // The contact page is a form; no address is published, with or without CONTACT_EMAIL.
  assert.match(await (await call("/contact")).text(), /<form method="post" action="\/contact"/);
  const full = { DB: db, CONTACT_EMAIL: "owner@example.test", LEGAL_PAGE_ENABLED: "true", LEGAL_PROVIDER_NAME: "X", LEGAL_START_OF_SERVICE: "Y", LEGAL_CANCELLATION: "Z", LEGAL_REFUNDS: "R" };
  const get = (p, e) => worker.fetch(new Request("http://t.test" + p), e, ctx);
  assert.doesNotMatch(await (await get("/contact", full)).text(), /owner@example\.test/);
  const legalPage = await get("/legal", full);
  assert.equal(legalPage.status, 200);
  assert.doesNotMatch(await legalPage.text(), /owner@example\.test/, "the legal notice points to the form, not an address");
  assert.equal((await get("/legal", { ...full, LEGAL_REFUNDS: "" })).status, 404);
  assert.equal((await get("/legal", { ...full, LEGAL_PAGE_ENABLED: "false" })).status, 404);
});
