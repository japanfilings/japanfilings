-- Trades listed in 5% reports ("最近60日間の取得又は処分の状況"), one row per table row.
CREATE TABLE IF NOT EXISTS holder_trades (
  doc_id TEXT,
  row_no INTEGER,
  trade_date TEXT,
  submit_date TEXT,
  issuer_ticker TEXT,
  filer_edinet_code TEXT,
  holder TEXT,
  security_ja TEXT,
  shares REAL,
  pct_of_outstanding REAL,
  venue TEXT,
  side TEXT,
  unit_price_jpy REAL,
  note_ja TEXT,
  kind TEXT,
  PRIMARY KEY (doc_id, row_no)
);
CREATE INDEX IF NOT EXISTS ht_ticker ON holder_trades(issuer_ticker, trade_date);
CREATE INDEX IF NOT EXISTS ht_filer ON holder_trades(filer_edinet_code, trade_date);
