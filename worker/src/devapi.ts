// Developer endpoints: batch lookups, usage, company and sector lists, screener, peers, benchmarks,
// llms.txt, and the MCP tools for the same features.
import { Hono } from "hono";
import { QUOTA, RPM, chargeExtra, type AuthOk } from "./auth";
import * as data from "./data";
import * as sc from "./screener";

const ATTRIBUTION = sc.ATTRIBUTION;
export const MAX_BATCH = 50;

type DevEnv = { Bindings: { DB: D1Database }; Variables: { plan: data.Plan; auth: AuthOk } };

const TICKER = /^[0-9A-Z]{1,8}$/;

export function parseTickers(raw: string | undefined): { tickers: string[] } | { error: string } {
  const tickers = [...new Set((raw ?? "").split(",").map((s) => s.trim().toUpperCase()).filter(Boolean))];
  if (!tickers.length) return { error: "tickers is required: a comma-separated list." };
  if (tickers.length > MAX_BATCH) return { error: `At most ${MAX_BATCH} tickers per call.` };
  if (!tickers.every((t) => TICKER.test(t))) return { error: "Tickers are letters and digits only." };
  return { tickers };
}

// ---------- Queries ----------

const marks = (n: number, from = 1) => Array.from({ length: n }, (_, i) => `?${i + from}`).join(",");

export async function batchCompanies(db: D1Database, tickers: string[]) {
  const { results } = await db.prepare(
    `SELECT i.ticker, i.edinet_code, i.corporate_number, i.name_en, i.name_ja, i.industry_ja, i.fiscal_year_end, i.listed, x.figi, x.composite_figi, x.share_class_figi
     FROM issuers i LEFT JOIN identifiers x ON x.ticker = i.ticker WHERE i.ticker IN (${marks(tickers.length)})`,
  ).bind(...tickers).all<Record<string, any>>();
  const by = new Map(results.map((r) => [r.ticker, r]));
  return {
    data: tickers.filter((t) => by.has(t)).map((t) => ({ ...by.get(t), ...data.englishFields(by.get(t)!), yahoo_symbol: `${t}.T`, bloomberg_ticker: `${t} JP` })),
    not_found: tickers.filter((t) => !by.has(t)),
    attribution: ATTRIBUTION,
  };
}

/** Five-year metrics per ticker. Statements are large, so they come only when asked for. */
export async function batchFinancials(db: D1Database, tickers: string[], statements: boolean) {
  const extra = statements ? ", json_extract(record, '$.statements') AS statements" : "";
  const { results } = await db.prepare(
    `SELECT ticker, doc_id, fiscal_year_end, submit_date, json_extract(record, '$.accounting_standard') AS accounting_standard, json_extract(record, '$.key_metrics') AS key_metrics${extra}
     FROM annual_reports WHERE ticker IN (${marks(tickers.length)}) ORDER BY ticker, fiscal_year_end DESC, submit_date DESC`,
  ).bind(...tickers).all<Record<string, any>>();
  const latest = new Map<string, Record<string, any>>();
  for (const r of results) if (!latest.has(r.ticker)) latest.set(r.ticker, r);
  return {
    data: tickers.filter((t) => latest.has(t)).map((t) => {
      const r = latest.get(t)!;
      return {
        ticker: t, accounting_standard: r.accounting_standard, fiscal_year_end: r.fiscal_year_end, source_doc_id: r.doc_id,
        key_metrics: r.key_metrics ? JSON.parse(r.key_metrics) : [], ...(statements && r.statements ? { statements: JSON.parse(r.statements) } : {}),
      };
    }),
    not_found: tickers.filter((t) => !latest.has(t)),
    attribution: ATTRIBUTION,
  };
}

/** Every listed company, in ticker order. Keyset paging: each page reads about limit rows however deep. */
export async function listCompanies(db: D1Database, cursor: string | undefined, requested: number) {
  const limit = Math.min(Math.max(requested || 100, 1), 1000);
  const after = (cursor ?? "").toUpperCase();
  if (after && !TICKER.test(after)) return { error: "Invalid cursor." };
  const { results } = await db.prepare(
    "SELECT i.ticker, i.edinet_code, i.name_en, i.name_ja, i.industry_ja, i.fiscal_year_end, i.corporate_number, x.figi " +
      "FROM issuers i LEFT JOIN identifiers x ON x.ticker = i.ticker WHERE i.ticker > ?1 ORDER BY i.ticker LIMIT ?2",
  ).bind(after, limit + 1).all<Record<string, any>>();
  const page = results.slice(0, limit);
  return {
    data: page.map((r) => ({ ...r, ...data.englishFields(r) })),
    next_cursor: results.length > limit ? page[page.length - 1].ticker : null,
    attribution: ATTRIBUTION,
  };
}

export async function usageReport(db: D1Database, auth: AuthOk) {
  const { results } = await db.prepare("SELECT month, requests FROM usage WHERE key_hash = ?1 ORDER BY month DESC LIMIT 12").bind(auth.keyHash).all<{ month: string; requests: number }>();
  const now = new Date();
  return {
    plan: auth.plan,
    month: now.toISOString().slice(0, 7),
    requests_used: auth.used,
    requests_limit: QUOTA[auth.plan],
    requests_remaining: Math.max(QUOTA[auth.plan] - auth.used, 0),
    resets_at: new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1)).toISOString(),
    per_minute_limit: RPM[auth.plan],
    per_minute_used: auth.minuteUsed,
    data: results,
  };
}

// ---------- Routes (mounted under /v1, after the key check) ----------

export const devRoutes = new Hono<DevEnv>();

// A batch of 50 is 50 lookups, so it counts as 50 toward the monthly quota.
async function charge(c: any, n: number): Promise<Response | null> {
  const auth: AuthOk = c.get("auth");
  const used = await chargeExtra(c.env.DB, auth.keyHash, n - 1);
  if (used > auth.quota) return c.json({ error: `Monthly quota of ${auth.quota} requests exceeded.` }, 429);
  c.header("X-Quota-Used", String(used));
  c.header("RateLimit-Remaining", String(Math.max(auth.quota - used, 0)));
  return null;
}

devRoutes.get("/batch/companies", async (c) => {
  const p = parseTickers(c.req.query("tickers"));
  if ("error" in p) return c.json({ error: p.error }, 400);
  return (await charge(c, p.tickers.length)) ?? c.json(await batchCompanies(c.env.DB, p.tickers));
});

devRoutes.get("/batch/financials", async (c) => {
  const p = parseTickers(c.req.query("tickers"));
  if ("error" in p) return c.json({ error: p.error }, 400);
  return (await charge(c, p.tickers.length)) ?? c.json(await batchFinancials(c.env.DB, p.tickers, c.req.query("statements") === "true"));
});

devRoutes.get("/usage", async (c) => c.json(await usageReport(c.env.DB, c.get("auth"))));

devRoutes.get("/companies", async (c) => {
  const res = await listCompanies(c.env.DB, c.req.query("cursor"), Number(c.req.query("limit")));
  return "error" in res ? c.json({ error: res.error }, 400) : c.json(res);
});

devRoutes.get("/sectors", async (c) => c.json(await sc.listSectors(c.env.DB)));

devRoutes.get("/sectors/:sector/benchmarks", async (c) => {
  const row = await sc.sectorBenchmarks(c.env.DB, decodeURIComponent(c.req.param("sector")));
  return row ? c.json(row) : c.json({ error: "Unknown sector, or no data yet. See /v1/sectors." }, 404);
});

devRoutes.get("/companies/:ticker/peers", async (c) => {
  const row = await sc.peers(c.env.DB, c.req.param("ticker"), c.get("plan"), Number(c.req.query("limit")));
  return row ? c.json(row) : c.json({ error: "No annual-report figures for that ticker." }, 404);
});

devRoutes.get("/screener", async (c) => {
  const q = sc.buildScreener(c.req.query());
  if ("error" in q) return c.json({ error: q.error }, 400);
  return c.json(await sc.cachedScreener(c.env.DB, q));
});

// ---------- OpenAPI additions ----------

const q = (name: string, required = false) => ({ name, in: "query", required });
const p = (name: string) => ({ name, in: "path", required: true });
const FILTERS = Object.keys(sc.NUMERIC_FIELDS).flatMap((f) => [q(`min_${f}`), q(`max_${f}`)]);

export const DEV_PATHS = {
  "/v1/batch/companies": { get: { summary: `Company profiles for up to ${MAX_BATCH} tickers. Counts one request per ticker`, parameters: [q("tickers", true)] } },
  "/v1/batch/financials": { get: { summary: `Five-year key metrics for up to ${MAX_BATCH} tickers; statements=true adds the statements. Counts one request per ticker`, parameters: [q("tickers", true), q("statements")] } },
  "/v1/usage": { get: { summary: "Your plan, requests used this month, remaining, reset time and per-minute limit" } },
  "/v1/companies": { get: { summary: "All listed companies in ticker order, paged with next_cursor", parameters: [q("cursor"), q("limit")] } },
  "/v1/sectors": { get: { summary: "Sectors with slug, English and Japanese names and company counts" } },
  "/v1/sectors/{sector}/benchmarks": { get: { summary: "Median and quartiles of filing figures across a sector's companies", parameters: [p("sector")] } },
  "/v1/companies/{ticker}/peers": { get: { summary: "Companies in the same sector with the nearest revenue", parameters: [p("ticker"), q("limit")] } },
  "/v1/screener": { get: { summary: "Filter companies on figures from filings: sector, revenue, margins, ROE, equity ratio, growth, cross-held share, percentiles, policy uncertainty, and a bond rating as stated in filings (the agency's opinion on that date, not ours). No stock prices", parameters: [q("sector"), ...FILTERS, q("has_stated_rating"), q("min_stated_rating"), q("sort"), q("order"), q("limit"), q("cursor")] } },
};

// ---------- llms.txt ----------

// Our indicator methods stay on our own documentation, so generated text describes them by name only.
const INDICATOR_SUMMARIES: Record<string, string> = {
  "/v1/companies/{ticker}/indicators": "Financial percentiles, policy-uncertainty counts, results wording balance and ownership activity for one company",
  "/v1/indicators/policy-uncertainty": "Japan corporate policy-uncertainty index by filing month",
  "/v1/indicators/sorted": "Companies in the order of one metric, optionally by sector",
};

type Spec = { info: { title: string; description: string; version: string }; paths: Record<string, Record<string, any>> };
const summaryOf = (path: string, op: any): string => INDICATOR_SUMMARIES[path] ?? op.summary ?? "";

const INTRO = (origin: string) =>
  `Japan ownership and activism data from public filings on EDINET, in English, as a REST API and an MCP server: 5% stakes and stated proposal intentions, tender offers, shareholder votes, disclosed trades, cross-shareholdings, plus company financials. Factual data, not investment advice. No stock prices: join your own with the FIGI or ticker symbols on each company. Two paid plans with every feature: Starter $9 a month (20,000 requests) and Pro $39 (300,000); a key without a plan gets HTTP 402.\n\n` +
  `- Base URL: ${origin}\n- Authentication: Authorization: Bearer <key>; keys at ${origin}/login\n- MCP: ${origin}/mcp (Streamable HTTP)\n- OpenAPI: ${origin}/openapi.json\n- Documentation: ${origin}/docs\n` +
  `- List endpoints accept format=csv. Data responses carry X-Data-As-Of.\n- Source credit is required when redistributing: Source: EDINET (Financial Services Agency of Japan); processed by JapanStake.\n`;

export function llmsTxt(spec: Spec, origin: string) {
  const lines = Object.entries(spec.paths).flatMap(([path, ops]) =>
    Object.entries(ops).map(([method, op]) => `- ${method.toUpperCase()} ${path}: ${summaryOf(path, op)}`));
  return `# ${spec.info.title}\n\n> ${spec.info.description}\n\n${INTRO(origin)}\n## Endpoints\n\n${lines.join("\n")}\n\n## More\n\n- [Full endpoint list with parameters](${origin}/llms-full.txt)\n`;
}

export function llmsFullTxt(spec: Spec, origin: string) {
  const sections = Object.entries(spec.paths).flatMap(([path, ops]) =>
    Object.entries(ops).map(([method, op]) => {
      const params = (op.parameters ?? []) as { name: string; in: string; required: boolean }[];
      const list = params.length ? params.map((x) => `- ${x.name} (${x.in}${x.required ? ", required" : ""})`).join("\n") : "- none";
      return `### ${method.toUpperCase()} ${path}\n\n${summaryOf(path, op)}\n\nParameters:\n${list}`;
    }));
  const fields = Object.entries(sc.NUMERIC_FIELDS).map(([k, v]) => `- ${k}: ${v}`).join("\n");
  return `# ${spec.info.title} ${spec.info.version}\n\n> ${spec.info.description}\n\n${INTRO(origin)}\n## Screener fields\n\nUse min_<field> and max_<field>, and sort=<field>.\n\n${fields}\n\n## Endpoints\n\n${sections.join("\n\n")}\n`;
}

// ---------- MCP tools ----------

const objectOf = (properties: Record<string, unknown>, required?: string[]) => ({ type: "object", properties, ...(required ? { required } : {}) });
const TICKERS = { type: "array", items: { type: "string" }, maxItems: MAX_BATCH, description: `Up to ${MAX_BATCH} Tokyo tickers` };

export const DEV_TOOLS = [
  { name: "get_companies_batch", description: `Company profiles for up to ${MAX_BATCH} tickers at once. Counts one request per ticker.`, inputSchema: objectOf({ tickers: TICKERS }, ["tickers"]) },
  { name: "get_financials_batch", description: `Five years of headline figures for up to ${MAX_BATCH} tickers at once. Counts one request per ticker.`, inputSchema: objectOf({ tickers: TICKERS }, ["tickers"]) },
  { name: "list_companies", description: "List all listed companies in ticker order. Pass next_cursor back as cursor for the next page.", inputSchema: objectOf({ cursor: { type: "string" }, limit: { type: "integer", maximum: 1000 } }) },
  { name: "list_sectors", description: "The sectors used by the screener, peers and benchmarks, with slugs and company counts.", inputSchema: objectOf({}) },
  { name: "get_sector_benchmarks", description: "Median and quartiles of filing figures across a sector (revenue, margins, ROE, equity ratio, growth and more).", inputSchema: objectOf({ sector: { type: "string", description: "Slug, English or Japanese name from list_sectors" } }, ["sector"]) },
  { name: "get_company_peers", description: "Companies in the same sector with the nearest revenue. Chosen by sector and revenue only.", inputSchema: objectOf({ ticker: { type: "string" }, limit: { type: "integer" } }, ["ticker"]) },
  {
    name: "screen_companies",
    description: "Filter listed companies on figures from filings. No stock prices or valuation; results describe filings and are not recommendations. Pass min_<field> and max_<field> in filters.",
    inputSchema: objectOf({
      filters: { type: "object", description: `Keys: min_<field>, max_<field>. Fields: ${Object.entries(sc.NUMERIC_FIELDS).map(([k, v]) => `${k} (${v})`).join("; ")}`, additionalProperties: { type: "number" } },
      sector: { type: "string", description: "One or more sector slugs, comma-separated" },
      has_stated_rating: { type: "boolean", description: "Whether the company states a bond rating in its filings" },
      min_stated_rating: { type: "string", description: "Bond rating stated in a filing at or above this letter position, such as A-. As stated on that date, not ours, may have changed; agencies' scales are not equivalent" },
      sort: { type: "string", enum: Object.keys(sc.NUMERIC_FIELDS) },
      order: { type: "string", enum: ["desc", "asc"] },
      limit: { type: "integer" },
      cursor: { type: "string" },
    }),
  },
  { name: "get_my_usage", description: "Your plan, requests used this month, remaining requests, and per-minute limit.", inputSchema: objectOf({}) },
];

const toolText = (value: unknown, isError = false) => ({ content: [{ type: "text", text: JSON.stringify(value, null, 2) }], isError });

/** Returns null when the tool is not one of these. */
export async function callDevTool(db: D1Database, plan: data.Plan, auth: AuthOk | undefined, name: string, a: any) {
  switch (name) {
    case "get_companies_batch":
    case "get_financials_batch": {
      const p = parseTickers(Array.isArray(a?.tickers) ? a.tickers.join(",") : String(a?.tickers ?? ""));
      if ("error" in p) return toolText({ error: p.error }, true);
      if (auth && (await chargeExtra(db, auth.keyHash, p.tickers.length - 1)) > auth.quota) return toolText({ error: "Monthly quota exceeded." }, true);
      return toolText(name === "get_companies_batch" ? await batchCompanies(db, p.tickers) : await batchFinancials(db, p.tickers, false));
    }
    case "list_companies": {
      const res = await listCompanies(db, a?.cursor && String(a.cursor), Number(a?.limit));
      return toolText(res, "error" in res);
    }
    case "list_sectors":
      return toolText(await sc.listSectors(db));
    case "get_sector_benchmarks": {
      const row = await sc.sectorBenchmarks(db, String(a?.sector ?? ""));
      return row ? toolText(row) : toolText({ error: "Unknown sector, or no data yet. Use list_sectors." }, true);
    }
    case "get_company_peers": {
      const row = await sc.peers(db, String(a?.ticker ?? ""), plan, Number(a?.limit));
      return row ? toolText(row) : toolText({ error: `No annual-report figures for ${a?.ticker}` }, true);
    }
    case "screen_companies": {
      const params: Record<string, string | undefined> = {};
      for (const [k, v] of Object.entries(a?.filters ?? {})) params[k] = String(v);
      for (const k of ["sector", "min_stated_rating", "sort", "order", "cursor"]) if (a?.[k] !== undefined) params[k] = String(a[k]);
      if (a?.has_stated_rating !== undefined) params.has_stated_rating = String(a.has_stated_rating);
      params.limit = String(Math.min(Number(a?.limit) || 20, 200));
      const q = sc.buildScreener(params);
      return "error" in q ? toolText({ error: q.error }, true) : toolText(await sc.cachedScreener(db, q));
    }
    case "get_my_usage":
      return auth ? toolText(await usageReport(db, auth)) : toolText({ error: "Not available." }, true);
    default:
      return null;
  }
}
