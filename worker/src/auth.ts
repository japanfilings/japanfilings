// API keys are stored only as SHA-256 hashes. Monthly quotas match the published plans.
import type { Plan } from "./data";

// Two plans are sold, Starter and Pro. "business" is no longer sold and gets what Pro gets.
export const QUOTA: Record<Plan, number> = { free: 0, starter: 20_000, pro: 300_000, business: 300_000 };

export async function sha256Hex(s: string): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export function readKey(req: Request): string | null {
  const auth = req.headers.get("authorization");
  if (auth?.toLowerCase().startsWith("bearer ")) return auth.slice(7).trim();
  return req.headers.get("x-api-key");
}

// Requests per minute. A fixed window counted on the key's usage row in D1: the count itself is exact,
// but a client can send up to twice the limit across a minute boundary. It needs no paid feature and
// adds no row writes, because the monthly counter's upsert carries it.
export const RPM: Record<Plan, number> = { free: 0, starter: 60, pro: 600, business: 600 };

const NO_PLAN = "A paid plan is required. Plans start at $9 a month; see /pricing on our website.";

export type AuthOk = { ok: true; plan: Plan; keyHash: string; owner: string; used: number; quota: number; minuteUsed: number; rpm: number; minuteReset: number };
export type AuthResult =
  | AuthOk
  | { ok: false; status: 401 | 402 | 429; error: string; retryAfter?: number; rpm?: number };

/** One upsert counts the month and the minute; a request refused by the minute limit is not billed to the month. */
export async function meter(db: D1Database, _keyHash: string, plan: Plan, owner: string): Promise<AuthResult> {
  if (!Object.hasOwn(QUOTA, plan) || !Object.hasOwn(RPM, plan))
    return { ok: false, status: 401, error: "Invalid API key plan." };
  // Also reached by OAuth tokens, which do not pass through authorize(): no plan is 402, not a 429 from a zero quota.
  if (plan === "free") return { ok: false, status: 402, error: NO_PLAN };
  const now = Date.now();
  const minute = Math.floor(now / 60_000);
  const minuteReset = Math.max(Math.ceil((minute + 1) * 60 - now / 1000), 1);
  const rpm = RPM[plan];
  const row = await db
    .prepare(
      "INSERT INTO usage (key_hash, month, requests, minute, minute_count) VALUES (?1, ?2, 1, ?3, 1) " +
        "ON CONFLICT(key_hash, month) DO UPDATE SET " +
        "requests = requests + CASE WHEN minute = ?3 AND minute_count >= ?4 THEN 0 ELSE 1 END, " +
        "minute_count = CASE WHEN minute = ?3 THEN minute_count + 1 ELSE 1 END, " +
        "minute = ?3 RETURNING requests, minute_count",
    )
    .bind(owner, new Date(now).toISOString().slice(0, 7), minute, rpm)
    .first<{ requests: number; minute_count: number }>();
  const minuteUsed = row?.minute_count ?? 1;
  if (minuteUsed > rpm) {
    return { ok: false, status: 429, error: `Rate limit of ${rpm} requests per minute exceeded. Retry after ${minuteReset} seconds.`, retryAfter: minuteReset, rpm };
  }
  const used = row?.requests ?? 1;
  const quota = QUOTA[plan];
  if (used > quota) return { ok: false, status: 429, error: `Monthly quota of ${quota} requests exceeded.` };
  return { ok: true, plan, keyHash: owner, owner, used, quota, minuteUsed, rpm, minuteReset };
}

/** Headers for the per-minute limit. RateLimit-* stay the monthly quota, as documented. */
export const minuteHeaders = (a: { rpm: number; minuteUsed: number }) => ({
  "X-RateLimit-Limit-Minute": String(a.rpm),
  "X-RateLimit-Remaining-Minute": String(Math.max(a.rpm - a.minuteUsed, 0)),
});

/** Batch calls count one request per item. Returns the month's new total. */
export async function chargeExtra(db: D1Database, owner: string, extra: number): Promise<number> {
  const amount = Math.max(Math.trunc(extra), 0);
  const hash = owner.startsWith("key:") ? owner.slice(4) : null;
  const key = await db
    .prepare("SELECT plan FROM api_keys WHERE revoked = 0 AND ((?2 IS NULL AND user_id = ?1) OR key_hash = ?2) ORDER BY created_at DESC LIMIT 1")
    .bind(owner, hash)
    .first<{ plan: string }>();
  if (!key || !Object.hasOwn(QUOTA, key.plan) || !Object.hasOwn(RPM, key.plan)) return Number.MAX_SAFE_INTEGER;
  const plan = key.plan as Plan;
  // Reuse the exact row and fixed-minute window opened by meter(). Besides closing the UTC-month
  // rollover gap, the conditional UPDATE makes concurrent batches reserve quota atomically.
  const current = await db
    .prepare("SELECT month, minute, requests FROM usage WHERE key_hash = ?1 ORDER BY minute DESC LIMIT 1")
    .bind(owner)
    .first<{ month: string; minute: number; requests: number }>();
  if (!current || amount === 0) return current?.requests ?? 0;
  const row = await db
    .prepare(
      "UPDATE usage SET requests = requests + ?4, minute_count = minute_count + ?4 " +
        "WHERE key_hash = ?1 AND month = ?2 AND minute = ?3 AND requests + ?4 <= ?5 AND minute_count + ?4 <= ?6 " +
        "RETURNING requests",
    )
    .bind(owner, current.month, current.minute, amount, QUOTA[plan], RPM[plan])
    .first<{ requests: number }>();
  return row?.requests ?? QUOTA[plan] + 1;
}

export async function authorize(db: D1Database, key: string | null, billingProvider?: string): Promise<AuthResult> {
  if (!key) return { ok: false, status: 401, error: "Missing API key. Send 'Authorization: Bearer <key>'." };
  const keyHash = await sha256Hex(key);
  const row = await db
    .prepare(
      "SELECT k.plan, k.user_id, " +
        "EXISTS(SELECT 1 FROM billing_subscriptions b WHERE b.user_id = k.user_id) AS billed, " +
        "(SELECT b.plan FROM billing_subscriptions b WHERE b.user_id = k.user_id AND b.provider = ?2 " +
        "AND b.status IN ('active','trialing','past_due') AND (b.current_period_end IS NULL OR b.current_period_end > ?3) " +
        "ORDER BY CASE b.plan WHEN 'business' THEN 3 WHEN 'pro' THEN 2 WHEN 'starter' THEN 1 ELSE 0 END DESC LIMIT 1) AS billed_plan " +
        "FROM api_keys k WHERE k.key_hash = ?1 AND k.revoked = 0",
    )
    .bind(keyHash, billingProvider ?? "paddle", new Date().toISOString())
    .first<{ plan: string; user_id: string | null; billed: number; billed_plan: string | null }>();
  if (!row) return { ok: false, status: 401, error: "Invalid or revoked API key." };
  if (!Object.hasOwn(QUOTA, row.plan) || !Object.hasOwn(RPM, row.plan))
    return { ok: false, status: 401, error: "Invalid API key plan." };
  const effective = row.billed ? (row.billed_plan ?? "free") : row.plan;
  if (!Object.hasOwn(QUOTA, effective) || !Object.hasOwn(RPM, effective))
    return { ok: false, status: 401, error: "Invalid API key plan." };
  // Every plan is paid: an account without one has a key but no access.
  if (effective === "free") return { ok: false, status: 402, error: NO_PLAN };
  // Webhooks belong to the account, so they survive key rotation; keys issued outside accounts own their own.
  return meter(db, keyHash, effective as Plan, row.user_id ?? `key:${keyHash}`);
}
