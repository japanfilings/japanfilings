CREATE TABLE IF NOT EXISTS annual_reports (
  doc_id TEXT PRIMARY KEY, edinet_code TEXT, ticker TEXT, fiscal_year_end TEXT, submit_date TEXT, record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ar_ticker ON annual_reports(ticker, fiscal_year_end);
CREATE INDEX IF NOT EXISTS ar_code ON annual_reports(edinet_code, fiscal_year_end);

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  plan TEXT NOT NULL DEFAULT 'free',
  created_at TEXT NOT NULL
);
-- One-time sign-in links; only the SHA-256 of the token is stored.
CREATE TABLE IF NOT EXISTS login_tokens (
  token_hash TEXT PRIMARY KEY, email TEXT NOT NULL, expires_at TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
  session_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at TEXT NOT NULL
);
ALTER TABLE api_keys ADD COLUMN user_id TEXT;
ALTER TABLE api_keys ADD COLUMN key_prefix TEXT;
CREATE INDEX IF NOT EXISTS api_keys_user ON api_keys(user_id);
