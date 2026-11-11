// Email sign-in links, sessions and self-service API keys. Tokens, sessions and keys are
// stored only as SHA-256 hashes.
import { sha256Hex } from "./auth";
import type { Plan } from "./data";

export type AccountEnv = { DB: D1Database; RESEND_API_KEY?: string; MAIL_FROM?: string; DEV_SHOW_LINKS?: string };

const LINK_TTL_MIN = 20;
const SESSION_DAYS = 30;
export const SESSION_COOKIE = "jf_session";

const randomToken = (bytes = 32) =>
  btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(bytes)))).replace(/[+/=]/g, (c) => ({ "+": "-", "/": "_", "=": "" })[c]!);
const isoIn = (ms: number) => new Date(Date.now() + ms).toISOString();

export function validEmail(email: string) {
  return /^[^\s@]{1,64}@[^\s@]{1,190}\.[^\s@]{2,}$/.test(email) && email.length <= 254;
}

/** Creates a one-time link. Returns it only when DEV_SHOW_LINKS is set (local testing without email). */
const MAX_LINKS_PER_WINDOW = 3;
const LINK_WINDOW_MIN = 15;

const MAX_LINKS_PER_NETWORK_HOUR = 10;

export async function startLogin(env: AccountEnv, email: string, origin: string, ip = ""): Promise<{ sent: boolean; devLink?: string; throttled?: boolean }> {
  // A few links per address per window, so the form cannot be used to flood someone's inbox.
  const recent = await env.DB.prepare("SELECT COUNT(*) AS n FROM login_tokens WHERE email = ?1 AND expires_at > ?2")
    .bind(email, isoIn((LINK_TTL_MIN - LINK_WINDOW_MIN) * 60_000))
    .first<{ n: number }>();
  if ((recent?.n ?? 0) >= MAX_LINKS_PER_WINDOW) return { sent: false, throttled: true };
  // And a few per network per hour, so a script cannot send links to many addresses and use up the
  // email quota that sign-in, contact forwarding and owner alerts share. The hash changes daily.
  // Cloudflare always gives the visitor's address in production; a local test server gives none or a
  // loopback address, and is not limited.
  const limited = ip && ip !== "127.0.0.1" && ip !== "::1";
  const ipHash = limited ? await sha256Hex(`${new Date().toISOString().slice(0, 10)}:${ip}`) : null;
  if (ipHash) {
    const fromNetwork = await env.DB.prepare("SELECT COUNT(*) AS n FROM login_tokens WHERE ip_hash = ?1 AND expires_at > ?2")
      .bind(ipHash, isoIn((LINK_TTL_MIN - 60) * 60_000))
      .first<{ n: number }>();
    if ((fromNetwork?.n ?? 0) >= MAX_LINKS_PER_NETWORK_HOUR) return { sent: false, throttled: true };
  }
  const token = randomToken();
  await env.DB.prepare("INSERT INTO login_tokens (token_hash, email, expires_at, ip_hash) VALUES (?1, ?2, ?3, ?4)")
    .bind(await sha256Hex(token), email, isoIn(LINK_TTL_MIN * 60_000), ipHash)
    .run();
  const link = `${origin}/auth/verify?token=${token}`;
  if (env.RESEND_API_KEY && env.MAIL_FROM) {
    const res = await fetch("https://api.resend.com/emails", {
      method: "POST",
      headers: { authorization: `Bearer ${env.RESEND_API_KEY}`, "content-type": "application/json" },
      body: JSON.stringify({
        from: env.MAIL_FROM,
        to: email,
        subject: "Your JapanStake sign-in link",
        text: `Sign in to JapanStake (valid for ${LINK_TTL_MIN} minutes):\n\n${link}\n\nIf you did not request this, ignore this email.`,
      }),
    });
    return { sent: res.ok };
  }
  return { sent: false, devLink: env.DEV_SHOW_LINKS === "1" ? link : undefined };
}

/** Daily: sign-in links a day past their expiry go, with the address they were sent to. */
export async function purgeLoginTokens(db: D1Database) {
  await db.prepare("DELETE FROM login_tokens WHERE expires_at < ?1").bind(isoIn(-86_400_000)).run();
}

/** Consumes a link and returns a new session token, creating the user on first sign-in. */
export async function finishLogin(env: AccountEnv, token: string): Promise<string | null> {
  const hash = await sha256Hex(token);
  const row = await env.DB.prepare(
    "UPDATE login_tokens SET used = 1 WHERE token_hash = ?1 AND used = 0 AND expires_at > ?2 RETURNING email",
  )
    .bind(hash, new Date().toISOString())
    .first<{ email: string }>();
  if (!row) return null;
  let user = await env.DB.prepare("SELECT id FROM users WHERE email = ?1").bind(row.email).first<{ id: string }>();
  if (!user) {
    user = { id: crypto.randomUUID() };
    // Every plan is paid, so a new account has none ("free" = no API access). A local test deployment
    // (DEV_SHOW_LINKS=1, never set in production) starts accounts on Starter so the API can be tried end to end.
    await env.DB.prepare("INSERT INTO users (id, email, created_at, plan) VALUES (?1, ?2, ?3, ?4)")
      .bind(user.id, row.email, isoIn(0), env.DEV_SHOW_LINKS === "1" ? "starter" : "free").run();
  }
  const session = randomToken();
  await env.DB.prepare("INSERT INTO sessions (session_hash, user_id, expires_at) VALUES (?1, ?2, ?3)")
    .bind(await sha256Hex(session), user.id, isoIn(SESSION_DAYS * 86_400_000))
    .run();
  return session;
}

export type User = { id: string; email: string; plan: Plan };

export async function currentUser(env: AccountEnv, session: string | undefined): Promise<User | null> {
  if (!session) return null;
  return env.DB.prepare(
    "SELECT u.id, u.email, u.plan FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.session_hash = ?1 AND s.expires_at > ?2",
  )
    .bind(await sha256Hex(session), new Date().toISOString())
    .first<User>();
}

export async function endSession(env: AccountEnv, session: string | undefined) {
  if (session) await env.DB.prepare("DELETE FROM sessions WHERE session_hash = ?1").bind(await sha256Hex(session)).run();
}

/** Revokes the user's keys and issues a new one. The plain key is returned once and never stored. */
export async function rotateKey(env: AccountEnv, user: User): Promise<string> {
  const key = "jsk_" + randomToken(32);
  await env.DB.batch([
    env.DB.prepare("UPDATE api_keys SET revoked = 1 WHERE user_id = ?1").bind(user.id),
    env.DB.prepare("INSERT INTO api_keys (key_hash, plan, user_id, key_prefix, created_at) VALUES (?1, ?2, ?3, ?4, ?5)").bind(
      await sha256Hex(key), user.plan, user.id, key.slice(0, 7), isoIn(0),
    ),
  ]);
  return key;
}

export async function keyStatus(env: AccountEnv, user: User) {
  const month = new Date().toISOString().slice(0, 7);
  return env.DB.prepare(
    "SELECT k.key_prefix, k.created_at, COALESCE(u.requests, 0) AS requests FROM api_keys k " +
      "LEFT JOIN usage u ON u.key_hash = k.key_hash AND u.month = ?2 WHERE k.user_id = ?1 AND k.revoked = 0",
  )
    .bind(user.id, month)
    .first<{ key_prefix: string; created_at: string; requests: number }>();
}
