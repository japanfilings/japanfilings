// Paid plans through a merchant of record, which handles VAT and sales tax for overseas customers.
//
// Paddle Billing is the only provider. Docs this code follows:
//   Signature:     https://developer.paddle.com/webhooks/about/signature-verification/
//   Delivery:      https://developer.paddle.com/webhooks/about/respond-to-webhooks/
//   Events:        https://developer.paddle.com/webhooks/subscriptions/subscription-updated/
//                  https://developer.paddle.com/webhooks/subscriptions/subscription-canceled/
//                  https://developer.paddle.com/webhooks/subscriptions/subscription-past-due/
//   Access rules:  https://developer.paddle.com/build/subscriptions/provision-access-webhooks/
//   Transactions:  https://developer.paddle.com/api-reference/transactions/create-transaction/
//   Custom data:   https://developer.paddle.com/build/transactions/custom-data/
//   Hosted link:   https://developer.paddle.com/paddlejs/hosted-checkout-url-parameters
//   Portal:        https://developer.paddle.com/api-reference/customer-portals/create-customer-portal-session/
//
// Lemon Squeezy is left out: its X-Signature is an HMAC of the body alone, with no timestamp
// (https://docs.lemonsqueezy.com/help/webhooks/signing-requests), so it needs its own replay
// handling and does not fit this interface briefly.
//
// Nothing here logs or stores card data, names or addresses. The checkout does not even receive
// the user's email from us; the only link between the two systems is our user ID in custom_data.
import type { Plan } from "./data";

export type BillingEnv = {
  DB: D1Database;
  PADDLE_API_KEY?: string;
  PADDLE_WEBHOOK_SECRET?: string;
  PADDLE_HOSTED_CHECKOUT_ID?: string;
  PADDLE_PRICE_STARTER?: string;
  PADDLE_PRICE_PRO?: string;
  PADDLE_ENV?: string; // "sandbox" uses Paddle's sandbox; anything else is live
  PADDLE_API_BASE?: string; // local tests only; honoured only with DEV_SHOW_LINKS=1
  DEV_SHOW_LINKS?: string;
};

export type PaidPlan = Exclude<Plan, "free">;

/** A provider's subscription event in our terms. */
export type BillingEvent = {
  // "subscription": a subscription's state. "revoke": an approved full refund or a chargeback, which
  // ends the paid access of the subscription it names (status then holds REFUNDED or CHARGED_BACK).
  kind: "subscription" | "revoke";
  id: string;
  type: string;
  occurredAt: string; // normalised ISO timestamp
  subscriptionId: string;
  customerId: string | null;
  status: string;
  priceIds: string[];
  userId: string | null; // our user ID, as we set it at checkout
  periodEnd: string | null;
  cancelAt: string | null;
};

export type Verdict = { ok: true } | { ok: false; reason: "bad_signature" | "stale" };

export interface BillingProvider {
  name: string;
  /** Whether every setting the provider needs is present. */
  configured(env: BillingEnv): boolean;
  verify(env: BillingEnv, rawBody: string, headers: Headers, nowSec: number): Promise<Verdict>;
  /** null: a validly signed event we do not act on. Throws on a malformed subscription event. */
  parse(rawBody: string): BillingEvent | null;
  planForPrices(env: BillingEnv, priceIds: string[]): PaidPlan | null;
  checkoutUrl(env: BillingEnv, plan: PaidPlan, userId: string): Promise<string>;
  portalUrl(env: BillingEnv, customerId: string): Promise<string>;
}

// Paddle's SDKs default to 5 seconds. We allow more for clock differences; the event ID store is
// what stops a replay inside the window.
export const TIMESTAMP_TOLERANCE_SEC = 300;

const hexToBytes = (hex: string) => Uint8Array.from(hex.match(/../g) ?? [], (b) => parseInt(b, 16));
const iso = (v: unknown) => {
  const ms = typeof v === "string" ? Date.parse(v) : NaN;
  return Number.isNaN(ms) ? null : new Date(ms).toISOString();
};

const paddleApi = (env: BillingEnv) =>
  env.PADDLE_API_BASE && env.DEV_SHOW_LINKS === "1"
    ? env.PADDLE_API_BASE
    : env.PADDLE_ENV === "sandbox" ? "https://sandbox-api.paddle.com" : "https://api.paddle.com";

async function paddlePost(env: BillingEnv, path: string, body: unknown): Promise<any> {
  const res = await fetch(paddleApi(env) + path, {
    method: "POST",
    headers: { authorization: `Bearer ${env.PADDLE_API_KEY}`, "content-type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(10_000),
  });
  // The response body is not logged: it can carry customer details.
  if (!res.ok) throw new Error(`Paddle API ${path.split("/")[1]} returned ${res.status}`);
  return ((await res.json()) as any).data;
}

const PADDLE_EVENTS = new Set([
  "subscription.created", "subscription.activated", "subscription.updated", "subscription.trialing",
  "subscription.past_due", "subscription.paused", "subscription.resumed", "subscription.canceled",
]);
// Refunds and chargebacks: https://developer.paddle.com/webhooks/adjustments/adjustment-updated/
const ADJUSTMENT_EVENTS = new Set(["adjustment.created", "adjustment.updated"]);
export const REFUNDED = "refunded";
export const CHARGED_BACK = "charged_back";

export const paddle: BillingProvider = {
  name: "paddle",

  configured: (env) =>
    !!(env.PADDLE_API_KEY && env.PADDLE_WEBHOOK_SECRET && env.PADDLE_HOSTED_CHECKOUT_ID && env.PADDLE_PRICE_STARTER && env.PADDLE_PRICE_PRO),

  // Header: `ts=1671552777;h1=<hex>`; more than one h1 may appear during secret rotation.
  // Signed payload: `${ts}:${rawBody}`, HMAC-SHA256 with the destination's secret.
  async verify(env, rawBody, headers, nowSec) {
    const parts = (headers.get("paddle-signature") ?? "").split(";").map((p) => p.split("=", 2));
    const ts = parts.find(([k]) => k === "ts")?.[1] ?? "";
    const sigs = parts.filter(([k, v]) => k === "h1" && /^[0-9a-f]{64}$/i.test(v ?? "")).map(([, v]) => v);
    if (!/^\d{1,12}$/.test(ts) || !sigs.length) return { ok: false, reason: "bad_signature" };
    const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(env.PADDLE_WEBHOOK_SECRET!), { name: "HMAC", hash: "SHA-256" }, false, ["verify"]);
    const signed = new TextEncoder().encode(`${ts}:${rawBody}`);
    // subtle.verify compares in constant time, unlike comparing two hex strings.
    let valid = false;
    for (const sig of sigs) if (await crypto.subtle.verify("HMAC", key, hexToBytes(sig), signed)) valid = true;
    if (!valid) return { ok: false, reason: "bad_signature" };
    // Checked after the signature, so an unsigned caller learns nothing from the difference.
    if (Math.abs(nowSec - Number(ts)) > TIMESTAMP_TOLERANCE_SEC) return { ok: false, reason: "stale" };
    return { ok: true };
  },

  parse(rawBody) {
    const e = JSON.parse(rawBody);
    const d = e?.data;
    const occurredAt = iso(e?.occurred_at);
    if (ADJUSTMENT_EVENTS.has(e?.event_type)) {
      if (typeof e.event_id !== "string" || !occurredAt || typeof d?.id !== "string" || typeof d?.status !== "string" || typeof d?.action !== "string") throw new Error("Malformed event");
      // A refund waits for approval (adjustment.updated). A partial refund leaves the subscription paid;
      // a chargeback is approved when created. Adjustments without a subscription change no access.
      const revoke = d.status === "approved" && (d.action === "chargeback" || (d.action === "refund" && d.type !== "partial"));
      if (!revoke || typeof d.subscription_id !== "string") return null;
      return {
        kind: "revoke", id: e.event_id, type: e.event_type, occurredAt, subscriptionId: d.subscription_id,
        customerId: typeof d.customer_id === "string" ? d.customer_id : null,
        status: d.action === "chargeback" ? CHARGED_BACK : REFUNDED,
        priceIds: [], userId: null, periodEnd: null, cancelAt: null,
      };
    }
    if (!PADDLE_EVENTS.has(e?.event_type)) return null;
    if (typeof e.event_id !== "string" || !occurredAt || typeof d?.id !== "string" || typeof d?.status !== "string") throw new Error("Malformed event");
    const scheduled = d.scheduled_change;
    const userId = d.custom_data?.user_id;
    return {
      kind: "subscription",
      id: e.event_id,
      type: e.event_type,
      occurredAt,
      subscriptionId: d.id,
      customerId: typeof d.customer_id === "string" ? d.customer_id : null,
      status: d.status,
      priceIds: Array.isArray(d.items) ? d.items.map((i: any) => i?.price?.id).filter((p: unknown) => typeof p === "string") : [],
      userId: typeof userId === "string" ? userId : null,
      periodEnd: iso(d.current_billing_period?.ends_at),
      cancelAt: scheduled?.action === "cancel" ? iso(scheduled.effective_at) : null,
    };
  },

  // Only the two prices on sale grant a plan. Any other price, including an old Business price, grants nothing.
  planForPrices(env, priceIds) {
    if (env.PADDLE_PRICE_PRO && priceIds.includes(env.PADDLE_PRICE_PRO)) return "pro";
    if (env.PADDLE_PRICE_STARTER && priceIds.includes(env.PADDLE_PRICE_STARTER)) return "starter";
    return null;
  },

  // A server-side transaction carries our user ID, which a plain price link cannot. Paddle's own
  // checkout.url needs Paddle.js on our page, which our CSP forbids, so we open the hosted checkout.
  async checkoutUrl(env, plan, userId) {
    // Billing is closed until every setting is present; never create a transaction before that.
    if (!paddle.configured(env)) throw new Error("Paid plans are not open yet.");
    if (plan !== "starter" && plan !== "pro") throw new Error("Only Starter and Pro are sold."); // Business is no longer sold
    const price = plan === "starter" ? env.PADDLE_PRICE_STARTER! : env.PADDLE_PRICE_PRO!;
    const txn = await paddlePost(env, "/transactions", {
      items: [{ price_id: price, quantity: 1 }],
      custom_data: { user_id: userId },
      collection_mode: "automatic",
    });
    const host = env.PADDLE_ENV === "sandbox" ? "sandbox.pay.paddle.io" : "pay.paddle.io";
    return `https://${host}/checkout/${encodeURIComponent(env.PADDLE_HOSTED_CHECKOUT_ID!)}?transaction_id=${encodeURIComponent(txn.id)}`;
  },

  // Portal links carry a temporary token, so a session is created per click and never stored.
  async portalUrl(env, customerId) {
    const session = await paddlePost(env, `/customers/${encodeURIComponent(customerId)}/portal-sessions`, {});
    return session.urls.general.overview;
  },
};

export const provider: BillingProvider = paddle;
export const billingOpen = (env: BillingEnv) => provider.configured(env);
export const providerKey = (env: BillingEnv) => `${provider.name}_${env.PADDLE_ENV === "sandbox" ? "sandbox" : "live"}`;

// Paddle's guidance is to keep access while a payment is being retried; a failed recovery ends in
// subscription.canceled. A cancellation at period end stays active until then, and the event that
// downgrades is that same subscription.canceled.
const LIVE_SQL = "('active','trialing','past_due')";
const LIVE = new Set(["active", "trialing", "past_due"]);
const REVOKED = new Set([REFUNDED, CHARGED_BACK]);
const PAID = new Set<string>(["starter", "pro"]);

/** Remove entitlements that no longer have a live, unexpired subscription in this Paddle environment. */
export async function reconcileEntitlements(env: BillingEnv, userId?: string): Promise<void> {
  const now = new Date().toISOString();
  const scope = userId ? " AND id = ?3" : "";
  const users = env.DB.prepare(
    `UPDATE users SET plan = COALESCE((SELECT plan FROM billing_subscriptions b WHERE b.user_id = users.id AND b.provider = ?1 ` +
      `AND b.status IN ${LIVE_SQL} AND (b.current_period_end IS NULL OR b.current_period_end > ?2) ` +
      "ORDER BY CASE plan WHEN 'business' THEN 3 WHEN 'pro' THEN 2 WHEN 'starter' THEN 1 ELSE 0 END DESC LIMIT 1), 'free') " +
      "WHERE EXISTS (SELECT 1 FROM billing_subscriptions any_b WHERE any_b.user_id = users.id)" + scope,
  );
  await (userId ? users.bind(providerKey(env), now, userId) : users.bind(providerKey(env), now)).run();
  const keys = env.DB.prepare(
    "UPDATE api_keys SET plan = (SELECT plan FROM users WHERE users.id = api_keys.user_id) " +
      "WHERE EXISTS (SELECT 1 FROM billing_subscriptions b WHERE b.user_id = api_keys.user_id)" + (userId ? " AND user_id = ?1" : ""),
  );
  await (userId ? keys.bind(userId) : keys).run();
}

type Outcome = { status: number; body: Record<string, unknown> };

export async function handleWebhook(env: BillingEnv, rawBody: string, headers: Headers): Promise<Outcome> {
  if (!billingOpen(env)) return { status: 503, body: { error: "Paid plans are not open yet." } };
  const verdict = await provider.verify(env, rawBody, headers, Math.floor(Date.now() / 1000));
  if (!verdict.ok) return { status: 401, body: { error: verdict.reason === "stale" ? "Stale timestamp." : "Invalid signature." } };

  let ev: BillingEvent | null;
  try {
    ev = provider.parse(rawBody);
  } catch {
    return { status: 400, body: { error: "Malformed event." } };
  }
  if (!ev) return { status: 200, body: { ok: true, ignored: true } };

  const db = env.DB;
  const name = providerKey(env);
  const seen = await db.prepare("SELECT 1 AS x FROM billing_events WHERE provider = ?1 AND event_id = ?2").bind(name, ev.id).first();
  if (seen) return { status: 200, body: { ok: true, duplicate: true } };

  const existing = await db
    .prepare("SELECT user_id, plan, status, current_period_end, last_event_at FROM billing_subscriptions WHERE provider = ?1 AND subscription_id = ?2")
    .bind(name, ev.subscriptionId)
    .first<{ user_id: string; plan: Plan; status: string; current_period_end: string | null; last_event_at: string }>();
  const now = new Date().toISOString();
  const recordEvent = db
    .prepare("INSERT INTO billing_events (provider, event_id, event_type, occurred_at, received_at) VALUES (?1, ?2, ?3, ?4, ?5)")
    .bind(name, ev.id, ev.type, ev.occurredAt, now);
  // Once a subscription is linked, the link is fixed.
  const userId = existing?.user_id ?? ev.userId;
  const user = userId ? await db.prepare("SELECT id, plan FROM users WHERE id = ?1").bind(userId).first<{ id: string; plan: Plan }>() : null;
  const audit = (action: string, newPlan: string | null) =>
    db
      .prepare("INSERT INTO billing_audit (at, provider, event_id, event_type, subscription_id, user_id, action, old_plan, new_plan) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)")
      .bind(now, name, ev!.id, ev!.type, ev!.subscriptionId, user?.id ?? null, action, user?.plan ?? null, newPlan);
  // No user ID at all (a subscription made outside our checkout, or a refund of one we never linked):
  // no retry can ever link it, so it is acknowledged and recorded rather than retried for days.
  if (!userId) {
    await recordOnce(db, name, ev.id, [recordEvent, audit("unmatched_user", null)]);
    return { status: 200, body: { ok: true, ignored: true } };
  }
  // A user ID we do not have yet: do not consume the event ID, so a retry can link the payment after
  // account creation catches up.
  if (!user) return { status: 503, body: { error: "Subscription user is not registered yet." } };

  const live = LIVE.has(ev.status);
  const paid = provider.planForPrices(env, ev.priceIds);

  // The event is recorded in the same batch as its effects: both happen or neither does. A concurrent
  // retry fails on the primary key and rolls back. Cases below change no plan but are still recorded.
  let statements: D1PreparedStatement[];
  let action = "applied";
  let status = ev.status;
  let plan: string = paid ?? "free";
  if (ev.kind === "revoke") {
    // A refund or chargeback ends access whatever the order of arrival; the plan is kept for the record.
    action = ev.status;
    plan = existing!.plan;
  } else if (existing && ev.occurredAt < existing.last_event_at) action = "stale_ignored";
  else if (existing && REVOKED.has(existing.status) && live &&
    !(ev.status === "active" && ev.periodEnd && existing.current_period_end && ev.periodEnd > existing.current_period_end)) {
    // After a refund or chargeback only a paid renewal (a later billing period, active) restores access.
    action = "revoked_kept";
    status = existing.status;
  } else if (live && !paid) {
    action = "unknown_price";
    // A price missing from our settings is configuration drift, not the customer's doing: an existing
    // paid subscription keeps its plan and the audit row flags it. A new one is granted nothing; its
    // customer and subscription IDs are still kept so the owner can reach Paddle's portal.
    if (existing && PAID.has(existing.plan)) plan = existing.plan;
  }

  if (action === "stale_ignored") {
    statements = [recordEvent, audit(action, null)];
  } else if (ev.kind === "revoke") {
    statements = [
      recordEvent,
      db
        .prepare(
          "UPDATE billing_subscriptions SET status = ?3, last_event_at = MAX(last_event_at, ?4), updated_at = ?5 " +
            "WHERE provider = ?1 AND subscription_id = ?2",
        )
        .bind(name, ev.subscriptionId, ev.status, ev.occurredAt, now),
      ...applyPlan(db, user.id, name, now, user.plan, ev, action),
    ];
  } else {
    statements = [
      recordEvent,
      db
        .prepare(
          "INSERT INTO billing_subscriptions (provider, subscription_id, user_id, customer_id, plan, status, current_period_end, cancel_at, last_event_at, updated_at) " +
            "VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10) ON CONFLICT(provider, subscription_id) DO UPDATE SET " +
            "customer_id = COALESCE(excluded.customer_id, customer_id), plan = excluded.plan, status = excluded.status, " +
            "current_period_end = excluded.current_period_end, cancel_at = excluded.cancel_at, last_event_at = excluded.last_event_at, updated_at = excluded.updated_at " +
            "WHERE excluded.last_event_at >= billing_subscriptions.last_event_at",
        )
        .bind(name, ev.subscriptionId, user.id, ev.customerId, plan, status, ev.periodEnd, ev.cancelAt, ev.occurredAt, now),
      ...applyPlan(db, user.id, name, now, user.plan, ev, action),
    ];
  }
  if (await recordOnce(db, name, ev.id, statements)) return { status: 200, body: { ok: true, duplicate: true } };
  return { status: 200, body: { ok: true, action } };
}

/** Recompute the user's plan from their best live subscription, copy it to their keys, and audit it. */
function applyPlan(db: D1Database, userId: string, name: string, now: string, oldPlan: string, ev: BillingEvent, action: string) {
  return [
    // The best live subscription decides, so ending an old one cannot undo a newer one.
    db
      .prepare(
        `UPDATE users SET plan = COALESCE((SELECT plan FROM billing_subscriptions WHERE user_id = ?1 AND provider = ?2 AND status IN ${LIVE_SQL} ` +
          "AND (current_period_end IS NULL OR current_period_end > ?3) " +
          "ORDER BY CASE plan WHEN 'business' THEN 3 WHEN 'pro' THEN 2 WHEN 'starter' THEN 1 ELSE 0 END DESC LIMIT 1), 'free') WHERE id = ?1",
      )
      .bind(userId, name, now),
    // authorize() reads api_keys.plan on every request, so this applies at once, without rotation.
    db.prepare("UPDATE api_keys SET plan = (SELECT plan FROM users WHERE id = ?1) WHERE user_id = ?1").bind(userId),
    db
      .prepare(
        "INSERT INTO billing_audit (at, provider, event_id, event_type, subscription_id, user_id, action, old_plan, new_plan) " +
        "SELECT ?1, ?2, ?3, ?4, ?5, ?6, ?8, ?7, plan FROM users WHERE id = ?6",
      )
      .bind(now, name, ev.id, ev.type, ev.subscriptionId, userId, oldPlan, action),
  ];
}

/** Run the batch. true: a concurrent delivery of the same event ID won, so nothing was applied. */
async function recordOnce(db: D1Database, name: string, eventId: string, statements: D1PreparedStatement[]): Promise<boolean> {
  try {
    await db.batch(statements);
    return false;
  } catch (err) {
    if (String(err).includes("UNIQUE") && (await db.prepare("SELECT 1 AS x FROM billing_events WHERE provider = ?1 AND event_id = ?2").bind(name, eventId).first()))
      return true;
    throw err; // a non-200 makes the provider retry
  }
}

export type BillingView = {
  open: boolean;
  live: boolean; // has a subscription that is still billing
  customerId: string | null;
  status: string | null;
  periodEnd: string | null;
  cancelAt: string | null;
};

export async function billingView(env: BillingEnv, userId: string): Promise<BillingView> {
  await reconcileEntitlements(env, userId);
  const now = new Date().toISOString();
  const row = await env.DB.prepare(
    `SELECT customer_id, status, current_period_end, cancel_at FROM billing_subscriptions WHERE user_id = ?1 AND provider = ?2 ` +
      `ORDER BY (status IN ${LIVE_SQL} AND (current_period_end IS NULL OR current_period_end > ?3)) DESC, last_event_at DESC LIMIT 1`,
  )
    .bind(userId, providerKey(env), now)
    .first<{ customer_id: string | null; status: string; current_period_end: string | null; cancel_at: string | null }>();
  return {
    open: billingOpen(env),
    live: !!row && LIVE.has(row.status) && (!row.current_period_end || row.current_period_end > now),
    customerId: row?.customer_id ?? null,
    status: row?.status ?? null,
    periodEnd: row?.current_period_end ?? null,
    cancelAt: row?.cancel_at ?? null,
  };
}
