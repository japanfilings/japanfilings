import * as contactForm from "./contact";
import { Hono } from "hono";
import { deleteCookie, getCookie, setCookie } from "hono/cookie";
import { cors } from "hono/cors";
import { secureHeaders } from "hono/secure-headers";
import * as accounts from "./accounts";
import { authorize, minuteHeaders, readKey, type AuthOk } from "./auth";
import * as billing from "./billing";
import * as data from "./data";
import * as freshness from "./freshness";
import * as ondemand from "./ondemand";
import { DEV_PATHS, devRoutes, llmsFullTxt, llmsTxt } from "./devapi";
import { CORS_EXPOSE, dataResponse } from "./format";
import { handleMcp } from "./mcp";
import * as ownership from "./ownership";
import * as oauth from "./oauth";
import { rebuildFacts, resolveSector } from "./screener";
import { filerBlocked } from "./privacy";
import * as site from "./site";
import * as webhooks from "./webhooks";

type Bindings = accounts.AccountEnv & billing.BillingEnv & oauth.OAuthEnv & site.LegalEnv & { CONTACT_EMAIL?: string; EDINET_API_KEY?: string };
type Env = { Bindings: Bindings; Variables: { plan: data.Plan; owner: string; user: accounts.User | null; auth: AuthOk } };

const app = new Hono<Env>();

const clampLimit = (v: string | undefined, max = 200) => Math.min(Math.max(Number(v) || 50, 1), max);
const origin = (url: string) => new URL(url).origin;

// ---------- Website ----------

app.use(
  "*",
  secureHeaders({
    contentSecurityPolicy: {
      defaultSrc: ["'self'"],
      styleSrc: ["'self'", "'unsafe-inline'", "https://fonts.googleapis.com"],
      fontSrc: ["https://fonts.gstatic.com"],
      imgSrc: ["'self'", "data:"],
      scriptSrc: ["'none'"],
      // Checkout and portal buttons POST here and redirect to Paddle; browsers apply form-action to the redirect too.
      formAction: ["'self'", "https://*.paddle.io", "https://*.paddle.com"],
      frameAncestors: ["'none'"],
    },
    referrerPolicy: "strict-origin-when-cross-origin",
  }),
);
// The API is called from servers and agents; browsers may call it too, with the key in a header.
app.use("/v1/*", cors({ origin: "*", allowHeaders: ["Authorization", "X-API-Key", "Content-Type"], exposeHeaders: ["X-Quota-Used", "X-Quota-Limit", "RateLimit-Limit", "RateLimit-Remaining", "RateLimit-Reset", "Retry-After", ...CORS_EXPOSE] }));

app.use("*", async (c, next) => {
  c.set("user", await accounts.currentUser(c.env, getCookie(c, accounts.SESSION_COOKIE)));
  await next();
});

// Forms must come from our own pages (session cookies are SameSite=Lax; this closes the rest).
const sameOrigin = (c: any) => {
  const o = c.req.header("origin");
  return !o || o === origin(c.req.url);
};

const contact = (c: any) => c.env.CONTACT_EMAIL ?? null;

app.get("/", async (c) => {
  const db = c.env.DB;
  const [stats, ev, toyota, trades, funds, brief] = await Promise.all([
    db.prepare(
      "SELECT (SELECT COUNT(DISTINCT ticker) FROM annual_reports) AS companies, (SELECT COUNT(*) FROM annual_reports) AS reports, " +
        "(SELECT COUNT(*) FROM large_holdings) AS filings5, (SELECT COUNT(*) FROM ownership_events) AS events, " +
        "(SELECT COUNT(*) FROM cross_holdings WHERE held_ticker IS NOT NULL) AS crossHoldings, (SELECT MAX(submit_date) FROM large_holdings) AS latest, (SELECT MIN(submit_date) FROM large_holdings) AS earliest",
    ).first<site.SiteStats>(),
    data.ownershipEvents(db, "business", { types: ["new_stake", "stake_increase", "stated_may_make_proposals", "threshold_crossed", "exit_below_5pct"], limit: 8 }),
    data.crossHoldings(db, "7203"),
    data.holderTrades(db, { ticker: "4042", limit: 50 }),
    data.usFunds(db, "7203"),
    data.companyBrief(db, "7203"),
  ]);
  return c.html(site.landing(c.get("user"), origin(c.req.url), stats!, ev.data, toyota, { trades, funds, brief, billingOpen: billing.billingOpen(c.env) }));
});
app.get("/pricing", async (c) => {
  const row = await c.env.DB.prepare("SELECT MIN(submit_date) AS earliest FROM large_holdings").first<{ earliest: string | null }>();
  return c.html(site.pricing(c.get("user"), row?.earliest ?? null, billing.billingOpen(c.env)));
});
app.get("/docs", (c) => c.html(site.docs(c.get("user"), OPENAPI, origin(c.req.url))));
app.get("/sources", (c) => c.html(site.sources(c.get("user"))));
app.get("/terms", (c) => c.html(site.terms(c.get("user"), contact(c))));
app.get("/privacy", (c) => c.html(site.privacy(c.get("user"), contact(c))));
app.get("/contact", (c) => c.html(site.contactPage(c.get("user"))));
app.post("/contact", async (c) => {
  if (!sameOrigin(c)) return c.text("Forbidden", 403);
  const form = await c.req.parseBody();
  const r = await contactForm.submit(c.env, form, c.req.header("cf-connecting-ip") ?? "unknown");
  if (r.ok) return c.html(site.contactPage(c.get("user"), { sent: true }));
  const keep = Object.fromEntries(["kind", "page", "message", "email"].map((k) => [k, String(form[k] ?? "")]));
  return c.html(site.contactPage(c.get("user"), { error: r.error, form: keep }), r.status);
});
// Off until the owner has filled in the blanks and switched it on.
app.get("/legal", (c) => (site.legalReady(c.env) ? c.html(site.legalPage(c.get("user"), c.env)) : c.html(site.notFoundPage(c.get("user")), 404)));
app.get("/status", async (c) => c.html(site.statusPage(c.get("user"), await freshness.freshnessBody(c.env.DB))));
app.get("/investor-groups/:id", async (c) => {
  const group = await ownership.getGroup(c.env.DB, c.req.param("id"));
  if (!group) return c.html(site.notFoundPage(c.get("user"), "investor"), 404);
  return c.html(site.investorGroupPage(c.get("user"), group, await ownership.portfolio(c.env.DB, c.req.param("id"), { limit: 200 })));
});

// Public pages show 30 days of changes; the API has the full history on every plan.
const PUBLIC_EVENT_DAYS = 30;
app.get("/events", async (c) => {
  const type = c.req.query("type") ?? "";
  const valid = (data.EVENT_TYPES as readonly string[]).includes(type);
  const since = new Date(Date.now() - PUBLIC_EVENT_DAYS * 86_400_000).toISOString().slice(0, 10);
  const ev = await data.ownershipEvents(c.env.DB, "business", { types: valid ? [type] : undefined, since, limit: 100 });
  return c.html(site.eventsPage(c.get("user"), ev.data, valid ? type : "", since));
});

app.get("/cross-holdings", async (c) => {
  const res = await data.crossHoldingUnwinding(c.env.DB, 30);
  return c.html(site.crossHoldingsPage(c.get("user"), res.data));
});

app.get("/indicators", async (c) => {
  const [idx, st, pu, pr] = await Promise.all([
    data.policyUncertaintyIndex(c.env.DB),
    data.sortedByMetric(c.env.DB, "roe", "desc", 15),
    data.sortedByMetric(c.env.DB, "policy_uncertainty", "desc", 15),
    data.sortedByMetric(c.env.DB, "new_5pct_holders_180d", "desc", 15),
  ]);
  return c.html(site.indicatorsPage(c.get("user"), idx.data, st.data, pu.data, pr.data));
});

app.get("/search", async (c) => {
  const q = (c.req.query("q") ?? "").trim().slice(0, 80);
  const results: any[] = q ? await data.searchCompanies(c.env.DB, q, 50) : [];
  if (results.length === 1 && results[0].ticker === q.toUpperCase()) return c.redirect(`/companies/${results[0].ticker}`);
  return c.html(site.searchPage(c.get("user"), q, results));
});

app.get("/companies/:ticker", async (c) => {
  const t = c.req.param("ticker").toUpperCase();
  let company: any = await data.getCompany(c.env.DB, t);
  if (!company) {
    // Newly listed issuers can appear in 5% filings before they reach the EDINET code list.
    const first = (await data.ownershipEvents(c.env.DB, "business", { ticker: t, limit: 1 })).data[0] as any;
    // Cross-holdings can name a ticker that the code list has dropped; links to it stay valid.
    const held = first ? null : await c.env.DB.prepare("SELECT name_ja FROM cross_holdings WHERE held_ticker = ?1 LIMIT 1").bind(t).first<{ name_ja: string }>();
    if (!first && !held) return c.html(site.notFoundPage(c.get("user"), "company"), 404);
    company = { ticker: t, name_en: first?.issuer_name_en ?? null, name_ja: first?.issuer_name_ja ?? held!.name_ja, yahoo_symbol: `${t}.T`, bloomberg_ticker: `${t} JP` };
  }
  const [fin, det, lh, funds, xh, ev, tr, ind, rel] = await Promise.all([
    data.financials(c.env.DB, t),
    data.companyDetails(c.env.DB, t),
    data.largeHoldings(c.env.DB, "business", { ticker: t, limit: 10 }),
    data.usFunds(c.env.DB, t),
    data.crossHoldings(c.env.DB, t),
    data.ownershipEvents(c.env.DB, "business", { ticker: t, limit: 10 }),
    data.holderTrades(c.env.DB, { ticker: t, limit: 400 }),
    data.companyIndicators(c.env.DB, t),
    data.earningsReleases(c.env.DB, t, 3),
  ]);
  const extra = await Promise.all([data.disclosedRatings(c.env.DB, t, 6), data.buybacks(c.env.DB, t, 1), data.governanceEvents(c.env.DB, { ticker: t, limit: 4 }), data.filingDetail(c.env.DB, t, "segments"), data.votingResults(c.env.DB, { ticker: t, limit: 4 }), data.tenderOffers(c.env.DB, { ticker: t, limit: 4 })]);
  return c.html(site.companyPage(c.get("user"), company, fin, det, ("data" in lh && lh.data) || [], funds, xh, ev.data, tr, ind, rel.data[0] ?? null, { ratings: extra[0], buyback: extra[1].data[0], governance: extra[2].data, segments: extra[3], voting: extra[4].data, tenderOffers: extra[5].data }));
});

app.get("/investors/:code", async (c) => {
  const inv = await data.investor(c.env.DB, c.req.param("code"), 200);
  if (!inv) {
    // Old links to natural persons stay valid but show nothing about them.
    const hidden = await c.env.DB.prepare("SELECT 1 FROM investor_excluded_filers WHERE edinet_code = ?1").bind(c.req.param("code")).first();
    return hidden ? c.html(site.notFoundPage(c.get("user"), "investor")) : c.html(site.notFoundPage(c.get("user"), "investor"), 404);
  }
  return c.html(site.investorPage(c.get("user"), inv));
});

app.get("/favicon.svg", (c) => c.body(site.FAVICON, 200, { "content-type": "image/svg+xml", "cache-control": "public, max-age=86400" }));
app.get("/favicon.ico", (c) => c.redirect("/favicon.svg", 301));
app.get("/robots.txt", (c) =>
  c.text(`User-agent: *\nDisallow: /dashboard\nDisallow: /login\nDisallow: /search\nDisallow: /v1/\nSitemap: ${origin(c.req.url)}/sitemap.xml\n`),
);
app.get("/sitemap.xml", async (c) => {
  const o = origin(c.req.url);
  const { results } = await c.env.DB.prepare("SELECT DISTINCT ticker FROM annual_reports WHERE ticker IS NOT NULL ORDER BY ticker").all<{ ticker: string }>();
  const filers = await c.env.DB.prepare("SELECT DISTINCT l.filer_edinet_code AS code FROM large_holdings l WHERE l.filer_edinet_code IS NOT NULL AND NOT EXISTS (SELECT 1 FROM investor_excluded_filers x WHERE x.edinet_code = l.filer_edinet_code) ORDER BY code").all<{ code: string }>();
  const groups = await c.env.DB.prepare("SELECT group_id FROM investor_groups ORDER BY group_id").all<{ group_id: string }>();
  const urls = ["/", "/events", "/indicators", "/status", "/cross-holdings", "/docs", "/pricing", "/sources", "/terms", "/privacy", "/contact", ...(site.legalReady(c.env) ? ["/legal"] : []), ...results.map((r) => `/companies/${r.ticker}`), ...filers.results.map((r) => `/investors/${r.code}`), ...groups.results.map((r) => `/investor-groups/${r.group_id}`)];
  return c.body(
    `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">${urls.map((u) => `<url><loc>${o}${u}</loc></url>`).join("")}</urlset>`,
    200,
    { "content-type": "application/xml" },
  );
});

app.get("/login", (c) => (c.get("user") ? c.redirect("/dashboard") : c.html(site.loginPage(null))));

app.post("/login", async (c) => {
  if (!sameOrigin(c)) return c.text("Forbidden", 403);
  const email = String((await c.req.parseBody()).email ?? "").trim().toLowerCase();
  if (!accounts.validEmail(email)) return c.html(site.loginPage(null, "Please enter a valid email address."), 400);
  const res = await accounts.startLogin(c.env, email, origin(c.req.url));
  const msg = res.throttled
    ? `We already sent several links to ${email}. Use the latest one, or try again in 15 minutes.`
    : res.sent ? `We sent a sign-in link to ${email}. It is valid for 20 minutes.` : res.devLink ? "" : "Email is not configured on this server yet.";
  return c.html(site.loginPage(null, msg, res.devLink));
});

app.get("/auth/verify", async (c) => {
  const session = await accounts.finishLogin(c.env, c.req.query("token") ?? "");
  if (!session) return c.html(site.loginPage(null, "That link has expired or was already used. Request a new one."), 400);
  setCookie(c, accounts.SESSION_COOKIE, session, {
    httpOnly: true, secure: new URL(c.req.url).protocol === "https:", sameSite: "Lax", path: "/", maxAge: 30 * 86400,
  });
  const resume = oauth.resumePath(c.env, getCookie(c, oauth.PENDING_COOKIE_NAME));
  return c.redirect(resume ?? "/dashboard");
});

app.route("/", oauth.oauthRoutes);

app.get("/llms.txt", (c) => c.text(llmsTxt(OPENAPI as any, origin(c.req.url))));
app.get("/llms-full.txt", (c) => c.text(llmsFullTxt(OPENAPI as any, origin(c.req.url))));

app.post("/logout", async (c) => {
  if (!sameOrigin(c)) return c.text("Forbidden", 403);
  await accounts.endSession(c.env, getCookie(c, accounts.SESSION_COOKIE));
  deleteCookie(c, accounts.SESSION_COOKIE, { path: "/" });
  return c.redirect("/");
});

const dashboardKeyStatus = async (env: Bindings, user: accounts.User) => {
  const status = await accounts.keyStatus(env, user);
  if (!status) return null;
  const usage = await env.DB.prepare("SELECT requests FROM usage WHERE key_hash = ?1 AND month = ?2")
    .bind(user.id, new Date().toISOString().slice(0, 7))
    .first<{ requests: number }>();
  return { ...status, requests: usage?.requests ?? status.requests };
};

app.get("/dashboard", async (c) => {
  const user = c.get("user");
  if (!user) return c.redirect("/login");
  return c.html(site.dashboardPage(user, await dashboardKeyStatus(c.env, user), undefined, await webhooks.list(c.env.DB, user.id), origin(c.req.url), await billing.billingView(c.env, user.id)));
});

app.post("/dashboard/key", async (c) => {
  const user = c.get("user");
  if (!user) return c.redirect("/login");
  if (!sameOrigin(c)) return c.text("Forbidden", 403);
  const key = await accounts.rotateKey(c.env, user);
  return c.html(site.dashboardPage(user, await dashboardKeyStatus(c.env, user), key, await webhooks.list(c.env.DB, user.id), origin(c.req.url), await billing.billingView(c.env, user.id)));
});

// ---------- Billing ----------

const NOT_OPEN = "Paid plans are not open yet.";
const PROVIDER_DOWN = "The payment provider did not respond. Nothing was charged; please try again.";

app.post("/billing/checkout", async (c) => {
  const user = c.get("user");
  if (!user) return c.redirect("/login", 303);
  if (!sameOrigin(c)) return c.text("Forbidden", 403);
  if (!billing.billingOpen(c.env)) return c.text(NOT_OPEN, 503);
  const plan = String((await c.req.parseBody()).plan ?? "");
  if (plan !== "starter" && plan !== "pro") return c.text("Unknown plan.", 400);
  // A second subscription would bill twice, so subscribers go to the portal instead.
  const view = await billing.billingView(c.env, user.id);
  try {
    if (view.live && view.customerId) return c.redirect(await billing.provider.portalUrl(c.env, view.customerId), 303);
    return c.redirect(await billing.provider.checkoutUrl(c.env, plan, user.id), 303);
  } catch (err) {
    console.error(err);
    return c.text(PROVIDER_DOWN, 502);
  }
});

app.post("/billing/portal", async (c) => {
  const user = c.get("user");
  if (!user) return c.redirect("/login", 303);
  if (!sameOrigin(c)) return c.text("Forbidden", 403);
  if (!billing.billingOpen(c.env)) return c.text(NOT_OPEN, 503);
  const view = await billing.billingView(c.env, user.id);
  if (!view.customerId) return c.redirect("/pricing", 303);
  try {
    return c.redirect(await billing.provider.portalUrl(c.env, view.customerId), 303);
  } catch (err) {
    console.error(err);
    return c.text(PROVIDER_DOWN, 502);
  }
});

// The signature covers the exact bytes, so the body is read as text and parsed only after verifying.
app.post("/billing/webhook", async (c) => {
  const res = await billing.handleWebhook(c.env, await c.req.text(), c.req.raw.headers);
  return c.json(res.body, res.status as 200);
});

// ---------- API ----------

app.get("/v1/status", async (c) => c.json(await freshness.statusBody(c.env.DB)));
app.get("/v1/freshness", async (c) => c.json(await freshness.freshnessBody(c.env.DB)));

// The only limit is the monthly quota, which resets at the start of the next UTC month.
const secondsToReset = () => {
  const now = new Date();
  return Math.ceil((Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1) - now.getTime()) / 1000);
};
const quotaHeaders = (used: number, quota: number) => ({
  "RateLimit-Limit": String(quota),
  "RateLimit-Remaining": String(Math.max(quota - used, 0)),
  "RateLimit-Reset": String(secondsToReset()),
});
const exceededHeaders = (status: number): Record<string, string> =>
  status === 429 ? { "Retry-After": String(secondsToReset()), "RateLimit-Remaining": "0", "RateLimit-Reset": String(secondsToReset()) } : {};

// A refusal by the per-minute limit waits seconds, not until the next month.
const limitHeaders = (auth: { status: number; retryAfter?: number; rpm?: number }): Record<string, string> =>
  auth.retryAfter
    ? { "Retry-After": String(auth.retryAfter), "X-RateLimit-Limit-Minute": String(auth.rpm), "X-RateLimit-Remaining-Minute": "0" }
    : exceededHeaders(auth.status);

// Everything else under /v1 needs an API key.
app.use("/v1/*", async (c, next) => {
  if (c.req.path === "/v1/status" || c.req.path === "/v1/freshness") return next();
  const auth = await authorize(c.env.DB, readKey(c.req.raw), billing.providerKey(c.env));
  if (!auth.ok) return c.json({ error: auth.error }, auth.status, limitHeaders(auth));
  c.set("auth", auth);
  c.set("plan", auth.plan);
  c.set("owner", auth.owner);
  c.header("X-Quota-Limit", String(auth.quota));
  c.header("X-Quota-Used", String(auth.used));
  for (const [k, v] of Object.entries({ ...quotaHeaders(auth.used, auth.quota), ...minuteHeaders(auth) })) c.header(k, v);
  await next();
});
app.use("/v1/*", dataResponse);
app.use("/v1/*", ondemand.annotate);
app.post("/v1/companies/:ticker/refresh", async (c) => {
  const r = await ondemand.requestRefresh(c.env.DB, c.req.param("ticker"), c.get("plan"), c.get("owner"));
  return c.json(r.body, r.status as 200, r.headers);
});
app.get("/v1/companies/:ticker/refresh", async (c) => c.json(await ondemand.refreshStatus(c.env.DB, c.req.param("ticker"))));
app.get("/v1/filings/latest", async (c) => c.json(await ondemand.latestFilings(c.env.DB, { ticker: c.req.query("ticker"), since: c.req.query("since"), limit: clampLimit(c.req.query("limit"), 200) })));
app.route("/v1", devRoutes as any);

app.get("/v1/companies/search", async (c) => {
  const q = c.req.query("q");
  if (!q) return c.json({ error: "Query parameter 'q' is required." }, 400);
  return c.json({ data: await data.searchCompanies(c.env.DB, q, clampLimit(c.req.query("limit"), 50)) });
});

const notFound = (c: any, what: string) => c.json({ error: `No ${what} for that ticker.` }, 404);

app.get("/v1/companies/:ticker", async (c) => {
  const row = await data.getCompany(c.env.DB, c.req.param("ticker"));
  return row ? c.json(row) : notFound(c, "listed company");
});

app.get("/v1/companies/:ticker/financials", async (c) => {
  const row = await data.financials(c.env.DB, c.req.param("ticker"));
  return row ? c.json(row) : notFound(c, "annual report");
});

app.get("/v1/companies/:ticker/details", async (c) => {
  const row = await data.companyDetails(c.env.DB, c.req.param("ticker"));
  return row ? c.json(row) : notFound(c, "annual report");
});

app.get("/v1/companies/:ticker/segments", async (c) => {
  const row = await data.filingDetail(c.env.DB, c.req.param("ticker"), "segments");
  return row ? c.json(row) : notFound(c, "segment data");
});

app.get("/v1/companies/:ticker/voting", async (c) =>
  c.json(await data.votingResults(c.env.DB, { ticker: c.req.param("ticker"), since: c.req.query("since"), limit: clampLimit(c.req.query("limit"), 200) })),
);
app.get("/v1/companies/:ticker/tender-offers", async (c) =>
  c.json(await data.tenderOffers(c.env.DB, { ticker: c.req.param("ticker"), since: c.req.query("since"), limit: clampLimit(c.req.query("limit"), 200) })),
);
app.get("/v1/voting", async (c) => {
  return c.json(await data.votingResults(c.env.DB, { since: c.req.query("since"), limit: clampLimit(c.req.query("limit"), 500) }));
});
app.get("/v1/tender-offers", async (c) => {
  return c.json(await data.tenderOffers(c.env.DB, { since: c.req.query("since"), limit: clampLimit(c.req.query("limit"), 500) }));
});

app.get("/v1/companies/:ticker/us-funds", async (c) => {
  const row = await data.usFunds(c.env.DB, c.req.param("ticker"));
  return row ? c.json(row) : notFound(c, "U.S. fund holdings");
});

app.get("/v1/large-holdings", async (c) => {
  const res = await data.largeHoldings(c.env.DB, c.get("plan"), {
    ticker: c.req.query("ticker"),
    filer: c.req.query("filer"),
    since: c.req.query("since"),
    limit: clampLimit(c.req.query("limit")),
  });
  return c.json(res);
});

app.get("/v1/investors/:code", async (c) => {
  const inv = await data.investor(c.env.DB, c.req.param("code"), clampLimit(c.req.query("limit"), 500));
  return inv ? c.json(inv) : c.json({ error: "No 5% filings for that filer." }, 404);
});

const csv = (v: string | undefined) => (v ? v.split(",").map((s) => s.trim()).filter(Boolean) : undefined);

app.get("/v1/companies/:ticker/cross-holdings", async (c) => {
  const row = await data.crossHoldings(c.env.DB, c.req.param("ticker"));
  return row ? c.json(row) : notFound(c, "cross-shareholding data");
});

app.get("/v1/companies/:ticker/trades", async (c) => {
  return c.json(await data.holderTrades(c.env.DB, { ticker: c.req.param("ticker"), since: c.req.query("since"), limit: clampLimit(c.req.query("limit"), 1000) }));
});

app.get("/v1/investors/:code/trades", async (c) => {
  if (await filerBlocked(c.env.DB, c.req.param("code"))) return c.json({ error: "No 5% filings for that filer." }, 404);
  return c.json(await data.holderTrades(c.env.DB, { filer: c.req.param("code"), since: c.req.query("since"), limit: clampLimit(c.req.query("limit"), 1000) }));
});

app.get("/v1/companies/:ticker/indicators", async (c) => {
  const row = await data.companyIndicators(c.env.DB, c.req.param("ticker"));
  return row ? c.json(row) : notFound(c, "annual report");
});

app.get("/v1/companies/:ticker/earnings-releases", async (c) =>
  c.json(await data.earningsReleases(c.env.DB, c.req.param("ticker"), clampLimit(c.req.query("limit"), 40))),
);

app.get("/v1/earnings-releases", async (c) => {
  return c.json(await data.recentReleases(c.env.DB, { since: c.req.query("since"), limit: clampLimit(c.req.query("limit"), 500) }));
});

app.get("/v1/companies/:ticker/credit-ratings", async (c) => c.json(await data.disclosedRatings(c.env.DB, c.req.param("ticker"), 40)));
app.get("/v1/credit-ratings", async (c) => {
  return c.json(await data.recentDisclosedRatings(c.env.DB, { agency: c.req.query("agency"), since: c.req.query("since"), limit: clampLimit(c.req.query("limit"), 200) }));
});

app.get("/v1/companies/:ticker/buybacks", async (c) => c.json(await data.buybacks(c.env.DB, c.req.param("ticker"), clampLimit(c.req.query("limit"), 60))));
app.get("/v1/companies/:ticker/governance", async (c) => c.json(await data.governanceEvents(c.env.DB, { ticker: c.req.param("ticker"), limit: clampLimit(c.req.query("limit"), 40) })));
app.get("/v1/governance", async (c) => {
  return c.json(await data.governanceEvents(c.env.DB, { type: c.req.query("type"), since: c.req.query("since"), limit: clampLimit(c.req.query("limit"), 200) }));
});
app.get("/v1/cross-holdings/trades", async (c) => {
  return c.json(await data.crossHoldingTrades(c.env.DB, { ticker: c.req.query("ticker"), holder: c.req.query("holder"), since: c.req.query("since"), limit: clampLimit(c.req.query("limit"), 200) }));
});

app.get("/v1/companies/:ticker/brief", async (c) => {
  const row = await data.companyBrief(c.env.DB, c.req.param("ticker"));
  return row ? c.json(row) : notFound(c, "annual report");
});

app.get("/v1/indicators/policy-uncertainty", async (c) => c.json(await data.policyUncertaintyIndex(c.env.DB)));

app.get("/v1/indicators/sorted", async (c) => {
  const metric = c.req.query("metric") ?? "";
  if (!Object.prototype.hasOwnProperty.call(data.SORT_METRICS, metric)) return c.json({ error: `metric must be one of ${Object.keys(data.SORT_METRICS).join(", ")}.` }, 400);
  const names = c.req.query("sector")?.split(",").map((s) => resolveSector(s));
  if (names?.some((n) => !n)) return c.json({ error: "Unknown sector. See /v1/sectors." }, 400);
  return c.json(await data.sortedByMetric(c.env.DB, metric, c.req.query("order") === "asc" ? "asc" : "desc", clampLimit(c.req.query("limit"), 1000), names?.map((n) => n!.sector_ja)));
});

app.get("/v1/investor-groups", async (c) => {
  const res = await ownership.searchGroups(c.env.DB, { q: c.req.query("q"), kind: c.req.query("kind"), limit: clampLimit(c.req.query("limit")) });
  return "error" in res ? c.json(res, 400) : c.json(res);
});
app.get("/v1/investor-groups/:id", async (c) => {
  const g = await ownership.getGroup(c.env.DB, c.req.param("id"));
  return g ? c.json(g) : c.json({ error: "No such investor group." }, 404);
});
app.get("/v1/investor-groups/:id/portfolio", async (c) => {
  const g = await ownership.portfolio(c.env.DB, c.req.param("id"), { status: c.req.query("status"), limit: clampLimit(c.req.query("limit"), 1000) });
  return g ? c.json(g) : c.json({ error: "No such investor group." }, 404);
});
app.get("/v1/investor-groups/:id/changes", async (c) => {
  const g = await ownership.groupChanges(c.env.DB, c.req.param("id"), { after_at: c.req.query("after_at"), after_id: c.req.query("after_id"), limit: clampLimit(c.req.query("limit")) });
  return g ? c.json(g) : c.json({ error: "No such investor group." }, 404);
});
app.get("/v1/investor-groups/:id/overlap", async (c) => {
  const g = await ownership.overlap(c.env.DB, c.req.param("id"), { source: c.req.query("source"), limit: clampLimit(c.req.query("limit")) });
  return g ? c.json(g) : c.json({ error: "No such investor group." }, 404);
});
app.get("/v1/companies/:ticker/timeline", async (c) =>
  c.json(await ownership.companyTimeline(c.env.DB, c.get("plan"), c.req.param("ticker"), { since: c.req.query("since"), types: csv(c.req.query("type")), limit: clampLimit(c.req.query("limit")) })),
);
app.get("/v1/companies/:ticker/unwinding-trades", async (c) => {
  return c.json(await ownership.unwindingTrades(c.env.DB, c.req.param("ticker"), clampLimit(c.req.query("limit"))));
});

app.get("/v1/cross-holdings/unwinding", async (c) => {
  return c.json(await data.crossHoldingUnwinding(c.env.DB, clampLimit(c.req.query("limit"))));
});

app.get("/v1/events", async (c) =>
  c.json(
    await data.ownershipEvents(c.env.DB, c.get("plan"), {
      ticker: c.req.query("ticker"),
      filer: c.req.query("filer"),
      types: csv(c.req.query("type")),
      since: c.req.query("since"),
      limit: clampLimit(c.req.query("limit")),
    }),
  ),
);

app.get("/v1/webhooks", async (c) => c.json({ data: await webhooks.list(c.env.DB, c.get("owner")) }));

app.post("/v1/webhooks", async (c) => {
  const body = await c.req.json().catch(() => null);
  const res = await webhooks.create(c.env.DB, c.get("owner"), body, c.env.DEV_SHOW_LINKS === "1");
  return "error" in res ? c.json(res, 400) : c.json(res, 201);
});

app.delete("/v1/webhooks/:id", async (c) =>
  (await webhooks.remove(c.env.DB, c.get("owner"), c.req.param("id"))) ? c.body(null, 204) : c.json({ error: "No such webhook." }, 404),
);

app.post("/v1/webhooks/:id/test", async (c) => {
  const res = await webhooks.sendTest(c.env.DB, c.get("owner"), c.req.param("id"));
  return res ? c.json(res) : c.json({ error: "No such webhook." }, 404);
});

app.get("/v1/filings", async (c) =>
  c.json(
    await data.filings(c.env.DB, {
      ticker: c.req.query("ticker"),
      docType: c.req.query("doc_type"),
      since: c.req.query("since"),
      limit: clampLimit(c.req.query("limit")),
    }),
  ),
);

// Stateless Streamable HTTP: no server-initiated stream, which the protocol signals with 405.
app.get("/mcp", (c) => c.json({ error: "Use POST for MCP requests." }, 405, { Allow: "POST" }));

app.post("/mcp", async (c) => {
  const token = readKey(c.req.raw);
  const auth = oauth.isOAuthToken(token) ? await oauth.authorizeToken(c.env, token!, c.req.url) : await authorize(c.env.DB, token, billing.providerKey(c.env));
  if (!auth.ok) {
    const challenge = auth.status === 401 ? oauth.challengeHeaders(c.env, c.req.url, token) : {};
    return c.json({ jsonrpc: "2.0", id: null, error: { code: -32001, message: auth.error } }, auth.status, { ...limitHeaders(auth), ...challenge });
  }
  for (const [k, v] of Object.entries({ ...quotaHeaders(auth.used, auth.quota), ...minuteHeaders(auth) })) c.header(k, v);
  const res = await handleMcp(c.env.DB, auth.plan, await c.req.json(), auth);
  return res === null ? c.body(null, 202) : c.json(res);
});

app.get("/openapi.json", (c) => c.json(OPENAPI));

const q = (name: string, required = false) => ({ name, in: "query", required });
const p = (name: string) => ({ name, in: "path", required: true });

const OPENAPI = {
  openapi: "3.1.0",
  info: { title: "japanfilings API", version: "0.4.0", description: "Japanese listed-company data from EDINET, in English." },
  components: { securitySchemes: { bearer: { type: "http", scheme: "bearer" } } },
  security: [{ bearer: [] }],
  paths: {
    "/v1/companies/search": { get: { summary: "Search listed companies by ticker or English/Japanese name", parameters: [q("q", true), q("limit")] } },
    "/v1/companies/{ticker}": { get: { summary: "Company profile with FIGI and common ticker symbols", parameters: [p("ticker")] } },
    "/v1/companies/{ticker}/financials": { get: { summary: "Five-year key metrics and current/prior income statement, balance sheet and cash flow", parameters: [p("ticker")] } },
    "/v1/companies/{ticker}/details": { get: { summary: "Officer titles (no names), workforce, major shareholders (individuals not named), shareholder mix, auditor and fees", parameters: [p("ticker")] } },
    "/v1/companies/{ticker}/segments": { get: { summary: "Segments stated in the latest annual securities report", parameters: [p("ticker")] } },
    "/v1/companies/{ticker}/voting": { get: { summary: "Shareholder-meeting voting results for one company", parameters: [p("ticker"), q("since"), q("limit")] } },
    "/v1/companies/{ticker}/tender-offers": { get: { summary: "Tender-offer notifications, results and target opinions for one company", parameters: [p("ticker"), q("since"), q("limit")] } },
    "/v1/voting": { get: { summary: "Shareholder-meeting voting results across companies", parameters: [q("since"), q("limit")] } },
    "/v1/tender-offers": { get: { summary: "Tender-offer notifications, results and target opinions across companies", parameters: [q("since"), q("limit")] } },
    "/v1/companies/{ticker}/us-funds": { get: { summary: "U.S. mutual funds and ETFs holding the stock, from SEC N-PORT", parameters: [p("ticker")] } },
    "/v1/large-holdings": { get: { summary: "5% rule filings with holders, purposes and changes", parameters: [q("ticker"), q("filer"), q("since"), q("limit")] } },
    "/v1/investors/{code}": { get: { summary: "An investor's 5% filings, latest positions and joint holders", parameters: [p("code"), q("limit")] } },
    "/v1/companies/{ticker}/cross-holdings": { get: { summary: "Cross-shareholdings: strategic stakes the company holds, listed companies holding it, mutual pairs, and unwinding totals", parameters: [p("ticker")] } },
    "/v1/companies/{ticker}/trades": { get: { summary: "Trades disclosed by 5% holders in the last 60 days of each report: date, shares, on/off market, unit price, with net flow and average disclosed price per holder", parameters: [p("ticker"), q("since"), q("limit")] } },
    "/v1/investors/{code}/trades": { get: { summary: "Trades one filer disclosed across all companies", parameters: [p("code"), q("since"), q("limit")] } },
    "/v1/companies/{ticker}/indicators": { get: { summary: "Per-metric financial percentile ranks (not a rating), policy-uncertainty counts by area, results wording balance, ownership activity counts and a 50 bp interest example on reported debt", parameters: [p("ticker")] } },
    "/v1/companies/{ticker}/earnings-releases": { get: { summary: "Period results from half-year and quarterly securities reports on EDINET, with year-on-year % as filed and the source filing", parameters: [p("ticker"), q("limit")] } },
    "/v1/earnings-releases": { get: { summary: "Latest period results across companies, from EDINET half-year and quarterly reports", parameters: [q("since"), q("limit")] } },
    "/v1/companies/{ticker}/brief": { get: { summary: "One company on one page, from its filings: latest results, indicator figures, ownership changes in the last 180 days and a mechanical 50 bp interest example on its reported debt, each dated", parameters: [p("ticker")] } },
    "/v1/companies/{ticker}/credit-ratings": { get: { summary: "Credit ratings as the company states them in its bond filings on EDINET: agency, rating, date, bond, source filing. We do not rate anything", parameters: [p("ticker")] } },
    "/v1/credit-ratings": { get: { summary: "Ratings stated in bond filings across issuers, including unlisted ones", parameters: [q("agency"), q("since"), q("limit")] } },
    "/v1/companies/{ticker}/buybacks": { get: { summary: "Monthly share-buyback reports: resolution limits, shares and yen acquired, progress and average price as arithmetic", parameters: [p("ticker"), q("limit")] } },
    "/v1/companies/{ticker}/governance": { get: { summary: "Internal-control conclusions and auditor changes as the company states them, with source", parameters: [p("ticker"), q("limit")] } },
    "/v1/governance": { get: { summary: "Governance filings across companies, by type: internal_control or auditor_change", parameters: [q("type"), q("since"), q("limit")] } },
    "/v1/cross-holdings/trades": { get: { summary: "Disclosed trades and prices where a listed filer also lists the stock as a strategic holding", parameters: [q("ticker"), q("holder"), q("since"), q("limit")] } },
    "/v1/indicators/policy-uncertainty": { get: { summary: "Japan corporate policy-uncertainty index by filing month, with shares by policy area" } },
    "/v1/indicators/sorted": { get: { summary: "Companies in the order of one figure or percentile, optionally by sector. Arithmetic order, no judgement", parameters: [q("metric", true), q("order"), q("sector"), q("limit")] } },
    "/v1/cross-holdings/unwinding": { get: { summary: "Companies with the largest stated sales of listed strategic shares in their latest fiscal year", parameters: [q("limit")] } },
    "/v1/investor-groups": { get: { summary: "Search investor groups by English or Japanese name, or list them by companies held. Natural persons are excluded", parameters: [q("q"), q("kind"), q("limit")] } },
    "/v1/investor-groups/{id}": { get: { summary: "One group: members, match rule and confidence per member", parameters: [p("id")] } },
    "/v1/investor-groups/{id}/portfolio": { get: { summary: "What a group's filers report holding, with dated entries and exits", parameters: [p("id"), q("status"), q("limit")] } },
    "/v1/investor-groups/{id}/changes": { get: { summary: "Ownership events of a group's filers, cursor-paged", parameters: [p("id"), q("after_at"), q("after_id"), q("limit")] } },
    "/v1/investor-groups/{id}/overlap": { get: { summary: "Groups with disclosed stakes in the same companies, and joint holders as filings state them", parameters: [p("id"), q("source"), q("limit")] } },
    "/v1/companies/{ticker}/timeline": { get: { summary: "Merged dated list: stake changes, buybacks, cross-holding changes and tender-offer filings, including disclosed trades", parameters: [p("ticker"), q("since"), q("type"), q("limit")] } },
    "/v1/companies/{ticker}/unwinding-trades": { get: { summary: "Strategic-holding reductions from annual reports next to trades the holder disclosed in 5% reports", parameters: [p("ticker"), q("limit")] } },
    "/v1/events": { get: { summary: "Ownership-change events from 5% filings with English summaries: new_stake, stake_increase, stake_decrease, exit_below_5pct, threshold_crossed, stated_may_make_proposals, purpose_changed, amendment", parameters: [q("ticker"), q("filer"), q("type"), q("since"), q("limit")] } },
    "/v1/webhooks": {
      get: { summary: "Your webhook subscriptions and delivery status" },
      post: { summary: "Subscribe a URL to ownership events. Body: {url, types?, tickers?, replay_from?}. Returns the signing secret once" },
    },
    "/v1/webhooks/{id}": { delete: { summary: "Delete a webhook", parameters: [p("id")] } },
    "/v1/webhooks/{id}/test": { post: { summary: "Send the latest event to the webhook, marked test: true", parameters: [p("id")] } },
    "/v1/filings": { get: { summary: "EDINET filing metadata", parameters: [q("ticker"), q("doc_type"), q("since"), q("limit")] } },
    "/v1/status": { get: { summary: "Overall data status and each dataset's status (no key)", security: [] } },
    "/v1/freshness": { get: { summary: "Every dataset: source, target interval, last successful run, newest source date, rows, status, coverage (no key)", security: [] } },
    "/v1/filings/latest": { get: { summary: "Filings newest first by when they were first seen on EDINET's list, within minutes", parameters: [q("ticker"), q("since"), q("limit")] } },
    "/v1/companies/{ticker}/refresh": { post: { summary: "Queue a re-check of one company's EDINET documents. Rate-limited; joins an open request", parameters: [p("ticker")] }, get: { summary: "Latest refresh status and when it was checked", parameters: [p("ticker")] } },
    ...DEV_PATHS,
  },
};

app.notFound((c) =>
  c.req.path.startsWith("/v1/") || c.req.path === "/mcp" ? c.json({ error: "No such endpoint. See /docs." }, 404) : c.html(site.notFoundPage(c.get("user") ?? null), 404),
);
app.onError((err, c) => {
  console.error(err);
  // Transient D1 lock/session errors: tell clients to retry rather than report a hard failure.
  if (/D1_ERROR: (?:Failed to get session commit token|.*SQLITE_BUSY)/.test(String(err?.message ?? err))) {
    c.header("Retry-After", "1");
    return c.req.path.startsWith("/v1/") ? c.json({ error: "Temporarily unavailable. Please retry shortly." }, 503) : c.text("Temporarily unavailable", 503);
  }
  return c.req.path.startsWith("/v1/") ? c.json({ error: "Internal error. Please retry; if it persists, contact support." }, 500) : c.text("Internal error", 500);
});

export default {
  fetch: app.fetch,
  // Every 5 minutes: push new ownership events to webhook subscribers.
  scheduled: async (event: ScheduledController, env: Bindings, ctx: ExecutionContext) => {
    ctx.waitUntil(webhooks.deliverAll(env.DB));
    ctx.waitUntil(ondemand.cron(env, new Date(event.scheduledTime)).catch((e) => console.error("ondemand", e)));
    ctx.waitUntil(billing.reconcileEntitlements(env).catch((e) => console.error("billing reconcile", e)));
    // Once a day at 21:00 JST, after the ingest window: rebuild screener facts, purge expired OAuth rows.
    const at = new Date(event.scheduledTime);
    if (event.cron === "daily" || (at.getUTCHours() === 12 && at.getUTCMinutes() < 5)) {
      ctx.waitUntil(rebuildFacts(env.DB).then((r) => console.log("facts", JSON.stringify(r))).catch((e) => console.error("facts", e)));
      ctx.waitUntil(oauth.purgeOAuth(env.DB).catch((e) => console.error("oauth purge", e)));
      ctx.waitUntil(contactForm.purge(env.DB).catch((e) => console.error("contact purge", e)));
    }
  },
};
