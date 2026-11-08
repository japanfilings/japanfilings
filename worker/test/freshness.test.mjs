// Rules run on real SQLite with the real migrations; API checks run against BASE with PRO_KEY/FREE_KEY.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { DatabaseSync } from "node:sqlite";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { build } from "esbuild";

const here = dirname(fileURLToPath(import.meta.url));
const out = join(mkdtempSync(join(tmpdir(), "jf-")), "m.mjs");
writeFileSync(out + ".entry.ts", `export * from "${here}/../src/freshness"; export * from "${here}/../src/ondemand";`);
await build({ entryPoints: [out + ".entry.ts"], bundle: true, format: "esm", outfile: out, platform: "neutral", logLevel: "error", external: ["hono"] });
const m = await import(pathToFileURL(out));

function fakeD1() {
  const s = new DatabaseSync(":memory:");
  for (const f of ["0001_init", "0002_reports_accounts", "0004_events_crossholdings_webhooks", "0005_holder_trades", "0008_earnings_releases", "0011_disclosed_ratings", "0012_buybacks_governance", "0006_indicators", "0020_freshness_ondemand", "0021_investor_groups", "0022_filing_details_voting_tender_investors", "0026_edinet_only"]) {
    s.exec(readFileSync(join(here, "../migrations", f + ".sql"), "utf8"));
  }
  // node:sqlite treats ?1 as a named parameter, so numbered ones are bound by name.
  const stmt = (sql, args = []) => {
    const q = sql.replace(/\?(\d+)/g, ":p$1");
    const named = () => Object.fromEntries(args.map((v, i) => [`p${i + 1}`, v === undefined ? null : v]).filter(([k]) => q.includes(":" + k)));
    return {
      sql,
      bind: (...a) => stmt(sql, a),
      first: async () => s.prepare(q).get(named()) ?? null,
      all: async () => ({ results: s.prepare(q).all(named()) }),
      run: async () => ({ meta: { changes: Number(s.prepare(q).run(named()).changes) } }),
    };
  };
  return { raw: s, prepare: (sql) => stmt(sql), batch: async (list) => Promise.all(list.map((x) => (/^\s*SELECT/i.test(x.sql) ? x.all() : x.run()))) };
}
const seedIssuer = (db, t = "7203", code = "E1") => db.raw.prepare("INSERT INTO issuers (edinet_code, ticker, listed) VALUES (?, ?, 1)").run(code, t);
const def = (id) => m.DATASETS.find((d) => d.id === id);
const NOON_MON = new Date("2026-11-02T03:00:00Z"); // Monday 12:00 JST

test("no run recorded is late, not fresh", () => assert.equal(m.classify(def("buybacks"), undefined, NOON_MON).status, "late"));
test("a failed last run is failed, even with an older success", () => {
  assert.equal(m.classify(def("buybacks"), { last_success_at: "2026-11-01T00:00:00Z", last_attempt_at: "2026-11-02T00:00:00Z", last_error: "URLError", newest_source_date: "2026-11-01" }, NOON_MON).status, "failed");
});
test("stale logic: past target plus grace is late", () => {
  const row = (at) => ({ last_success_at: at, last_attempt_at: at, newest_source_date: "2026-10-30" });
  assert.equal(m.classify(def("buybacks"), row("2026-11-01T12:00:00Z"), NOON_MON).status, "fresh");
  const r = m.classify(def("buybacks"), row("2026-10-30T12:00:00Z"), NOON_MON);
  assert.equal(r.status, "late"); assert.deepEqual(r.reasons, ["run_overdue"]);
});
test("business calendar ignores the weekend and nights", () => {
  const row = { last_success_at: "2026-10-30T11:50:00Z", last_attempt_at: "2026-10-30T11:50:00Z", newest_source_date: "2026-10-30" }; // Fri 20:50 JST
  assert.equal(m.classify(def("large_holdings"), row, new Date("2026-11-01T03:00:00Z")).status, "fresh"); // Sunday
  assert.equal(m.classify(def("large_holdings"), row, NOON_MON).status, "late"); // 4h into Monday
  assert.equal(Math.round(m.businessMinutes(new Date(row.last_success_at), new Date("2026-11-01T23:10:00Z"))), 20); // 10 min Fri + 10 min Mon
});
test("old source date or unread filings make a dataset late", () => {
  const at = "2026-11-02T02:50:00Z";
  assert.deepEqual(m.classify(def("large_holdings"), { last_success_at: at, last_attempt_at: at, newest_source_date: "2026-10-01" }, NOON_MON).reasons, ["source_too_old"]);
  assert.deepEqual(m.classify(def("large_holdings"), { last_success_at: at, last_attempt_at: at, newest_source_date: "2026-10-30", open_gap: 3 }, NOON_MON).reasons, ["documents_not_parsed"]);
});
test("dates from every source normalise", () => {
  assert.equal(m.normDate("20261002"), "2026-10-02"); assert.equal(m.normDate("2026q3"), "2026-09-30"); assert.equal(m.normDate("2026-10-29 17:09"), "2026-10-29");
});
test("section: newer filing is stale; nothing filed is not stale", () => {
  const at = "2026-11-02T02:50:00Z", ok = { last_success_at: at, last_attempt_at: at, newest_source_date: "2026-10-30" };
  const a = m.sectionView(def("large_holdings"), { asOf: "2026-10-01", datasetRow: ok, filing: { doc_id: "S1", d: "2026-10-30 10:00" } }, NOON_MON);
  assert.equal(a.stale, true); assert.equal(a.newer_filing.doc_id, "S1"); assert.equal(m.needsRefresh(a), true);
  const h = m.sectionView(def("half_year"), { asOf: null, datasetRow: ok, filing: null }, NOON_MON);
  assert.equal(h.reason, "not_filed");
  const n = m.sectionView(def("buybacks"), { asOf: null, datasetRow: ok, filing: null }, NOON_MON);
  assert.equal(n.reason, "not_filed"); assert.equal(n.stale, false);
  assert.equal(m.sectionView(def("annual"), { asOf: "2024-01-01", datasetRow: ok, filing: null }, NOON_MON).stale_reasons.includes("older_than_target"), true);
});
test("dataset late marks every company section stale", () => {
  assert.deepEqual(m.sectionView(def("annual"), { asOf: "2026-10-01", datasetRow: undefined, filing: null }, NOON_MON).stale_reasons, ["dataset_late"]);
});
test("refresh: 202, then dedupes while open, no extra row", async () => {
  const db = fakeD1(); seedIssuer(db);
  const a = await m.requestRefresh(db, "7203", "pro", "u1", NOON_MON);
  assert.equal(a.status, 202); assert.equal(a.body.status, "queued"); assert.ok(a.body.expected_by);
  const b = await m.requestRefresh(db, "7203", "pro", "u2", NOON_MON);
  assert.equal(b.status, 200); assert.equal(b.body.deduped, true); assert.equal(b.body.request_id, a.body.request_id);
  assert.equal(db.raw.prepare("SELECT COUNT(*) n FROM refresh_requests").get().n, 1);
  assert.equal((await m.requestRefresh(db, "9999", "pro", "u1", NOON_MON)).status, 404);
});
test("refresh: per-company limit, with Retry-After", async () => {
  const db = fakeD1(); seedIssuer(db);
  for (let i = 0; i < m.COMPANY_LIMIT_PER_HOUR; i++) {
    assert.equal((await m.requestRefresh(db, "7203", "pro", `u${i}`, NOON_MON)).status, 202);
    db.raw.exec("UPDATE refresh_requests SET status = 'done'");
  }
  const r = await m.requestRefresh(db, "7203", "pro", "other", NOON_MON);
  assert.equal(r.status, 429); assert.ok(Number(r.headers["Retry-After"]) > 0);
});
test("refresh: per-key limit", async () => {
  const db = fakeD1();
  for (let i = 0; i <= m.KEY_LIMIT_PER_HOUR.pro; i++) seedIssuer(db, String(1000 + i), `E${i}`);
  for (let i = 0; i < m.KEY_LIMIT_PER_HOUR.pro; i++) assert.equal((await m.requestRefresh(db, String(1000 + i), "pro", "u", NOON_MON)).status, 202);
  assert.equal((await m.requestRefresh(db, String(1000 + m.KEY_LIMIT_PER_HOUR.pro), "pro", "u", NOON_MON)).status, 429);
});
test("auto enqueue dedupes and then waits a day", async () => {
  const db = fakeD1(); seedIssuer(db);
  assert.equal((await m.autoEnqueue(db, "7203", NOON_MON)).source, "auto");
  assert.equal((await m.autoEnqueue(db, "7203", NOON_MON)).deduped, true);
  db.raw.exec("UPDATE refresh_requests SET status = 'done'");
  assert.equal(await m.autoEnqueue(db, "7203", new Date(NOON_MON.getTime() + 3600_000)), null);
  assert.equal((await m.autoEnqueue(db, "7203", new Date(NOON_MON.getTime() + 25 * 3600_000))).source, "auto");
});
const edinetStub = (docs, calls, processed = "2026-11-02 12:00") => async (url) => {
  calls.push(String(url));
  const type = new URL(url).searchParams.get("type");
  return new Response(JSON.stringify({ metadata: { status: "200", processDateTime: processed, resultset: { count: docs.length } }, ...(type === "2" ? { results: docs } : {}) }));
};
const doc = (id, o = {}) => ({ docID: id, submitDateTime: "2026-11-02 11:50", edinetCode: "E1", secCode: "72030", filerName: "x", docTypeCode: "350", docDescription: "d", withdrawalStatus: "0", csvFlag: "1", xbrlFlag: "1", englishDocFlag: "0", ...o });
test("poll: only the documents list, spaced, new filing visible at once, unchanged list skipped", async () => {
  const db = fakeD1(); seedIssuer(db);
  const calls = [], gaps = [];
  const r = await m.pollEdinet({ DB: db, EDINET_API_KEY: "k" }, NOON_MON, edinetStub([doc("S1")], calls), async (ms) => gaps.push(ms));
  assert.equal(r.new, 1); assert.deepEqual(gaps, [1100]);
  assert.ok(calls.every((u) => u.startsWith("https://api.edinet-fsa.go.jp/api/v2/documents.json?")));
  assert.equal((await m.latestFilings(db, { limit: 5 })).data[0].doc_id, "S1");
  const again = [];
  await m.pollEdinet({ DB: db, EDINET_API_KEY: "k" }, NOON_MON, edinetStub([doc("S1")], again), async () => {});
  assert.equal(again.length, 1);
  assert.equal(db.raw.prepare("SELECT last_success_at IS NOT NULL ok FROM dataset_status WHERE dataset='filings'").get().ok, 1);
});
test("poll: a withdrawal removes derived rows at once", async () => {
  const db = fakeD1(); seedIssuer(db);
  db.raw.exec("INSERT INTO large_holdings VALUES ('S1','7203','E1','2026-10-01','{}'); INSERT INTO ownership_events (event_id, doc_id, record) VALUES ('e','S1','{}')");
  const r = await m.pollEdinet({ DB: db, EDINET_API_KEY: "k" }, NOON_MON, edinetStub([doc("S1", { withdrawalStatus: "2", docTypeCode: null })], []), async () => {});
  assert.equal(r.withdrawn, 1);
  assert.equal(db.raw.prepare("SELECT COUNT(*) n FROM large_holdings").get().n, 0);
  assert.equal(db.raw.prepare("SELECT COUNT(*) n FROM ownership_events").get().n, 0);
  assert.equal(db.raw.prepare("SELECT withdrawn FROM filings WHERE doc_id='S1'").get().withdrawn, 1);
});
test("poll: failure is failed, without the key; no secret is failed too", async () => {
  const db = fakeD1();
  await m.pollEdinet({ DB: db, EDINET_API_KEY: "SECRETKEY" }, NOON_MON, async () => { throw new Error("fetch https://x?Subscription-Key=SECRETKEY"); }, async () => {});
  const row = db.raw.prepare("SELECT last_error FROM dataset_status WHERE dataset='filings'").get();
  assert.ok(row.last_error && !row.last_error.includes("SECRETKEY"));
  assert.equal(m.classify(def("filings"), db.raw.prepare("SELECT * FROM dataset_status WHERE dataset='filings'").get(), NOON_MON).status, "failed");
  const db2 = fakeD1();
  assert.equal((await m.pollEdinet({ DB: db2 }, NOON_MON)).polled, false);
  assert.match(db2.raw.prepare("SELECT last_error FROM dataset_status").get().last_error, /not set/);
});
test("filing webhooks: opt-in, cursor moves only after success", async () => {
  const db = fakeD1(); seedIssuer(db);
  db.raw.exec("INSERT INTO users (id, email, created_at, plan) VALUES ('o','o@example.test','2026-01-01','starter')"); // filing webhooks go to paid owners only
  db.raw.exec("INSERT INTO webhooks (id, owner, url, secret, types, created_at, cursor_at) VALUES ('w','o','https://x','s','[\"filing\"]','2026-01-01','2026-01-01'), ('v','o','https://y','s','[\"new_stake\"]','2026-01-01','2026-01-01')");
  await m.pollEdinet({ DB: db, EDINET_API_KEY: "k" }, NOON_MON, edinetStub([doc("S1")], []), async () => {});
  const sent = [];
  await m.deliverFilings(db, async (w) => { sent.push(w.id); return { ok: false }; });
  assert.deepEqual(sent, ["w"]);
  assert.equal(db.raw.prepare("SELECT COUNT(*) n FROM webhook_filing_cursor").get().n, 0);
  await m.deliverFilings(db, async (w, ev) => { sent.push(ev[0].doc_id); return { ok: true }; });
  assert.equal(sent[1], "S1");
  sent.length = 0;
  await m.deliverFilings(db, async () => { sent.push("again"); return { ok: true }; });
  assert.deepEqual(sent, []);
});

const BASE = process.env.BASE ?? "http://localhost:8788", PRO = process.env.PRO_KEY, FREE = process.env.FREE_KEY;
const api = (path, key, method = "GET") => fetch(BASE + path, { method, headers: key ? { authorization: `Bearer ${key}` } : {} });
test("API: status and freshness are public and list every dataset", { skip: !PRO }, async () => {
  const s = await (await api("/v1/status")).json();
  assert.equal(s.ok, true); assert.ok(["fresh", "late", "failed"].includes(s.overall));
  const f = await (await api("/v1/freshness")).json();
  assert.equal(f.data.length, m.DATASETS.length);
  for (const d of f.data) for (const k of ["source", "target_refresh_minutes", "last_successful_run", "newest_source_date", "rows", "status"]) assert.ok(k in d, k);
});
test("API: company response has data_as_of, stale and reasons", { skip: !PRO }, async () => {
  const c = await (await api("/v1/companies/7203", PRO)).json();
  assert.equal(c.name_en != null, true); assert.equal(typeof c.stale, "boolean");
  assert.ok(c.data_as_of.annual); assert.ok(c.freshness.sections.annual);
  const r = await api("/v1/companies/7203/financials", PRO);
  assert.equal(r.headers.get("x-data-stale") != null, true);
  assert.deepEqual(Object.keys((await r.json()).data_as_of), ["annual"]);
  assert.ok("data_as_of" in (await (await api("/v1/earnings-releases?limit=1", FREE)).json()));
  assert.equal((await api("/v1/macro/series", FREE)).status, 404);
});
test("API: refresh needs a key, dedupes, reports status", { skip: !PRO }, async () => {
  assert.equal((await api("/v1/companies/7203/refresh", null, "POST")).status, 401);
  assert.ok([200, 202].includes((await api("/v1/companies/7203/refresh", FREE, "POST")).status), "on every plan");
  const a = await api("/v1/companies/4042/refresh", PRO, "POST");
  assert.ok([200, 202].includes(a.status));
  const b = await (await api("/v1/companies/4042/refresh", PRO, "POST")).json();
  assert.equal(b.deduped, true); assert.equal(b.status, "queued");
  assert.equal((await (await api("/v1/companies/4042/refresh", PRO)).json()).request_id, b.request_id);
  assert.equal((await api("/v1/companies/0000/refresh", PRO, "POST")).status, 404);
});
test("API: latest filings answers", { skip: !PRO }, async () => {
  const b = await (await api("/v1/filings/latest?limit=1", FREE)).json();
  assert.ok(Array.isArray(b.data)); assert.ok("list_checked_at" in b); assert.equal(typeof b.stale, "boolean");
});
