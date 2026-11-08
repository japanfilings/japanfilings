-- Credit ratings as issuers state them in bond filings on EDINET.
CREATE TABLE IF NOT EXISTS disclosed_ratings (
  doc_id TEXT,
  agency TEXT,
  rating TEXT,
  rating_date TEXT,
  submit_date TEXT,
  edinet_code TEXT,
  ticker TEXT,
  record TEXT NOT NULL,
  PRIMARY KEY (doc_id, agency, rating)
);
CREATE INDEX IF NOT EXISTS dr_ticker ON disclosed_ratings(ticker, submit_date);
