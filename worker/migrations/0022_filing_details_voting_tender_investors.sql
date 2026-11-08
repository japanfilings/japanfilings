CREATE TABLE IF NOT EXISTS filing_details (
  doc_id TEXT NOT NULL, item TEXT NOT NULL, ticker TEXT, edinet_code TEXT,
  fiscal_year_end TEXT, submit_date TEXT, status TEXT NOT NULL, source_url TEXT NOT NULL, record TEXT NOT NULL,
  PRIMARY KEY (doc_id, item)
);
CREATE INDEX IF NOT EXISTS fd_ticker ON filing_details(ticker, item, fiscal_year_end);

CREATE TABLE IF NOT EXISTS voting_results (
  doc_id TEXT NOT NULL, seq INTEGER NOT NULL, ticker TEXT, edinet_code TEXT, meeting_date TEXT, submit_date TEXT,
  proposal_no INTEGER, proposal_title_ja TEXT, candidate_ja TEXT, votes_for INTEGER, votes_against INTEGER,
  votes_abstain INTEGER, stated_rate_pct REAL, rate_basis TEXT, result TEXT, counts_method TEXT,
  amends_doc_id TEXT, source_url TEXT NOT NULL, PRIMARY KEY (doc_id, seq)
);
CREATE INDEX IF NOT EXISTS vr_ticker ON voting_results(ticker, meeting_date);

CREATE TABLE IF NOT EXISTS tender_offers (
  doc_id TEXT PRIMARY KEY, doc_type_code TEXT NOT NULL, related_offer_doc_id TEXT, ticker TEXT, edinet_code TEXT,
  submit_datetime TEXT, target_name_ja TEXT, offeror_name_ja TEXT, price_jpy INTEGER, period_start TEXT,
  period_end TEXT, minimum_shares INTEGER, maximum_shares INTEGER, tendered_shares INTEGER,
  purchased_shares INTEGER, parse_status TEXT NOT NULL, source_url TEXT NOT NULL, record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS to_target ON tender_offers(target_name_ja, submit_datetime);
CREATE INDEX IF NOT EXISTS to_family ON tender_offers(related_offer_doc_id, submit_datetime);

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
  status TEXT, exit_date TEXT, n_filings INTEGER, history TEXT, PRIMARY KEY (group_id, ticker, source, holder_key)
);
CREATE INDEX IF NOT EXISTS ip_ticker ON investor_positions(ticker, status);
CREATE TABLE IF NOT EXISTS investor_joint_filings (
  group_a TEXT, group_b TEXT, ticker TEXT, latest_doc_id TEXT, latest_date TEXT, n_filings INTEGER,
  PRIMARY KEY (group_a, group_b, ticker)
);
CREATE INDEX IF NOT EXISTS ijf_b ON investor_joint_filings(group_b);
CREATE TABLE IF NOT EXISTS investor_excluded_filers (edinet_code TEXT PRIMARY KEY);
