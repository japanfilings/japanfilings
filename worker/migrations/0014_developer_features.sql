-- Per-minute counter lives on the monthly usage row, so one upsert per request does both jobs.
ALTER TABLE usage ADD COLUMN minute INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage ADD COLUMN minute_count INTEGER NOT NULL DEFAULT 0;

-- MAX(date) for X-Data-As-Of and /v1/status reads one row instead of scanning.
CREATE INDEX IF NOT EXISTS lh_submit ON large_holdings(submit_date);
CREATE INDEX IF NOT EXISTS ar_submit ON annual_reports(submit_date);
CREATE INDEX IF NOT EXISTS filings_submit ON filings(submit_datetime);
CREATE INDEX IF NOT EXISTS er_release ON earnings_releases(release_date);

-- One small typed row per company, so the screener never parses annual_reports JSON at request time.
-- Rebuilt from annual_reports, company_indicators and disclosed_ratings by the daily job (screener.ts).
CREATE TABLE IF NOT EXISTS company_facts (
  ticker TEXT PRIMARY KEY,
  name_en TEXT, name_ja TEXT, sector_ja TEXT,
  fiscal_year_end TEXT, doc_id TEXT, source_submit_date TEXT,
  revenue REAL, net_income REAL, total_assets REAL,
  ordinary_margin REAL, net_margin REAL, roe REAL, equity_ratio REAL,
  revenue_growth REAL, net_income_growth REAL, ocf_to_assets REAL,
  cross_held_pct REAL,
  strength_score REAL, strength_industry_percentile REAL,
  policy_uncertainty REAL, policy_uncertainty_percentile REAL,
  stated_rating TEXT, stated_rating_agency TEXT, stated_rating_date TEXT, stated_rating_rank INTEGER
);
CREATE INDEX IF NOT EXISTS cf_sector_revenue ON company_facts(sector_ja, revenue, ticker);
CREATE INDEX IF NOT EXISTS cf_revenue ON company_facts(revenue, ticker);
CREATE INDEX IF NOT EXISTS cf_roe ON company_facts(roe, ticker);
CREATE INDEX IF NOT EXISTS cf_equity ON company_facts(equity_ratio, ticker);
CREATE INDEX IF NOT EXISTS cf_margin ON company_facts(ordinary_margin, ticker);
CREATE INDEX IF NOT EXISTS cf_growth ON company_facts(revenue_growth, ticker);
CREATE INDEX IF NOT EXISTS cf_strength ON company_facts(strength_score, ticker);
CREATE INDEX IF NOT EXISTS cf_policy ON company_facts(policy_uncertainty, ticker);

-- Precomputed medians and quartiles: one row read per call.
CREATE TABLE IF NOT EXISTS sector_benchmarks (
  sector_ja TEXT PRIMARY KEY,
  company_count INTEGER NOT NULL,
  as_of TEXT,
  record TEXT NOT NULL
);

-- MCP OAuth. Codes and tokens are stored only as SHA-256 hashes.
CREATE TABLE IF NOT EXISTS oauth_clients (
  client_id TEXT PRIMARY KEY,
  client_name TEXT,
  redirect_uris TEXT NOT NULL,        -- JSON array, matched exactly (loopback: port ignored, RFC 8252)
  reg_bucket TEXT,                    -- hash of IP + hour, for the registration throttle; no raw IP stored
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS oauth_clients_bucket ON oauth_clients(reg_bucket);
CREATE INDEX IF NOT EXISTS oauth_clients_created ON oauth_clients(created_at);
CREATE TABLE IF NOT EXISTS oauth_codes (
  code_hash TEXT PRIMARY KEY,
  family_id TEXT NOT NULL,
  client_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  redirect_uri TEXT NOT NULL,
  code_challenge TEXT NOT NULL,
  resource TEXT NOT NULL,
  scope TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS oauth_codes_expires ON oauth_codes(expires_at);
CREATE TABLE IF NOT EXISTS oauth_tokens (
  token_hash TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                 -- access | refresh
  family_id TEXT NOT NULL,            -- one grant; refresh reuse revokes the whole family
  client_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  resource TEXT NOT NULL,
  scope TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS oauth_tokens_family ON oauth_tokens(family_id);
CREATE INDEX IF NOT EXISTS oauth_tokens_user ON oauth_tokens(user_id);
CREATE INDEX IF NOT EXISTS oauth_tokens_client ON oauth_tokens(client_id);
CREATE INDEX IF NOT EXISTS oauth_tokens_expires ON oauth_tokens(expires_at);
