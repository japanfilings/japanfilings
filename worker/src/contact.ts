// The /contact form: correction, removal and personal-data requests, stored in D1 and forwarded to the owner by email.
import { validEmail } from "./accounts";
import { sha256Hex } from "./auth";

export type ContactEnv = { DB: D1Database; RESEND_API_KEY?: string; MAIL_FROM?: string; CONTACT_EMAIL?: string };

export const CONTACT_KINDS = {
  correction: "Correct a record",
  stop: "Stop showing information about a person",
  personal_data: "Your own account or personal data",
  other: "Something else",
} as const;
export type ContactKind = keyof typeof CONTACT_KINDS;

const MAX_PER_DAY = 5; // per network address
const RETENTION_DAYS = 365;

export type ContactResult = { ok: true } | { ok: false; error: string; status: 400 | 429 };

export async function submit(env: ContactEnv, form: Record<string, unknown>, ip: string, now = new Date()): Promise<ContactResult> {
  // A hidden field people never fill: anything in it is a bot. Answer as if sent, store nothing.
  if (String(form.website ?? "").trim()) return { ok: true };
  const kind = String(form.kind ?? "") as ContactKind;
  const message = String(form.message ?? "").trim();
  const email = String(form.email ?? "").trim().toLowerCase();
  const page = String(form.page ?? "").trim().slice(0, 300) || null;
  if (!(kind in CONTACT_KINDS)) return { ok: false, error: "Please choose what the request is about.", status: 400 };
  if (message.length < 10 || message.length > 5000) return { ok: false, error: "Please write between 10 and 5,000 characters.", status: 400 };
  if (!validEmail(email)) return { ok: false, error: "Please enter an email address we can answer.", status: 400 };
  const day = now.toISOString().slice(0, 10);
  const ipHash = await sha256Hex(`${day}:${ip}`); // changes daily, so it cannot follow a sender over time
  const recent = await env.DB.prepare("SELECT COUNT(*) AS n FROM contact_messages WHERE ip_hash = ?1").bind(ipHash).first<{ n: number }>();
  if ((recent?.n ?? 0) >= MAX_PER_DAY) return { ok: false, error: "Too many requests from this network today. Please try again tomorrow.", status: 429 };
  const id = crypto.randomUUID();
  await env.DB.prepare("INSERT INTO contact_messages (id, created_at, kind, page, message, reply_email, ip_hash) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)")
    .bind(id, now.toISOString(), kind, page, message, email, ipHash).run();
  if (env.RESEND_API_KEY && env.MAIL_FROM && env.CONTACT_EMAIL) {
    const res = await fetch("https://api.resend.com/emails", {
      method: "POST",
      headers: { authorization: `Bearer ${env.RESEND_API_KEY}`, "content-type": "application/json" },
      body: JSON.stringify({
        from: env.MAIL_FROM,
        to: env.CONTACT_EMAIL,
        reply_to: email,
        subject: `[japanfilings] ${CONTACT_KINDS[kind]} (${id.slice(0, 8)})`,
        text: `Kind: ${CONTACT_KINDS[kind]}\nPage: ${page ?? "-"}\nReply to: ${email}\nReceived: ${now.toISOString()}\nID: ${id}\n\n${message}\n`,
      }),
    }).catch(() => null);
    if (res?.ok) await env.DB.prepare("UPDATE contact_messages SET notified = 1 WHERE id = ?1").bind(id).run();
  }
  return { ok: true };
}

/** Daily: requests older than a year go. */
export async function purge(db: D1Database, now = new Date()) {
  const cutoff = new Date(now.getTime() - RETENTION_DAYS * 86_400_000).toISOString();
  await db.prepare("DELETE FROM contact_messages WHERE created_at < ?1").bind(cutoff).run();
}
