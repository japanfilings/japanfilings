-- Monthly share-buyback reports, and internal-control conclusions and auditor changes, as filed.
CREATE TABLE IF NOT EXISTS buyback_reports (
  doc_id TEXT PRIMARY KEY,
  doc_type_code TEXT,
  amends_doc_id TEXT,
  family_doc_id TEXT,
  ticker TEXT,
  edinet_code TEXT,
  submit_datetime TEXT,
  submit_date TEXT,
  period_start TEXT,
  period_end TEXT,
  n_resolutions INTEGER,
  parse_status TEXT,
  record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bb_ticker ON buyback_reports(ticker, period_end);
CREATE INDEX IF NOT EXISTS bb_family ON buyback_reports(family_doc_id, submit_datetime);
CREATE TABLE IF NOT EXISTS governance_events (
  doc_id TEXT NOT NULL,
  type TEXT NOT NULL,
  ticker TEXT,
  edinet_code TEXT,
  submit_date TEXT,
  record TEXT NOT NULL,
  PRIMARY KEY (doc_id, type)
);
CREATE INDEX IF NOT EXISTS ge_ticker ON governance_events(ticker, submit_date);
CREATE INDEX IF NOT EXISTS ge_type ON governance_events(type, submit_date);
