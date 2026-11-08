// Investor groups, company timelines, portfolios, group change feeds, co-holding overlap, and unwinding joined to trades.
// Tables come from japanfilings/investors.py. Natural persons are not in them: a filer in investor_excluded_filers is shown
// only as "an individual filer" here, with the source filing, and cannot be searched or ranked.
import type { Plan } from "./data";
import { INDIVIDUAL, filerFlags, filerHidden, publicEvent, publicFilerName } from "./privacy";

const ATTRIBUTION = "Source: EDINET (Financial Services Agency of Japan), SEC Form N-PORT; processed by japanfilings. Not investment advice.";
const GROUP_NOTES =
  "A group collects entities by shared EDINET code, identical name, or a reviewed list of corporate relationships; confidence and match_rule are given per member. " +
  "Membership does not mean members act together. Only filings that name joint holders state that, and those are listed separately. Uncertain names stay separate.";
const CUSTODIAN = "custodian_nominee";
const TENDER_TYPES: Record<string, string> = {
  "240": "tender_offer_registration", "250": "tender_offer_registration_amendment", "260": "tender_offer_withdrawal", "270": "tender_offer_report",
  "280": "tender_offer_report_amendment", "290": "target_opinion_report", "300": "target_opinion_report_amendment", "310": "reply_to_questions_report",
};
export const TIMELINE_TYPES = ["stake_change", "disclosed_trades", "buyback", "cross_holding_change", "tender_offer"] as const;

const daysAgo = (n: number) => new Date(Date.now() - n * 86_400_000).toISOString().slice(0, 10);
/** Same form as search_key in japanfilings/investors.py. */
export const searchKey = (q: string) => q.normalize("NFKC").replace(/[\s　・･,.．，、'’`"&/\\\-−‐–—=＝_:;!+*#]+/g, "").toUpperCase();

const groupView = (g: any) => ({ ...JSON.parse(g.record), n_members: g.n_members, n_companies: g.n_companies });

export async function searchGroups(db: D1Database, opts: { q?: string; kind?: string; limit: number }) {
  const key = searchKey(opts.q ?? "");
  if (opts.q && key.length < 2) return { error: "q must have at least 2 characters." };
  const where: string[] = [];
  const args: unknown[] = [];
  if (key) { where.push("instr(search_text, ?) > 0"); args.push(key); }
  if (opts.kind) { where.push("kind = ?"); args.push(opts.kind); }
  else if (!key) where.push(`kind <> '${CUSTODIAN}'`);
  const { results } = await db
    .prepare(`SELECT * FROM investor_groups ${where.length ? "WHERE " + where.join(" AND ") : ""} ORDER BY n_companies DESC, group_id LIMIT ?`)
    .bind(...args, opts.limit)
    .all<any>();
  return { data: results.map(groupView), notes: GROUP_NOTES + " Natural persons are not searchable or ranked. n_companies counts companies with a current 5% filing or a place among the latest top shareholders.", attribution: ATTRIBUTION };
}

export async function getGroup(db: D1Database, id: string) {
  const g = await db.prepare("SELECT * FROM investor_groups WHERE group_id = ?1").bind(id).first<any>();
  if (!g) return null;
  const { results } = await db.prepare("SELECT name, edinet_code, sources, match_rule, confidence FROM investor_group_members WHERE group_id = ?1 ORDER BY name").bind(id).all();
  return { ...groupView(g), members: results, notes: GROUP_NOTES, attribution: ATTRIBUTION };
}

/** What a group holds, and when entries and exits were filed. */
export async function portfolio(db: D1Database, id: string, opts: { status?: string; limit: number }) {
  const g = await db.prepare("SELECT record FROM investor_groups WHERE group_id = ?1").bind(id).first<{ record: string }>();
  if (!g) return null;
  const { results } = await db
    .prepare(
      "SELECT p.*, i.name_en AS issuer_name_en, i.name_ja AS issuer_name_ja FROM investor_positions p LEFT JOIN issuers i ON i.ticker = p.ticker AND i.listed = 1 " +
        "WHERE p.group_id = ?1 AND (?2 IS NULL OR p.status = ?2) ORDER BY p.last_date DESC, p.ticker LIMIT ?3",
    )
    .bind(id, opts.status ?? null, opts.limit)
    .all<any>();
  const moves: any[] = [];
  const positions = results.map(({ history, group_id: _g, holder_key: _k, ...p }) => {
    for (const h of JSON.parse(history ?? "[]")) moves.push({ ...h, ticker: p.ticker, issuer_name_en: p.issuer_name_en, filer: p.holder_name });
    return p;
  });
  moves.sort((a, b) => String(b.date).localeCompare(String(a.date)));
  const rec = JSON.parse(g.record);
  let strategic: unknown[] = [];
  if (rec.listed_tickers?.length) {
    const marks = rec.listed_tickers.map(() => "?").join(",");
    strategic = (await db
      .prepare(`SELECT holder_ticker, held_ticker, name_ja, shares, prior_shares, book_value_jpy, fiscal_year_end, doc_id FROM cross_holdings WHERE holder_ticker IN (${marks}) AND held_ticker IS NOT NULL ORDER BY book_value_jpy DESC LIMIT ?`)
      .bind(...rec.listed_tickers, opts.limit)
      .all()).results;
  }
  return {
    group_id: id, name_en: rec.name_en, name_ja: rec.name_ja, positions, entries_and_exits: moves, strategic_holdings_from_annual_reports: strategic,
    notes: "filing_5pct rows are per filer, as filed, and are not added up because filers can overlap. first_filing_on_record means the stake may be older than our earliest stored filing. " +
      "below_5pct means the latest filing is under 5%; later changes need no filing. top_shareholder rows are one annual-report snapshot, and a bank or broker can hold in its own name for clients.",
    attribution: ATTRIBUTION,
  };
}

/** The filer codes of a group, for filtering ownership events (webhooks can use this for a group filter). */
export async function filerCodesForGroup(db: D1Database, id: string) {
  const { results } = await db.prepare("SELECT DISTINCT edinet_code FROM investor_group_members WHERE group_id = ?1 AND edinet_code IS NOT NULL").bind(id).all<{ edinet_code: string }>();
  return results.map((r) => r.edinet_code);
}

/** Ownership events of a group's filers in first-seen order, with the same cursor as webhooks. Backfilled history carries its filing date. */
export async function groupChanges(db: D1Database, id: string, opts: { after_at?: string; after_id?: string; limit: number }) {
  const exists = await db.prepare("SELECT 1 FROM investor_groups WHERE group_id = ?1").bind(id).first();
  if (!exists) return null;
  const at = opts.after_at ?? "", eid = opts.after_id ?? "";
  const { results } = await db
    .prepare(
      `SELECT e.event_id, e.ingested_at, e.record, i.name_ja AS master_issuer_name_ja, i.name_en AS master_issuer_name_en, ${filerFlags("e.filer_edinet_code")} FROM ownership_events e LEFT JOIN issuers i ON i.ticker = e.issuer_ticker AND i.listed = 1 WHERE e.filer`+"_edinet_code IN (SELECT edinet_code FROM investor_group_members WHERE group_id = ?1 AND edinet_code IS NOT NULL) " +
        "AND (ingested_at > ?2 OR (ingested_at = ?2 AND event_id > ?3)) ORDER BY ingested_at, event_id LIMIT ?4",
    )
    .bind(id, at, eid, opts.limit)
    .all<any>();
  const last = results[results.length - 1];
  return {
    group_id: id,
    data: results.map((e) => ({ ...publicEvent(e.record, e), ingested_at: e.ingested_at })),
    next: last ? { after_at: last.ingested_at, after_id: last.event_id } : { after_at: at, after_id: eid },
    notes: "Each event is one filer's filing. Events of different members are separate filings. Not investment advice.",
    attribution: ATTRIBUTION,
  };
}

/** Other groups reporting stakes in the same companies. Sharing a stock says nothing about coordination. */
export async function overlap(db: D1Database, id: string, opts: { source?: string; limit: number }) {
  const exists = await db.prepare("SELECT 1 FROM investor_groups WHERE group_id = ?1").bind(id).first();
  if (!exists) return null;
  const source = opts.source === "all" ? null : "filing_5pct";
  const { results } = await db
    .prepare(
      "SELECT b.group_id, g.name_en, g.name_ja, g.kind, COUNT(DISTINCT a.ticker) AS shared_companies, GROUP_CONCAT(DISTINCT a.ticker) AS tickers " +
        "FROM investor_positions a JOIN investor_positions b ON b.ticker = a.ticker AND b.group_id <> a.group_id AND b.status <> 'below_5pct' AND (?2 IS NULL OR b.source = ?2) " +
        `JOIN investor_groups g ON g.group_id = b.group_id AND g.kind <> '${CUSTODIAN}' ` +
        "WHERE a.group_id = ?1 AND a.status <> 'below_5pct' AND (?2 IS NULL OR a.source = ?2) GROUP BY b.group_id ORDER BY shared_companies DESC, b.group_id LIMIT ?3",
    )
    .bind(id, source, opts.limit)
    .all<any>();
  const joint = await db
    .prepare("SELECT CASE WHEN group_a = ?1 THEN group_b ELSE group_a END AS group_id, ticker, latest_doc_id, latest_date, n_filings FROM investor_joint_filings WHERE group_a = ?1 OR group_b = ?1 ORDER BY latest_date DESC LIMIT 200")
    .bind(id)
    .all<any>();
  return {
    group_id: id,
    data: results.map((r) => ({ ...r, tickers: String(r.tickers ?? "").split(",").filter(Boolean).slice(0, 50) })),
    stated_joint_holders_in_filings: joint.results,
    notes: "Overlap only counts companies where both groups have a current disclosed stake. It does not mean the groups know of or coordinate with each other. Only stated_joint_holders_in_filings comes from filings that name joint holders, for that company and filing.",
    attribution: ATTRIBUTION,
  };
}

/** One merged, dated list of ownership-related disclosures for a company. */
export async function companyTimeline(db: D1Database, plan: Plan, ticker: string, opts: { since?: string; types?: string[]; limit: number }) {
  const t = ticker.toUpperCase();
  let since = opts.since ?? "";
  const want = (x: (typeof TIMELINE_TYPES)[number]) => !opts.types?.length || opts.types.includes(x);
  const n = opts.limit;
  const items: any[] = [];

  if (want("stake_change")) {
    const { results } = await db
      .prepare(`SELECT e.record, i.name_ja AS master_issuer_name_ja, i.name_en AS master_issuer_name_en, ${filerFlags("e.filer_edinet_code")} FROM ownership_events e LEFT JOIN issuers i ON i.ticker = e.issuer_ticker AND i.listed = 1 WHERE`+"  e.issuer_ticker = ?1 AND e.submit_date >= ?2 ORDER BY e.submit_date DESC, e.event_id LIMIT ?3")
      .bind(t, since, n)
      .all<any>();
    for (const r of results) {
      const e: any = publicEvent(r.record, r);
      items.push({
        type: "stake_change", subtype: e.type, date: e.submit_date, date_precision: "day", summary_en: e.summary_en,
        filer: e.filer_name_en ?? e.filer_name_ja, filer_edinet_code: e.filer_edinet_code,
        holding_ratio_pct: e.holding_ratio_pct, previous_ratio_pct: e.previous_ratio_pct, doc_id: e.doc_id, source_url: e.source_url,
      });
    }
  }
  // Disclosed trade rows are a paid dataset even when embedded in the merged timeline.
  if (want("disclosed_trades")) {
    const { results } = await db
      .prepare(
        "SELECT t.trade_date, t.filer_edinet_code, t.side, t.venue, SUM(t.shares) AS shares, SUM(t.pct_of_outstanding) AS pct, COUNT(*) AS n_rows, MAX(t.doc_id) AS doc_id, x.edinet_code AS person, " +
          "(SELECT COALESCE(g.name_en, g.name_ja) FROM investor_group_members m JOIN investor_groups g USING (group_id) WHERE m.edinet_code = t.filer_edinet_code LIMIT 1) AS filer_group " +
          "FROM holder_trades t LEFT JOIN investor_excluded_filers x ON x.edinet_code = t.filer_edinet_code WHERE t.issuer_ticker = ?1 AND t.kind = 'trade' AND t.trade_date >= ?2 " +
          "GROUP BY t.trade_date, t.filer_edinet_code, t.side, t.venue ORDER BY t.trade_date DESC LIMIT ?3",
      )
      .bind(t, since, n)
      .all<any>();
    for (const r of results)
      items.push({
        type: "disclosed_trades", subtype: r.side, venue: r.venue, date: r.trade_date, date_precision: "day", shares: r.shares, pct_of_outstanding: Math.round(r.pct * 100) / 100, rows: r.n_rows,
        filer: r.person ? INDIVIDUAL : r.filer_group, filer_edinet_code: r.person ? null : r.filer_edinet_code, doc_id: r.doc_id,
      });
  }
  if (want("buyback")) {
    const { results } = await db
      .prepare("SELECT r.record FROM buyback_reports r WHERE r.ticker = ?1 AND r.period_end >= ?2 AND NOT EXISTS (SELECT 1 FROM buyback_reports n WHERE n.family_doc_id = r.family_doc_id AND (n.submit_datetime, n.doc_id) > (r.submit_datetime, r.doc_id)) ORDER BY r.period_end DESC LIMIT ?3")
      .bind(t, since, n)
      .all<{ record: string }>();
    for (const r of results) {
      const b = JSON.parse(r.record);
      const sum = (k: string) => (b.resolutions ?? []).reduce((a: number, x: any) => a + (Number(x[k]) || 0), 0);
      items.push({ type: "buyback", subtype: b.doc_type, date: b.reporting_period_end, date_precision: "month_end", shares_acquired_in_period: sum("period_acquired_shares"), yen_spent_in_period: sum("period_acquired_yen"), doc_id: b.doc_id });
    }
  }
  if (want("cross_holding_change")) {
    const { results } = await db
      .prepare("SELECT holder_ticker, held_ticker, name_ja, shares, prior_shares, fiscal_year_end, doc_id FROM cross_holdings WHERE (holder_ticker = ?1 OR held_ticker = ?1) AND prior_shares IS NOT NULL AND (shares IS NULL OR shares <> prior_shares) AND fiscal_year_end >= ?2 ORDER BY fiscal_year_end DESC, book_value_jpy DESC LIMIT ?3")
      .bind(t, since, n)
      .all<any>();
    for (const r of results)
      items.push({
        type: "cross_holding_change", subtype: r.shares == null ? "no_longer_listed_in_report" : r.shares < r.prior_shares ? "shares_lower" : "shares_higher", role: r.holder_ticker === t ? "company_is_holder" : "company_is_held",
        date: r.fiscal_year_end, date_precision: "fiscal_year_end", holder_ticker: r.holder_ticker, held_ticker: r.held_ticker, held_name_ja: r.name_ja, shares: r.shares, prior_shares: r.prior_shares, doc_id: r.doc_id,
      });
  }
  if (want("tender_offer")) {
    const code = await db.prepare("SELECT edinet_code FROM issuers WHERE ticker = ?1 ORDER BY listed DESC LIMIT 1").bind(t).first<{ edinet_code: string }>();
    if (code) {
      const marks = Object.keys(TENDER_TYPES).map((c) => `'${c}'`).join(",");
      const { results } = await db
        .prepare(
          `SELECT f.doc_id, f.doc_type_code, f.submit_datetime, f.filer_name_ja, f.edinet_code, ${filerFlags("f.edinet_code")} FROM filings f ` +
            `WHERE f.doc_type_code IN (${marks}) AND (f.subject_edinet_code = ?1 OR f.edinet_code = ?1) AND substr(f.submit_datetime, 1, 10) >= ?2 AND f.withdrawn = 0 ORDER BY f.submit_datetime DESC LIMIT ?3`,
        )
        .bind(code.edinet_code, since, n)
        .all<any>();
      for (const r of results)
        items.push({ type: "tender_offer", subtype: TENDER_TYPES[r.doc_type_code], date: String(r.submit_datetime).slice(0, 10), date_precision: "day", filer: filerHidden(r, [r.filer_name_ja]) ? INDIVIDUAL : publicFilerName(r.filer_name_ja), doc_id: r.doc_id });
    }
  }
  items.sort((a, b) => String(b.date).localeCompare(String(a.date)) || a.type.localeCompare(b.type) || String(a.doc_id).localeCompare(String(b.doc_id)));
  return {
    ticker: t,
    data: items.slice(0, n),
    history_limited_to: null,
    not_covered: ["voting_results"],
    notes: `Disclosures in date order. The date_precision field says how exact each date is; annual-report changes carry the fiscal year end, not the trade date.${""} Individual filers are not named here; the source filing is cited. Not investment advice.`,
    attribution: ATTRIBUTION,
  };
}

/** Annual-report reductions in a company's strategic holdings next to the trades it disclosed in 5% reports during that fiscal year. */
export async function unwindingTrades(db: D1Database, ticker: string, limit: number) {
  const t = ticker.toUpperCase();
  const { results } = await db
    .prepare(
      "SELECT x.held_ticker, x.name_ja, x.shares, x.prior_shares, x.fiscal_year_end, x.doc_id, " +
        "COALESCE(SUM(CASE WHEN h.side = 'disposal' THEN h.shares END), 0) AS disclosed_disposal_shares, COALESCE(SUM(CASE WHEN h.side = 'acquisition' THEN h.shares END), 0) AS disclosed_acquisition_shares, " +
        "COUNT(h.doc_id) AS disclosed_trade_rows, MIN(h.trade_date) AS first_trade_date, MAX(h.trade_date) AS last_trade_date, " +
        "SUM(CASE WHEN h.side = 'disposal' AND h.unit_price_jpy IS NOT NULL THEN h.shares * h.unit_price_jpy END) / NULLIF(SUM(CASE WHEN h.side = 'disposal' AND h.unit_price_jpy IS NOT NULL THEN h.shares END), 0) AS avg_disclosed_disposal_price_jpy " +
        "FROM cross_holdings x LEFT JOIN holder_trades h ON h.issuer_ticker = x.held_ticker AND h.kind = 'trade' AND h.trade_date > date(x.fiscal_year_end, '-1 year') AND h.trade_date <= x.fiscal_year_end " +
        "AND h.filer_edinet_code IN (SELECT edinet_code FROM issuers WHERE ticker = x.holder_ticker) " +
        "WHERE x.holder_ticker = ?1 AND x.held_ticker IS NOT NULL AND x.prior_shares IS NOT NULL AND (x.shares IS NULL OR x.shares < x.prior_shares) " +
        "GROUP BY x.held_ticker, x.fiscal_year_end ORDER BY disclosed_trade_rows DESC, x.prior_shares - COALESCE(x.shares, 0) DESC LIMIT ?2",
    )
    .bind(t, limit)
    .all<any>();
  return {
    holder_ticker: t,
    data: results.map((r) => ({ ...r, reduction_shares_annual_report: r.prior_shares - (r.shares ?? 0) })),
    notes: "Trades appear only when the holder itself filed a 5% report on the stock, and each report lists 60 days, so most reductions have no matching trade and the totals need not agree. Share splits can change share counts. Reasons for a sale are not stated. Not investment advice.",
    attribution: ATTRIBUTION,
  };
}
