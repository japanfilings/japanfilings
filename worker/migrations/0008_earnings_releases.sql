-- Earnings releases (決算短信) read from company websites: results, full-year forecast, revision flag.
CREATE TABLE IF NOT EXISTS earnings_releases (
  ticker TEXT,
  fiscal_year_end_month TEXT,
  period TEXT,
  release_date TEXT,
  url TEXT,
  record TEXT NOT NULL,
  PRIMARY KEY (ticker, fiscal_year_end_month, period)
);
CREATE INDEX IF NOT EXISTS er_date ON earnings_releases(release_date);
