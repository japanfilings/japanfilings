// Billing tests with locally signed fixtures. No real payment API is called: Paddle's API is a mock
// on 127.0.0.1. Two modes, against the running `wrangler dev`:
//
//   Billing closed (no PADDLE_* in .dev.vars):
//     BASE=http://localhost:8788 npm test
//   Billing open: put the values of TEST_VARS below in .dev.vars, then also export
//     PADDLE_WEBHOOK_SECRET=test_only_not_a_secret
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { createHmac, randomUUID } from "node:crypto";
import { createServer } from "node:http";
import test, { after, before } from "node:test";
import { promisify } from "node:util";

const BASE = process.env.BASE ?? "http://localhost:8788";
const SECRET = process.env.PADDLE_WEBHOOK_SECRET;
// .dev.vars for the open mode. DEV_SHOW_LINKS=1 is what allows the mock API base.
// PADDLE_API_KEY=test PADDLE_WEBHOOK_SECRET=test_only_not_a_secret PADDLE_HOSTED_CHECKOUT_ID=hsc_test
// PADDLE_PRICE_STARTER=pri_starter PADDLE_PRICE_PRO=pri_pro PADDLE_ENV=sandbox PADDLE_API_BASE=http://127.0.0.1:8799
// LEGAL_PAGE_ENABLED=true LEGAL_PROVIDER_NAME=t LEGAL_START_OF_SERVICE=t LEGAL_CANCELLATION=t LEGAL_REFUNDS=t
// (sales stay closed without the legal notice, even with every PADDLE_* value set)
// (Business is no longer sold: a leftover PADDLE_PRICE_BUSINESS is ignored and its price grants nothing.)
const MOCK_PORT = 8799;

const sign = (body, secret = SECRET, ts = Math.floor(Date.now() / 1000)) =>
  `ts=${ts};h1=${createHmac("sha256", secret).update(`${ts}:${body}`).digest("hex")}`;
const post = (body, signature) =>
  fetch(BASE + "/billing/webhook", { method: "POST", headers: signature ? { "paddle-signature": signature } : {}, body });
const deliver = (body) => post(body, sign(body));

let clock = Date.now() - 3_600_000;
function event(type, sub, userId, { status = "active", price = "pri_pro", scheduled = null, id = "evt_" + randomUUID(), at, periodEnd } = {}) {
  clock += 1000;
  return JSON.stringify({
    event_id: id,
    event_type: type,
    occurred_at: at ?? new Date(clock).toISOString().replace("Z", "000Z"),
    data: {
      id: sub, status, customer_id: "ctm_test", custom_data: { user_id: userId },
      items: [{ price: { id: price } }],
      current_billing_period: status === "canceled" ? null : { ends_at: periodEnd ?? new Date(Date.now() + 86_400_000 * 20).toISOString() },
      scheduled_change: scheduled,
    },
  });
}

async function signIn() {
  const email = `billing-${randomUUID()}@example.test`;
  const login = await fetch(BASE + "/login", { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body: new URLSearchParams({ email }) });
  const link = (await login.text()).match(/href="([^"]*\/auth\/verify\?token=[^"]+)"/)[1].replace(/&amp;/g, "&");
  const verify = await fetch(link, { redirect: "manual" });
  return verify.headers.get("set-cookie").split(";")[0];
}
const form = (cookie, path, fields = {}) =>
  fetch(BASE + path, { method: "POST", redirect: "manual", headers: { cookie, "content-type": "application/x-www-form-urlencoded" }, body: new URLSearchParams(fields) });
async function newKey(cookie) {
  return (await (await form(cookie, "/dashboard/key")).text()).match(/jsk_[A-Za-z0-9_-]+/)[0];
}
// "none": no plan (HTTP 402); otherwise the plan /v1/usage reports (features are the same on every plan).
const planOf = async (key) => {
  const r = await fetch(BASE + "/v1/usage", { headers: { authorization: `Bearer ${key}` } });
  return r.status === 402 ? "none" : (await r.json()).plan;
};

const exec = promisify(execFile);
async function sql(command) {
  const persist = process.env.D1_PERSIST_TO ? ["--persist-to", process.env.D1_PERSIST_TO] : [];
  let failure;
  for (let attempt = 0; attempt < 4; attempt++) {
    try {
      const { stdout } = await exec(
        "npx",
        ["wrangler", "d1", "execute", "japanfilings", "--local", ...persist, "--json", "--command", command],
        { env: { ...process.env, WRANGLER_LOG_PATH: process.env.WRANGLER_LOG_PATH ?? `/tmp/jf-wrangler-test-${randomUUID()}.log` } },
      );
      return JSON.parse(stdout)[0].results;
    } catch (err) {
      failure = err;
      await new Promise((resolve) => setTimeout(resolve, 100 * (attempt + 1)));
    }
  }
  throw failure;
}
const ownerOf = async (key) => (await sql(`SELECT user_id FROM api_keys WHERE key_prefix = '${key.slice(0, 7)}' AND revoked = 0`))[0].user_id;

test("API responses carry RateLimit headers", async () => {
  const key = process.env.FREE_KEY;
  if (!key) return;
  const r = await fetch(BASE + "/v1/companies/7203", { headers: { authorization: `Bearer ${key}` } });
  assert.equal(r.headers.get("ratelimit-limit"), "20000");
  assert.ok(Number(r.headers.get("ratelimit-remaining")) >= 0);
  assert.ok(Number(r.headers.get("ratelimit-reset")) > 0);
});

test("over quota: 429 with Retry-After, and the limit is unchanged", async () => {
  const cookie = await signIn();
  const key = await newKey(cookie);
  await fetch(BASE + "/v1/status");
  const month = new Date().toISOString().slice(0, 7);
  const auth = { headers: { authorization: `Bearer ${key}` } };
  assert.equal((await fetch(BASE + "/v1/usage", auth)).status, 200);
  await sql(`UPDATE usage SET requests = 20000 WHERE month = '${month}' AND key_hash = '${await ownerOf(key)}'`);
  const r = await fetch(BASE + "/v1/usage", auth);
  assert.equal(r.status, 429);
  assert.ok(Number(r.headers.get("retry-after")) > 0);
  assert.equal(r.headers.get("ratelimit-remaining"), "0");
  const m = await fetch(BASE + "/mcp", { method: "POST", ...auth, body: "{}" });
  assert.equal(m.status, 429);
  assert.ok(Number(m.headers.get("retry-after")) > 0);
});

test("account quota survives key rotation", async () => {
  const cookie = await signIn();
  const firstKey = await newKey(cookie);
  const first = await fetch(BASE + "/v1/usage", { headers: { authorization: `Bearer ${firstKey}` } });
  const used = Number(first.headers.get("x-quota-used"));
  const secondKey = await newKey(cookie);
  const second = await fetch(BASE + "/v1/usage", { headers: { authorization: `Bearer ${secondKey}` } });
  assert.equal(Number(second.headers.get("x-quota-used")), used + 1);
});

test("unknown plans are rejected", async () => {
  const cookie = await signIn();
  const key = await newKey(cookie);
  await sql(`UPDATE api_keys SET plan = 'enterprise' WHERE key_prefix = '${key.slice(0, 7)}' AND revoked = 0`);
  assert.equal((await fetch(BASE + "/v1/usage", { headers: { authorization: `Bearer ${key}` } })).status, 401);
});

test("batch reserves both monthly quota and RPM before reading data", async () => {
  const cookie = await signIn();
  const key = await newKey(cookie);
  const headers = { authorization: `Bearer ${key}` };
  await fetch(BASE + "/v1/usage", { headers });
  const owner = await ownerOf(key);
  const month = new Date().toISOString().slice(0, 7);
  await sql(`UPDATE usage SET requests = 19999, minute_count = 1 WHERE key_hash = '${owner}' AND month = '${month}'`);
  assert.equal((await fetch(BASE + "/v1/batch/companies?tickers=7203,6758", { headers })).status, 429);
  assert.equal((await sql(`SELECT requests FROM usage WHERE key_hash = '${owner}' AND month = '${month}'`))[0].requests, 20000);
  await sql(`UPDATE usage SET requests = 10, minute_count = 58 WHERE key_hash = '${owner}' AND month = '${month}'`);
  assert.equal((await fetch(BASE + "/v1/batch/companies?tickers=7203,6758,4042", { headers })).status, 429);
});

if (!SECRET) {
  test("closed: webhook is 503 and buttons say paid plans are not open", async () => {
    const res = await post("{}", "ts=1;h1=00");
    assert.equal(res.status, 503);
    assert.match(await (await fetch(BASE + "/pricing")).text(), /Paid plans are not open yet/);
    const cookie = await signIn();
    assert.equal((await form(cookie, "/billing/checkout", { plan: "pro" })).status, 503);
    assert.equal((await form(cookie, "/billing/portal")).status, 503);
    assert.match(await (await fetch(BASE + "/dashboard", { headers: { cookie } })).text(), /You are on Starter|Plans go on sale at launch/);
  });
} else {
  const calls = [];
  let mock;
  before(async () => {
    mock = createServer(async (req, res) => {
      let raw = "";
      for await (const chunk of req) raw += chunk;
      calls.push({ path: req.url, auth: req.headers.authorization, body: JSON.parse(raw || "{}") });
      res.setHeader("content-type", "application/json");
      res.statusCode = 201;
      res.end(JSON.stringify(req.url === "/transactions"
        ? { data: { id: "txn_test" } }
        : { data: { urls: { general: { overview: "https://sandbox-customer-portal.paddle.com/cpl_test/overview?token=t" } } } }));
    }).listen(MOCK_PORT, "127.0.0.1");
    await new Promise((resolve) => mock.once("listening", resolve));
  });
  after(() => mock.close());

  // Checks out through the mock, which also reveals our user ID as sent in custom_data.
  async function subscriber() {
    const cookie = await signIn();
    const key = await newKey(cookie);
    const res = await form(cookie, "/billing/checkout", { plan: "pro" });
    assert.equal(res.status, 303);
    assert.equal(res.headers.get("location"), "https://sandbox.pay.paddle.io/checkout/hsc_test?transaction_id=txn_test");
    const call = calls.at(-1);
    assert.equal(call.auth, "Bearer test");
    assert.deepEqual(call.body.items, [{ price_id: "pri_pro", quantity: 1 }]);
    return { cookie, key, userId: call.body.custom_data.user_id, sub: "sub_" + randomUUID() };
  }

  test("open: pricing offers checkout; bad plan and other origins are refused", async () => {
    assert.doesNotMatch(await (await fetch(BASE + "/pricing")).text(), /not open yet/);
    const cookie = await signIn();
    for (const plan of ["enterprise", "business", "free"]) assert.equal((await form(cookie, "/billing/checkout", { plan })).status, 400, plan);
    const r = await fetch(BASE + "/billing/checkout", { method: "POST", redirect: "manual", headers: { cookie, origin: "https://evil.example", "content-type": "application/x-www-form-urlencoded" }, body: "plan=pro" });
    assert.equal(r.status, 403);
  });

  test("open: checkout is refused from the EU, EEA, UK and Switzerland; renewal terms are shown", async () => {
    const pricing = await (await fetch(BASE + "/pricing")).text();
    assert.match(pricing, /renews automatically every month/);
    assert.match(pricing, /href="\/legal"/);
    assert.match(await (await fetch(BASE + "/legal")).text(), /契約期間・自動更新/);
    const cookie = await signIn();
    const before = calls.length;
    for (const country of ["DE", "GB", "CH", "NO", "re"]) {
      const res = await fetch(BASE + "/billing/checkout", { method: "POST", redirect: "manual", headers: { cookie, "cf-ipcountry": country, "content-type": "application/x-www-form-urlencoded" }, body: "plan=pro" });
      assert.equal(res.status, 403, country);
    }
    assert.equal(calls.length, before, "no transaction is created");
    const ok = await fetch(BASE + "/billing/checkout", { method: "POST", redirect: "manual", headers: { cookie, "cf-ipcountry": "US", "content-type": "application/x-www-form-urlencoded" }, body: "plan=pro" });
    assert.equal(ok.status, 303);
  });

  test("valid signature upgrades the existing key without rotation", async () => {
    const { key, userId, sub } = await subscriber();
    assert.equal(await planOf(key), "starter");
    const res = await deliver(event("subscription.created", sub, userId));
    assert.equal(res.status, 200);
    assert.equal((await res.json()).action, "applied");
    assert.equal(await planOf(key), "pro");
    await deliver(event("subscription.updated", sub, userId, { price: "pri_starter" }));
    assert.equal(await planOf(key), "starter");
    const audit = await sql(`SELECT action, old_plan, new_plan FROM billing_audit WHERE subscription_id = '${sub}' ORDER BY id`);
    assert.deepEqual(audit, [{ action: "applied", old_plan: "starter", new_plan: "pro" }, { action: "applied", old_plan: "pro", new_plan: "starter" }]);
  });

  test("bad signatures are rejected and change nothing", async () => {
    const { key, userId, sub } = await subscriber();
    const body = event("subscription.created", sub, userId);
    for (const sig of [undefined, "garbage", "ts=1;h1=zz", sign(body, "wrong_secret"), sign(body + " ")]) {
      assert.equal((await post(body, sig)).status, 401, String(sig));
    }
    assert.equal(await planOf(key), "starter");
  });

  test("stale and future timestamps are rejected", async () => {
    const { key, userId, sub } = await subscriber();
    const body = event("subscription.created", sub, userId);
    const now = Math.floor(Date.now() / 1000);
    assert.equal((await post(body, sign(body, SECRET, now - 301))).status, 401);
    assert.equal((await post(body, sign(body, SECRET, now + 301))).status, 401);
    assert.equal(await planOf(key), "starter");
    assert.equal((await post(body, sign(body, SECRET, now - 60))).status, 200);
    assert.equal(await planOf(key), "pro");
  });

  test("a duplicate event ID is applied once", async () => {
    const { key, userId, sub } = await subscriber();
    await deliver(event("subscription.created", sub, userId));
    const cancel = event("subscription.canceled", sub, userId, { status: "canceled" });
    assert.equal((await (await deliver(cancel)).json()).action, "applied");
    await deliver(event("subscription.updated", sub, userId));
    assert.equal(await planOf(key), "pro");
    const again = await deliver(cancel);
    assert.equal(again.status, 200);
    assert.equal((await again.json()).duplicate, true);
    assert.equal(await planOf(key), "pro");
    assert.equal((await sql(`SELECT COUNT(*) AS n FROM billing_audit WHERE subscription_id = '${sub}'`))[0].n, 3);
  });

  test("older events arriving late do not undo newer ones", async () => {
    const { key, userId, sub } = await subscriber();
    const old = new Date(clock - 60_000).toISOString();
    await deliver(event("subscription.canceled", sub, userId, { status: "canceled" }));
    await deliver(event("subscription.created", sub, userId));
    assert.equal(await planOf(key), "pro");
    const res = await deliver(event("subscription.canceled", sub, userId, { status: "canceled", at: old }));
    assert.equal((await res.json()).action, "stale_ignored");
    assert.equal(await planOf(key), "pro");
  });

  test("cancellation downgrades at period end, not before; the key survives", async () => {
    const { cookie, key, userId, sub } = await subscriber();
    await deliver(event("subscription.created", sub, userId));
    const end = new Date(Date.now() + 86_400_000 * 20).toISOString();
    await deliver(event("subscription.updated", sub, userId, { scheduled: { action: "cancel", effective_at: end, resume_at: null } }));
    assert.equal(await planOf(key), "pro");
    assert.match(await (await fetch(BASE + "/dashboard", { headers: { cookie } })).text(), new RegExp(`ends on ${end.slice(0, 10)}`));
    await deliver(event("subscription.canceled", sub, userId, { status: "canceled" }));
    assert.equal(await planOf(key), "none");
  });

  test("past due keeps access and warns; paused removes it", async () => {
    const { cookie, key, userId, sub } = await subscriber();
    await deliver(event("subscription.created", sub, userId));
    await deliver(event("subscription.past_due", sub, userId, { status: "past_due" }));
    assert.equal(await planOf(key), "pro");
    assert.match(await (await fetch(BASE + "/dashboard", { headers: { cookie } })).text(), /last payment failed/);
    await deliver(event("subscription.paused", sub, userId, { status: "paused" }));
    assert.equal(await planOf(key), "none");
  });

  test("an elapsed billing period removes access without a cancellation webhook", async () => {
    const { key, userId, sub } = await subscriber();
    await deliver(event("subscription.created", sub, userId));
    assert.equal(await planOf(key), "pro");
    await deliver(event("subscription.updated", sub, userId, { periodEnd: new Date(Date.now() - 60_000).toISOString() }));
    assert.equal(await planOf(key), "none");
  });

  test("an unknown price keeps an existing paid plan and is audited; it grants nothing to a new subscription", async () => {
    const { key, userId, sub } = await subscriber();
    await deliver(event("subscription.created", sub, userId));
    assert.equal(await planOf(key), "pro");
    // Price missing from our settings (configuration drift): no downgrade, flagged in the audit.
    const res = await deliver(event("subscription.updated", sub, userId, { price: "pri_other" }));
    assert.equal((await res.json()).action, "unknown_price");
    assert.equal(await planOf(key), "pro");
    assert.deepEqual(await sql(`SELECT action, old_plan, new_plan FROM billing_audit WHERE subscription_id = '${sub}' ORDER BY id DESC LIMIT 1`),
      [{ action: "unknown_price", old_plan: "pro", new_plan: "pro" }]);
    // A new subscription on a price not on sale (an old Business price included) grants nothing.
    const other = await subscriber();
    assert.equal((await (await deliver(event("subscription.created", other.sub, other.userId, { price: "pri_biz" }))).json()).action, "unknown_price");
    assert.equal(await planOf(other.key), "none");
  });

  test("an unregistered user ID is retried; an event with no user ID is acknowledged", async () => {
    const id = "evt_" + randomUUID();
    const nobody = await deliver(event("subscription.created", "sub_" + randomUUID(), "no-such-user", { id }));
    assert.equal(nobody.status, 503);
    assert.equal((await sql(`SELECT COUNT(*) AS n FROM billing_events WHERE event_id = '${id}'`))[0].n, 0);
    const body = JSON.parse(event("subscription.created", "sub_" + randomUUID(), null, { id: "evt_" + randomUUID() }));
    delete body.data.custom_data;
    const res = await deliver(JSON.stringify(body));
    assert.equal(res.status, 200);
    assert.equal((await res.json()).ignored, true);
    assert.deepEqual(await sql(`SELECT action FROM billing_audit WHERE event_id = '${body.event_id}'`), [{ action: "unmatched_user" }]);
  });

  const adjustment = (sub, { action = "refund", status = "approved", type = "full", id = "evt_" + randomUUID(), type_ = "adjustment.updated" } = {}) => {
    clock += 1000;
    return JSON.stringify({
      event_id: id, event_type: type_, occurred_at: new Date(clock).toISOString(),
      data: { id: "adj_" + randomUUID(), action, status, type, subscription_id: sub, customer_id: "ctm_test", transaction_id: "txn_test" },
    });
  };

  test("an approved full refund or a chargeback removes access; a renewal restores it", async () => {
    const { key, userId, sub } = await subscriber();
    const periodEnd = new Date(Date.now() + 86_400_000 * 20).toISOString();
    await deliver(event("subscription.created", sub, userId, { periodEnd }));
    // Pending and partial refunds change nothing.
    for (const opts of [{ status: "pending_approval", type_: "adjustment.created" }, { type: "partial" }]) {
      assert.equal((await (await deliver(adjustment(sub, opts))).json()).ignored, true);
      assert.equal(await planOf(key), "pro");
    }
    assert.equal((await (await deliver(adjustment(sub))).json()).action, "refunded");
    assert.equal(await planOf(key), "none");
    // The subscription is cancelled at Paddle at once, so it is not charged again.
    const cancel = calls.at(-1);
    assert.equal(cancel.path, `/subscriptions/${sub}/cancel`);
    assert.deepEqual(cancel.body, { effective_from: "immediately" });
    // Should Paddle still send an update the subscription active: its next update in the same period does not restore access.
    const same = await (await deliver(event("subscription.updated", sub, userId, { periodEnd }))).json();
    assert.equal(same.action, "revoked_kept");
    assert.equal(await planOf(key), "none");
    // A paid renewal (a later period) does.
    await deliver(event("subscription.updated", sub, userId, { periodEnd: new Date(Date.now() + 86_400_000 * 50).toISOString() }));
    assert.equal(await planOf(key), "pro");
    assert.equal((await (await deliver(adjustment(sub, { action: "chargeback", type_: "adjustment.created" }))).json()).action, "charged_back");
    assert.equal(await planOf(key), "none");
    // A refund of something we never linked is acknowledged.
    assert.equal((await (await deliver(adjustment("sub_" + randomUUID()))).json()).ignored, true);
  });

  test("a refund after the account was deleted still cancels the subscription at Paddle", async () => {
    const { userId, sub } = await subscriber();
    await deliver(event("subscription.created", sub, userId));
    await sql(`DELETE FROM api_keys WHERE user_id = '${userId}'`);
    await sql(`DELETE FROM users WHERE id = '${userId}'`);
    const res = await deliver(adjustment(sub));
    assert.equal(res.status, 200);
    assert.equal((await res.json()).action, "refunded_deleted_account");
    assert.equal(calls.at(-1).path, `/subscriptions/${sub}/cancel`);
  });

  test("a live subscription of a deleted account is recorded and cancelled, not retried", async () => {
    const { userId, sub } = await subscriber();
    await deliver(event("subscription.created", sub, userId));
    await sql(`DELETE FROM api_keys WHERE user_id = '${userId}'`);
    await sql(`DELETE FROM users WHERE id = '${userId}'`);
    const before = calls.length;
    const res = await deliver(event("subscription.updated", sub, userId));
    assert.equal(res.status, 200);
    assert.equal((await res.json()).action, "deleted_account");
    assert.equal(calls.length, before + 1);
    assert.equal(calls.at(-1).path, `/subscriptions/${sub}/cancel`);
  });

  test("live subscriptions do not grant sandbox access", async () => {
    const { key, userId, sub } = await subscriber();
    const now = new Date().toISOString();
    const end = new Date(Date.now() + 86_400_000).toISOString();
    await sql(`INSERT INTO billing_subscriptions (provider, subscription_id, user_id, customer_id, plan, status, current_period_end, last_event_at, updated_at) VALUES ('paddle_live','${sub}','${userId}','ctm_live','pro','active','${end}','${now}','${now}')`);
    await sql(`UPDATE users SET plan = 'pro' WHERE id = '${userId}'; UPDATE api_keys SET plan = 'pro' WHERE user_id = '${userId}'`);
    assert.equal(await planOf(key), "none");
  });

  test("subscribers reach the portal, also instead of a second checkout", async () => {
    const { cookie, userId, sub } = await subscriber();
    await deliver(event("subscription.created", sub, userId));
    const portal = await form(cookie, "/billing/portal");
    assert.equal(portal.status, 303);
    assert.match(portal.headers.get("location"), /customer-portal\.paddle\.com/);
    assert.equal(calls.at(-1).path, "/customers/ctm_test/portal-sessions");
    const second = await form(cookie, "/billing/checkout", { plan: "starter" });
    assert.match(second.headers.get("location"), /customer-portal\.paddle\.com/);
  });

  test("other signed events are acknowledged and ignored", async () => {
    const res = await deliver(JSON.stringify({ event_id: "evt_x", event_type: "customer.updated", occurred_at: new Date().toISOString(), data: {} }));
    assert.equal(res.status, 200);
    assert.equal((await res.json()).ignored, true);
  });
}
