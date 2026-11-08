-- Investor groups, positions and stated joint filings (japanfilings/investors.py). No natural persons are stored.
CREATE TABLE IF NOT EXISTS investor_groups (
  group_id TEXT PRIMARY KEY, name_en TEXT, name_ja TEXT, kind TEXT, confidence TEXT,
  n_members INTEGER, n_companies INTEGER, search_text TEXT, record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ig_rank ON investor_groups(kind, n_companies);
CREATE TABLE IF NOT EXISTS investor_group_members (
  group_id TEXT, member_key TEXT, name TEXT, edinet_code TEXT, sources TEXT, match_rule TEXT, confidence TEXT,
  PRIMARY KEY (group_id, member_key)
);
CREATE INDEX IF NOT EXISTS igm_code ON investor_group_members(edinet_code);
CREATE TABLE IF NOT EXISTS investor_positions (
  group_id TEXT, ticker TEXT, source TEXT, holder_key TEXT, filer_edinet_code TEXT, holder_name TEXT,
  first_date TEXT, first_kind TEXT, first_ratio_pct REAL, last_date TEXT, last_ratio_pct REAL, last_doc_id TEXT,
  status TEXT, exit_date TEXT, n_filings INTEGER, history TEXT,
  PRIMARY KEY (group_id, ticker, source, holder_key)
);
CREATE INDEX IF NOT EXISTS ip_ticker ON investor_positions(ticker, status);
CREATE TABLE IF NOT EXISTS investor_joint_filings (
  group_a TEXT, group_b TEXT, ticker TEXT, latest_doc_id TEXT, latest_date TEXT, n_filings INTEGER,
  PRIMARY KEY (group_a, group_b, ticker)
);
CREATE INDEX IF NOT EXISTS ijf_b ON investor_joint_filings(group_b);
CREATE TABLE IF NOT EXISTS investor_excluded_filers (edinet_code TEXT PRIMARY KEY);
