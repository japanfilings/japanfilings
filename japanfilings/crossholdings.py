"""Cross-shareholding network built from every company's latest annual report.

Each annual report lists the strategic shareholdings (政策保有株式) the company keeps: investee name,
shares and book value this year and last, the stated purpose, and whether the investee holds the
company back. Names are matched to Tokyo tickers here, and a holding is marked `reciprocal` when the
investee's own report lists the holder too.
"""
import html
import json
import re
import unicodedata

SCHEMA = """
CREATE TABLE IF NOT EXISTS cross_holdings (
  holder_ticker TEXT,
  fiscal_year_end TEXT,
  doc_id TEXT,
  row_no INTEGER,
  holder TEXT,
  kind TEXT,
  name_ja TEXT,
  held_ticker TEXT,
  shares REAL,
  prior_shares REAL,
  book_value_jpy REAL,
  prior_book_value_jpy REAL,
  held_back INTEGER,
  reciprocal INTEGER,
  purpose_ja TEXT
);
CREATE INDEX IF NOT EXISTS xh_holder ON cross_holdings(holder_ticker);
CREATE INDEX IF NOT EXISTS xh_held ON cross_holdings(held_ticker);
-- One row per holder: totals from the report's summary table plus counts from the named list.
CREATE TABLE IF NOT EXISTS cross_holding_summary (
  ticker TEXT PRIMARY KEY,
  fiscal_year_end TEXT,
  doc_id TEXT,
  record TEXT NOT NULL
);
"""

_SUFFIXES = re.compile(r"株式会社|\(株\)|\(有\)|有限会社|合同会社|\s|・|　")


def name_key(name):
    """'ＫＤＤＩ㈱', '株式会社KDDI' and 'KDDI株式会社' all become 'KDDI'."""
    s = unicodedata.normalize("NFKC", html.unescape(name or ""))
    s = re.sub(r"[\(（]注.*?[\)）]|\*+|※\d*", "", s)  # footnote marks
    return _SUFFIXES.sub("", s).upper()


def _name_index(con):
    idx = {}
    # The code list can lack a ticker that the company's own annual report states (e.g. 九州電力, 9508).
    for r in con.execute(
            "SELECT COALESCE(i.ticker, (SELECT a.ticker FROM annual_reports a WHERE a.edinet_code = i.edinet_code "
            "ORDER BY a.fiscal_year_end DESC LIMIT 1)) AS ticker, i.name_ja FROM issuers i ORDER BY i.listed"):
        if r["ticker"]:
            idx[name_key(r["name_ja"])] = r["ticker"]  # listed rows come last and win
    return idx


def rebuild(con):
    """Recreate both tables from the latest annual report of every ticker. Returns (holdings, linked)."""
    con.executescript(SCHEMA)
    con.execute("DELETE FROM cross_holdings")
    con.execute("DELETE FROM cross_holding_summary")
    idx = _name_index(con)
    rows, summaries = [], []
    latest = con.execute(
        "SELECT a.ticker, a.record FROM annual_reports a WHERE a.ticker IS NOT NULL AND a.rowid = ("
        "SELECT b.rowid FROM annual_reports b WHERE b.ticker = a.ticker ORDER BY b.fiscal_year_end DESC, b.submit_date DESC LIMIT 1)")
    for r in latest:
        rec = json.loads(r["record"])
        xh = rec.get("cross_holdings") or {}
        holdings = xh.get("holdings") or []
        for n, h in enumerate(holdings, 1):
            held = idx.get(name_key(h.get("name_ja")))
            rows.append([rec["ticker"], rec["fiscal_year_end"], rec["doc_id"], n, h["holder"], h["kind"], h.get("name_ja"),
                         held if held != rec["ticker"] else None, h.get("shares"), h.get("prior_shares"),
                         h.get("book_value_jpy"), h.get("prior_book_value_jpy"),
                         None if h.get("held_back") is None else int(h["held_back"]), None, h.get("purpose_ja")])
        summary = dict(xh.get("summary") or {})
        # Some filers tag thousands of yen as yen (e.g. SOFT99 2026: ¥3.5 trillion of shares against ¥59 billion
        # of net assets). Holdings cannot exceed total assets, so such reports are flagged and left out of rankings.
        assets = max((m.get("total_assets") or 0 for m in (rec.get("key_metrics") or [])[:1]), default=0)
        if assets and (summary.get("listed_carrying_amount_jpy") or 0) > assets:
            summary["data_warning"] = "Amounts exceed the company's total assets; the filing likely tags thousands of yen as yen."
        summaries.append((rec["ticker"], rec["fiscal_year_end"], rec["doc_id"], summary))
    con.executemany(f"INSERT INTO cross_holdings VALUES ({','.join('?' * 15)})", rows)
    # Reciprocal: the investee's own latest report lists the holder.
    con.execute("""UPDATE cross_holdings SET reciprocal = EXISTS (
        SELECT 1 FROM cross_holdings o WHERE o.holder_ticker = cross_holdings.held_ticker AND o.held_ticker = cross_holdings.holder_ticker)
        WHERE held_ticker IS NOT NULL""")
    for ticker, fye, doc_id, summary in summaries:
        stats = con.execute(
            "SELECT COUNT(*) n, SUM(held_ticker IS NOT NULL) linked, SUM(held_back = 1) held_back, SUM(reciprocal = 1) reciprocal, "
            "SUM(book_value_jpy) book, SUM(shares < prior_shares) reduced, SUM(shares > prior_shares) increased, "
            "SUM(shares IS NULL AND prior_shares IS NOT NULL) exited "
            "FROM cross_holdings WHERE holder_ticker = ? AND kind = 'specified'", (ticker,)).fetchone()
        held_by = con.execute("SELECT COUNT(DISTINCT holder_ticker) FROM cross_holdings WHERE held_ticker = ?", (ticker,)).fetchone()[0]
        if not stats["n"] and not summary and not held_by:
            continue
        record = {
            "ticker": ticker, "fiscal_year_end": fye, "source_doc_id": doc_id,
            "named_holdings": stats["n"], "named_holdings_linked_to_ticker": stats["linked"] or 0,
            "named_book_value_jpy": stats["book"], "mutual_as_stated": stats["held_back"] or 0,
            "mutual_confirmed_both_reports": stats["reciprocal"] or 0,
            "named_reduced": stats["reduced"] or 0, "named_increased": stats["increased"] or 0, "named_exited": stats["exited"] or 0,
            "held_by_listed_companies": held_by,
            **summary,
        }
        con.execute("INSERT INTO cross_holding_summary VALUES (?,?,?,?)", (ticker, fye, doc_id, json.dumps(record, ensure_ascii=False)))
    con.commit()
    linked = con.execute("SELECT COUNT(*), SUM(held_ticker IS NOT NULL) FROM cross_holdings").fetchone()
    return linked[0], linked[1] or 0
