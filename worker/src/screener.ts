// Screener, peers and sector benchmarks over company_facts, a small typed table built once a day.
// No stock prices and no valuation: every field comes from filings or our filing-based indicators.
//
// Read costs (D1 bills rows read):
//   request paths read company_facts or sector_benchmarks only, never annual_reports JSON;
//   screener: a request with an indexed sort reads about limit + 1 rows when filters match often,
//   and at most one pass over company_facts (about 3,800 rows) when they rarely match. Identical
//   queries are served from the Workers Cache API for an hour, so that pass is not repeated.
//   peers: about 2 x limit + 1. benchmarks and a sector lookup: 1 to 40.
import { INDUSTRY_EN, type Plan } from "./data";

export const ATTRIBUTION = "Source: EDINET (Financial Services Agency of Japan); processed by japanfilings. Not investment advice.";
const NOTE = "Filters and figures are descriptive, from filings. No stock prices or valuation; not a recommendation. stated_rating is one bond's rating as the issuer stated it in a filing on stated_rating_date: the agency's opinion, not ours, and it may have changed. Agencies' scales are not equivalent.";

// The allow-list. Column names only ever come from these keys, values are always bound.
export const NUMERIC_FIELDS = {
  revenue: "Revenue, yen",
  net_income: "Net income, yen",
  total_assets: "Total assets, yen",
  ordinary_margin: "Ordinary income (or pre-tax profit) / revenue, ratio",
  net_margin: "Net income / revenue, ratio",
  roe: "Return on equity as stated, %",
  equity_ratio: "Equity ratio as stated, %",
  revenue_growth: "Revenue change on the prior year, ratio",
  net_income_growth: "Net income change on the prior year, ratio",
  ocf_to_assets: "Operating cash flow / total assets, ratio",
  cross_held_pct: "Share held by listed companies as strategic holdings, %",
  mean_percentile: "Arithmetic mean of the metric percentiles in financial_percentiles; not a rating",
  mean_percentile_within_sector: "Percentile of mean_percentile within the sector",
  policy_uncertainty: "Policy-uncertainty measure",
  policy_uncertainty_percentile: "Policy-uncertainty percentile",
} as const;
type NumericField = keyof typeof NUMERIC_FIELDS;
const isNumericField = (s: string): s is NumericField => Object.prototype.hasOwnProperty.call(NUMERIC_FIELDS, s);

const COLUMNS = ["ticker", "name_en", "name_ja", "sector_ja", "fiscal_year_end", "doc_id", ...Object.keys(NUMERIC_FIELDS), "stated_rating", "stated_rating_agency", "stated_rating_date"];
const SELECT = COLUMNS.join(", ");

// Position on the letter scale, for "at or above" filters only. Agencies' opinions are not equivalent.
const RATING_SCALE: Record<string, number> = {};
[["AAA"], ["AA+"], ["AA"], ["AA-"], ["A+"], ["A"], ["A-"], ["BBB+"], ["BBB"], ["BBB-"], ["BB+"], ["BB"], ["BB-"], ["B+"], ["B"], ["B-"], ["CCC"], ["CC"], ["C"], ["D"]]
  .forEach(([r], i) => { RATING_SCALE[r] = i + 1; });
["Aaa", "Aa1", "Aa2", "Aa3", "A1", "A2", "A3", "Baa1", "Baa2", "Baa3", "Ba1", "Ba2", "Ba3", "B1", "B2", "B3", "Caa", "Ca"]
  .forEach((r, i) => { RATING_SCALE[r] = i + 1; });
const RATING_RANK_SQL = `CASE r.rating ${Object.entries(RATING_SCALE).map(([k, v]) => `WHEN '${k}' THEN ${v}`).join(" ")} END`;

// ---------- Sectors ----------

const slug = (en: string) => en.toLowerCase().replace(/&/g, " ").replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
const REIT = "REIT";
const SECTORS = [...Object.entries(INDUSTRY_EN), [REIT, REIT] as [string, string]].map(([ja, en]) => ({ sector_ja: ja, sector_en: en, slug: slug(en) }));

/** Accepts a slug, English name or Japanese name. */
export function resolveSector(input: string) {
  const s = input.trim();
  const low = s.toLowerCase();
  return SECTORS.find((x) => x.slug === low || x.sector_en.toLowerCase() === low || x.sector_ja === s) ?? null;
}
const sectorOf = (ja: string | null) => SECTORS.find((x) => x.sector_ja === ja) ?? null;
const present = (r: Record<string, any>) => ({ ...r, sector_en: sectorOf(r.sector_ja)?.sector_en ?? null, sector: sectorOf(r.sector_ja)?.slug ?? null });

export async function listSectors(db: D1Database) {
  const { results } = await db.prepare("SELECT sector_ja, company_count, as_of FROM sector_benchmarks ORDER BY company_count DESC").all<any>();
  return { data: results.map((r) => ({ ...sectorOf(r.sector_ja), company_count: r.company_count, as_of: r.as_of })), attribution: ATTRIBUTION };
}

export async function sectorBenchmarks(db: D1Database, input: string) {
  const sector = resolveSector(input);
  if (!sector) return null;
  const row = await db.prepare("SELECT company_count, as_of, record FROM sector_benchmarks WHERE sector_ja = ?1").bind(sector.sector_ja).first<any>();
  if (!row) return null;
  return {
    ...sector, company_count: row.company_count, as_of: row.as_of, metrics: JSON.parse(row.record),
    method: "Latest annual report per company. Quartiles by linear interpolation; null where fewer than 5 companies report the field.",
    note: NOTE, attribution: ATTRIBUTION,
  };
}

// ---------- Screener ----------

export type ScreenerQuery = { sql: string; args: unknown[]; limit: number; sort: NumericField; order: "asc" | "desc" };
const RESERVED = new Set(["sector", "has_stated_rating", "min_stated_rating", "sort", "order", "limit", "cursor", "format", "bom"]);
const CURSOR = /^(-?\d+(?:\.\d+)?(?:e[+-]?\d+)?)\|([0-9A-Z]{1,8})$/i;

/** Turns query parameters into parameterized SQL, or says what is wrong with them. Unknown parameters are errors. */
export function buildScreener(params: Record<string, string | undefined>, maxLimit = 200): ScreenerQuery | { error: string } {
  const where: string[] = [];
  const args: unknown[] = [];
  for (const [k, raw] of Object.entries(params)) {
    if (raw === undefined || raw === "" || RESERVED.has(k)) continue;
    const m = /^(min|max)_(.+)$/.exec(k);
    if (!m || !isNumericField(m[2])) return { error: `Unknown parameter '${k.slice(0, 40)}'. Filters are min_<field> and max_<field>; fields: ${Object.keys(NUMERIC_FIELDS).join(", ")}.` };
    const n = Number(raw);
    if (!Number.isFinite(n)) return { error: `${k} must be a number.` };
    where.push(`${m[2]} ${m[1] === "min" ? ">=" : "<="} ?${args.push(n)}`);
  }
  if (params.sector) {
    const names = params.sector.split(",").slice(0, 34).map((s) => resolveSector(s));
    if (names.some((n) => !n)) return { error: "Unknown sector. See /v1/sectors." };
    where.push(`sector_ja IN (${names.map((n) => `?${args.push(n!.sector_ja)}`).join(",")})`);
  }
  if (params.has_stated_rating !== undefined && params.has_stated_rating !== "") {
    if (!["true", "false"].includes(params.has_stated_rating)) return { error: "has_stated_rating must be true or false." };
    where.push(`stated_rating ${params.has_stated_rating === "true" ? "IS NOT NULL" : "IS NULL"}`);
  }
  if (params.min_stated_rating) {
    const rank = RATING_SCALE[params.min_stated_rating];
    if (!rank) return { error: "min_stated_rating must be a letter rating such as A- or A3." };
    where.push(`stated_rating_rank <= ?${args.push(rank)}`);
  }
  const sort = params.sort ?? "revenue";
  if (!isNumericField(sort)) return { error: `sort must be one of ${Object.keys(NUMERIC_FIELDS).join(", ")}.` };
  const order = params.order === "asc" ? "asc" : "desc";
  if (params.order && !["asc", "desc"].includes(params.order)) return { error: "order must be asc or desc." };
  where.push(`${sort} IS NOT NULL`);
  if (params.cursor) {
    const m = CURSOR.exec(params.cursor);
    if (!m) return { error: "Invalid cursor." };
    const v = args.push(Number(m[1]));
    const t = args.push(m[2].toUpperCase());
    where.push(`(${sort}, ticker) ${order === "asc" ? ">" : "<"} (?${v}, ?${t})`);
  }
  const limit = Math.min(Math.max(Number(params.limit) || 50, 1), maxLimit);
  const dir = order.toUpperCase();
  const lim = args.push(limit + 1);
  return { sql: `SELECT ${SELECT} FROM company_facts WHERE ${where.join(" AND ")} ORDER BY ${sort} ${dir}, ticker ${dir} LIMIT ?${lim}`, args, limit, sort: sort as NumericField, order };
}

export async function runScreener(db: D1Database, q: ScreenerQuery) {
  const res = await db.prepare(q.sql).bind(...q.args).all<Record<string, any>>();
  const rows = res.results.slice(0, q.limit);
  const last = rows[rows.length - 1];
  return {
    data: rows.map(present),
    next_cursor: res.results.length > q.limit && last ? `${last[q.sort]}|${last.ticker}` : null,
    sort: q.sort, order: q.order,
    meta: { rows_read: res.meta?.rows_read ?? null, cached: false },
    note: NOTE, attribution: ATTRIBUTION,
  };
}

/** Same query, same answer for an hour: the facts change once a day. Free on every Workers plan. */
export async function cachedScreener(db: D1Database, q: ScreenerQuery) {
  const cache: Cache | undefined = (globalThis as any).caches?.default;
  const key = new Request("https://screener.cache.internal/?" + encodeURIComponent(JSON.stringify([q.sql, q.args])));
  const hit = cache && (await cache.match(key).catch(() => undefined));
  if (hit) {
    const body: any = await hit.json();
    return { ...body, meta: { rows_read: 0, cached: true } };
  }
  const body = await runScreener(db, q);
  if (cache) await cache.put(key, new Response(JSON.stringify(body), { headers: { "content-type": "application/json", "cache-control": "public, max-age=3600" } })).catch(() => undefined);
  return body;
}

export const screenerFieldDocs = () => ({ numeric_fields: NUMERIC_FIELDS });

// ---------- Peers ----------

/** Same sector, nearest revenue on a log scale. Two index range reads, so about 2 x limit rows. */
export async function peers(db: D1Database, ticker: string, plan: Plan, requested: number) {
  const limit = Math.min(Math.max(requested || 10, 1), 50);
  const self = await db.prepare(`SELECT ${SELECT} FROM company_facts WHERE ticker = ?1`).bind(ticker.toUpperCase()).first<Record<string, any>>();
  if (!self) return null;
  const base = { company: present(self), note: NOTE + " Peers are chosen by stated sector and revenue only.", attribution: ATTRIBUTION };
  if (!self.sector_ja || !self.revenue || self.revenue <= 0) return { ...base, data: [], reason: "This company has no sector or positive revenue on file." };
  const side = (op: string, dir: string) =>
    db.prepare(`SELECT ${SELECT} FROM company_facts WHERE sector_ja = ?1 AND revenue ${op} ?2 AND ticker <> ?3 ORDER BY revenue ${dir} LIMIT ?4`)
      .bind(self.sector_ja, self.revenue, self.ticker, limit);
  const [up, down] = await db.batch<Record<string, any>>([side(">=", "ASC"), side("<", "DESC")]);
  const distance = (r: Record<string, any>) => (r.revenue > 0 ? Math.abs(Math.log(r.revenue / self.revenue)) : Infinity);
  const data = [...up.results, ...down.results]
    .sort((a, b) => distance(a) - distance(b) || String(a.ticker).localeCompare(String(b.ticker)))
    .slice(0, limit)
    .map((r) => ({ ...present(r), revenue_ratio: r.revenue > 0 ? Math.round((r.revenue / self.revenue) * 1000) / 1000 : null }));
  return { ...base, data };
}

// ---------- Daily rebuild ----------

const FACT_COLUMNS = [
  "name_en", "name_ja", "sector_ja", "fiscal_year_end", "doc_id", "source_submit_date", "revenue", "net_income", "total_assets",
  "ordinary_margin", "net_margin", "roe", "equity_ratio", "revenue_growth", "net_income_growth", "ocf_to_assets", "cross_held_pct",
  "mean_percentile", "mean_percentile_within_sector", "policy_uncertainty", "policy_uncertainty_percentile",
  "stated_rating", "stated_rating_agency", "stated_rating_date", "stated_rating_rank",
];

// Writes only rows that changed, so a quiet day costs reads and almost no writes.
// A share above 100% is a filer's tagging error (a few filings are off by 1000x), so it reads as unknown.
const REBUILD_SQL = `
WITH metric AS (
  SELECT c.ticker, c.fiscal_year_end, c.doc_id, a.submit_date, c.policy_uncertainty, c.record AS rec,
    (SELECT value FROM json_each(a.record, '$.key_metrics') WHERE json_extract(value, '$.fiscal_year_end') = a.fiscal_year_end LIMIT 1) AS km
  FROM company_indicators c JOIN annual_reports a ON a.doc_id = c.doc_id
),
rated AS (
  SELECT r.ticker, r.rating, r.agency, r.rating_date, ${RATING_RANK_SQL} AS rank,
    ROW_NUMBER() OVER (PARTITION BY r.ticker ORDER BY r.rating_date DESC, ${RATING_RANK_SQL} DESC, r.agency) AS rn
  FROM disclosed_ratings r WHERE r.ticker IS NOT NULL AND ${RATING_RANK_SQL} IS NOT NULL
)
INSERT INTO company_facts (ticker, ${FACT_COLUMNS.join(", ")})
SELECT m.ticker, i.name_en, i.name_ja,
  CASE WHEN i.name_ja LIKE '%投資法人%' THEN '${REIT}' ELSE i.industry_ja END,
  m.fiscal_year_end, m.doc_id, m.submit_date,
  json_extract(m.km, '$.revenue'), json_extract(m.km, '$.net_income'), json_extract(m.km, '$.total_assets'),
  json_extract(m.rec, '$.financial_percentiles.metrics.ordinary_margin.value'),
  CASE WHEN json_extract(m.km, '$.revenue') > 0 THEN round(json_extract(m.km, '$.net_income') * 1.0 / json_extract(m.km, '$.revenue'), 4) END,
  json_extract(m.rec, '$.financial_percentiles.metrics.roe.value'), json_extract(m.rec, '$.financial_percentiles.metrics.equity_ratio.value'),
  json_extract(m.rec, '$.financial_percentiles.metrics.revenue_growth.value'), json_extract(m.rec, '$.financial_percentiles.metrics.net_income_growth.value'),
  json_extract(m.rec, '$.financial_percentiles.metrics.ocf_to_assets.value'),
  CASE WHEN json_extract(m.rec, '$.ownership_activity.held_by_listed_companies_pct') <= 100 THEN json_extract(m.rec, '$.ownership_activity.held_by_listed_companies_pct') END,
  json_extract(m.rec, '$.financial_percentiles.mean_percentile'), json_extract(m.rec, '$.financial_percentiles.mean_percentile_within_sector'),
  m.policy_uncertainty, json_extract(m.rec, '$.policy_uncertainty.percentile'),
  d.rating, d.agency, d.rating_date, d.rank
FROM metric m
LEFT JOIN issuers i ON i.ticker = m.ticker
LEFT JOIN rated d ON d.ticker = m.ticker AND d.rn = 1
WHERE true
ON CONFLICT(ticker) DO UPDATE SET ${FACT_COLUMNS.map((c) => `${c} = excluded.${c}`).join(", ")}
WHERE (${FACT_COLUMNS.map((c) => `company_facts.${c}`).join(", ")}) IS NOT (${FACT_COLUMNS.map((c) => `excluded.${c}`).join(", ")})`;

const BENCHMARK_FIELDS: NumericField[] = ["revenue", "ordinary_margin", "net_margin", "roe", "equity_ratio", "revenue_growth", "net_income_growth", "ocf_to_assets", "cross_held_pct", "mean_percentile", "policy_uncertainty"];
const MIN_SAMPLE = 5;

export function quartiles(values: number[]) {
  const n = values.length;
  if (n < MIN_SAMPLE) return { n, q1: null, median: null, q3: null };
  const s = [...values].sort((a, b) => a - b);
  const at = (p: number) => {
    const i = (n - 1) * p;
    const lo = Math.floor(i);
    const v = s[lo] + (s[Math.min(lo + 1, n - 1)] - s[lo]) * (i - lo);
    return Math.round(v * 10000) / 10000;
  };
  return { n, q1: at(0.25), median: at(0.5), q3: at(0.75) };
}

export async function rebuildFacts(db: D1Database) {
  const [upsert, removed] = await db.batch([
    db.prepare(REBUILD_SQL),
    db.prepare("DELETE FROM company_facts WHERE ticker NOT IN (SELECT ticker FROM company_indicators)"),
  ]);
  const { results, meta } = await db.prepare(`SELECT sector_ja, source_submit_date, ${BENCHMARK_FIELDS.join(", ")} FROM company_facts WHERE sector_ja IS NOT NULL`).all<Record<string, any>>();
  const groups = new Map<string, Record<string, any>[]>();
  for (const r of results) {
    if (!sectorOf(r.sector_ja)) continue;
    if (!groups.has(r.sector_ja)) groups.set(r.sector_ja, []);
    groups.get(r.sector_ja)!.push(r);
  }
  const statements = [...groups].map(([sector, rows]) => {
    const metrics = Object.fromEntries(BENCHMARK_FIELDS.map((f) => [f, quartiles(rows.map((r) => r[f]).filter((v) => typeof v === "number"))]));
    const asOf = rows.reduce((m, r) => (r.source_submit_date > m ? r.source_submit_date : m), "");
    return db.prepare(
      "INSERT INTO sector_benchmarks (sector_ja, company_count, as_of, record) VALUES (?1, ?2, ?3, ?4) " +
        "ON CONFLICT(sector_ja) DO UPDATE SET company_count = excluded.company_count, as_of = excluded.as_of, record = excluded.record " +
        "WHERE (sector_benchmarks.company_count, sector_benchmarks.as_of, sector_benchmarks.record) IS NOT (excluded.company_count, excluded.as_of, excluded.record)",
    ).bind(sector, rows.length, asOf, JSON.stringify(metrics));
  });
  statements.push(db.prepare(`DELETE FROM sector_benchmarks WHERE sector_ja NOT IN (${[...groups.keys()].map((_, i) => `?${i + 1}`).join(",") || "''"})`).bind(...groups.keys()));
  const written = await db.batch(statements);
  return {
    facts_rows_read: upsert.meta.rows_read, facts_rows_written: upsert.meta.rows_written, facts_removed: removed.meta.changes,
    benchmark_rows_read: meta.rows_read, benchmark_rows_written: written.reduce((n, r) => n + (r.meta.rows_written ?? 0), 0),
  };
}
