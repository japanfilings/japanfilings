// Whether each data product is current. Status is computed at read time from when the data last reached
// D1, so nothing stale can be reported as current. worker/src/datasets.json is shared with japanfilings/refresh.py.
import registry from "./datasets.json";

export type DatasetDef = {
  id: string; label: string; source: string; owner?: string; target_minutes: number; calendar: string; max_source_age_days?: number;
  table: string; date?: string; company?: string; where?: string; filing_types?: string[]; filing_key?: string; needs_csv?: boolean;
  company_max_age_days?: number; missing?: string;
};
export const DATASETS = registry as DatasetDef[];
export type Status = "fresh" | "late" | "failed";
const byId = new Map(DATASETS.map((d) => [d.id, d]));

const JST = 9 * 3600_000;
const WINDOW = [8 * 60, 21 * 60]; // the ingest's 10-minute runs, in minutes of the JST day

export const inBusinessWindow = (at: Date) => {
  const j = new Date(at.getTime() + JST);
  const m = j.getUTCHours() * 60 + j.getUTCMinutes();
  return j.getUTCDay() >= 1 && j.getUTCDay() <= 5 && m >= WINDOW[0] && m < WINDOW[1];
};

/** Minutes between two times that fall inside Mon-Fri 08:00-21:00 JST. */
export function businessMinutes(from: Date, to: Date) {
  let total = 0;
  for (let t = Math.floor(from.getTime() / 86_400_000 - 1) * 86_400_000 - JST; t < to.getTime(); t += 86_400_000) {
    const dow = new Date(t + JST).getUTCDay();
    if (dow === 0 || dow === 6) continue;
    const a = Math.max(from.getTime(), t + WINDOW[0] * 60_000), b = Math.min(to.getTime(), t + WINDOW[1] * 60_000);
    if (b > a) total += (b - a) / 60_000;
  }
  return total;
}

/** YYYYMMDD, 2026q3 and YYYY-MM become a plain date; anything else keeps its first ten characters. */
export function normDate(v: unknown): string | null {
  if (v == null || v === "") return null;
  const s = String(v);
  let m = /^(\d{4})(\d{2})(\d{2})$/.exec(s);
  if (m) return `${m[1]}-${m[2]}-${m[3]}`;
  m = /^(\d{4})q([1-4])$/i.exec(s);
  if (m) return `${m[1]}-${["03-31", "06-30", "09-30", "12-31"][Number(m[2]) - 1]}`;
  if (/^\d{4}-\d{2}$/.test(s)) return `${s}-01`;
  return s.slice(0, 10);
}

export const graceMinutes = (target: number) => Math.max(30, target * 0.25);
const days = (a: string, now: Date) => (now.getTime() - Date.parse(a.slice(0, 10) + "T00:00:00+09:00")) / 86_400_000;

export function classify(def: DatasetDef, row: any, now: Date): { status: Status; reasons: string[] } {
  if (!row?.last_success_at) return row?.last_error ? { status: "failed", reasons: ["no_successful_run"] } : { status: "late", reasons: ["no_run_recorded"] };
  const last = new Date(row.last_success_at);
  const elapsed = def.calendar === "business" ? businessMinutes(last, now) : (now.getTime() - last.getTime()) / 60_000;
  // One failed attempt while the last success is still within the target is a blip (EDINET refuses a
  // request now and then and the next run succeeds), not a failed dataset.
  if (row.last_error && row.last_attempt_at > row.last_success_at && elapsed > def.target_minutes) return { status: "failed", reasons: ["last_run_failed"] };
  const reasons: string[] = [];
  if (elapsed > def.target_minutes + graceMinutes(def.target_minutes)) reasons.push("run_overdue");
  const newest = normDate(row.newest_source_date);
  if (def.max_source_age_days && (!newest || days(newest, now) > def.max_source_age_days)) reasons.push("source_too_old");
  if (row.open_gap > 0) reasons.push("documents_not_parsed");
  return { status: reasons.length ? "late" : "fresh", reasons };
}

const rowsCache = new WeakMap<object, { at: number; rows: Map<string, any> }>();
const CACHE_MS = 60_000;

export async function statusRows(db: D1Database, cached = false) {
  const hit = rowsCache.get(db);
  if (cached && hit && Date.now() - hit.at < CACHE_MS) return hit.rows;
  const { results } = await db.prepare("SELECT * FROM dataset_status").all<any>();
  const rows = new Map(results.map((r) => [r.dataset as string, r]));
  rowsCache.set(db, { at: Date.now(), rows });
  return rows;
}

export async function freshnessBody(db: D1Database, now = new Date()) {
  const rows = await statusRows(db);
  const listed = rows.get("issuers")?.companies ?? null;
  const data = DATASETS.map((d) => {
    const r = rows.get(d.id);
    const { status, reasons } = classify(d, r, now);
    return {
      dataset: d.id, label: d.label, source: d.source, target_refresh_minutes: d.target_minutes,
      target_calendar: d.calendar === "business" ? "Mon-Fri 08:00-21:00 JST" : "always",
      last_successful_run: r?.last_success_at ?? null, last_attempt: r?.last_attempt_at ?? null, newest_source_date: normDate(r?.newest_source_date),
      rows: r?.rows ?? null, status, status_reasons: reasons, last_error: status === "failed" ? r?.last_error ?? null : null,
      documents_not_parsed: r?.open_gap ?? null,
      coverage: d.company && d.id !== "issuers" ? { companies_with_data: r?.companies ?? null, listed_companies: listed, pct: r?.companies != null && listed ? Math.round((r.companies / listed) * 1000) / 10 : null } : null,
    };
  });
  const { results: changes } = await db.prepare("SELECT ticker, edinet_code, change, day FROM listing_changes ORDER BY day DESC LIMIT 50").all();
  return { checked_at: now.toISOString(), overall: overall(data), data, listing_changes: changes, notes: "Status is computed when you ask. fresh: the last run reached the database within the target and its newest source date is plausible. late: past the target, or filed documents are not read yet. failed: the latest run failed. A dataset with no recorded run is late." };
}

const overall = (data: { status: Status }[]): Status => (data.some((d) => d.status === "failed") ? "failed" : data.some((d) => d.status === "late") ? "late" : "fresh");

export async function statusBody(db: D1Database, now = new Date()) {
  const f = await freshnessBody(db, now);
  const lh = f.data.find((d) => d.dataset === "large_holdings");
  const row = lh?.newest_source_date ? null : await db.prepare("SELECT MAX(submit_date) AS latest FROM large_holdings").first<{ latest: string }>();
  return {
    ok: true, data_current: f.overall === "fresh", overall: f.overall, checked_at: f.checked_at,
    latest_large_holding_submit_date: lh?.newest_source_date ?? row?.latest ?? null,
    datasets: f.data.map((d) => ({ dataset: d.dataset, status: d.status, last_successful_run: d.last_successful_run, newest_source_date: d.newest_source_date, target_refresh_minutes: d.target_refresh_minutes })),
  };
}

/** Counts from D1 itself, so the table shows what is served. A table not created yet leaves nulls. */
export async function collectStats(db: D1Database, now = new Date()) {
  for (const d of DATASETS) {
    let r: any = {};
    try {
      r = (await db.prepare(`SELECT COUNT(*) AS n, ${d.date ? `MAX(${d.date})` : "NULL"} AS newest, ${d.company ? `COUNT(DISTINCT ${d.company})` : "NULL"} AS c FROM ${d.table}${d.where ? ` WHERE ${d.where}` : ""}`).first<any>()) ?? {};
    } catch { /* not built yet */ }
    await db.prepare(
      "INSERT INTO dataset_status (dataset, newest_source_date, rows, companies, stats_at) VALUES (?1, ?2, ?3, ?4, ?5) " +
        "ON CONFLICT(dataset) DO UPDATE SET newest_source_date = excluded.newest_source_date, rows = excluded.rows, companies = excluded.companies, stats_at = excluded.stats_at",
    ).bind(d.id, normDate(r.newest), r.n ?? null, r.c ?? null, now.toISOString()).run();
  }
}

export const COMPANY_SECTIONS = DATASETS.filter((d) => d.company && !["issuers", "filings"].includes(d.id) || d.id === "filings").map((d) => d.id);
const SUBPATH: Record<string, string[]> = {
  financials: ["annual"], details: ["annual"], indicators: ["indicators"], "cross-holdings": ["cross_holdings"], trades: ["trades"], "us-funds": ["nport"],
  "earnings-releases": ["half_year"], "credit-ratings": ["ratings"], buybacks: ["buybacks"], governance: ["governance"],
  brief: ["annual", "indicators", "half_year"],
};
export const sectionsFor = (sub?: string) => (sub ? SUBPATH[sub] ?? null : COMPANY_SECTIONS);

const MARKET: [RegExp, string[]][] = [
  [/^\/v1\/large-holdings$/, ["large_holdings"]], [/^\/v1\/events$/, ["ownership_events"]],
  [/^\/v1\/credit-ratings$/, ["ratings"]], [/^\/v1\/governance$/, ["governance"]], [/^\/v1\/earnings-releases$/, ["half_year"]], [/^\/v1\/filings(\/latest)?$/, ["filings"]],
  [/^\/v1\/indicators\//, ["indicators"]], [/^\/v1\/cross-holdings\//, ["cross_holdings"]],
];
export const marketDatasets = (path: string) => MARKET.find(([re]) => re.test(path))?.[1] ?? null;

export async function marketFreshness(db: D1Database, ids: string[], now = new Date()) {
  const rows = await statusRows(db, true);
  const out = ids.map((id) => ({ dataset: id, ...classify(byId.get(id)!, rows.get(id), now), data_as_of: normDate(rows.get(id)?.newest_source_date) }));
  return { data_as_of: Object.fromEntries(out.map((o) => [o.dataset, o.data_as_of])), stale: out.some((o) => o.status !== "fresh"), freshness: { checked_at: now.toISOString(), datasets: out } };
}

export type Section = { data_as_of: string | null; available: boolean; stale: boolean; dataset_status: Status; stale_reasons: string[]; reason: string | null; newer_filing: { doc_id: string; submitted_at: string } | null };

/** One section from its facts; pure, so the rules can be tested alone. */
export function sectionView(def: DatasetDef, f: { asOf: string | null; datasetRow: any; filing: { doc_id: string; d: string } | null; listed?: boolean; tableMissing?: boolean }, now: Date): Section {
  const { status } = classify(def, f.datasetRow, now);
  const asOf = normDate(f.asOf);
  const stale_reasons: string[] = status === "fresh" ? [] : [`dataset_${status}`];
  const newer = f.filing && (!asOf || f.filing.d.slice(0, 10) > asOf) ? { doc_id: f.filing.doc_id, submitted_at: f.filing.d } : null;
  if (newer) stale_reasons.push("newer_filing_not_parsed");
  if (asOf && def.company_max_age_days && days(asOf, now) > def.company_max_age_days) stale_reasons.push("older_than_target");
  let reason: string | null = null;
  if (!asOf) reason = f.tableMissing ? "dataset_not_built" : newer ? "pending_parse" : def.missing ?? "not_available";
  return { data_as_of: asOf, available: !!asOf, stale: stale_reasons.length > 0, dataset_status: status, stale_reasons, reason, newer_filing: newer };
}

export async function companySections(db: D1Database, ticker: string, ids: string[], now = new Date()) {
  const rows = await statusRows(db, true);
  const issuer = await db.prepare("SELECT edinet_code, listed FROM issuers WHERE ticker = ?1 ORDER BY listed DESC LIMIT 1").bind(ticker).first<any>();
  const code = issuer?.edinet_code ?? "";
  const defs = ids.map((id) => byId.get(id)!).filter(Boolean);
  const newest = new Map<string, any[]>();
  const pick = (r: any[]) => { for (const x of r) newest.set(x.t, [...(newest.get(x.t) ?? []), x]); };
  const FILED = "SELECT doc_type_code t, has_csv c, MAX(submit_datetime) d, doc_id FROM filings WHERE withdrawn = 0 AND ";
  // 5% reports name the company as issuer; the last 30 days are enough here, as the nightly reconciliation covers older ones.
  const [own, issued] = await db.batch<any>([db.prepare(`${FILED}edinet_code = ?1 GROUP BY 1, 2`).bind(code), db.prepare(`${FILED}issuer_edinet_code = ?1 AND doc_type_code IN ('350', '360') AND submit_datetime >= ?2 GROUP BY 1, 2`).bind(code, new Date(now.getTime() - 30 * 86_400_000).toISOString().slice(0, 10))]);
  const issuerKeyed = new Map<string, any[]>();
  for (const x of issued.results) issuerKeyed.set(x.t, [...(issuerKeyed.get(x.t) ?? []), x]);
  pick(own.results);
  const out: Record<string, Section> = {};
  await Promise.all(defs.map(async (d) => {
    let asOf: string | null = null, tableMissing = false;
    if (d.id === "filings") asOf = own.results.reduce((m: string | null, x: any) => (!m || x.d > m ? x.d : m), null);
    else if (!d.company) asOf = rows.get(d.id)?.newest_source_date ?? null;
    else {
      try {
        const r = await db.prepare(`SELECT MAX(${d.date}) v FROM ${d.table} WHERE ${d.company} = ?1${d.where ? ` AND ${d.where}` : ""}`).bind(ticker).first<any>();
        asOf = r?.v ?? null;
      } catch { tableMissing = true; }
    }
    let filing: { doc_id: string; d: string } | null = null;
    for (const t of d.filing_types ?? []) for (const x of (d.filing_key === "issuer" ? issuerKeyed : newest).get(t) ?? []) {
      if (d.needs_csv && !x.c) continue;
      if (!filing || x.d > filing.d) filing = { doc_id: x.doc_id, d: x.d };
    }
    out[d.id] = sectionView(d, { asOf, datasetRow: rows.get(d.id), filing, tableMissing }, now);
  }));
  return out;
}
