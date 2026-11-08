-- One row per data product. Run outcomes come from the ingest (japanfilings/refresh.py publish) or, for
-- the filing list, from the Worker's own poll. Status is computed at read time, never stored.
CREATE TABLE IF NOT EXISTS dataset_status (
  dataset TEXT PRIMARY KEY,
  last_attempt_at TEXT, last_success_at TEXT, last_error TEXT,
  newest_source_date TEXT, rows INTEGER, companies INTEGER,
  open_gap INTEGER, stats_at TEXT
);
-- Why a company's release could not be read (published from the store's ir_crawl).
CREATE TABLE IF NOT EXISTS ir_status (
  ticker TEXT PRIMARY KEY, checked_at TEXT, outcome TEXT, pages_read INTEGER, tanshin_links INTEGER, seed_pages TEXT
);
CREATE TABLE IF NOT EXISTS listing_changes (ticker TEXT, edinet_code TEXT, change TEXT, day TEXT, PRIMARY KEY (edinet_code, change, day));
-- At most one open request per company, enforced by the database.
CREATE TABLE IF NOT EXISTS refresh_requests (
  id TEXT PRIMARY KEY, ticker TEXT NOT NULL, owner TEXT, source TEXT NOT NULL, priority INTEGER NOT NULL,
  status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
  requested_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, checked_at TEXT, result TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS refresh_open ON refresh_requests(ticker) WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS refresh_ticker ON refresh_requests(ticker, requested_at);
CREATE INDEX IF NOT EXISTS refresh_owner ON refresh_requests(owner, requested_at);
CREATE INDEX IF NOT EXISTS refresh_queue ON refresh_requests(status, priority, requested_at);
-- When the Worker first saw a filing; the feed behind /v1/filings/latest and filing webhooks.
CREATE TABLE IF NOT EXISTS filing_feed (doc_id TEXT PRIMARY KEY, first_seen_at TEXT NOT NULL, day TEXT NOT NULL, withdrawn INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS feed_seen ON filing_feed(first_seen_at, doc_id);
CREATE INDEX IF NOT EXISTS feed_day ON filing_feed(day);
CREATE TABLE IF NOT EXISTS filing_poll_state (day TEXT PRIMARY KEY, process_datetime TEXT, doc_count INTEGER, polled_at TEXT);
CREATE TABLE IF NOT EXISTS webhook_filing_cursor (webhook_id TEXT PRIMARY KEY, cursor_at TEXT NOT NULL, cursor_id TEXT NOT NULL DEFAULT '');
