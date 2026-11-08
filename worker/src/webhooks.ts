// Webhook subscriptions for ownership-change events, delivered by the cron trigger.
// Each POST body is {"events": [...]} signed as Stripe does: header
//   X-JapanStake-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256 of "<t>.<body>" with the subscription secret>
import { EVENT_TYPES } from "./data";
import { filerFlags, publicEvent, publicEventType, storedEventType } from "./privacy";

const MAX_PER_OWNER = 5;
const BATCH = 100;
const MAX_PAGES_PER_RUN = 50; // up to 5,000 events per subscription per run
const DISABLE_AFTER_FAILURES = 50; // about four hours of 5-minute retries
const DISCLOSURE_TYPES = ["filing", "tender_offer", "voting"] as const;

/** SQL condition on webhook row `w`: its owner has a paid plan now. Accounts own their webhooks by user ID;
 * keys issued outside accounts own theirs as "key:<hash>". A no-plan ("free") or revoked owner matches nothing. */
export const PAID_OWNER_SQL =
  "(EXISTS (SELECT 1 FROM users u WHERE u.id = w.owner AND u.plan IN ('starter','pro','business')) OR " +
  "(w.owner LIKE 'key:%' AND EXISTS (SELECT 1 FROM api_keys k WHERE k.key_hash = substr(w.owner, 5) AND k.user_id IS NULL AND k.revoked = 0 AND k.plan IN ('starter','pro','business'))))";

const randomHex = (bytes: number) => [...crypto.getRandomValues(new Uint8Array(bytes))].map((b) => b.toString(16).padStart(2, "0")).join("");

async function hmacHex(secret: string, message: string) {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** HTTPS to a public host name. DNS is checked separately immediately before storing or sending. */
export function checkUrl(raw: string, _dev = false): string | null {
  let u: URL;
  try {
    u = new URL(raw);
  } catch {
    return "url must be an absolute URL.";
  }
  if (u.protocol !== "https:") return "url must use https.";
  const host = u.hostname.replace(/\.+$/, ""); // "localhost." is localhost
  if (!host || /^(localhost|metadata|.*\.localhost|.*\.local|.*\.internal|.*\.home\.arpa)$/i.test(host) || /^[\d.]+$|^\[/.test(u.hostname)) return "url must use a public host name.";
  return null;
}

const ipv4 = (s: string) => {
  const p = s.split(".");
  if (p.length !== 4 || p.some((x) => !/^\d{1,3}$/.test(x) || Number(x) > 255)) return null;
  return p.map(Number);
};

/** IPv4 special-purpose space (RFC 6890 and later IANA additions). */
function privateV4([a, b, c]: number[]): boolean {
  return a === 0 || a === 10 || a === 127 || (a === 169 && b === 254) ||
    (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 0 && c === 0) ||
    (a === 192 && b === 0 && c === 2) || (a === 192 && b === 88 && c === 99) ||
    (a === 192 && b === 168) || (a === 198 && (b === 18 || b === 19)) ||
    (a === 198 && b === 51 && c === 100) || (a === 203 && b === 0 && c === 113) ||
    (a === 100 && b >= 64 && b <= 127) || a >= 224;
}

/** Parses any IPv6 spelling (leading zeros, "::", trailing dotted IPv4) to 16 bytes; null if it is not one. */
export function parseIPv6(raw: string): Uint8Array | null {
  let s = raw;
  const dotted = /^(.*:)(\d{1,3}(?:\.\d{1,3}){3})$/.exec(s);
  if (dotted) {
    const v4 = ipv4(dotted[2]);
    if (!v4) return null;
    s = dotted[1] + ((v4[0] << 8) | v4[1]).toString(16) + ":" + ((v4[2] << 8) | v4[3]).toString(16);
  }
  const halves = s.split("::");
  if (halves.length > 2) return null;
  const groups = (h: string) => (h === "" ? [] : h.split(":"));
  const head = groups(halves[0]);
  const tail = halves.length === 2 ? groups(halves[1]) : [];
  const missing = 8 - head.length - tail.length;
  if (halves.length === 2 ? missing < 1 : missing !== 0) return null;
  const all = [...head, ...Array(halves.length === 2 ? missing : 0).fill("0"), ...tail];
  if (all.some((g) => !/^[0-9a-f]{1,4}$/.test(g))) return null;
  const out = new Uint8Array(16);
  all.forEach((g, i) => { const n = parseInt(g, 16); out[2 * i] = n >> 8; out[2 * i + 1] = n & 255; });
  return out;
}

/** Blocks IPv4/IPv6 special-purpose space. Fails closed: anything that is not a parseable address is treated as private. */
export function privateAddress(raw: string): boolean {
  const s = raw.trim().toLowerCase().replace(/^\[|\]$/g, "").split("%")[0]; // %zone ids are link-local only
  if (!s.includes(":")) {
    const v4 = ipv4(s);
    return v4 ? privateV4(v4) : true;
  }
  const b = parseIPv6(s);
  if (!b) return true;
  const zero = (from: number, to: number) => b.subarray(from, to).every((x) => x === 0);
  const embedded = (at: number) => privateV4([...b.subarray(at, at + 4)]);
  if (zero(0, 12)) return true; // ::/96: unspecified, loopback and deprecated IPv4-compatible (::7f00:1)
  if (zero(0, 10) && b[10] === 0xff && b[11] === 0xff) return embedded(12); // ::ffff:0:0/96 IPv4-mapped
  if (zero(0, 8) && b[8] === 0xff && b[9] === 0xff && zero(10, 12)) return embedded(12); // ::ffff:0:0:0/96 IPv4-translated
  if (b[0] === 0x00 && b[1] === 0x64 && b[2] === 0xff && b[3] === 0x9b) {
    if (zero(4, 12)) return embedded(12); // 64:ff9b::/96 NAT64 well-known prefix
    return true; // 64:ff9b:1::/48 local-use NAT64 and the rest of 64:ff9b::/32
  }
  if (b[0] === 0x20 && b[1] === 0x02) return embedded(2); // 2002::/16 6to4
  // Only 2000::/3 is global unicast. This covers ::/8, 100::/64, fc00::/7, fe80::/10, fec0::/10 and ff00::/8.
  if ((b[0] & 0xe0) !== 0x20) return true;
  if (b[0] === 0x20 && b[1] === 0x01 && b[2] < 0x02) return true; // 2001::/23 IETF protocol assignments (Teredo, benchmarking, ORCHID)
  if (b[0] === 0x20 && b[1] === 0x01 && b[2] === 0x0d && b[3] === 0xb8) return true; // 2001:db8::/32 documentation
  if (b[0] === 0x3f && b[1] === 0xff && b[2] < 0x10) return true; // 3fff::/20 documentation
  return false;
}

async function doh(host: string, type: "A" | "AAAA") {
  const u = `https://cloudflare-dns.com/dns-query?name=${encodeURIComponent(host)}&type=${type}`;
  const r = await fetch(u, { headers: { accept: "application/dns-json" }, redirect: "manual", signal: AbortSignal.timeout(5_000) });
  if (!r.ok) throw new Error(`DNS over HTTPS returned ${r.status}`);
  const j: any = await r.json();
  if (j.Status !== 0) throw new Error(`DNS lookup status ${j.Status}`);
  const wanted = type === "A" ? 1 : 28;
  return (j.Answer ?? []).filter((x: any) => x.type === wanted).map((x: any) => String(x.data));
}

/** Resolve through DoH and fail closed. This rejects every returned private address, not just the
 * first one.
 * KNOWN GAP (DNS rebinding / TOCTOU): Workers fetch() resolves the host itself and offers no way to pin the
 * connection to the addresses checked here, so an attacker-controlled name can answer this lookup with a
 * public address and the connection's lookup with a private one. This check cannot close that gap. It is
 * narrowed, not removed, by rechecking before every delivery (post) and by redirect:"manual", which stops a
 * public endpoint from redirecting into a private one. Fully closing it needs a connect-time egress control
 * (for example a filtering proxy that resolves and connects itself); that is deliberately not done here. */
export async function validateUrl(raw: string, dev = false): Promise<string | null> {
  const syntax = checkUrl(raw, dev);
  if (syntax) return syntax;
  const host = new URL(raw).hostname;
  try {
    const addresses = (await Promise.all([doh(host, "A"), doh(host, "AAAA")])).flat();
    if (!addresses.length) return "url host could not be resolved.";
    if (addresses.some(privateAddress)) return "url host resolves to a private or local address.";
    return null;
  } catch {
    return "url host could not be resolved.";
  }
}

export type NewWebhook = { url: string; types?: string[]; tickers?: string[]; replay_from?: string };

export async function create(db: D1Database, owner: string, body: NewWebhook, dev: boolean) {
  const isStrings = (v: unknown) => v === undefined || (Array.isArray(v) && v.every((x) => typeof x === "string"));
  if (!isStrings(body.types) || !isStrings(body.tickers)) return { error: "types and tickers must be arrays of strings." };
  const types = body.types?.length ? body.types : null;
  const bad = types?.filter((t) => !(DISCLOSURE_TYPES as readonly string[]).includes(t) && !(EVENT_TYPES as readonly string[]).includes(t));
  if (bad?.length) return { error: `Unknown event types: ${bad.join(", ")}. Valid: ${EVENT_TYPES.join(", ")}, ${DISCLOSURE_TYPES.join(", ")}.` };
  if ((body.types?.length ?? 0) > 50 || [...(body.types ?? []), ...(body.tickers ?? [])].some((t) => t.length > 64)) return { error: "types: at most 50 entries; every entry at most 64 characters." };
  const tickers = body.tickers?.length ? body.tickers.map((t) => String(t).toUpperCase()).slice(0, 500) : null;
  // Cheap checks first: an account at its limit must not be able to make the Worker run DNS lookups.
  const count = await db.prepare("SELECT COUNT(*) AS n FROM webhooks WHERE owner = ?1").bind(owner).first<{ n: number }>();
  if ((count?.n ?? 0) >= MAX_PER_OWNER) return { error: `At most ${MAX_PER_OWNER} webhooks per account.` };
  const urlError = await validateUrl(String(body?.url ?? ""), dev);
  if (urlError) return { error: urlError };
  const now = new Date().toISOString();
  // replay_from lets a subscriber receive events first seen after that time (e.g. after an outage on their side).
  const cursorAt = body.replay_from && !Number.isNaN(Date.parse(body.replay_from)) ? new Date(body.replay_from).toISOString() : now;
  const id = "wh_" + randomHex(8);
  const secret = "whsec_" + randomHex(24);
  // The limit is checked again inside the insert, so concurrent requests cannot pass it together.
  const inserted = await db
    .prepare(
      "INSERT INTO webhooks (id, owner, url, secret, types, tickers, created_at, cursor_at) " +
        "SELECT ?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8 WHERE (SELECT COUNT(*) FROM webhooks WHERE owner = ?2) < ?9",
    )
    .bind(id, owner, body.url, secret, types && JSON.stringify(types), tickers && JSON.stringify(tickers), now, cursorAt, MAX_PER_OWNER)
    .run();
  if (!inserted.meta.changes) return { error: `At most ${MAX_PER_OWNER} webhooks per account.` };
  return { id, url: body.url, types, tickers, secret, created_at: now, delivering_events_first_seen_after: cursorAt };
}

const publicView = (w: any) => ({
  id: w.id, url: w.url, types: w.types ? JSON.parse(w.types) : null, tickers: w.tickers ? JSON.parse(w.tickers) : null,
  created_at: w.created_at, enabled: w.enabled === 1, delivered_events: w.delivered, consecutive_failures: w.failures,
  last_attempt_at: w.last_attempt_at, last_status: w.last_status, last_error: w.last_error,
});

export async function list(db: D1Database, owner: string) {
  const { results } = await db.prepare("SELECT * FROM webhooks WHERE owner = ?1 ORDER BY created_at").bind(owner).all();
  return results.map(publicView);
}

export async function remove(db: D1Database, owner: string, id: string) {
  const r = await db.prepare("DELETE FROM webhooks WHERE owner = ?1 AND id = ?2").bind(owner, id).run();
  return r.meta.changes > 0;
}

export async function post(w: any, events: unknown[]) {
  const urlError = await validateUrl(String(w.url ?? ""));
  if (urlError) throw new Error(`Unsafe webhook URL: ${urlError}`);
  const body = JSON.stringify({ webhook_id: w.id, events });
  const t = Math.floor(Date.now() / 1000);
  return fetch(w.url, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "user-agent": "japanstake-webhooks/1",
      "x-japanstake-signature": `t=${t},v1=${await hmacHex(w.secret, `${t}.${body}`)}`,
    },
    body,
    redirect: "manual",
    signal: AbortSignal.timeout(10_000),
  });
}

// Columns and transform shared by cron delivery and the test send, so a test payload looks exactly like a real one.
const EVENT_SELECT = () =>
  `SELECT e.event_id, e.ingested_at, e.type, e.issuer_ticker, e.record, i.name_ja AS master_issuer_name_ja, i.name_en AS master_issuer_name_en, ${filerFlags("e.filer_edinet_code")} ` +
  "FROM ownership_events e LEFT JOIN issuers i ON i.ticker = e.issuer_ticker AND i.listed = 1 ";
const deliveredEvent = (e: any) => ({ ...publicEvent(e.record, e), ingested_at: e.ingested_at });
const subscription = (w: any) => ({
  types: (w.types ? JSON.parse(w.types) : null) as string[] | null,
  tickers: (w.tickers ? JSON.parse(w.tickers) : null) as string[] | null,
});

/** Sends one sample event so a subscriber can test signature checks: the latest stored event that this
 * subscription would receive (its types and tickers), shaped as cron delivery shapes it. No match sends an empty list. */
export async function sendTest(db: D1Database, owner: string, id: string) {
  const w = await db.prepare(`SELECT w.* FROM webhooks w WHERE w.owner = ?1 AND w.id = ?2 AND ${PAID_OWNER_SQL}`).bind(owner, id).first<any>();
  if (!w) return null;
  let ev: any = null;
  try {
    const { types, tickers } = subscription(w);
    // Filters are public type names; the table stores the internal ones. Disclosure types match no ownership event.
    const stored = types && types.filter((t) => (EVENT_TYPES as readonly string[]).includes(t)).map(storedEventType);
    ev = await db
      .prepare(
        EVENT_SELECT() +
          "WHERE (?1 IS NULL OR e.type IN (SELECT value FROM json_each(?1))) AND (?2 IS NULL OR e.issuer_ticker IN (SELECT value FROM json_each(?2))) " +
          "ORDER BY e.ingested_at DESC, e.event_id DESC LIMIT 1",
      )
      .bind(stored && JSON.stringify(stored), tickers && JSON.stringify(tickers))
      .first<any>();
    const res = await post(w, ev ? [{ ...deliveredEvent(ev), test: true }] : []);
    return { status: res.status, ok: res.ok };
  } catch (e) {
    return { status: 0, ok: false, error: String(e) };
  }
}

/** Cron: deliver new events to every enabled subscription, page by page until caught up.
 * The cursor moves past events that do not match the filters, and past matching ones only after a 2xx. */
export async function deliverAll(db: D1Database) {
  const { results: hooks } = await db.prepare(
    `SELECT w.* FROM webhooks w WHERE w.enabled = 1 AND ${PAID_OWNER_SQL}`,
  ).all<any>();
  for (const w of hooks) {
    try { await deliverOne(db, w); } catch (e) { console.error("webhook", w.id, String(e).slice(0, 100)); } // a corrupt row must not stop the rest
  }
}

async function deliverOne(db: D1Database, w: any) {
  const { types, tickers } = subscription(w);
  let cursor = { at: w.cursor_at as string, id: w.cursor_id as string };
  let delivered = 0;
  let attempt: { at: string; status: number } | null = null;
  for (let page = 0; page < MAX_PAGES_PER_RUN; page++) {
    const { results } = await db
      .prepare(
        EVENT_SELECT() +
          "WHERE ingested_at > ?1 OR (ingested_at = ?1 AND event_id > ?2) ORDER BY ingested_at, event_id LIMIT ?3",
      )
      .bind(cursor.at, cursor.id, BATCH)
      .all<any>();
    if (!results.length) break;
    const matching = results
      .filter((e) => (!types || types.includes(publicEventType(e.type))) && (!tickers || tickers.includes(e.issuer_ticker)))
      .map(deliveredEvent);
    if (matching.length) {
      let error: string | null = null;
      let status = 0;
      try {
        const res = await post(w, matching);
        status = res.status;
        if (!res.ok) error = `HTTP ${res.status}`;
      } catch (e) {
        error = String(e).slice(0, 200);
      }
      attempt = { at: new Date().toISOString(), status };
      if (error) {
        await db
          .prepare(
            "UPDATE webhooks SET cursor_at = ?2, cursor_id = ?3, delivered = delivered + ?4, failures = failures + 1, " +
              "enabled = CASE WHEN failures + 1 >= ?5 THEN 0 ELSE 1 END, last_attempt_at = ?6, last_status = ?7, last_error = ?8 WHERE id = ?1",
          )
          .bind(w.id, cursor.at, cursor.id, delivered, DISABLE_AFTER_FAILURES, attempt.at, status, error)
          .run();
        return;
      }
      delivered += matching.length;
    }
    const last = results[results.length - 1];
    cursor = { at: last.ingested_at, id: last.event_id };
    if (results.length < BATCH) break;
  }
  await db
    .prepare(
      "UPDATE webhooks SET cursor_at = ?2, cursor_id = ?3, delivered = delivered + ?4, failures = CASE WHEN ?5 THEN 0 ELSE failures END, " +
        "last_attempt_at = COALESCE(?6, last_attempt_at), last_status = COALESCE(?7, last_status), last_error = CASE WHEN ?5 THEN NULL ELSE last_error END WHERE id = ?1",
    )
    .bind(w.id, cursor.at, cursor.id, delivered, attempt ? 1 : 0, attempt?.at ?? null, attempt?.status ?? null)
    .run();
}
