// Response helpers for /v1: the X-Data-As-Of header and ?format=csv.
import type { MiddlewareHandler } from "hono";

// ---------- X-Data-As-Of ----------

// Fixed table/column pairs, never built from input. The header is the newest record date loaded
// into the dataset behind the endpoint, so a consumer can tell how fresh the data is.
const DATASETS = {
  annual: ["annual_reports", "submit_date"],
  ownership: ["large_holdings", "submit_date"],
  filings: ["filings", "submit_datetime"],
  releases: ["earnings_releases", "release_date"],
  ratings: ["disclosed_ratings", "submit_date"],
  buybacks: ["buyback_reports", "submit_date"],
  governance: ["governance_events", "submit_date"],
  funds: ["us_fund_holdings", "quarter"],
  uncertainty: ["policy_uncertainty_monthly", "month"],
  facts: ["company_facts", "source_submit_date"],
} as const;
type Dataset = keyof typeof DATASETS;

const ROUTES: [RegExp, Dataset][] = [
  [/^\/v1\/(screener|sectors)|\/peers$/, "facts"],
  [/^\/v1\/(large-holdings|events|investors)|^\/v1\/companies\/[^/]+\/trades$/, "ownership"],
  [/^\/v1\/filings/, "filings"],
  [/earnings-releases$/, "releases"],
  [/credit-ratings$/, "ratings"],
  [/buybacks$/, "buybacks"],
  [/governance$/, "governance"],
  [/us-funds$/, "funds"],
  [/^\/v1\/indicators\/policy-uncertainty$/, "uncertainty"],
  [/^\/v1\/(companies|batch|cross-holdings|indicators)/, "annual"],
];

// Per isolate, so most requests read no rows for this header at all.
const AS_OF_TTL_MS = 10 * 60_000;
const asOfCache = new Map<Dataset, { value: string | null; expires: number }>();

export async function dataAsOf(db: D1Database, path: string): Promise<string | null> {
  const ds = ROUTES.find(([re]) => re.test(path))?.[1];
  if (!ds) return null;
  const hit = asOfCache.get(ds);
  if (hit && hit.expires > Date.now()) return hit.value;
  const [table, col] = DATASETS[ds];
  try {
    const row = await db.prepare(`SELECT MAX(${col}) AS v FROM ${table}`).first<{ v: string | null }>();
    const value = row?.v ? String(row.v).slice(0, 10) : null;
    asOfCache.set(ds, { value, expires: Date.now() + AS_OF_TTL_MS });
    return value;
  } catch {
    return null; // a missing header must not fail a data response
  }
}

// ---------- CSV ----------

const MAX_DEPTH = 3;
const MAX_COLUMNS = 300;
// Webhook subscriptions are account settings, not data.
const NOT_EXPORTABLE = /^\/v1\/(webhooks|usage)/;

/** Nested objects become dotted columns; arrays stay in one cell as JSON. */
export function flatten(row: unknown, prefix = "", depth = 0, out: Map<string, unknown> = new Map()) {
  if (row !== null && typeof row === "object" && !Array.isArray(row) && depth < MAX_DEPTH) {
    for (const [k, v] of Object.entries(row as Record<string, unknown>)) flatten(v, prefix ? `${prefix}.${k}` : k, depth + 1, out);
  } else if (prefix) {
    out.set(prefix, row !== null && typeof row === "object" ? JSON.stringify(row) : row);
  } else {
    out.set("value", row);
  }
  return out;
}

// A cell starting with one of these runs as a formula in Excel, Sheets or LibreOffice, including
// the full-width forms that spreadsheet apps may normalise. Filer-written text reaches these cells.
const FORMULA_START = /^[\s\u0000-\u001f 　]*[=+\-@＝＋－＠]|^[\t\r\n]/;

export function csvCell(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "number") return Number.isFinite(v) ? String(v) : "";
  if (typeof v === "boolean") return String(v);
  let s = String(v);
  if (FORMULA_START.test(s)) s = "'" + s;
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export function toCsv(rows: unknown[]): string {
  const flat = rows.map((r) => flatten(r));
  const cols: string[] = [];
  const seen = new Set<string>();
  for (const f of flat) {
    for (const k of f.keys()) {
      if (!seen.has(k) && cols.length < MAX_COLUMNS) { seen.add(k); cols.push(k); }
    }
  }
  const lines = [cols.map(csvCell).join(",")];
  for (const f of flat) lines.push(cols.map((c) => csvCell(f.get(c))).join(","));
  return lines.join("\r\n") + "\r\n";
}

// Header values must be ASCII; attribution text may not be.
const headerSafe = (s: string) => (/^[\x20-\x7e]*$/.test(s) ? s : encodeURIComponent(s));

/** Runs after the route handler: adds X-Data-As-Of and converts list responses when asked. */
export const dataResponse: MiddlewareHandler<{ Bindings: { DB: D1Database } }> = async (c, next) => {
  const format = c.req.query("format");
  if (format !== undefined && format !== "csv" && format !== "json") {
    return c.json({ error: "format must be json or csv." }, 400);
  }
  await next();
  if (c.req.method !== "GET" || c.res.status !== 200) return;
  const path = c.req.path;
  const asOf = await dataAsOf(c.env.DB, path);
  if (asOf) c.res.headers.set("X-Data-As-Of", asOf);
  if (format !== "csv") return;
  const body: any = await c.res.clone().json().catch(() => null);
  if (!Array.isArray(body?.data) || NOT_EXPORTABLE.test(path)) {
    c.res = c.json({ error: "format=csv is available on list endpoints (those returning a data array)." }, 400);
    return;
  }
  const headers = new Headers(c.res.headers);
  headers.set("Content-Type", "text/csv; charset=utf-8");
  headers.set("Content-Disposition", `attachment; filename="japanfilings-${path.split("/").filter(Boolean).slice(1).join("-").replace(/[^A-Za-z0-9-]/g, "") || "data"}.csv"`);
  // The attribution travels with the file's response: source credit is a condition of the data's licence.
  if (typeof body.attribution === "string") headers.set("X-Attribution", headerSafe(body.attribution));
  if (body.next_cursor) headers.set("X-Next-Cursor", headerSafe(String(body.next_cursor)));
  if (body.history_limited_to) headers.set("X-History-Limited-To", headerSafe(String(body.history_limited_to)));
  const bom = c.req.query("bom") === "1" ? "﻿" : ""; // for Excel opening Japanese text directly
  c.res = undefined; // Hono would otherwise copy the old JSON content type over
  c.res = new Response(bom + toCsv(body.data), { status: 200, headers });
};

export const CORS_EXPOSE = ["X-Data-As-Of", "X-Attribution", "X-Next-Cursor", "X-History-Limited-To", "X-RateLimit-Limit-Minute", "X-RateLimit-Remaining-Minute"];
