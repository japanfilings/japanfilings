// New filings seen within minutes (cron polls EDINET's official documents list, metadata only), and the
// queue of company refreshes that japanfilings/refresh.py works through in GitHub Actions.
import type { MiddlewareHandler } from "hono";
import { collectStats, companySections, inBusinessWindow, marketDatasets, marketFreshness, sectionsFor } from "./freshness";
import { filerFlags, publicFiling } from "./privacy";
import { PAID_OWNER_SQL, post } from "./webhooks";

const API = "https://api.edinet-fsa.go.jp/api/v2/documents.json";
const REQUEST_GAP_MS = 1100; // EDINET: at most one request a second
export const KEY_LIMIT_PER_HOUR: Record<string, number> = { pro: 20, business: 100 };
export const COMPANY_LIMIT_PER_HOUR = 3;
const AUTO_COOLDOWN_MS = 24 * 3600_000;
const MAX_OPEN_AUTO = 2000;
const HOUR = 3600_000;

const randomHex = (n: number) => [...crypto.getRandomValues(new Uint8Array(n))].map((b) => b.toString(16).padStart(2, "0")).join("");
export const jstDay = (at: Date) => new Date(at.getTime() + 9 * HOUR).toISOString().slice(0, 10);
export const shouldPoll = (at: Date) => inBusinessWindow(at) || at.getUTCMinutes() % 30 < 5;

/** When the ingest should next reach the queue: an estimate, since GitHub's schedule can run late. */
export function expectedBy(now: Date) {
  if (inBusinessWindow(now)) return new Date(now.getTime() + 20 * 60_000).toISOString();
  let t = new Date(now.getTime() + 9 * HOUR);
  t = new Date(Date.UTC(t.getUTCFullYear(), t.getUTCMonth(), t.getUTCDate(), 8, 20) - 9 * HOUR);
  while (t.getTime() <= now.getTime() || !inBusinessWindow(t)) t = new Date(t.getTime() + 24 * HOUR);
  return t.toISOString();
}

// ---------- filing poll ----------

const DERIVED = [["large_holdings", "doc_id"], ["ownership_events", "doc_id"], ["holder_trades", "doc_id"], ["annual_reports", "doc_id"], ["cross_holdings", "doc_id"],
  ["cross_holding_summary", "doc_id"], ["buyback_reports", "doc_id"], ["governance_events", "doc_id"], ["disclosed_ratings", "doc_id"]];

async function recordPoll(db: D1Database, now: Date, error: string | null, newest?: string | null) {
  const at = now.toISOString();
  await db.prepare(
    "INSERT INTO dataset_status (dataset, last_attempt_at, last_success_at, last_error, newest_source_date) VALUES ('filings', ?1, ?2, ?3, ?4) " +
      "ON CONFLICT(dataset) DO UPDATE SET last_attempt_at = ?1, last_success_at = COALESCE(?2, last_success_at), last_error = ?3, " +
      "newest_source_date = CASE WHEN ?4 > COALESCE(newest_source_date, '') THEN ?4 ELSE newest_source_date END",
  ).bind(at, error ? null : at, error, newest ?? null).run();
}

async function list(fetchFn: typeof fetch, key: string, day: string, type: 1 | 2) {
  const res = await fetchFn(`${API}?date=${day}&type=${type}&Subscription-Key=${encodeURIComponent(key)}`, { signal: AbortSignal.timeout(30_000) });
  if (!res.ok) throw new Error(`EDINET HTTP ${res.status}`);
  const body: any = await res.json();
  const status = body?.metadata?.status ?? body?.StatusCode ?? body?.statusCode;
  if (String(status) !== "200") throw new Error(`EDINET status ${status}`); // never the URL: it holds the key
  return body;
}

export async function pollEdinet(env: { DB: D1Database; EDINET_API_KEY?: string }, now = new Date(), fetchFn: typeof fetch = fetch, sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))) {
  const db = env.DB;
  if (!env.EDINET_API_KEY) { await recordPoll(db, now, "EDINET_API_KEY secret not set"); return { polled: false, new: 0, withdrawn: 0 }; }
  const day = jstDay(now);
  try {
    const meta = await list(fetchFn, env.EDINET_API_KEY, day, 1);
    const count = meta.metadata?.resultset?.count ?? 0, processed = meta.metadata?.processDateTime ?? null;
    const state = await db.prepare("SELECT process_datetime, doc_count FROM filing_poll_state WHERE day = ?1").bind(day).first<any>();
    if (state && state.process_datetime === processed && state.doc_count === count) {
      await recordPoll(db, now, null);
      return { polled: true, new: 0, withdrawn: 0 };
    }
    await sleep(REQUEST_GAP_MS);
    const docs: any[] = (await list(fetchFn, env.EDINET_API_KEY, day, 2)).results ?? [];
    const { results: seen } = await db.prepare("SELECT doc_id, withdrawn FROM filing_feed WHERE day = ?1").bind(day).all<any>();
    const known = new Map(seen.map((r) => [r.doc_id, r.withdrawn]));
    const at = now.toISOString(), stmts: D1PreparedStatement[] = [];
    let fresh = 0, gone = 0, newest: string | null = null;
    for (const d of docs) {
      const withdrawn = d.withdrawalStatus !== "0" ? 1 : 0;
      if (d.submitDateTime && (!newest || d.submitDateTime > newest)) newest = d.submitDateTime;
      if (known.get(d.docID) === withdrawn) continue;
      if (!known.has(d.docID)) fresh++;
      stmts.push(db.prepare(
        "INSERT INTO filings (doc_id, submit_datetime, edinet_code, ticker, filer_name_ja, doc_type_code, description_ja, subject_edinet_code, issuer_edinet_code, parent_doc_id, withdrawn, has_xbrl, has_csv, has_english) " +
          "VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14) ON CONFLICT(doc_id) DO UPDATE SET withdrawn = excluded.withdrawn, " +
          "submit_datetime = COALESCE(excluded.submit_datetime, submit_datetime), doc_type_code = COALESCE(excluded.doc_type_code, doc_type_code)",
      ).bind(d.docID, d.submitDateTime ?? null, d.edinetCode ?? null, (d.secCode ?? "").trim().slice(0, 4) || null, d.filerName ?? null, d.docTypeCode ?? null, d.docDescription ?? null,
        d.subjectEdinetCode ?? null, d.issuerEdinetCode ?? null, d.parentDocID ?? null, withdrawn, d.xbrlFlag === "1" ? 1 : 0, d.csvFlag === "1" ? 1 : 0, d.englishDocFlag === "1" ? 1 : 0));
      stmts.push(db.prepare("INSERT INTO filing_feed (doc_id, first_seen_at, day, withdrawn) VALUES (?1, ?2, ?3, ?4) ON CONFLICT(doc_id) DO UPDATE SET withdrawn = excluded.withdrawn").bind(d.docID, at, day, withdrawn));
      if (withdrawn) { // a withdrawn filing must stop being served at once; the ingest removes it from the store too
        gone++;
        for (const [t, col] of DERIVED) stmts.push(db.prepare(`DELETE FROM ${t} WHERE ${col} = ?1`).bind(d.docID));
        stmts.push(db.prepare("DELETE FROM earnings_releases WHERE url = ?1").bind(`edinet:${d.docID}`));
      }
    }
    for (let i = 0; i < stmts.length; i += 90) await db.batch(stmts.slice(i, i + 90));
    await db.prepare("INSERT OR REPLACE INTO filing_poll_state VALUES (?1, ?2, ?3, ?4)").bind(day, processed, count, at).run();
    await recordPoll(db, now, null, newest);
    return { polled: true, new: fresh, withdrawn: gone };
  } catch (e) {
    await recordPoll(db, now, String((e as Error).message ?? e).slice(0, 120).replace(/Subscription-Key=[^&\s]*/g, "Subscription-Key=***"));
    return { polled: true, new: 0, withdrawn: 0, error: true };
  }
}

const FEED_SQL =
  "SELECT f.doc_id, f.submit_datetime, f.edinet_code, COALESCE(f.ticker, i.ticker) AS ticker, f.filer_name_ja, f.doc_type_code, f.description_ja, f.parent_doc_id, f.issuer_edinet_code, f.withdrawn, s.first_seen_at, " + filerFlags("f.edinet_code") + " " +
  "FROM filing_feed s JOIN filings f ON f.doc_id = s.doc_id LEFT JOIN issuers i ON i.edinet_code = f.issuer_edinet_code ";

export async function latestFilings(db: D1Database, opts: { ticker?: string; since?: string; limit: number }) {
  const { results } = await db.prepare(`${FEED_SQL}WHERE (?1 IS NULL OR s.first_seen_at >= ?1) AND (?2 IS NULL OR f.ticker = ?2 OR i.ticker = ?2) ORDER BY s.first_seen_at DESC, s.doc_id DESC LIMIT ?3`)
    .bind(opts.since ?? null, opts.ticker?.toUpperCase() ?? null, opts.limit).all();
  const p = await db.prepare("SELECT MAX(polled_at) AS at FROM filing_poll_state").first<{ at: string | null }>();
  return { data: results.map(publicFiling), list_checked_at: p?.at ?? null, notes: "Metadata from EDINET's documents list as soon as it is seen. Parsed contents follow from the ingest.", attribution: "Source: EDINET (Financial Services Agency of Japan); processed by JapanStake. Not investment advice." };
}

/** Subscribers opt in with types: ["filing"]. The cursor moves only after a 2xx. */
export async function deliverFilings(db: D1Database, send: typeof post = post) {
  const { results: hooks } = await db.prepare(`SELECT w.*, c.cursor_at AS fa, c.cursor_id AS fi FROM webhooks w LEFT JOIN webhook_filing_cursor c ON c.webhook_id = w.id WHERE w.enabled = 1 AND ${PAID_OWNER_SQL} AND (w.types LIKE '%"filing"%' OR w.types LIKE '%"tender_offer"%' OR w.types LIKE '%"voting"%')`).all<any>();
  for (const w of hooks) {
    let tickers: string[] | null, wanted: string[];
    try { tickers = w.tickers ? JSON.parse(w.tickers) : null; wanted = JSON.parse(w.types ?? "[]"); } catch { continue; } // one corrupt row must not stop the others
    const at = w.fa ?? w.created_at, id = w.fi ?? "";
    const { results } = await db.prepare(`${FEED_SQL}WHERE s.first_seen_at > ?1 OR (s.first_seen_at = ?1 AND s.doc_id > ?2) ORDER BY s.first_seen_at, s.doc_id LIMIT 100`).bind(at, id).all<any>();
    if (!results.length) continue;
    const kind = (e: any) => ["240", "270", "290"].includes(e.doc_type_code) ? "tender_offer" : ["180", "190"].includes(e.doc_type_code) ? "voting" : "filing";
    const events = results.filter((e) => (!tickers || tickers.includes(e.ticker)) && (wanted.includes("filing") || wanted.includes(kind(e)))).map((e) => ({ type: kind(e), ...publicFiling(e) }));
    let ok = true;
    if (events.length) {
      try { ok = (await send(w, events)).ok; } catch { ok = false; }
    }
    if (!ok) { await db.prepare("UPDATE webhooks SET failures = failures + 1, last_attempt_at = ?2, last_error = 'filing delivery failed' WHERE id = ?1").bind(w.id, new Date().toISOString()).run(); continue; }
    const last = results[results.length - 1];
    await db.prepare("INSERT OR REPLACE INTO webhook_filing_cursor VALUES (?1, ?2, ?3)").bind(w.id, last.first_seen_at, last.doc_id).run();
  }
}

export async function cron(env: { DB: D1Database; EDINET_API_KEY?: string }, at = new Date()) {
  if (shouldPoll(at)) await pollEdinet(env, at).catch((e) => console.error("poll", String(e).slice(0, 100)));
  await deliverFilings(env.DB).catch((e) => console.error("filing webhooks", e));
  const s = await env.DB.prepare("SELECT MIN(COALESCE(stats_at, '')) AS oldest FROM dataset_status").first<{ oldest: string | null }>();
  if (!s?.oldest || at.getTime() - Date.parse(s.oldest) > 24 * HOUR) {
    await collectStats(env.DB, at).catch((e) => console.error("stats", e));
    await env.DB.prepare("DELETE FROM filing_feed WHERE first_seen_at < ?1").bind(new Date(at.getTime() - 30 * 24 * HOUR).toISOString()).run();
  }
}

// ---------- refresh queue ----------

const view = (r: any, deduped: boolean, now: Date, lastChecked: string | null) => ({
  request_id: r.id, ticker: r.ticker, status: r.status, deduped, source: r.source, requested_at: r.requested_at, started_at: r.started_at, finished_at: r.finished_at,
  checked_at: r.checked_at, last_checked_at: r.checked_at ?? lastChecked, expected_by: ["queued", "running"].includes(r.status) ? expectedBy(now) : null, result: r.result ? JSON.parse(r.result) : null,
});

const OPEN = "SELECT * FROM refresh_requests WHERE ticker = ?1 AND status IN ('queued', 'running')";

async function insert(db: D1Database, ticker: string, source: "manual" | "auto", owner: string | null, now: Date) {
  const id = "rf_" + randomHex(8);
  const r = await db.prepare("INSERT OR IGNORE INTO refresh_requests (id, ticker, owner, source, priority, status, requested_at) VALUES (?1, ?2, ?3, ?4, ?5, 'queued', ?6)")
    .bind(id, ticker, owner, source, source === "manual" ? 0 : 1, now.toISOString()).run();
  return r.meta.changes > 0 ? id : null;
}

const lastChecked = async (db: D1Database, t: string) => (await db.prepare("SELECT MAX(checked_at) AS at FROM refresh_requests WHERE ticker = ?1").bind(t).first<{ at: string | null }>())?.at ?? null;

export async function requestRefresh(db: D1Database, tickerIn: string, plan: string, owner: string, now = new Date()): Promise<{ status: number; body: any; headers?: Record<string, string> }> {
  const ticker = tickerIn.toUpperCase();
  if (!/^[0-9A-Z]{4}$/.test(ticker) || !(await db.prepare("SELECT 1 FROM issuers WHERE ticker = ?1 LIMIT 1").bind(ticker).first())) return { status: 404, body: { error: "No listed company for that ticker." } };
  const lc = await lastChecked(db, ticker);
  const open = await db.prepare(OPEN).bind(ticker).first<any>();
  if (open) { // joining an open request costs nothing and lifts an automatic one to the front
    if (open.priority) await db.prepare("UPDATE refresh_requests SET priority = 0 WHERE id = ?1").bind(open.id).run();
    return { status: 200, body: view(open, true, now, lc) };
  }
  const since = new Date(now.getTime() - HOUR).toISOString();
  const limit = Object.hasOwn(KEY_LIMIT_PER_HOUR, plan) ? KEY_LIMIT_PER_HOUR[plan] : 0; // "constructor" is not a plan
  const mine = await db.prepare("SELECT COUNT(*) n, MIN(requested_at) o FROM refresh_requests WHERE owner = ?1 AND source = 'manual' AND requested_at > ?2").bind(owner, since).first<any>();
  const co = await db.prepare("SELECT COUNT(*) n, MIN(requested_at) o FROM refresh_requests WHERE ticker = ?1 AND source = 'manual' AND requested_at > ?2").bind(ticker, since).first<any>();
  const refuse = (o: string, error: string) => ({ status: 429, body: { error }, headers: { "Retry-After": String(Math.max(1, Math.ceil((Date.parse(o) + HOUR - now.getTime()) / 1000))) } });
  if (mine.n >= limit) return refuse(mine.o, `At most ${limit} refresh requests an hour on this plan.`);
  if (co.n >= COMPANY_LIMIT_PER_HOUR) return refuse(co.o, `At most ${COMPANY_LIMIT_PER_HOUR} refreshes an hour for one company.`);
  const id = await insert(db, ticker, "manual", owner, now);
  const row = await db.prepare(id ? "SELECT * FROM refresh_requests WHERE id = ?1" : OPEN).bind(id ?? ticker).first<any>();
  return { status: id ? 202 : 200, body: view(row, !id, now, lc) };
}

export async function refreshStatus(db: D1Database, tickerIn: string, now = new Date()) {
  const ticker = tickerIn.toUpperCase();
  const r = await db.prepare("SELECT * FROM refresh_requests WHERE ticker = ?1 ORDER BY requested_at DESC LIMIT 1").bind(ticker).first<any>();
  const lc = await lastChecked(db, ticker);
  return r ? view(r, false, now, lc) : { ticker, status: "none", last_checked_at: lc };
}

/** A section found stale for this company is queued once, then left alone for a day. */
export async function autoEnqueue(db: D1Database, ticker: string, now = new Date()) {
  const open = await db.prepare(OPEN).bind(ticker).first<any>();
  if (open) return view(open, true, now, null);
  const last = await db.prepare("SELECT requested_at FROM refresh_requests WHERE ticker = ?1 ORDER BY requested_at DESC LIMIT 1").bind(ticker).first<any>();
  if (last && now.getTime() - Date.parse(last.requested_at) < AUTO_COOLDOWN_MS) return null;
  const n = await db.prepare("SELECT COUNT(*) n FROM refresh_requests WHERE source = 'auto' AND status IN ('queued', 'running')").first<any>();
  if (n.n >= MAX_OPEN_AUTO) return null;
  const id = await insert(db, ticker, "auto", null, now);
  return id ? view(await db.prepare("SELECT * FROM refresh_requests WHERE id = ?1").bind(id).first<any>(), false, now, null) : null;
}

const COMPANY_PATH = /^\/v1\/companies\/([^/]+)(?:\/([^/]+))?$/;
export const needsRefresh = (s: { stale_reasons: string[]; reason: string | null }) =>
  s.stale_reasons.some((r) => r === "newer_filing_not_parsed" || r === "older_than_target");

/** Adds data_as_of, stale and reasons to JSON responses, and queues what is stale. A failure here never fails the response. */
export const annotate: MiddlewareHandler<any> = async (c, next) => {
  await next();
  if (c.req.method !== "GET" || (c.res.status !== 200 && c.res.status !== 404) || c.req.query("format") === "csv") return;
  const path = c.req.path, m = COMPANY_PATH.exec(path);
  try {
    let extra: any = null;
    if (m && m[1] !== "search" && m[2] !== "refresh") {
      const ids = sectionsFor(m[2]);
      if (!ids) return;
      const ticker = m[1].toUpperCase();
      const known = c.res.status === 200 || (await c.env.DB.prepare("SELECT 1 FROM issuers WHERE ticker = ?1 LIMIT 1").bind(ticker).first());
      if (!known) return;
      const sections = await companySections(c.env.DB, ticker, ids);
      const list = Object.entries(sections);
      const refresh = list.some(([, s]) => needsRefresh(s)) ? await autoEnqueue(c.env.DB, ticker) : null;
      extra = {
        data_as_of: Object.fromEntries(list.map(([k, s]) => [k, s.data_as_of])), stale: list.some(([, s]) => s.stale),
        unavailable: Object.fromEntries(list.filter(([, s]) => s.reason).map(([k, s]) => [k, s.reason])),
        freshness: { checked_at: new Date().toISOString(), sections, refresh },
      };
    } else if (c.res.status === 200) {
      const ids = marketDatasets(path);
      if (ids) extra = await marketFreshness(c.env.DB, ids);
    }
    if (!extra) return;
    const body: any = await c.res.clone().json().catch(() => null);
    if (!body || Array.isArray(body) || typeof body !== "object") return;
    const headers = new Headers(c.res.headers);
    headers.set("X-Data-Stale", String(extra.stale));
    headers.delete("content-length");
    const status = c.res.status;
    c.res = undefined;
    c.res = new Response(JSON.stringify({ ...body, ...extra }), { status, headers });
  } catch (e) {
    console.error("freshness", e);
  }
};
