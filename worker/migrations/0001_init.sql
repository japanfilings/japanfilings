-- Same tables as the Python store (japanfilings/store.py), plus API keys and usage.
CREATE TABLE IF NOT EXISTS issuers (
  edinet_code TEXT PRIMARY KEY, ticker TEXT, corporate_number TEXT, name_ja TEXT, name_en TEXT,
  listed INTEGER, industry_ja TEXT, fiscal_year_end TEXT, capital_million_jpy REAL, filer_type TEXT
);
CREATE INDEX IF NOT EXISTS issuers_ticker ON issuers(ticker);
CREATE TABLE IF NOT EXISTS identifiers (
  ticker TEXT PRIMARY KEY, figi TEXT, composite_figi TEXT, share_class_figi TEXT, security_type TEXT, name_figi TEXT
);
CREATE TABLE IF NOT EXISTS large_holdings (
  doc_id TEXT PRIMARY KEY, issuer_ticker TEXT, filer_edinet_code TEXT, submit_date TEXT, record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS lh_issuer ON large_holdings(issuer_ticker, submit_date);
CREATE INDEX IF NOT EXISTS lh_filer ON large_holdings(filer_edinet_code, submit_date);
CREATE TABLE IF NOT EXISTS filings (
  doc_id TEXT PRIMARY KEY, submit_datetime TEXT, edinet_code TEXT, ticker TEXT, filer_name_ja TEXT,
  doc_type_code TEXT, description_ja TEXT, subject_edinet_code TEXT, issuer_edinet_code TEXT,
  parent_doc_id TEXT, withdrawn INTEGER, has_xbrl INTEGER, has_csv INTEGER, has_english INTEGER
);
CREATE INDEX IF NOT EXISTS filings_code ON filings(edinet_code, submit_datetime);
CREATE INDEX IF NOT EXISTS filings_type ON filings(doc_type_code, submit_datetime);

CREATE TABLE IF NOT EXISTS api_keys (
  key_hash TEXT PRIMARY KEY,          -- SHA-256 hex of the key; the key itself is never stored
  plan TEXT NOT NULL,                 -- free | pro | business
  customer_ref TEXT,                  -- merchant-of-record customer/subscription ID
  created_at TEXT NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS usage (
  key_hash TEXT NOT NULL, month TEXT NOT NULL, requests INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (key_hash, month)
);
