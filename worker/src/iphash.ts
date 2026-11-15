// Hashes of network addresses, keyed with a random value per UTC day that is deleted after two days.
// A plain hash of an IPv4 address can be reversed by hashing all 2^32 addresses; with the key gone,
// a stored hash no longer leads back to anyone, even for us. See migrations/0028_ip_salts.sql.
import { sha256Hex } from "./auth";

const today = () => new Date().toISOString().slice(0, 10);

async function saltFor(db: D1Database, day: string): Promise<string> {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  const fresh = [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
  // Two requests at the turn of the day may both insert; the first one wins and both read it back.
  await db.prepare("INSERT OR IGNORE INTO ip_salts (day, salt) VALUES (?1, ?2)").bind(day, fresh).run();
  const row = await db.prepare("SELECT salt FROM ip_salts WHERE day = ?1").bind(day).first<{ salt: string }>();
  return row!.salt;
}

/** A hash of `value` (an address, or an address with a time bucket) that is stable for the UTC day. */
export async function ipHash(db: D1Database, value: string): Promise<string> {
  return sha256Hex(`${await saltFor(db, today())}:${value}`);
}

/** Deletes keys older than yesterday (daily cron). */
export async function purgeIpSalts(db: D1Database) {
  const cutoff = new Date(Date.now() - 2 * 86_400_000).toISOString().slice(0, 10);
  await db.prepare("DELETE FROM ip_salts WHERE day < ?1").bind(cutoff).run();
}
