// OAuth 2.1 authorization server for the MCP endpoint. Off unless OAUTH_ENABLED=1.
//
// Follows the MCP authorization specification, revision 2025-11-25 (and the current draft's iss rule):
//   https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization
//   https://modelcontextprotocol.io/specification/draft/basic/authorization
// RFC 9728 protected resource metadata, RFC 8414 server metadata, RFC 7591 registration, RFC 8707
// resource binding, RFC 9207 iss, RFC 7009 revocation, RFC 8252 loopback redirects.
// Client requirements checked against:
//   https://claude.com/docs/connectors/building/authentication
//   https://developers.openai.com/plugins/build/auth
//
// Decisions: public clients only, S256 PKCE required, redirect URIs matched exactly (a loopback
// URI ignores the port, as RFC 8252 7.3 requires), codes live 60 seconds and work once, tokens are
// stored as SHA-256 hashes, refresh tokens rotate and reuse revokes the grant. Tokens work on /mcp
// only, not on /v1. Client ID Metadata Documents are not supported: fetching a client-chosen URL
// needs SSRF controls that are not built, and both Claude and ChatGPT fall back to registration.
import { Hono } from "hono";
import { deleteCookie, getCookie, setCookie } from "hono/cookie";
import { cors } from "hono/cors";
import { SESSION_COOKIE, currentUser, type AccountEnv, type User } from "./accounts";
import { meter, sha256Hex, type AuthResult } from "./auth";
import type { Plan } from "./data";

export type OAuthEnv = AccountEnv & { OAUTH_ENABLED?: string; PUBLIC_ORIGIN?: string };
type C = { Bindings: OAuthEnv };

const SCOPE = "mcp:read";
const CODE_TTL_MS = 60_000;
const ACCESS_TTL_S = 3600;
const REFRESH_TTL_S = 30 * 86400;
const PENDING_COOKIE = "jf_oauth_pending";
const PENDING_TTL_S = 30 * 60; // long enough to read the sign-in email
const MAX_REGISTRATIONS_PER_HOUR = 60; // per IP; Claude registers per connection from a shared egress range
const TOKEN_PREFIX = "jfo_";

export const oauthEnabled = (env: OAuthEnv) => env.OAUTH_ENABLED === "1";
export const isOAuthToken = (t: string | null) => !!t && t.startsWith(TOKEN_PREFIX);

const isLoopbackHost = (h: string) => h === "localhost" || h === "127.0.0.1" || h === "[::1]";
const issuerOf = (env: OAuthEnv, url: string) => (env.PUBLIC_ORIGIN ?? new URL(url).origin).replace(/\/$/, "");
const resourceOf = (env: OAuthEnv, url: string) => `${issuerOf(env, url)}/mcp`;
const prmUrl = (env: OAuthEnv, url: string) => `${issuerOf(env, url)}/.well-known/oauth-protected-resource/mcp`;

const randomToken = (bytes = 32) =>
  btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(bytes)))).replace(/[+/=]/g, (c) => ({ "+": "-", "/": "_", "=": "" })[c]!);
const isoIn = (ms: number) => new Date(Date.now() + ms).toISOString();
const esc = (s: string) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);

async function s256(verifier: string) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

// ---------- Redirect URIs ----------

/** HTTPS, or HTTP on a loopback host. No fragment, no user info, no wildcard, no custom scheme. */
export function validRedirectUri(raw: unknown): raw is string {
  if (typeof raw !== "string" || raw.length > 512 || raw.includes("*")) return false;
  let u: URL;
  try { u = new URL(raw); } catch { return false; }
  if (u.hash || raw.includes("#") || u.username || u.password) return false;
  return u.protocol === "https:" || (u.protocol === "http:" && isLoopbackHost(u.hostname));
}

/** Exact string match; for a registered loopback URI the port alone may differ (RFC 8252 7.3). */
export function redirectMatches(registered: string[], presented: string): boolean {
  if (registered.includes(presented)) return true;
  let p: URL;
  try { p = new URL(presented); } catch { return false; }
  if (p.protocol !== "http:" || !isLoopbackHost(p.hostname) || p.username || p.password || p.hash) return false;
  return registered.some((r) => {
    let u: URL;
    try { u = new URL(r); } catch { return false; }
    return u.protocol === "http:" && u.hostname === p.hostname && u.pathname === p.pathname && u.search === p.search;
  });
}

// ---------- Authorization request ----------

type AuthzRequest = { clientId: string; clientName: string; redirectUri: string; state: string | null; challenge: string; resource: string; registeredLoopbackOnly: boolean };
type Parsed = { fatal: string } | { redirectError: { uri: string; state: string | null; error: string; description: string } } | { ok: AuthzRequest };

function normalizeResource(raw: string): string | null {
  try {
    const u = new URL(raw);
    if (u.hash || raw.includes("#") || u.search || u.username || u.password) return null;
    return `${u.protocol}//${u.host}${u.pathname.replace(/\/$/, "")}`; // URL lowercases scheme and host
  } catch { return null; }
}

async function parseAuthz(env: OAuthEnv, url: string, p: (k: string) => string | undefined): Promise<Parsed> {
  // Until client and redirect URI are proven, nothing may redirect: errors stay on our page.
  const clientId = p("client_id") ?? "";
  const redirectUri = p("redirect_uri") ?? "";
  const client = clientId ? await env.DB.prepare("SELECT client_name, redirect_uris FROM oauth_clients WHERE client_id = ?1").bind(clientId).first<{ client_name: string | null; redirect_uris: string }>() : null;
  if (!client) return { fatal: "Unknown client. The application must register first." };
  const registered: string[] = JSON.parse(client.redirect_uris);
  if (!redirectUri || !redirectMatches(registered, redirectUri)) return { fatal: "This redirect address is not registered for the application." };

  const state = p("state") ?? null;
  const fail = (error: string, description: string): Parsed => ({ redirectError: { uri: redirectUri, state, error, description } });
  if (state && state.length > 1024) return { fatal: "The state value is too long." };
  if (p("response_type") !== "code") return fail("unsupported_response_type", "response_type must be code.");
  const challenge = p("code_challenge") ?? "";
  if (p("code_challenge_method") !== "S256" || !/^[A-Za-z0-9_-]{43}$/.test(challenge)) return fail("invalid_request", "PKCE with S256 is required.");
  const scopes = (p("scope") ?? "").split(/\s+/).filter(Boolean);
  if (scopes.some((s) => s !== SCOPE && s !== "offline_access")) return fail("invalid_scope", `Supported scopes: ${SCOPE}, offline_access.`);
  const expected = resourceOf(env, url);
  const given = p("resource");
  if (given !== undefined && normalizeResource(given) !== expected) return fail("invalid_target", `resource must be ${expected}.`);
  return {
    ok: {
      clientId, clientName: client.client_name ?? "Unnamed application", redirectUri, state, challenge, resource: expected,
      registeredLoopbackOnly: registered.every((r) => isLoopbackHost(new URL(r).hostname)),
    },
  };
}

function authzResponseUrl(env: OAuthEnv, url: string, redirectUri: string, params: Record<string, string | null>) {
  const u = new URL(redirectUri);
  for (const [k, v] of Object.entries(params)) if (v !== null) u.searchParams.set(k, v);
  u.searchParams.set("iss", issuerOf(env, url)); // RFC 9207, on success and on error
  return u.toString();
}

// ---------- Pages (own markup: scripts are blocked by the site's CSP) ----------

const PAGE_CSS = "body{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 16px;color:#1a1a1a;background:#fff}h1{font-size:1.4rem}button,a.button{font:inherit;padding:.5rem 1rem;margin-right:.5rem;cursor:pointer}.host{font-family:ui-monospace,monospace;word-break:break-all}.warn{border-left:3px solid #b45309;padding-left:.75rem}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:.4rem .4rem .4rem 0;border-bottom:1px solid #ddd}@media (prefers-color-scheme:dark){body{background:#141414;color:#eee}}";
const page = (title: string, body: string, head = "") =>
  `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex"><title>${esc(title)}</title><style>${PAGE_CSS}</style>${head}</head><body>${body}</body></html>`;

// The site's CSP has form-action 'self', and browsers apply that to a redirect after a form post.
// So the post answers with this page, which moves on by meta refresh with a link as fallback.
const leavePage = (target: string) =>
  page("Returning to the application", `<h1>Returning to the application</h1><p><a href="${esc(target)}">Continue</a></p>`, `<meta http-equiv="refresh" content="0;url=${esc(target)}">`);

const hiddenFields = (fields: Record<string, string | undefined>) =>
  Object.entries(fields).filter(([, v]) => v !== undefined).map(([k, v]) => `<input type="hidden" name="${esc(k)}" value="${esc(v!)}">`).join("");

const AUTHZ_FIELDS = ["client_id", "redirect_uri", "response_type", "code_challenge", "code_challenge_method", "scope", "state", "resource"];

function consentPage(user: User, req: AuthzRequest, fields: Record<string, string | undefined>) {
  const host = new URL(req.redirectUri).host;
  return page("Allow access?", `<h1>Allow access to japanfilings?</h1>
<p><strong>${esc(req.clientName)}</strong> asks to use japanfilings tools as <strong>${esc(user.email)}</strong>.</p>
<p>After you allow it, you return to <span class="host">${esc(host)}</span>.</p>
${req.registeredLoopbackOnly ? `<p class="warn">This application receives access on your own computer. Any program there could claim this name, so allow it only if you just started the connection yourself.</p>` : ""}
<p>The name above is provided by the application and is not verified by us. Compare the address with the service you are connecting.</p>
<p>It can read company data through MCP at your plan's limits, counted with your API key's monthly quota. It cannot see your API key or change your account. You can withdraw access at <a href="/oauth/connections">Connected applications</a>.</p>
<form method="post" action="/oauth/authorize">${hiddenFields(fields)}
<button name="decision" value="allow" type="submit">Allow</button><button name="decision" value="deny" type="submit">Deny</button></form>`);
}

// ---------- Tokens ----------

async function issueTokens(db: D1Database, g: { family: string; clientId: string; userId: string; resource: string; scope: string }) {
  const access = TOKEN_PREFIX + randomToken();
  const refresh = TOKEN_PREFIX + "r_" + randomToken();
  const insert = (hash: string, kind: string, ttl: number) =>
    db.prepare("INSERT INTO oauth_tokens (token_hash, kind, family_id, client_id, user_id, resource, scope, expires_at, created_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)")
      .bind(hash, kind, g.family, g.clientId, g.userId, g.resource, g.scope, isoIn(ttl * 1000), isoIn(0));
  await db.batch([insert(await sha256Hex(access), "access", ACCESS_TTL_S), insert(await sha256Hex(refresh), "refresh", REFRESH_TTL_S)]);
  return { access_token: access, token_type: "Bearer", expires_in: ACCESS_TTL_S, refresh_token: refresh, scope: g.scope };
}

const revokeFamily = (db: D1Database, family: string) => db.prepare("UPDATE oauth_tokens SET revoked = 1 WHERE family_id = ?1").bind(family).run();

/** Validates an access token for /mcp: hash, kind, expiry, audience, scope. Then meters like an API key. */
export async function authorizeToken(env: OAuthEnv, token: string, url: string): Promise<AuthResult> {
  const invalid: AuthResult = { ok: false, status: 401, error: "Invalid or expired access token." };
  if (!oauthEnabled(env)) return invalid;
  const row = await env.DB.prepare(
    "SELECT t.user_id, t.scope, u.plan FROM oauth_tokens t JOIN users u ON u.id = t.user_id " +
      "WHERE t.token_hash = ?1 AND t.kind = 'access' AND t.revoked = 0 AND t.expires_at > ?2 AND t.resource = ?3",
  ).bind(await sha256Hex(token), isoIn(0), resourceOf(env, url)).first<{ user_id: string; scope: string; plan: Plan }>();
  if (!row || !row.scope.split(" ").includes(SCOPE)) return invalid;
  // Shares the account's monthly count with its API key, so a connector is not extra quota.
  const key = await env.DB.prepare("SELECT key_hash FROM api_keys WHERE user_id = ?1 AND revoked = 0 LIMIT 1").bind(row.user_id).first<{ key_hash: string }>();
  return meter(env.DB, key?.key_hash ?? `user:${row.user_id}`, row.plan, row.user_id);
}

/** The challenge on a 401 from /mcp that starts a connector's sign-in. */
export function challengeHeaders(env: OAuthEnv, url: string, token: string | null): Record<string, string> {
  if (!oauthEnabled(env)) return {};
  const err = isOAuthToken(token) ? ` error="invalid_token",` : "";
  return { "WWW-Authenticate": `Bearer${err} resource_metadata="${prmUrl(env, url)}", scope="${SCOPE}"` };
}

/** After an email sign-in: back to the waiting authorization request, if this browser has one. */
export function resumePath(env: OAuthEnv, pending: string | undefined): string | null {
  if (!oauthEnabled(env) || !pending) return null;
  // Rebuilt from parsed pairs under a fixed path, so the cookie's content cannot choose where we go.
  const q = new URLSearchParams(pending);
  const safe = new URLSearchParams();
  for (const k of AUTHZ_FIELDS) { const v = q.get(k); if (v !== null) safe.set(k, v); }
  return `/oauth/authorize?${safe}`;
}
export const PENDING_COOKIE_NAME = PENDING_COOKIE;

// ---------- Routes ----------

export const oauthRoutes = new Hono<C>();
const PATHS = ["/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/*", "/.well-known/oauth-authorization-server", "/oauth/*"];

for (const path of PATHS) {
  oauthRoutes.use(path, async (c, next) => {
    if (!oauthEnabled(c.env)) return c.notFound();
    const u = new URL(c.req.url);
    if (u.protocol !== "https:" && !isLoopbackHost(u.hostname)) return c.json({ error: "invalid_request", error_description: "HTTPS is required." }, 400);
    await next();
  });
}
// Browser-based MCP clients need these; no cookies are read on them, so any origin is safe.
for (const path of ["/.well-known/*", "/oauth/token", "/oauth/register", "/oauth/revoke"]) {
  oauthRoutes.use(path, cors({ origin: "*", allowMethods: ["GET", "POST", "OPTIONS"], allowHeaders: ["Content-Type", "MCP-Protocol-Version"] }));
}

const protectedResource = (c: any) =>
  c.json({ resource: resourceOf(c.env, c.req.url), authorization_servers: [issuerOf(c.env, c.req.url)], scopes_supported: [SCOPE], bearer_methods_supported: ["header"], resource_name: "japanfilings" });
oauthRoutes.get("/.well-known/oauth-protected-resource", protectedResource);
oauthRoutes.get("/.well-known/oauth-protected-resource/mcp", protectedResource);

oauthRoutes.get("/.well-known/oauth-authorization-server", (c) => {
  const iss = issuerOf(c.env, c.req.url);
  return c.json({
    issuer: iss,
    authorization_endpoint: `${iss}/oauth/authorize`,
    token_endpoint: `${iss}/oauth/token`,
    registration_endpoint: `${iss}/oauth/register`,
    revocation_endpoint: `${iss}/oauth/revoke`,
    response_types_supported: ["code"],
    grant_types_supported: ["authorization_code", "refresh_token"],
    code_challenge_methods_supported: ["S256"],
    token_endpoint_auth_methods_supported: ["none"],
    revocation_endpoint_auth_methods_supported: ["none"],
    scopes_supported: [SCOPE, "offline_access"],
    authorization_response_iss_parameter_supported: true,
  });
});

// RFC 7591. Anyone may register, so nothing about a client is trusted: the consent page labels the
// name unverified and shows the redirect host, and the throttle bounds table growth.
oauthRoutes.post("/oauth/register", async (c) => {
  const bad = (error: string, description: string) => c.json({ error, error_description: description }, 400, { "Cache-Control": "no-store" });
  const raw = await c.req.text();
  if (raw.length > 8192) return bad("invalid_client_metadata", "Metadata too large.");
  let body: any;
  try { body = JSON.parse(raw); } catch { return bad("invalid_client_metadata", "Body must be JSON."); }
  const uris = body?.redirect_uris;
  if (!Array.isArray(uris) || !uris.length || uris.length > 10) return bad("invalid_redirect_uri", "Provide 1 to 10 redirect_uris.");
  if (!uris.every(validRedirectUri)) return bad("invalid_redirect_uri", "Each redirect URI must be HTTPS or loopback HTTP, without fragment, user info or wildcard.");
  const grants: string[] = Array.isArray(body.grant_types) ? body.grant_types : ["authorization_code"];
  if (grants.some((g) => g !== "authorization_code" && g !== "refresh_token")) return bad("invalid_client_metadata", "Only authorization_code and refresh_token grants are supported.");
  if (Array.isArray(body.response_types) && body.response_types.some((r: unknown) => r !== "code")) return bad("invalid_client_metadata", "Only the code response type is supported.");

  const ip = c.req.header("cf-connecting-ip") ?? "unknown";
  const bucket = await sha256Hex(`${ip}|${new Date().toISOString().slice(0, 13)}`);
  const seen = await c.env.DB.prepare("SELECT COUNT(*) AS n FROM oauth_clients WHERE reg_bucket = ?1").bind(bucket).first<{ n: number }>();
  if ((seen?.n ?? 0) >= MAX_REGISTRATIONS_PER_HOUR) return c.json({ error: "too_many_requests", error_description: "Too many registrations. Try again later." }, 429, { "Retry-After": "3600" });

  const clientId = "jfc_" + randomToken(18);
  const name = typeof body.client_name === "string" ? body.client_name.replace(/[\u0000-\u001f\u007f]/g, "").trim().slice(0, 100) || null : null;
  const now = Math.floor(Date.now() / 1000);
  await c.env.DB.prepare("INSERT INTO oauth_clients (client_id, client_name, redirect_uris, reg_bucket, created_at) VALUES (?1, ?2, ?3, ?4, ?5)")
    .bind(clientId, name, JSON.stringify(uris), bucket, isoIn(0)).run();
  return c.json({
    client_id: clientId, client_id_issued_at: now, client_name: name ?? undefined, redirect_uris: uris,
    grant_types: ["authorization_code", "refresh_token"], response_types: ["code"], token_endpoint_auth_method: "none", scope: SCOPE,
  }, 201, { "Cache-Control": "no-store" });
});

const errorPage = (c: any, message: string) => c.html(page("Cannot continue", `<h1>Cannot continue</h1><p>${esc(message)}</p>`), 400);

oauthRoutes.get("/oauth/authorize", async (c) => {
  const parsed = await parseAuthz(c.env, c.req.url, (k) => c.req.query(k));
  if ("fatal" in parsed) return errorPage(c, parsed.fatal);
  if ("redirectError" in parsed) {
    const e = parsed.redirectError;
    return c.redirect(authzResponseUrl(c.env, c.req.url, e.uri, { error: e.error, error_description: e.description, state: e.state }), 302);
  }
  const fields = Object.fromEntries(AUTHZ_FIELDS.map((k) => [k, c.req.query(k)]));
  const user = await currentUser(c.env, getCookie(c, SESSION_COOKIE));
  if (!user) {
    const pending = new URLSearchParams(Object.entries(fields).filter(([, v]) => v !== undefined) as [string, string][]).toString();
    setCookie(c, PENDING_COOKIE, pending, { httpOnly: true, secure: new URL(c.req.url).protocol === "https:", sameSite: "Lax", path: "/", maxAge: PENDING_TTL_S });
    return c.html(page("Sign in to continue", `<h1>Sign in to connect ${esc(parsed.ok.clientName)}</h1><p>Sign in with your email. Open the link in this same browser and you will come back here to review the request.</p><p><a class="button" href="/login">Sign in</a></p>`));
  }
  return c.html(consentPage(user, parsed.ok, fields));
});

oauthRoutes.post("/oauth/authorize", async (c) => {
  // A real browser always sends Origin on a form post. Requiring it, with SameSite=Lax, stops cross-site approval.
  if (c.req.header("origin") !== new URL(c.req.url).origin && c.req.header("origin") !== issuerOf(c.env, c.req.url)) return c.text("Forbidden", 403);
  const user = await currentUser(c.env, getCookie(c, SESSION_COOKIE));
  if (!user) return errorPage(c, "Your session ended. Start the connection again from the application.");
  const form = await c.req.parseBody();
  const field = (k: string) => (typeof form[k] === "string" ? (form[k] as string) : undefined);
  const parsed = await parseAuthz(c.env, c.req.url, field);
  if ("fatal" in parsed) return errorPage(c, parsed.fatal);
  deleteCookie(c, PENDING_COOKIE, { path: "/" });
  if ("redirectError" in parsed) {
    const e = parsed.redirectError;
    return c.html(leavePage(authzResponseUrl(c.env, c.req.url, e.uri, { error: e.error, error_description: e.description, state: e.state })));
  }
  const req = parsed.ok;
  if (field("decision") !== "allow") {
    return c.html(leavePage(authzResponseUrl(c.env, c.req.url, req.redirectUri, { error: "access_denied", state: req.state })));
  }
  const code = randomToken();
  await c.env.DB.prepare(
    "INSERT INTO oauth_codes (code_hash, family_id, client_id, user_id, redirect_uri, code_challenge, resource, scope, expires_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
  ).bind(await sha256Hex(code), crypto.randomUUID(), req.clientId, user.id, req.redirectUri, req.challenge, req.resource, SCOPE, isoIn(CODE_TTL_MS)).run();
  return c.html(leavePage(authzResponseUrl(c.env, c.req.url, req.redirectUri, { code, state: req.state })));
});

oauthRoutes.post("/oauth/token", async (c) => {
  const noStore = { "Cache-Control": "no-store", Pragma: "no-cache" };
  const fail = (error: string, description: string, status: 400 | 401 = 400) => c.json({ error, error_description: description }, status, noStore);
  if (!(c.req.header("content-type") ?? "").toLowerCase().startsWith("application/x-www-form-urlencoded")) return fail("invalid_request", "Use application/x-www-form-urlencoded.");
  const form = new URLSearchParams(await c.req.text());
  const clientId = form.get("client_id") ?? "";
  if (!clientId) return fail("invalid_client", "client_id is required.", 401);
  const client = await c.env.DB.prepare("SELECT 1 AS ok FROM oauth_clients WHERE client_id = ?1").bind(clientId).first();
  if (!client) return fail("invalid_client", "Unknown client.", 401);
  const db = c.env.DB;
  const expected = resourceOf(c.env, c.req.url);
  const given = form.get("resource");
  if (given !== null && normalizeResource(given) !== expected) return fail("invalid_target", `resource must be ${expected}.`);

  const grant = form.get("grant_type");
  if (grant === "authorization_code") {
    const code = form.get("code") ?? "";
    const verifier = form.get("code_verifier") ?? "";
    if (!code || !/^[A-Za-z0-9\-._~]{43,128}$/.test(verifier)) return fail("invalid_request", "code and a valid code_verifier are required.");
    const hash = await sha256Hex(code);
    // Consumed atomically before any other check, so a code gets exactly one attempt.
    const row = await db.prepare(
      "UPDATE oauth_codes SET used = 1 WHERE code_hash = ?1 AND used = 0 AND expires_at > ?2 RETURNING family_id, client_id, user_id, redirect_uri, code_challenge, resource, scope",
    ).bind(hash, isoIn(0)).first<any>();
    if (!row) {
      // A second use suggests the code leaked, so what the first use produced is withdrawn.
      const replay = await db.prepare("SELECT family_id FROM oauth_codes WHERE code_hash = ?1 AND used = 1").bind(hash).first<{ family_id: string }>();
      if (replay) await revokeFamily(db, replay.family_id);
      return fail("invalid_grant", "The code is invalid, expired or already used.");
    }
    if (row.client_id !== clientId || row.redirect_uri !== form.get("redirect_uri") || row.code_challenge !== (await s256(verifier))) {
      return fail("invalid_grant", "The code does not match this client, redirect URI or verifier.");
    }
    return c.json(await issueTokens(db, { family: row.family_id, clientId, userId: row.user_id, resource: row.resource, scope: row.scope }), 200, noStore);
  }
  if (grant === "refresh_token") {
    const hash = await sha256Hex(form.get("refresh_token") ?? "");
    const known = await db.prepare("SELECT family_id, client_id, user_id, resource, scope, revoked, expires_at FROM oauth_tokens WHERE token_hash = ?1 AND kind = 'refresh'").bind(hash).first<any>();
    if (!known || known.client_id !== clientId) return fail("invalid_grant", "The refresh token is not valid.");
    if (known.expires_at <= isoIn(0)) return fail("invalid_grant", "The refresh token has expired.");
    const rotated = await db.prepare("UPDATE oauth_tokens SET revoked = 1 WHERE token_hash = ?1 AND revoked = 0 RETURNING token_hash").bind(hash).first();
    if (!rotated) {
      await revokeFamily(db, known.family_id); // reuse of a rotated token: assume theft
      return fail("invalid_grant", "The refresh token was already used. Sign in again.");
    }
    return c.json(await issueTokens(db, { family: known.family_id, clientId, userId: known.user_id, resource: known.resource, scope: known.scope }), 200, noStore);
  }
  return fail("unsupported_grant_type", "Use authorization_code or refresh_token.");
});

// RFC 7009: the answer is 200 whether or not the token was known.
oauthRoutes.post("/oauth/revoke", async (c) => {
  const form = new URLSearchParams(await c.req.text());
  const row = await c.env.DB.prepare("SELECT family_id, client_id FROM oauth_tokens WHERE token_hash = ?1").bind(await sha256Hex(form.get("token") ?? "")).first<{ family_id: string; client_id: string }>();
  if (row && (!form.get("client_id") || form.get("client_id") === row.client_id)) await revokeFamily(c.env.DB, row.family_id);
  return c.body(null, 200, { "Cache-Control": "no-store" });
});

oauthRoutes.get("/oauth/connections", async (c) => {
  const user = await currentUser(c.env, getCookie(c, SESSION_COOKIE));
  if (!user) return c.redirect("/login");
  const { results } = await c.env.DB.prepare(
    "SELECT t.family_id, c.client_name, c.redirect_uris, MAX(t.created_at) AS last FROM oauth_tokens t LEFT JOIN oauth_clients c ON c.client_id = t.client_id " +
      "WHERE t.user_id = ?1 AND t.revoked = 0 AND t.expires_at > ?2 GROUP BY t.family_id ORDER BY last DESC",
  ).bind(user.id, isoIn(0)).all<{ family_id: string; client_name: string | null; redirect_uris: string | null; last: string }>();
  const rows = results.map((r) => {
    let host = "";
    try { host = r.redirect_uris ? new URL(JSON.parse(r.redirect_uris)[0]).host : ""; } catch { /* a malformed registration still lists its connection */ }
    return `<tr><td>${esc(r.client_name ?? "Unnamed application")}<br><span class="host">${esc(host)}</span></td><td>${esc(r.last.slice(0, 10))}</td><td><form method="post" action="/oauth/connections/revoke"><input type="hidden" name="family" value="${esc(r.family_id)}"><button type="submit">Withdraw</button></form></td></tr>`;
  }).join("");
  return c.html(page("Connected applications", `<h1>Connected applications</h1>${rows ? `<table><tr><th>Application</th><th>Last access granted</th><th></th></tr>${rows}</table>` : "<p>No applications have access.</p>"}<p><a href="/dashboard">Back to dashboard</a></p>`));
});

oauthRoutes.post("/oauth/connections/revoke", async (c) => {
  if (c.req.header("origin") !== new URL(c.req.url).origin && c.req.header("origin") !== issuerOf(c.env, c.req.url)) return c.text("Forbidden", 403);
  const user = await currentUser(c.env, getCookie(c, SESSION_COOKIE));
  if (!user) return c.redirect("/login");
  const family = String((await c.req.parseBody()).family ?? "");
  await c.env.DB.prepare("UPDATE oauth_tokens SET revoked = 1 WHERE family_id = ?1 AND user_id = ?2").bind(family, user.id).run();
  return c.redirect("/oauth/connections", 303);
});

/** Daily: drop expired codes and tokens, and clients that never obtained a token. */
export async function purgeOAuth(db: D1Database) {
  await db.batch([
    db.prepare("DELETE FROM oauth_codes WHERE expires_at < ?1").bind(isoIn(-86_400_000)),
    db.prepare("DELETE FROM oauth_tokens WHERE expires_at < ?1").bind(isoIn(-7 * 86_400_000)),
    db.prepare("DELETE FROM oauth_clients WHERE created_at < ?1 AND client_id NOT IN (SELECT client_id FROM oauth_tokens)").bind(isoIn(-7 * 86_400_000)),
  ]);
}
