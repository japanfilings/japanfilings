// Stateless MCP over Streamable HTTP: each POST carries one JSON-RPC message, answered as JSON.
import type { AuthOk } from "./auth";
import * as data from "./data";
import * as ownership from "./ownership";
import { resolveSector } from "./screener";
import { DEV_TOOLS, callDevTool } from "./devapi";

const PROTOCOL_VERSION = "2025-06-18";

const INSTRUCTIONS =
  "Japanese listed-company data from official filings (EDINET, Financial Services Agency of Japan). " +
  "Names are in English where available; Japanese originals are in *_ja fields. Records carry a filing ID or a source link. Natural persons who file 5% reports are not named. " +
  "No stock prices: use yahoo_symbol or figi with your own price source. Factual data, not investment advice.";

const TOOLS = [
  {
    name: "search_companies",
    description: "Find listed Japanese companies by ticker (e.g. '7203', '648A') or English/Japanese name.",
    inputSchema: { type: "object", properties: { query: { type: "string" }, limit: { type: "integer" } }, required: ["query"] },
  },
  {
    name: "get_company",
    description: "Company profile by Tokyo ticker: English name, corporate number, industry, FIGI and common ticker symbols.",
    inputSchema: { type: "object", properties: { ticker: { type: "string" } }, required: ["ticker"] },
  },
  {
    name: "get_financials",
    description: "Five years of headline figures (revenue, net income, assets, equity, ROE, EPS, cash flows) and current/prior financial statements from the latest annual securities report.",
    inputSchema: { type: "object", properties: { ticker: { type: "string" } }, required: ["ticker"] },
  },
  {
    name: "get_company_details",
    description: "Officer titles (names are not published), workforce (employees, average salary, tenure), top shareholders, shareholder mix by category, auditor and audit fees.",
    inputSchema: { type: "object", properties: { ticker: { type: "string" } }, required: ["ticker"] },
  },
  {
    name: "get_company_segments",
    description: "Segments stated in the latest annual securities report, with the source filing.",
    inputSchema: { type: "object", properties: { ticker: { type: "string" } }, required: ["ticker"] },
  },
  {
    name: "get_voting_results",
    description: "Shareholder-meeting voting results. Give a ticker for one company; cross-company lists require Pro.",
    inputSchema: { type: "object", properties: { ticker: { type: "string" }, since: { type: "string" }, limit: { type: "integer" } } },
  },
  {
    name: "get_tender_offers",
    description: "Tender-offer notifications, results and target opinions. Give a ticker for one company; cross-company lists require Pro.",
    inputSchema: { type: "object", properties: { ticker: { type: "string" }, since: { type: "string" }, limit: { type: "integer" } } },
  },
  {
    name: "get_investor",
    description: "An investor's 5% filings by EDINET filer code: latest stake in each company and joint holders.",
    inputSchema: { type: "object", properties: { filer_edinet_code: { type: "string" } }, required: ["filer_edinet_code"] },
  },
  {
    name: "get_us_fund_holdings",
    description: "U.S. mutual funds and ETFs holding a Tokyo-listed stock (shares, USD value, % of fund) from the latest SEC N-PORT quarter.",
    inputSchema: { type: "object", properties: { ticker: { type: "string" } }, required: ["ticker"] },
  },
  {
    name: "get_large_holdings",
    description:
      "Large-shareholding (5% rule) filings: holder, % held, change vs. previous report, stated purpose (may make proposals, pure investment, ...). " +
      "Filter by target ticker, by filer EDINET code (investor reverse lookup), and by submit date (YYYY-MM-DD).",
    inputSchema: {
      type: "object",
      properties: { ticker: { type: "string" }, filer_edinet_code: { type: "string" }, since: { type: "string" }, limit: { type: "integer" } },
    },
  },
  {
    name: "get_ownership_events",
    description:
      "Ownership-change events from 5% filings, each with a one-line English summary: new_stake, stake_increase, stake_decrease, exit_below_5pct, " +
      "threshold_crossed, stated_may_make_proposals (filer now says it may make proposals to management), purpose_changed, amendment. " +
      "Filter by ticker, filer EDINET code, types and submit date.",
    inputSchema: {
      type: "object",
      properties: {
        ticker: { type: "string" }, filer_edinet_code: { type: "string" },
        types: { type: "array", items: { type: "string", enum: [...data.EVENT_TYPES] } },
        since: { type: "string" }, limit: { type: "integer" },
      },
    },
  },
  {
    name: "get_cross_holdings",
    description:
      "Cross-shareholdings (strategic stakes) from annual reports: what the company holds (shares, book value, change vs. prior year, purpose, " +
      "whether held back), which listed companies hold it, confirmed mutual pairs, and how much it sold in the year.",
    inputSchema: { type: "object", properties: { ticker: { type: "string" } }, required: ["ticker"] },
  },
  {
    name: "get_disclosed_trades",
    description:
      "Trades that 5% holders listed in their reports (last 60 days of each report): date, shares, on/off market, buy/sell and unit price, " +
      "plus net shares and average disclosed price per holder. Give a ticker or a filer EDINET code.",
    inputSchema: { type: "object", properties: { ticker: { type: "string" }, filer: { type: "string" }, since: { type: "string", description: "YYYY-MM-DD" }, limit: { type: "integer" } } },
  },
  {
    name: "get_indicators",
    description:
      "Figures for one company computed from its filings: per-metric financial percentile ranks among listed companies (not a rating), the share of annual-report text discussing policy-related uncertainty " +
      "(by policy area; proprietary text method), results wording balance, and ownership activity counts (filers stating they may make proposals, new 5% holders, net stake change, share held by listed companies).",
    inputSchema: { type: "object", properties: { ticker: { type: "string" } }, required: ["ticker"] },
  },
  {
    name: "sort_companies_by_metric",
    description: "List listed companies in the order of one figure or percentile, optionally within sectors. The order is arithmetic, not a judgement.",
    inputSchema: { type: "object", properties: { metric: { type: "string", enum: Object.keys(data.SORT_METRICS) }, order: { type: "string", enum: ["desc", "asc"] }, sector: { type: "string", description: "Sector slugs, comma-separated" }, limit: { type: "integer" } }, required: ["metric"] },
  },
  {
    name: "get_earnings_releases",
    description:
      "Period results from half-year and quarterly securities reports on EDINET, with the source filing: revenue and profits for the period with year-on-year % as filed. " +
      "Give a ticker, or omit it for the latest across companies.",
    inputSchema: { type: "object", properties: { ticker: { type: "string" }, since: { type: "string" }, limit: { type: "integer" } } },
  },
  {
    name: "get_company_brief",
    description:
      "One company on one page, from its filings: latest period results, indicator figures, ownership changes in the last 180 days and a mechanical 50 bp interest example on its own reported debt, each dated.",
    inputSchema: { type: "object", properties: { ticker: { type: "string" } }, required: ["ticker"] },
  },
  {
    name: "get_disclosed_credit_ratings",
    description: "Credit ratings as a company states them in its bond filings on EDINET: agency, rating, date, bond and source filing. A rating is the agency's opinion on that date, not necessarily its current rating.",
    inputSchema: { type: "object", properties: { ticker: { type: "string" } }, required: ["ticker"] },
  },
  {
    name: "get_share_buybacks",
    description: "A company's monthly share-buyback reports: limits per board resolution, shares and yen acquired, and progress as arithmetic on filed numbers.",
    inputSchema: { type: "object", properties: { ticker: { type: "string" }, limit: { type: "integer" } }, required: ["ticker"] },
  },
  {
    name: "get_governance_filings",
    description: "Internal-control conclusions and auditor changes as a company states them, with short Japanese quotes and the source filing. Omit ticker for a cross-company list by type.",
    inputSchema: { type: "object", properties: { ticker: { type: "string" }, type: { type: "string", enum: ["internal_control", "auditor_change"] }, since: { type: "string" }, limit: { type: "integer" } } },
  },
  {
    name: "get_cross_holding_trades",
    description: "Disclosed trades and prices where a listed 5% filer also lists the stock among its strategic holdings.",
    inputSchema: { type: "object", properties: { ticker: { type: "string" }, holder: { type: "string" }, since: { type: "string" }, limit: { type: "integer" } } },
  },
  {
    name: "get_policy_uncertainty_index",
    description: "Japan corporate policy-uncertainty index by month from annual reports, with the share of reports mentioning each policy area.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "get_cross_holding_unwinding",
    description: "Companies with the largest stated sales of listed strategic shares in their latest fiscal year.",
    inputSchema: { type: "object", properties: { limit: { type: "integer" } } },
  },
  {
    name: "search_investor_groups",
    description: "Find consolidated investor groups by English or Japanese name, with members and confidence. Natural persons are not searchable. A group does not imply its members act together.",
    inputSchema: { type: "object", properties: { query: { type: "string" }, limit: { type: "integer" } } },
  },
  {
    name: "get_investor_group_portfolio",
    description: "What an investor group's filers report holding across companies, with dated entries and exits.",
    inputSchema: { type: "object", properties: { group_id: { type: "string" }, limit: { type: "integer" } }, required: ["group_id"] },
  },
  {
    name: "get_investor_group_changes",
    description: "Ownership events of an investor group's filers, in first-seen order, cursor-paged.",
    inputSchema: { type: "object", properties: { group_id: { type: "string" }, after_at: { type: "string" }, after_id: { type: "string" }, limit: { type: "integer" } }, required: ["group_id"] },
  },
  {
    name: "get_investor_group_overlap",
    description: "Groups with disclosed stakes in the same companies, and joint holders only as filings state them. Overlap does not imply coordination.",
    inputSchema: { type: "object", properties: { group_id: { type: "string" }, limit: { type: "integer" } }, required: ["group_id"] },
  },
  {
    name: "get_company_ownership_timeline",
    description: "Merged dated list for one company: stake changes, buybacks, cross-holding changes and tender-offer filings; Pro also includes disclosed trades.",
    inputSchema: { type: "object", properties: { ticker: { type: "string" }, since: { type: "string" }, limit: { type: "integer" } }, required: ["ticker"] },
  },
  {
    name: "get_cross_holding_unwinding_trades",
    description: "A company's strategic-holding reductions from its annual report next to the trades it disclosed in 5% reports that fiscal year.",
    inputSchema: { type: "object", properties: { ticker: { type: "string" }, limit: { type: "integer" } }, required: ["ticker"] },
  },
  ...DEV_TOOLS,
];

type Rpc = { jsonrpc: "2.0"; id?: string | number | null; method: string; params?: any };

const ok = (id: Rpc["id"], result: unknown) => ({ jsonrpc: "2.0", id, result });
const err = (id: Rpc["id"], code: number, message: string) => ({ jsonrpc: "2.0", id, error: { code, message } });
const text = (value: unknown, isError = false) => ({ content: [{ type: "text", text: JSON.stringify(value, null, 2) }], isError });

async function callTool(db: D1Database, plan: data.Plan, name: string, a: any) {
  const limit = Math.min(Math.max(Number(a?.limit) || 20, 1), 200);
  switch (name) {
    case "search_companies":
      return text(await data.searchCompanies(db, String(a?.query ?? ""), limit));
    case "get_company": {
      const row = await data.getCompany(db, String(a?.ticker ?? ""));
      return row ? text(row) : text({ error: `No listed company with ticker ${a?.ticker}` }, true);
    }
    case "get_financials":
    case "get_company_details": {
      const fn = name === "get_financials" ? data.financials : data.companyDetails;
      const row = await fn(db, String(a?.ticker ?? ""));
      return row ? text(row) : text({ error: `No annual report for ticker ${a?.ticker}` }, true);
    }
    case "get_company_segments": {
      const row = await data.filingDetail(db, String(a?.ticker ?? ""), "segments");
      return row ? text(row) : text({ error: `No segment data for ticker ${a?.ticker}` }, true);
    }
    case "get_voting_results":
      return text(await data.votingResults(db, { ticker: a?.ticker, since: a?.since, limit }));
    case "get_tender_offers":
      return text(await data.tenderOffers(db, { ticker: a?.ticker, since: a?.since, limit }));
    case "get_investor": {
      const inv = await data.investor(db, String(a?.filer_edinet_code ?? ""), 200);
      return inv ? text(inv) : text({ error: "No 5% filings for that filer." }, true);
    }
    case "get_us_fund_holdings": {
      const row = await data.usFunds(db, String(a?.ticker ?? ""));
      return row ? text(row) : text({ error: `No U.S. fund holdings for ${a?.ticker}` }, true);
    }
    case "get_large_holdings": {
      const res = await data.largeHoldings(db, plan, { ticker: a?.ticker, filer: a?.filer_edinet_code, since: a?.since, limit });
      return "error" in res ? text({ error: res.error }, true) : text(res);
    }
    case "get_ownership_events":
      return text(await data.ownershipEvents(db, plan, {
        ticker: a?.ticker, filer: a?.filer_edinet_code, types: Array.isArray(a?.types) ? a.types : undefined, since: a?.since, limit,
      }));
    case "get_cross_holdings": {
      const row = await data.crossHoldings(db, String(a?.ticker ?? ""));
      return row ? text(row) : text({ error: `No cross-shareholding data for ${a?.ticker}` }, true);
    }
    case "get_disclosed_trades":
      if (!a?.ticker && !a?.filer) return text({ error: "Give ticker or filer." }, true);
      return text(await data.holderTrades(db, { ticker: a.ticker && String(a.ticker), filer: a.filer && String(a.filer), since: a.since && String(a.since), limit }));
    case "get_indicators": {
      const row = await data.companyIndicators(db, String(a?.ticker ?? ""));
      return row ? text(row) : text({ error: `No annual report for ticker ${a?.ticker}` }, true);
    }
    case "sort_companies_by_metric": {
      const metric = String(a?.metric ?? "");
      if (!Object.prototype.hasOwnProperty.call(data.SORT_METRICS, metric)) return text({ error: `metric must be one of ${Object.keys(data.SORT_METRICS).join(", ")}.` }, true);
      const names = a?.sector ? String(a.sector).split(",").map((s) => resolveSector(s)) : undefined;
      if (names?.some((n) => !n)) return text({ error: "Unknown sector. Use list_sectors." }, true);
      return text(await data.sortedByMetric(db, metric, a?.order === "asc" ? "asc" : "desc", limit, names?.map((n) => n!.sector_ja)));
    }
    case "get_earnings_releases":
      if (a?.ticker) return text(await data.earningsReleases(db, String(a.ticker), limit));
      return text(await data.recentReleases(db, { since: a?.since && String(a.since), limit }));
    case "get_company_brief": {
      const row = await data.companyBrief(db, String(a?.ticker ?? ""));
      return row ? text(row) : text({ error: "No annual report for this ticker." }, true);
    }
    case "get_disclosed_credit_ratings":
      return text(await data.disclosedRatings(db, String(a?.ticker ?? ""), 40));
    case "get_share_buybacks":
      return text(await data.buybacks(db, String(a?.ticker ?? ""), limit));
    case "get_governance_filings":
      return text(await data.governanceEvents(db, { ticker: a?.ticker as string | undefined, type: a?.type as string | undefined, since: a?.since as string | undefined, limit }));
    case "get_cross_holding_trades":
      return text(await data.crossHoldingTrades(db, { ticker: a?.ticker as string | undefined, holder: a?.holder as string | undefined, since: a?.since as string | undefined, limit }));
    case "get_policy_uncertainty_index":
      return text(await data.policyUncertaintyIndex(db));
    case "get_cross_holding_unwinding":
      return text(await data.crossHoldingUnwinding(db, limit));
    case "search_investor_groups": {
      const res = await ownership.searchGroups(db, { q: a?.query, limit });
      return "error" in res ? text(res, true) : text(res);
    }
    case "get_company_ownership_timeline":
      return text(await ownership.companyTimeline(db, plan, String(a?.ticker ?? ""), { since: a?.since, limit }));
    case "get_cross_holding_unwinding_trades":
      return text(await ownership.unwindingTrades(db, String(a?.ticker ?? ""), limit));
    case "get_investor_group_portfolio":
    case "get_investor_group_changes":
    case "get_investor_group_overlap": {
      const id = String(a?.group_id ?? "");
      const res = name === "get_investor_group_portfolio" ? await ownership.portfolio(db, id, { limit })
        : name === "get_investor_group_changes" ? await ownership.groupChanges(db, id, { after_at: a?.after_at, after_id: a?.after_id, limit })
        : await ownership.overlap(db, id, { limit });
      return res ? text(res) : text({ error: "No such investor group." }, true);
    }
    default:
      return null;
  }
}

/** Returns the JSON-RPC response, or null for notifications (no response body). */
export async function handleMcp(db: D1Database, plan: data.Plan, msg: Rpc, auth?: AuthOk) {
  if (msg.id === undefined) return null;
  switch (msg.method) {
    case "initialize":
      return ok(msg.id, {
        protocolVersion: PROTOCOL_VERSION,
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: "japanfilings", version: "0.4.0" },
        instructions: INSTRUCTIONS,
      });
    case "ping":
      return ok(msg.id, {});
    case "tools/list":
      return ok(msg.id, { tools: TOOLS });
    case "tools/call": {
      const res = (await callTool(db, plan, msg.params?.name, msg.params?.arguments)) ?? (await callDevTool(db, plan, auth, msg.params?.name, msg.params?.arguments));
      return res ? ok(msg.id, res) : err(msg.id, -32602, `Unknown tool: ${msg.params?.name}`);
    }
    default:
      return err(msg.id, -32601, `Method not found: ${msg.method}`);
  }
}
