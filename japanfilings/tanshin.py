"""Table for half-year and quarterly results read from EDINET (halfyear.py, refresh.py). Company websites are not read."""

SCHEMA = """
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
"""
