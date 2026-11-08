-- Ownership-change events derived from 5% filings (japanfilings/events.py).
CREATE TABLE IF NOT EXISTS ownership_events (
  event_id TEXT PRIMARY KEY, type TEXT, submit_date TEXT, doc_id TEXT, issuer_ticker TEXT,
  filer_edinet_code TEXT, ingested_at TEXT, record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ev_date ON ownership_events(submit_date);
CREATE INDEX IF NOT EXISTS ev_ingested ON ownership_events(ingested_at, event_id);
CREATE INDEX IF NOT EXISTS ev_ticker ON ownership_events(issuer_ticker, submit_date);

-- Cross-shareholdings from annual reports (japanfilings/crossholdings.py).
CREATE TABLE IF NOT EXISTS cross_holdings (
  holder_ticker TEXT, fiscal_year_end TEXT, doc_id TEXT, row_no INTEGER, holder TEXT, kind TEXT, name_ja TEXT,
  held_ticker TEXT, shares REAL, prior_shares REAL, book_value_jpy REAL, prior_book_value_jpy REAL,
  held_back INTEGER, reciprocal INTEGER, purpose_ja TEXT
);
CREATE INDEX IF NOT EXISTS xh_holder ON cross_holdings(holder_ticker);
CREATE INDEX IF NOT EXISTS xh_held ON cross_holdings(held_ticker);
CREATE TABLE IF NOT EXISTS cross_holding_summary (ticker TEXT PRIMARY KEY, fiscal_year_end TEXT, doc_id TEXT, record TEXT NOT NULL);

-- Webhook subscriptions. owner = user id (or key hash for keys issued outside accounts).
-- The cursor is the last delivered (ingested_at, event_id); new subscriptions start at creation time.
CREATE TABLE IF NOT EXISTS webhooks (
  id TEXT PRIMARY KEY, owner TEXT NOT NULL, url TEXT NOT NULL, secret TEXT NOT NULL,
  types TEXT, tickers TEXT, created_at TEXT NOT NULL,
  cursor_at TEXT NOT NULL, cursor_id TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1, failures INTEGER NOT NULL DEFAULT 0,
  last_attempt_at TEXT, last_status INTEGER, last_error TEXT, delivered INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS wh_owner ON webhooks(owner);
