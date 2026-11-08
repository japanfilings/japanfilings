// Read-only queries over D1. Mirrors japanfilings/store.py.

import { INDIVIDUAL, PROPOSALS, filerBlocked, filerFlags, filerHidden, publicCompanyPeople, publicEvent, publicFiling, publicHolding, publicFilerName, publicTenderOffer, publicHolderList, quote, scrubFreeText, scrubText, storedEventType, tradeHolderHidden } from "./privacy";

// "free" is an account with no paid plan: it has no API access. Starter is the entry plan.
export type Plan = "free" | "starter" | "pro" | "business";

const ATTRIBUTION = "Source: EDINET (Financial Services Agency of Japan); processed by japanfilings. Not investment advice.";

// TSE 33-sector names in English (JPX's official English names).
export const INDUSTRY_EN: Record<string, string> = {
  "水産・農林業": "Fishery, Agriculture & Forestry", "鉱業": "Mining", "建設業": "Construction", "食料品": "Foods",
  "繊維製品": "Textiles & Apparels", "パルプ・紙": "Pulp & Paper", "化学": "Chemicals", "医薬品": "Pharmaceutical",
  "石油・石炭製品": "Oil & Coal Products", "ゴム製品": "Rubber Products", "ガラス・土石製品": "Glass & Ceramics Products",
  "鉄鋼": "Iron & Steel", "非鉄金属": "Nonferrous Metals", "金属製品": "Metal Products", "機械": "Machinery",
  "電気機器": "Electric Appliances", "輸送用機器": "Transportation Equipment", "精密機器": "Precision Instruments",
  "その他製品": "Other Products", "電気・ガス業": "Electric Power & Gas", "陸運業": "Land Transportation",
  "海運業": "Marine Transportation", "空運業": "Air Transportation", "倉庫・運輸関連": "Warehousing & Harbor Transportation Services",
  "情報・通信業": "Information & Communication", "卸売業": "Wholesale Trade", "小売業": "Retail Trade", "銀行業": "Banks",
  "証券、商品先物取引業": "Securities & Commodity Futures", "保険業": "Insurance", "その他金融業": "Other Financing Business",
  "不動産業": "Real Estate", "サービス業": "Services",
};
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** English fields for codes the EDINET list only has in Japanese. REITs carry a filer type instead of a sector. */
export function englishFields(row: Record<string, any>) {
  const fye = /^(\d{1,2})月(\d{1,2})日$/.exec(row.fiscal_year_end ?? "");
  return {
    industry_en: INDUSTRY_EN[row.industry_ja] ?? (String(row.name_ja ?? "").includes("投資法人") ? "REIT" : null),
    fiscal_year_end_en: fye ? `${MONTHS[Number(fye[1]) - 1]} ${Number(fye[2])}` : null,
  };
}

function daysAgo(n: number): string {
  return new Date(Date.now() - n * 86_400_000).toISOString().slice(0, 10);
}

// REITs carry listed = 0 in EDINET's code list but have a ticker, so a ticker is the listing test.
export async function searchCompanies(db: D1Database, q: string, limit: number) {
  // An EDINET code is an exact key: one row read instead of a name scan over every issuer.
  if (/^E\d{5}$/i.test(q.trim())) {
    const { results } = await db
      .prepare(
        "SELECT i.ticker, i.edinet_code, i.name_en, i.name_ja, i.industry_ja, i.fiscal_year_end, i.corporate_number, x.figi " +
          "FROM issuers i LEFT JOIN identifiers x ON x.ticker = i.ticker WHERE i.edinet_code = ?1 AND i.ticker IS NOT NULL",
      )
      .bind(q.trim().toUpperCase())
      .all<Record<string, any>>();
    return results.map((r) => ({ ...r, ...englishFields(r) }));
  }
  const like = `%${q}%`;
  const { results } = await db
    .prepare(
      "SELECT i.ticker, i.name_en, i.name_ja, i.industry_ja, i.fiscal_year_end, i.corporate_number, x.figi " +
        "FROM issuers i LEFT JOIN identifiers x ON x.ticker = i.ticker " +
        "WHERE i.ticker IS NOT NULL AND (i.ticker = ?1 OR i.name_en LIKE ?2 OR i.name_ja LIKE ?2) ORDER BY i.listed DESC, i.ticker LIMIT ?3",
    )
    .bind(q.toUpperCase(), like, limit)
    .all<Record<string, any>>();
  return results.map((r) => ({ ...r, ...englishFields(r) }));
}

export async function getCompany(db: D1Database, ticker: string) {
  const row = await db
    .prepare(
      "SELECT i.*, x.figi, x.composite_figi, x.share_class_figi FROM issuers i " +
        "LEFT JOIN identifiers x ON x.ticker = i.ticker WHERE i.ticker = ?1 ORDER BY i.listed DESC LIMIT 1",
    )
    .bind(ticker.toUpperCase())
    .first<Record<string, unknown>>();
  if (!row) return null;
  return { ...row, ...englishFields(row), yahoo_symbol: `${row.ticker}.T`, bloomberg_ticker: `${row.ticker} JP`, attribution: ATTRIBUTION };
}

export async function largeHoldings(
  db: D1Database,
  plan: Plan,
  opts: { ticker?: string; filer?: string; since?: string; limit: number },
) {
  let since = opts.since ?? "";
  const history_limited_to = null;
  if (await filerBlocked(db, opts.filer)) return { data: [], history_limited_to, attribution: ATTRIBUTION };
  const where: string[] = ["1=1"];
  const args: unknown[] = [];
  if (opts.ticker) { where.push("issuer_ticker = ?"); args.push(opts.ticker.toUpperCase()); }
  if (opts.filer) { where.push("filer_edinet_code = ?"); args.push(opts.filer); }
  if (since) { where.push("submit_date >= ?"); args.push(since); }
  args.push(opts.limit);
  const { results } = await db
    .prepare(`SELECT l.record, i.name_ja AS master_issuer_name_ja, i.name_en AS master_issuer_name_en, ${filerFlags("l.filer_edinet_code")} FROM large_holdings l LEFT JOIN issuers i ON i.ticker = l.issuer_ticker AND i.listed = 1 WHERE ${where.join(" AND ")} ORDER BY l.submit_date DESC LIMIT ?`)
    .bind(...args)
    .all<any>();
  const rows = results.map((r) => publicHolding(r.record, r));
  return {
    data: opts.filer ? rows.filter((r: any) => r.filer_edinet_code) : rows,
    history_limited_to,
    attribution: ATTRIBUTION,
  };
}

export async function filings(db: D1Database, opts: { ticker?: string; docType?: string; since?: string; limit: number }) {
  const where: string[] = ["withdrawn = 0"];
  const args: unknown[] = [];
  if (opts.ticker) { where.push("ticker = ?"); args.push(opts.ticker.toUpperCase()); }
  if (opts.docType) { where.push("doc_type_code = ?"); args.push(opts.docType); }
  if (opts.since) { where.push("submit_datetime >= ?"); args.push(opts.since); }
  args.push(opts.limit);
  const { results } = await db
    .prepare(`SELECT f.*, ${filerFlags("f.edinet_code")} FROM filings f WHERE ${where.join(" AND ")} ORDER BY submit_datetime DESC LIMIT ?`)
    .bind(...args)
    .all<any>();
  return { data: results.map(publicFiling), attribution: ATTRIBUTION };
}

async function latestAnnual(db: D1Database, ticker: string): Promise<any | null> {
  const row = await db
    .prepare("SELECT record FROM annual_reports WHERE ticker = ?1 ORDER BY fiscal_year_end DESC, submit_date DESC LIMIT 1")
    .bind(ticker.toUpperCase())
    .first<{ record: string }>();
  return row ? JSON.parse(row.record) : null;
}

/** Five-year key metrics plus current/prior financial statements from the latest annual report. */
export async function financials(db: D1Database, ticker: string) {
  const r = await latestAnnual(db, ticker);
  if (!r) return null;
  return {
    ticker: r.ticker,
    accounting_standard: r.accounting_standard,
    fiscal_year_end: r.fiscal_year_end,
    key_metrics: r.key_metrics,
    statements: r.statements,
    source_doc_id: r.doc_id,
    attribution: ATTRIBUTION,
  };
}

/** Officer titles (no names), workforce, major shareholders, shareholder mix, auditor and fees. */
export async function companyDetails(db: D1Database, ticker: string) {
  const r = await latestAnnual(db, ticker);
  if (!r) return null;
  return { ticker: r.ticker, fiscal_year_end: r.fiscal_year_end, ...scrubFreeText(publicCompanyPeople(r.company)), source_doc_id: r.doc_id, attribution: ATTRIBUTION };
}

/** Latest structured annual-report detail item. */
export async function filingDetail(db: D1Database, ticker: string, item: string) {
  const row = await db.prepare(
    "SELECT record FROM filing_details WHERE ticker = ?1 AND item = ?2 ORDER BY fiscal_year_end DESC, submit_date DESC LIMIT 1",
  ).bind(ticker.toUpperCase(), item).first<{ record: string }>();
  return row ? { ...scrubFreeText(JSON.parse(row.record)), attribution: ATTRIBUTION } : null;
}

/** Officer and candidate names are never served; a name stuck to a proposal title is cut (same rule as the export). */
export function scrubProposalTitle(t: string | null) {
  if (!t) return t;
  return scrubText(t)!.replace(/(選任)\s+(?!の件)[^\s、。]{1,4}\s*[^\s、。]{0,4}$/u, "$1").replace(/候補者\s*[^\s、。をにがはの]{1,10}?氏/gu, "候補者");
}

export async function votingResults(db: D1Database, opts: { ticker?: string; since?: string; limit: number }) {
  const { results } = await db.prepare(
    "SELECT * FROM voting_results WHERE (?1 IS NULL OR ticker = ?1) AND (?2 IS NULL OR submit_date >= ?2) ORDER BY submit_date DESC, doc_id DESC, seq LIMIT ?3",
  ).bind(opts.ticker?.toUpperCase() ?? null, opts.since ?? null, opts.limit).all<any>();
  return {
    data: results.map(({ candidate_ja, ...r }) => ({ ...r, proposal_title_ja: scrubProposalTitle(r.proposal_title_ja) })),
    notes: "Coverage is centered on annual shareholder meetings: extraordinary reports filed around the annual securities report are scanned first. Results of extraordinary meetings outside that window may be absent. Uncertain vote cells are null.",
    attribution: ATTRIBUTION,
  };
}

export async function tenderOffers(db: D1Database, opts: { ticker?: string; since?: string; limit: number }) {
  const { results } = await db.prepare(
    `SELECT record, ${filerFlags("json_extract(record, '$.edinet_code')")} FROM tender_offers WHERE`+" (?1 IS NULL OR ticker = ?1) AND (?2 IS NULL OR substr(submit_datetime,1,10) >= ?2) ORDER BY submit_datetime DESC, doc_id DESC LIMIT ?3",
  ).bind(opts.ticker?.toUpperCase() ?? null, opts.since ?? null, opts.limit).all<any>();
  return { data: results.map((r) => scrubFreeText(publicTenderOffer(r.record, r))), attribution: ATTRIBUTION };
}

/** Everything one filer (investor) has reported under the 5% rule, newest first. */
export async function investor(db: D1Database, filerCode: string, limit: number) {
  if (await filerBlocked(db, filerCode)) return null;
  const { results } = await db
    .prepare(`SELECT l.record, i.name_ja AS master_issuer_name_ja, i.name_en AS master_issuer_name_en, ${filerFlags("l.filer_edinet_code")} FROM large_holdings l LEFT JOIN issuers i ON i.ticker = l.issuer_ticker AND i.listed = 1 WHERE l.filer_edinet_code = ?1 ORDER BY l.submit_date DESC LIMIT ?2`)
    .bind(filerCode, limit)
    .all<any>();
  const filings: any[] = results.map((r) => publicHolding(r.record, r)).filter((f: any) => f.filer_edinet_code);
  if (!filings.length) return null;
  const jointHolders = new Set<string>();
  for (const f of filings) for (const h of f.holders ?? []) if (h.name && h.name !== INDIVIDUAL) jointHolders.add(h.name);
  // Latest stake per target company.
  const positions = new Map<string, any>();
  for (const f of filings) {
    const key = f.issuer_ticker ?? f.issuer_name_ja;
    if (!positions.has(key)) {
      positions.set(key, {
        ticker: f.issuer_ticker, issuer_name_en: f.issuer_name_en, issuer_name_ja: f.issuer_name_ja,
        holding_ratio_pct: f.holding_ratio_pct, as_of: f.submit_date, doc_id: f.doc_id,
      });
    }
  }
  return {
    filer_edinet_code: filerCode,
    filer_name_ja: filings[0].filer_name_ja,
    filer_name_en: filings[0].filer_name_en ?? filings[0].filer_name_en_filing ?? null,
    joint_holder_names: [...jointHolders],
    latest_positions: [...positions.values()],
    filings,
    attribution: ATTRIBUTION,
  };
}

const SEC_ATTRIBUTION = "Source: SEC Form N-PORT data sets (U.S. Securities and Exchange Commission); mapped to Tokyo tickers via OpenFIGI. Not investment advice.";

/** U.S. registered funds holding a Tokyo-listed stock, from the latest N-PORT quarter. */
export async function usFunds(db: D1Database, ticker: string) {
  const row = await db.prepare("SELECT record FROM us_fund_holdings WHERE ticker = ?1").bind(ticker.toUpperCase()).first<{ record: string }>();
  return row ? { ...JSON.parse(row.record), attribution: SEC_ATTRIBUTION } : null;
}

export const EVENT_TYPES = [
  "new_stake", "stake_increase", "stake_decrease", "exit_below_5pct", "threshold_crossed",
  PROPOSALS, "purpose_changed", "amendment",
] as const;

/** Ownership-change events from 5% filings, newest first. */
export async function ownershipEvents(
  db: D1Database,
  plan: Plan,
  opts: { ticker?: string; filer?: string; types?: string[]; since?: string; limit: number },
) {
  let since = opts.since ?? "";
  const history_limited_to = null;
  if (await filerBlocked(db, opts.filer)) return { data: [], history_limited_to, attribution: ATTRIBUTION };
  const where: string[] = ["1=1"];
  const args: unknown[] = [];
  if (opts.ticker) { where.push("issuer_ticker = ?"); args.push(opts.ticker.toUpperCase()); }
  if (opts.filer) { where.push("filer_edinet_code = ?"); args.push(opts.filer); }
  if (opts.types?.length) { where.push(`type IN (${opts.types.map(() => "?").join(",")})`); args.push(...opts.types.map(storedEventType)); }
  if (since) { where.push("submit_date >= ?"); args.push(since); }
  args.push(opts.limit);
  const { results } = await db
    .prepare(`SELECT e.record, i.name_ja AS master_issuer_name_ja, i.name_en AS master_issuer_name_en, ${filerFlags("e.filer_edinet_code")} FROM ownership_events e LEFT JOIN issuers i ON i.ticker = e.issuer_ticker AND i.listed = 1 WHERE ${where.join(" AND ")} ORDER BY e.submit_date DESC, e.event_id LIMIT ?`)
    .bind(...args)
    .all<any>();
  const rows = results.map((r) => publicEvent(r.record, r));
  return {
    data: opts.filer ? rows.filter((r: any) => r.filer_edinet_code) : rows,
    history_limited_to,
    attribution: ATTRIBUTION,
  };
}

const xhRow = (r: any) => ({
  holder: r.holder, kind: r.kind, name_ja: r.name_ja, shares: r.shares, prior_shares: r.prior_shares,
  shares_change_pct: r.shares != null && r.prior_shares ? Math.round((r.shares / r.prior_shares - 1) * 1000) / 10 : null,
  book_value_jpy: r.book_value_jpy, prior_book_value_jpy: r.prior_book_value_jpy,
  held_back_as_stated: r.held_back == null ? null : r.held_back === 1,
  reciprocal_confirmed: r.reciprocal == null ? null : r.reciprocal === 1,
  purpose_ja: quote(r.purpose_ja),
});

/** Strategic shareholdings a company holds, who holds it, and mutual pairs. */
export async function crossHoldings(db: D1Database, ticker: string) {
  const t = ticker.toUpperCase();
  const [summary, holds, heldBy] = await Promise.all([
    db.prepare("SELECT record FROM cross_holding_summary WHERE ticker = ?1").bind(t).first<{ record: string }>(),
    db.prepare(
      "SELECT x.*, i.name_en FROM cross_holdings x LEFT JOIN issuers i ON i.ticker = x.held_ticker AND i.listed = 1 " +
        "WHERE x.holder_ticker = ?1 ORDER BY x.book_value_jpy IS NULL, x.book_value_jpy DESC",
    ).bind(t).all<any>(),
    db.prepare(
      "SELECT x.*, i.name_en, i.name_ja AS holder_name_ja FROM cross_holdings x LEFT JOIN issuers i ON i.ticker = x.holder_ticker AND i.listed = 1 " +
        "WHERE x.held_ticker = ?1 ORDER BY x.book_value_jpy IS NULL, x.book_value_jpy DESC",
    ).bind(t).all<any>(),
  ]);
  if (!summary && !heldBy.results.length) return null;
  return {
    ticker: t,
    summary: summary ? JSON.parse(summary.record) : null,
    holds: holds.results.map((r) => ({ ticker: r.held_ticker, name_en: r.name_en ?? null, ...xhRow(r) })),
    held_by: heldBy.results.map((r) => ({
      holder_ticker: r.holder_ticker, holder_name_en: r.name_en ?? null, holder_name_ja: r.holder_name_ja ?? null,
      fiscal_year_end: r.fiscal_year_end, source_doc_id: r.doc_id, ...xhRow(r),
    })),
    notes:
      "From each company's latest annual securities report (strategic shareholdings section). 'held_back_as_stated' is the holder's own statement; " +
      "'reciprocal_confirmed' means the investee's report also lists the holder. Shares can change with stock splits. Japanese names are matched to tickers by name.",
    attribution: ATTRIBUTION,
  };
}

/** Companies with the largest stated sales of listed strategic shares in their latest fiscal year. */
export async function crossHoldingUnwinding(db: D1Database, limit: number) {
  const { results } = await db
    .prepare(
      "SELECT s.record, i.name_en FROM cross_holding_summary s LEFT JOIN issuers i ON i.ticker = s.ticker AND i.listed = 1 " +
        "WHERE json_extract(s.record, '$.listed_sale_amount_jpy') > 0 AND json_extract(s.record, '$.data_warning') IS NULL " +
        "ORDER BY json_extract(s.record, '$.listed_sale_amount_jpy') DESC LIMIT ?1",
    )
    .bind(limit)
    .all<{ record: string; name_en: string | null }>();
  return { data: results.map((r) => ({ name_en: r.name_en, ...JSON.parse(r.record) })), attribution: ATTRIBUTION };
}

const COMMON = "(security_ja IS NULL OR (security_ja NOT LIKE '%予約権%' AND security_ja NOT LIKE '%社債%'))";

const TRADE_NOTES =
  "Rows of the 60-day trade table in each 5% report, as filed. Unit prices are usually stated only for off-market trades; " +
  "'common' marks shares and fund units (not warrants or bonds); 'kind' separates real trades from stock lending, custody transfers and option exercises. Net flow counts kind = 'trade' only. " +
  "Amendments are left out, and a row repeated by the holder's next report (the 60-day windows overlap) is kept once. Stated unit prices are not market prices and not stock prices. Individual holders are not named.";

/** Trades disclosed by 5% holders, with net flow and price points per holder. */
export async function holderTrades(db: D1Database, opts: { ticker?: string; filer?: string; since?: string; limit: number }) {
  if (await filerBlocked(db, opts.filer)) return { by_holder: [], data: [], notes: TRADE_NOTES, attribution: ATTRIBUTION };
  const where: string[] = [];
  const args: unknown[] = [];
  if (opts.ticker) where.push(`issuer_ticker = ?${args.push(opts.ticker.toUpperCase())}`);
  if (opts.filer) where.push(`filer_edinet_code = ?${args.push(opts.filer)}`);
  if (opts.since) where.push(`trade_date >= ?${args.push(opts.since)}`);
  const w = where.length ? "WHERE " + where.join(" AND ") : "";
  // Prices are compared only for shares and fund units, not warrants or convertible bonds.
  const priced = `kind = 'trade' AND unit_price_jpy IS NOT NULL AND ${COMMON}`;
  const [rows, summary] = await Promise.all([
    db.prepare(`SELECT *, ${COMMON} AS common, ${filerFlags("filer_edinet_code")} FROM holder_trades ${w} ORDER BY trade_date DESC, doc_id DESC, row_no LIMIT ?${args.length + 1}`).bind(...args, opts.limit).all<any>(),
    db
      .prepare(
        `SELECT filer_edinet_code, ${filerFlags("filer_edinet_code")}, holder, issuer_ticker, MIN(trade_date) first_date, MAX(trade_date) last_date, COUNT(*) rows,
           SUM(CASE WHEN kind = 'trade' AND side = 'acquisition' THEN shares ELSE 0 END) bought,
           SUM(CASE WHEN kind = 'trade' AND side = 'disposal' THEN shares ELSE 0 END) sold,
           SUM(CASE WHEN kind = 'trade' THEN (CASE side WHEN 'acquisition' THEN 1 ELSE -1 END) * pct_of_outstanding ELSE 0 END) net_pct,
           SUM(CASE WHEN ${priced} THEN unit_price_jpy * shares END) / SUM(CASE WHEN ${priced} THEN shares END) vwap,
           MIN(CASE WHEN ${priced} THEN unit_price_jpy END) low, MAX(CASE WHEN ${priced} THEN unit_price_jpy END) high
         FROM holder_trades ${w}
         GROUP BY filer_edinet_code, holder, issuer_ticker ORDER BY last_date DESC LIMIT 100`,
      )
      .bind(...args)
      .all<any>(),
  ]);
  // With a filer filter, a person's rows are dropped; with a company filter they stay, without the name.
  const shown = (r: any) => !opts.filer || !tradeHolderHidden(r, r.holder);
  return {
    by_holder: summary.results.filter(shown).map((s) => {
      const hide = tradeHolderHidden(s, s.holder);
      return {
        filer_edinet_code: hide ? null : s.filer_edinet_code, holder: hide ? INDIVIDUAL : publicHolderList(publicFilerName(s.holder)), ticker: s.issuer_ticker, first_trade_date: s.first_date, last_trade_date: s.last_date,
        shares_bought: s.bought, shares_sold: s.sold, net_shares: s.bought - s.sold, net_pct_of_outstanding: Math.round(s.net_pct * 100) / 100,
        disclosed_vwap_jpy: s.vwap == null ? null : Math.round(s.vwap * 100) / 100, disclosed_low_jpy: s.low, disclosed_high_jpy: s.high,
      };
    }),
    data: rows.results.filter(shown).map((r) => {
      const hide = tradeHolderHidden(r, r.holder);
      return {
        trade_date: r.trade_date, ticker: r.issuer_ticker, filer_edinet_code: hide ? null : r.filer_edinet_code, holder: hide ? INDIVIDUAL : publicHolderList(publicFilerName(r.holder)), security_ja: r.security_ja,
        shares: r.shares, pct_of_outstanding: r.pct_of_outstanding, venue: r.venue, side: r.side, unit_price_jpy: r.unit_price_jpy,
        kind: r.kind, common: r.common === 1, note_ja: hide ? null : scrubText(r.note_ja), source_doc_id: r.doc_id, filing_date: r.submit_date,
      };
    }),
    notes: TRADE_NOTES,
    attribution: ATTRIBUTION,
  };
}

const PERCENTILE_NOTE =
  "Percentile rank among listed companies. mean_percentile averages the available metric percentiles; it is not a rating or evaluation.";
const PU_NOTE = "Share of disclosure text discussing policy-related uncertainty, by policy area; proprietary text method. Descriptive, not a forecast.";
const WORDING_NOTE = "Balance of positive and negative results wording; proprietary text method. It counts wording only, does not interpret context and says nothing about results or prospects.";
const INDICATOR_NOTES =
  "financial_percentiles: each metric's value and its percentile rank among listed companies; mean_percentile is arithmetic only, not a rating or evaluation. " +
  "policy_uncertainty: share of disclosure text discussing policy-related uncertainty, by policy area (proprietary text method). results_wording_balance: balance of results wording, without interpreting context (proprietary text method). " +
  "ownership_activity: counts from filings, unweighted. Descriptive, not investment advice.";

/** The public view of a stored indicators record: outputs only, no formulas, inputs or method text. */
function publicIndicators(rec: any) {
  const fp = rec.financial_percentiles;
  if (fp) {
    const { percentile_formula: _pf, mean_formula: _mf, metrics, ...rest } = fp;
    rec.financial_percentiles = {
      ...rest,
      metrics: Object.fromEntries(Object.entries(metrics ?? {}).map(([k, m]: [string, any]) => { const { formula: _f, ...v } = m ?? {}; return [k, v]; })),
      note: PERCENTILE_NOTE,
    };
  }
  if (rec.policy_uncertainty) {
    const { sentences: _s, examples: _e, ...pu } = rec.policy_uncertainty;
    rec.policy_uncertainty = { ...pu, note: PU_NOTE };
  }
  if (rec.results_wording_balance) rec.results_wording_balance = { ...rec.results_wording_balance, note: WORDING_NOTE };
  return rec;
}

const PCT = (k: string) => `$.financial_percentiles.metrics.${k}`;
// The allow-list for sorted lists. Paths only ever come from here.
export const SORT_METRICS: Record<string, string> = {
  roe: `${PCT("roe")}.value`, roe_percentile: `${PCT("roe")}.percentile`,
  ordinary_margin: `${PCT("ordinary_margin")}.value`, ordinary_margin_percentile: `${PCT("ordinary_margin")}.percentile`,
  equity_ratio: `${PCT("equity_ratio")}.value`, equity_ratio_percentile: `${PCT("equity_ratio")}.percentile`,
  revenue_growth: `${PCT("revenue_growth")}.value`, revenue_growth_percentile: `${PCT("revenue_growth")}.percentile`,
  net_income_growth: `${PCT("net_income_growth")}.value`, net_income_growth_percentile: `${PCT("net_income_growth")}.percentile`,
  ocf_to_assets: `${PCT("ocf_to_assets")}.value`, ocf_to_assets_percentile: `${PCT("ocf_to_assets")}.percentile`,
  mean_percentile: "$.financial_percentiles.mean_percentile",
  policy_uncertainty: "$.policy_uncertainty.policy_uncertainty_per_100", policy_uncertainty_percentile: "$.policy_uncertainty.percentile",
  filers_stating_may_make_proposals_365d: "$.ownership_activity.filers_stating_may_make_proposals_365d",
  new_5pct_holders_180d: "$.ownership_activity.new_5pct_holders_180d",
  net_stake_change_pt_90d: "$.ownership_activity.net_stake_change_pt_90d",
  held_by_listed_companies_pct: "$.ownership_activity.held_by_listed_companies_pct",
};

export async function companyIndicators(db: D1Database, ticker: string) {
  const r = await db.prepare("SELECT record FROM company_indicators WHERE ticker = ?1").bind(ticker.toUpperCase()).first<{ record: string }>();
  if (!r) return null;
  const rec = publicIndicators(JSON.parse(r.record));
  return { ...rec, notes: INDICATOR_NOTES, attribution: ATTRIBUTION };
}

const BUYBACK_NOTES =
  "From monthly share-buyback reports on EDINET. A report replaced by a later amendment is left out. Each board resolution stays separate. progress and remaining figures and avg_price_jpy are arithmetic on the filed numbers, and computed_fields says how. Not investment advice.";

/** A company's buyback reports, newest period first, without those a later amendment replaced. */
export async function buybacks(db: D1Database, ticker: string, limit: number) {
  const { results } = await db.prepare(
    "SELECT r.record FROM buyback_reports r WHERE r.ticker = ?1 AND r.parse_status != 'failed' AND NOT EXISTS (SELECT 1 FROM buyback_reports n WHERE n.family_doc_id = r.family_doc_id AND (n.submit_datetime, n.doc_id) > (r.submit_datetime, r.doc_id)) ORDER BY r.period_end DESC, r.submit_datetime DESC LIMIT ?2",
  ).bind(ticker.toUpperCase(), limit).all<{ record: string }>();
  const excluded = await db.prepare(
    "SELECT COUNT(*) AS n FROM buyback_reports r WHERE r.ticker = ?1 AND r.parse_status = 'failed' AND NOT EXISTS (SELECT 1 FROM buyback_reports n WHERE n.family_doc_id = r.family_doc_id AND (n.submit_datetime, n.doc_id) > (r.submit_datetime, r.doc_id))",
  ).bind(ticker.toUpperCase()).first<{ n: number }>();
  return { ticker: ticker.toUpperCase(), data: results.map((r) => JSON.parse(r.record)), excluded_unparseable_reports: excluded?.n ?? 0, notes: BUYBACK_NOTES, attribution: ATTRIBUTION };
}

const GOVERNANCE_NOTES =
  "What companies state in internal control reports and in extraordinary reports on auditor changes, with short quotes in Japanese and the source filing. We do not judge or score these statements, and coverage of auditor changes may be incomplete. Not investment advice.";

/** Governance filings, for one company or across companies by type. */
export async function governanceEvents(db: D1Database, opts: { ticker?: string; type?: string; since?: string; limit: number; history?: boolean }) {
  const superseded = "EXISTS (SELECT 1 FROM governance_events n WHERE json_extract(n.record, '$.fields.amends_doc_id') = governance_events.doc_id)";
  const { results } = await db.prepare(
    `SELECT record, ${superseded} AS superseded FROM governance_events WHERE (?1 IS NULL OR ticker = ?1) AND (?2 IS NULL OR type = ?2) AND (?3 IS NULL OR submit_date >= ?3) ${opts.history ? "" : `AND NOT ${superseded}`} ORDER BY submit_date DESC LIMIT ?4`,
  ).bind(opts.ticker?.toUpperCase() ?? null, opts.type ?? null, opts.since ?? null, opts.limit).all<{ record: string }>();
  return { data: results.map((r: any) => { const g = JSON.parse(r.record); const pub = scrubFreeText(g.quote_ja ? { ...g, quote_ja: quote(g.quote_ja) } : g); return opts.history ? { ...pub, superseded: !!r.superseded } : pub; }), notes: GOVERNANCE_NOTES, attribution: ATTRIBUTION };
}

/** Disclosed 5% trades where the filer is a listed company that also lists the stock among its strategic holdings. */
export async function crossHoldingTrades(db: D1Database, opts: { ticker?: string; holder?: string; since?: string; limit: number }) {
  const { results } = await db.prepare(
    `SELECT ${filerFlags("t.filer_edinet_code")}, t.trade_date, t.submit_date, t.issuer_ticker, t.holder,`+"  t.shares, t.pct_of_outstanding, t.venue, t.side, t.unit_price_jpy, t.doc_id, i.ticker AS filer_ticker, " +
      "x.fiscal_year_end AS holding_fiscal_year_end, x.shares AS shares_in_annual_report, x.reciprocal " +
      "FROM holder_trades t JOIN issuers i ON i.edinet_code = t.filer_edinet_code JOIN cross_holdings x ON x.holder_ticker = i.ticker AND x.held_ticker = t.issuer_ticker " +
      "AND x.fiscal_year_end = (SELECT MAX(y.fiscal_year_end) FROM cross_holdings y WHERE y.holder_ticker = x.holder_ticker AND y.held_ticker = x.held_ticker) " +
      "WHERE t.kind = 'trade' AND (?1 IS NULL OR t.issuer_ticker = ?1) AND (?2 IS NULL OR i.ticker = ?2) AND (?3 IS NULL OR t.trade_date >= ?3) ORDER BY t.trade_date DESC LIMIT ?4",
  ).bind(opts.ticker?.toUpperCase() ?? null, opts.holder?.toUpperCase() ?? null, opts.since ?? null, opts.limit).all<any>();
  return {
    data: results.map(({ excluded, known_org, ...r }) => (tradeHolderHidden({ excluded, known_org }, r.holder) ? { ...r, holder: INDIVIDUAL } : { ...r, holder: publicHolderList(r.holder) })),
    notes: "Trades from the 60-day tables of 5% reports, joined to the filer's own list of strategic holdings. A filing group's trade can belong to a group member; check holder. Why a holder traded is not stated. Stated prices are not market prices. Not investment advice.",
    attribution: ATTRIBUTION,
  };
}

const RATING_NOTES =
  "Ratings as each issuer states them in bond filings on EDINET, with the source filing. A row says what the filing said on its date; agencies can change or withdraw a rating, and current ratings are on the agencies' own services. " +
  "Each row is the rating of one bond, not an issuer rating. Whether it is still current is not checked, because agencies' websites are not read. Each agency defines its own scale, so symbols are not comparable across agencies. " +
  "Issuers without a recent rated bond have no row. A rating is the agency's opinion. We do not rate anything, nothing here is investment advice, and this data is not meant for use in soliciting financial transactions.";
const ratingRow = (rec: any) => ({ ...rec, rating_type: "bond", source_document_type: "shelf_registration_supplement" });

/** Ratings a company stated in its bond filings, newest first, and the latest per agency. */
export async function disclosedRatings(db: D1Database, ticker: string, limit = 40) {
  const { results } = await db.prepare("SELECT record FROM disclosed_ratings WHERE ticker = ?1 ORDER BY submit_date DESC LIMIT ?2").bind(ticker.toUpperCase(), limit).all<{ record: string }>();
  const data = results.map((r) => ratingRow(JSON.parse(r.record)));
  const latest: Record<string, any> = {};
  for (const r of data) latest[r.agency] ??= { rating: r.rating, rating_type: r.rating_type, rating_date: r.rating_date, submit_date: r.submit_date, bond_ja: r.bond_ja, doc_id: r.doc_id };
  return { ticker: ticker.toUpperCase(), latest_stated: latest, latest_stated_note: "The latest statement on file per agency: one bond's rating as the issuer stated it on that date. Not ours, and it may have changed.", data, notes: RATING_NOTES, attribution: ATTRIBUTION };
}

/** All stated ratings across issuers, including unlisted ones, optionally by agency. */
export async function recentDisclosedRatings(db: D1Database, opts: { agency?: string; since?: string; limit: number }) {
  const { results } = await db.prepare("SELECT record FROM disclosed_ratings WHERE (?1 IS NULL OR agency = ?1) AND (?2 IS NULL OR submit_date >= ?2) ORDER BY submit_date DESC LIMIT ?3").bind(opts.agency ?? null, opts.since ?? null, opts.limit).all<{ record: string }>();
  return { data: results.map((r) => ratingRow(JSON.parse(r.record))), notes: RATING_NOTES, attribution: ATTRIBUTION };
}

const BRIEF_NOTES =
  "One company's own filings in one response, each part with its own date, so that it is clear what was known when. rate_lens is arithmetic on the company's reported debt, not a forecast. " +
  "No evaluation or forecast is made here, and nothing here is investment advice.";

/** Everything needed to prepare for one company in one response, all from its filings. */
export async function companyBrief(db: D1Database, ticker: string) {
  const t = ticker.toUpperCase();
  const [ind, rel, ev, ratings, bb, gov] = await Promise.all([
    companyIndicators(db, t), earningsReleases(db, t, 1),
    ownershipEvents(db, "business", { ticker: t, since: daysAgo(180), limit: 10 }), disclosedRatings(db, t, 6), buybacks(db, t, 1), governanceEvents(db, { ticker: t, limit: 4 }),
  ]);
  if (!ind) return null;
  return {
    ticker: t, name_en_filing: ind.name_en_filing, fiscal_year_end: ind.fiscal_year_end,
    latest_results: rel.data[0] ?? null,
    indicators: { financial_percentiles: ind.financial_percentiles, policy_uncertainty: ind.policy_uncertainty, results_wording_balance: ind.results_wording_balance, ownership_activity: ind.ownership_activity },
    ownership_events_180d: ev.data,
    rate_lens: ind.rate_lens ?? null,
    latest_buyback_report: bb.data[0] ?? null,
    governance_filings: gov.data,
    credit_ratings_stated: { latest_stated: ratings.latest_stated, notes: ratings.notes },
    notes: BRIEF_NOTES,
    attribution: ATTRIBUTION,
  };
}

const YEN_MN = (v: number) => (Math.abs(v) >= 1e6 ? `¥${(v / 1e6).toFixed(2)} trillion` : `¥${(v / 1e3).toLocaleString("en-US", { maximumFractionDigits: 1 })} billion`);
const SUMMARY_FIELDS: [string, string][] = [["revenue", "Revenue"], ["operating_income", "Operating income"], ["ordinary_income", "Ordinary income"], ["profit_before_tax", "Profit before tax"], ["net_income_parent", "Net income"], ["net_income", "Net income"]];

/** A plain English sentence built only from the record's own fields, so it cannot say more than the filing does. */
export function releaseSummaryEn(rec: any): string | null {
  const res = rec.results ?? {};
  const period = rec.period === "FY" ? "the full year" : rec.period === "H1" ? "the first half" : `${rec.period} (cumulative)`;
  const seen = new Set<string>();
  const parts: string[] = [];
  for (const [k, label] of SUMMARY_FIELDS) {
    const v = res[k + "_mn_jpy"];
    if (typeof v !== "number" || seen.has(label) || parts.length >= 3) continue;
    seen.add(label);
    const yoy = res[k + "_yoy_pct"];
    parts.push(`${label} ${YEN_MN(v)}${typeof yoy === "number" ? ` (${yoy > 0 ? "+" : ""}${yoy.toFixed(1)}% YoY)` : ""}`);
  }
  if (!parts.length) return null;
  let out = `For ${period} of the fiscal year to ${rec.fiscal_year_end_month}: ${parts.join(", ")}.`;
  return out;
}

/** Companies in the order of one metric, optionally within sectors. The caller picks the metric and direction; no judgement is added. */
export async function sortedByMetric(db: D1Database, metric: string, order: "asc" | "desc", limit: number, sectorsJa?: string[]) {
  const path = SORT_METRICS[metric];
  const args: unknown[] = [path];
  const sectorSql = sectorsJa?.length ? ` AND json_extract(c.record, '$.sector_ja') IN (${sectorsJa.map(() => `?${args.push(0)}`).join(",")})` : "";
  sectorsJa?.forEach((v, i) => { args[i + 1] = v; });
  const lim = args.push(limit);
  const { results } = await db
    .prepare(
      `SELECT c.ticker, i.name_en, i.name_ja, json_extract(c.record, '$.sector_ja') AS sector_ja, c.fiscal_year_end, c.doc_id AS source_doc_id, json_extract(c.record, ?1) AS value ` +
        `FROM company_indicators c LEFT JOIN issuers i ON i.ticker = c.ticker AND i.listed = 1 WHERE json_extract(c.record, ?1) IS NOT NULL${sectorSql} ORDER BY value ${order === "asc" ? "ASC" : "DESC"}, c.ticker LIMIT ?${lim}`,
    )
    .bind(...args)
    .all<any>();
  return { metric, order, data: results, notes: "A list in the order of one figure, as requested. The order is arithmetic and says nothing about which company is better. " + INDICATOR_NOTES, attribution: ATTRIBUTION };
}

export async function policyUncertaintyIndex(db: D1Database) {
  const { results } = await db.prepare("SELECT record FROM policy_uncertainty_monthly ORDER BY month").all<{ record: string }>();
  return {
    data: results.map((r) => {
      const { mean_per_100_sentences: mean, ...rest } = JSON.parse(r.record);
      return { ...rest, mean_policy_uncertainty: mean ?? null };
    }),
    notes:
      "Japan corporate policy-uncertainty index from annual securities reports, by filing month: the average share of disclosure text discussing policy-related uncertainty " +
      "(proprietary text method), and the share of reports discussing each policy area. Most Japanese companies file in June, so months with few reports are noisy; check 'reports'. Descriptive, not investment advice.",
    attribution: ATTRIBUTION,
  };
}

const RELEASE_NOTES =
  "Period results from half-year and quarterly securities reports on EDINET, with the source filing in url (edinet:<doc_id>). Amounts in millions of yen; *_yoy_pct is the change " +
  "against the same period a year earlier as filed. These reports arrive some weeks after the period ends and carry no full-year forecast.";
// Only rows read from EDINET filings are served.
const EDINET_ONLY = "url LIKE 'edinet:%'";

export async function earningsReleases(db: D1Database, ticker: string, limit = 8) {
  const { results } = await db
    .prepare(`SELECT record FROM earnings_releases WHERE ticker = ?1 AND ${EDINET_ONLY} ORDER BY release_date DESC LIMIT ?2`)
    .bind(ticker.toUpperCase(), limit)
    .all<{ record: string }>();
  return { ticker: ticker.toUpperCase(), data: results.map((r) => { const rec = JSON.parse(r.record); return { ...rec, summary_en: releaseSummaryEn(rec) }; }), notes: RELEASE_NOTES, attribution: ATTRIBUTION };
}

/** Latest period results across companies. */
export async function recentReleases(db: D1Database, opts: { since?: string; limit: number }) {
  const { results } = await db
    .prepare(
      "SELECT e.record, i.name_en FROM earnings_releases e LEFT JOIN issuers i ON i.ticker = e.ticker AND i.listed = 1 " +
        "WHERE e.url LIKE 'edinet:%' AND (?1 IS NULL OR e.release_date >= ?1) ORDER BY e.release_date DESC LIMIT ?2",
    )
    .bind(opts.since ?? null, opts.limit)
    .all<{ record: string; name_en: string | null }>();
  return { data: results.map((r) => { const rec = JSON.parse(r.record); return { name_en: r.name_en, ...rec, summary_en: releaseSummaryEn(rec) }; }), notes: RELEASE_NOTES, attribution: ATTRIBUTION };
}
