-- Requests sent through the /contact form. The sender's address is kept only to answer; the network address is kept
-- only as a day-salted hash for rate limiting. Rows older than a year are deleted by the daily cron.
CREATE TABLE IF NOT EXISTS contact_messages (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  kind TEXT NOT NULL,
  page TEXT,
  message TEXT NOT NULL,
  reply_email TEXT NOT NULL,
  ip_hash TEXT NOT NULL,
  notified INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'new'
);
CREATE INDEX IF NOT EXISTS contact_messages_ip ON contact_messages (ip_hash, created_at);
