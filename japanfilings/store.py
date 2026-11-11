"""SQLite store. One file, no server."""
import json
import os
import re
import sqlite3
import unicodedata

DEFAULT_DB = os.environ.get("JAPANFILINGS_DB", os.path.join(os.path.dirname(__file__), "..", "data", "japanfilings.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS issuers (
  edinet_code TEXT PRIMARY KEY,
  ticker TEXT,
  corporate_number TEXT,
  name_ja TEXT,
  name_en TEXT,
  listed INTEGER,
  industry_ja TEXT,
  fiscal_year_end TEXT,
  capital_million_jpy REAL,
  filer_type TEXT
);
CREATE INDEX IF NOT EXISTS issuers_ticker ON issuers(ticker);
CREATE TABLE IF NOT EXISTS large_holdings (
  doc_id TEXT PRIMARY KEY,
  issuer_ticker TEXT,
  filer_edinet_code TEXT,
  submit_date TEXT,
  record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS lh_issuer ON large_holdings(issuer_ticker, submit_date);
CREATE INDEX IF NOT EXISTS lh_filer ON large_holdings(filer_edinet_code, submit_date);
CREATE TABLE IF NOT EXISTS identifiers (
  ticker TEXT PRIMARY KEY,
  figi TEXT,
  composite_figi TEXT,
  share_class_figi TEXT,
  security_type TEXT,
  name_figi TEXT
);
-- Every EDINET filing seen in the documents list (metadata only, no document text).
CREATE TABLE IF NOT EXISTS filings (
  doc_id TEXT PRIMARY KEY,
  submit_datetime TEXT,
  edinet_code TEXT,
  ticker TEXT,
  filer_name_ja TEXT,
  doc_type_code TEXT,
  description_ja TEXT,
  subject_edinet_code TEXT,
  issuer_edinet_code TEXT,
  parent_doc_id TEXT,
  withdrawn INTEGER,
  has_xbrl INTEGER,
  has_csv INTEGER,
  has_english INTEGER
);
CREATE INDEX IF NOT EXISTS filings_code ON filings(edinet_code, submit_datetime);
CREATE INDEX IF NOT EXISTS filings_type ON filings(doc_type_code, submit_datetime);
-- Parsed annual securities reports (key metrics, statements, company items) as JSON.
CREATE TABLE IF NOT EXISTS annual_reports (
  doc_id TEXT PRIMARY KEY,
  edinet_code TEXT,
  ticker TEXT,
  fiscal_year_end TEXT,
  submit_date TEXT,
  record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ar_ticker ON annual_reports(ticker, fiscal_year_end);
CREATE INDEX IF NOT EXISTS ar_code ON annual_reports(edinet_code, fiscal_year_end);
-- U.S. fund holdings per Tokyo ticker from SEC N-PORT (one summary per ticker per quarter).
CREATE TABLE IF NOT EXISTS us_fund_holdings (
  ticker TEXT PRIMARY KEY,
  quarter TEXT,
  record TEXT NOT NULL
);
"""

FILING_FIELDS = {  # column -> EDINET documents.json field
    "doc_id": "docID", "submit_datetime": "submitDateTime", "edinet_code": "edinetCode",
    "filer_name_ja": "filerName", "doc_type_code": "docTypeCode", "description_ja": "docDescription",
    "subject_edinet_code": "subjectEdinetCode", "issuer_edinet_code": "issuerEdinetCode",
    "parent_doc_id": "parentDocID",
}

ISSUER_COLS = ["edinet_code", "ticker", "corporate_number", "name_ja", "name_en", "listed",
               "industry_ja", "fiscal_year_end", "capital_million_jpy", "filer_type"]


def connect(path: str = DEFAULT_DB) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path, timeout=60)  # parallel ingest workers share this file
    con.execute("PRAGMA journal_mode=WAL")  # a long read must not block writers
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def upsert_issuers(con, issuers):
    # Keep a ticker learned from filings when the code list still has none (new listings).
    updates = ", ".join(f"{c} = excluded.{c}" for c in ISSUER_COLS[2:])
    con.executemany(
        f"INSERT INTO issuers ({','.join(ISSUER_COLS)}) VALUES ({','.join('?' * len(ISSUER_COLS))}) "
        f"ON CONFLICT(edinet_code) DO UPDATE SET ticker = COALESCE(excluded.ticker, issuers.ticker), {updates}",
        [[i[c] for c in ISSUER_COLS] for i in issuers])
    con.commit()


def upsert_filings(con, docs):
    rows = []
    for d in docs:
        row = {col: d.get(src) for col, src in FILING_FIELDS.items()}
        sec = (d.get("secCode") or "").strip()
        row["ticker"] = sec[:4] or None
        row["withdrawn"] = d.get("withdrawalStatus") != "0"
        row["has_xbrl"] = d.get("xbrlFlag") == "1"
        row["has_csv"] = d.get("csvFlag") == "1"
        row["has_english"] = d.get("englishDocFlag") == "1"
        rows.append(row)
    if rows:
        cols = list(rows[0])
        con.executemany(f"INSERT OR REPLACE INTO filings ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        [[r[c] for c in cols] for r in rows])
        con.commit()


def upsert_identifiers(con, ticker, rec):
    con.execute("INSERT OR REPLACE INTO identifiers VALUES (?,?,?,?,?,?)",
                (ticker, rec.get("figi"), rec.get("compositeFIGI"), rec.get("shareClassFIGI"),
                 rec.get("securityType"), rec.get("name")))


def upsert_annual_report(con, rec, submit_date):
    con.execute("INSERT OR REPLACE INTO annual_reports VALUES (?,?,?,?,?,?)",
                (rec["doc_id"], rec["edinet_code"], rec["ticker"], rec["fiscal_year_end"], submit_date,
                 json.dumps(rec, ensure_ascii=False)))
    con.commit()


def has_annual_report(con, doc_id):
    return con.execute("SELECT 1 FROM annual_reports WHERE doc_id = ?", (doc_id,)).fetchone() is not None


def latest_annual_report(con, ticker):
    """Latest fiscal year; for the same year, the most recently submitted (amendments win)."""
    r = con.execute("SELECT record FROM annual_reports WHERE ticker = ? ORDER BY fiscal_year_end DESC, submit_date DESC LIMIT 1",
                    (ticker.upper(),)).fetchone()
    return json.loads(r["record"]) if r else None


def replace_us_fund_holdings(con, summary):
    con.execute("DELETE FROM us_fund_holdings")
    con.executemany("INSERT INTO us_fund_holdings VALUES (?,?,?)",
                    [(t, v["quarter"], json.dumps(v, ensure_ascii=False)) for t, v in summary.items()])
    con.commit()


def us_fund_holdings(con, ticker):
    r = con.execute("SELECT record FROM us_fund_holdings WHERE ticker = ?", (ticker.upper(),)).fetchone()
    return json.loads(r["record"]) if r else None


# A record with neither a title nor a filer means no text was read from the PDF (on 2026-11-05 the
# runner image lost poppler-data and 240 filings were stored empty). It does not count as read, so
# the next run reads the filing again.
EMPTY_LARGE_HOLDING = "json_extract(record, '$.report_title_ja') IS NULL AND filer_edinet_code IS NULL"


def has_large_holding(con, doc_id):
    return con.execute(f"SELECT 1 FROM large_holdings WHERE doc_id = ? AND NOT ({EMPTY_LARGE_HOLDING})", (doc_id,)).fetchone() is not None


def _normalize_name(name):
    """Filings write '日本板硝子(株)', 'XX投資法人  投資証券' or 'Abalance株式会社'; the code list writes
    '日本板硝子株式会社', 'XX投資法人' and full-width 'Ａｂａｌａｎｃｅ株式会社'. Compare NFKC forms."""
    s = unicodedata.normalize("NFKC", name).strip()
    s = re.sub(r"\s*投資証券$", "", s)
    return s.replace("(株)", "株式会社")


_name_index = {}  # id(connection) -> {normalized name: edinet_code}


def _find_by_name(con, name):
    cached = _name_index.get(id(con))
    idx = cached[1] if cached and cached[0] is con else None  # id() is reused once a connection is collected
    if idx is None:
        idx = {}
        for r in con.execute("SELECT edinet_code, name_ja FROM issuers ORDER BY listed"):  # listed rows win
            idx[_normalize_name(r["name_ja"])] = r["edinet_code"]
        _name_index[id(con)] = (con, idx)
    code = idx.get(_normalize_name(name))
    return con.execute("SELECT * FROM issuers WHERE edinet_code = ?", (code,)).fetchone() if code else None


_TICKER_FORM = re.compile(r"^\d{3}[0-9A-Z]$")


def _name_key(name):
    s = unicodedata.normalize("NFKC", name or "")
    s = re.sub(r"\s+", "", s)
    return re.sub(r"株式会社|\(株\)|有限会社|投資法人|投資証券", "", s).lower()


def names_agree(a, b):
    """Loose company-name comparison: equal after NFKC, spacing and legal-form removal, or one contains the other
    (filers append titles to the name, and companies rename: GMOインターネット -> GMOインターネットグループ)."""
    a, b = _name_key(a), _name_key(b)
    return len(a) >= 2 and len(b) >= 2 and (a == b or a in b or b in a)


def _filing_meta(con, doc_id):
    """(issuerEdinetCode, description) EDINET published for this document, or (None, None)."""
    try:
        r = con.execute("SELECT issuer_edinet_code, description_ja FROM filings WHERE doc_id = ?", (doc_id,)).fetchone()
    except sqlite3.OperationalError:
        return None, None
    return (r[0], r[1]) if r else (None, None)


def _issuer_by_code(con, code):
    return con.execute("SELECT * FROM issuers WHERE edinet_code = ?", (code,)).fetchone() if code else None


def link_issuer(con, rec):
    """Attach the target company's English name and IDs, and correct a mistyped issuer ticker.

    Filers type the issuer's securities code and name by hand and get either wrong (7051 for 7501 ティムコ,
    A176 for 176A, 'CaSy' for 9215).  Resolution order:
      1. EDINET's own issuerEdinetCode for the document, when the code list knows it and either the typed ticker
         or the typed name agrees with that company ("edinet_code") -- unless the typed ticker names another
         company that the typed name also fits: EDINET's code is picked by the filer too, and two companies can
         share a name (株式会社アルファ 3434 / 4760; GMOインターネット 4784 / GMOインターネットグループ 9449);
      2. the typed ticker in the code list ("ticker"); the typed name disagreeing is recorded, not acted on;
      3. the typed name in the code list ("name"), which supplies the ticker the filer got wrong, when exactly one
         company (listed ones first) carries that name.
    A company renamed after delisting (デザインワン・ジャパン -> ＧＭＯデザインワン) agrees with neither typed value; EDINET's
    code is still taken when the typed ticker and name point at no other company and the code is not the filer's own.
    When two sources name different companies and nothing breaks the tie, the ticker is left as filed and
    `issuer_match` says so.  The filed value is kept in `issuer_ticker_filed` whenever it is replaced.
    The code list has no ticker for delisted companies and for some new listings. A delisted company's ticker is
    learned only from the company's own filings, never from a report about it: filers mistype codes (UBS filed DD
    Group, 3073, as 3037).  A listed company still missing one (a new listing) takes the typed ticker when no
    other company has it.  The code list flags REITs (and some issuers being delisted) as not listed, so a ticker
    match does not require it."""
    filed = unicodedata.normalize("NFKC", rec.get("issuer_ticker_filed") or rec.get("issuer_ticker") or "").strip() or None
    name = rec.get("issuer_name_ja")
    meta_code, description = _filing_meta(con, rec.get("doc_id"))
    # A change report whose cover page still reads 大量保有報告書 is a change report (EDINET's title says so).
    if rec.get("report_kind") == "initial_report" and "変更報告書" in (description or ""):
        rec["report_kind"] = "change_report"

    def agree(a, b):  # names_agree, also across dash and middle-dot spellings (LTV−X / ＬＴＶ－Ｘ, トシン・グループ)
        clean = lambda x: re.sub(r"[・･]", "", re.sub(r"[‐‑‒–—―−ｰ]", "-", unicodedata.normalize("NFKC", x or "")))
        return names_agree(clean(a), clean(b))

    def same_name(a, b):
        clean = lambda x: re.sub(r"[・･\-]", "", _name_key(re.sub(r"[‐‑‒–—―−ｰ]", "-", unicodedata.normalize("NFKC", x or ""))))
        return bool(clean(a)) and clean(a) == clean(b)

    def own_ticker(code):
        """The one securities code the company's own EDINET filings carry, or None."""
        try:
            got = con.execute("SELECT DISTINCT ticker FROM filings WHERE edinet_code = ? AND ticker IS NOT NULL "
                              "AND ticker NOT IN ('', '0000') LIMIT 2", (code,)).fetchall()
        except sqlite3.OperationalError:  # a store without the filings table
            return None
        return got[0][0] if len(got) == 1 else None

    by_ticker = con.execute("SELECT * FROM issuers WHERE ticker = ? ORDER BY listed DESC", (filed,)).fetchone() if filed else None
    by_name = _find_by_name(con, name) if name else None
    if by_name is not None and con.execute(  # two companies of this name, neither preferred as the listed one
            "SELECT 1 FROM issuers WHERE name_ja = ? AND edinet_code != ? AND COALESCE(listed, 0) >= COALESCE(?, 0) LIMIT 1",
            (by_name["name_ja"], by_name["edinet_code"], by_name["listed"])).fetchone():
        by_name = None
    by_meta = _issuer_by_code(con, meta_code)
    row = match = None
    if by_meta is not None and by_ticker is not None and by_ticker["edinet_code"] != by_meta["edinet_code"] \
            and by_meta["ticker"] != filed and agree(by_meta["name_ja"], name) and agree(by_ticker["name_ja"], name) \
            and (same_name(by_ticker["name_ja"], name) or not same_name(by_meta["name_ja"], name)):
        # The typed ticker and typed name agree on another company. When only that company carries exactly the typed
        # name (EDINET's code is a subsidiary: サカイ for サカイホールディングス 9446), the ticker settles it; when both do
        # (株式会社アルファ), it is left as filed and marked.
        if not (same_name(by_ticker["name_ja"], name) and not same_name(by_meta["name_ja"], name)):
            match = "metadata_conflict"
    elif by_meta is not None and ((filed is not None and by_meta["ticker"] == filed) or agree(by_meta["name_ja"], name)):
        row, match = by_meta, "edinet_code"
    elif by_meta is not None:
        match = "metadata_conflict"  # EDINET's code agrees with neither the typed ticker nor the typed name
        filer_names = [rec.get("filer_name_ja")] + [h.get("name") for h in rec.get("holders") or []]
        if by_ticker is None and by_name is None and not (by_meta["listed"] and by_meta["ticker"]) \
                and by_meta["edinet_code"] != rec.get("filer_edinet_code") \
                and not any(n and same_name(by_meta["name_ja"], n) for n in filer_names):
            row, match = by_meta, "edinet_code_renamed"  # renamed since: nothing typed points elsewhere
    learned = None
    if row is None:
        if by_ticker is not None and by_name is not None and by_ticker["edinet_code"] != by_name["edinet_code"]:
            row, match = by_ticker, "ticker_name_mismatch"  # not resolved: keep the ticker as filed
        elif by_ticker is not None:
            row = by_ticker
            if match is None:
                match = "ticker" if not name or agree(by_ticker["name_ja"], name) else "ticker_name_mismatch"
        elif by_name is not None:
            row = by_name
            match = match or "name"
        else:
            match = match or "unresolved"
    if row is not None and match in ("edinet_code", "edinet_code_renamed", "name"):
        own = own_ticker(row["edinet_code"]) if not row["listed"] or not row["ticker"] else None
        if own is not None and own != row["ticker"] and not row["listed"]:
            learned = own  # a delisted company: its own filings over the code list's (often empty) ticker
        elif row["ticker"] is None and own is not None:
            learned = own
        elif row["ticker"] is None and row["listed"] and filed and filed != "0000" and _TICKER_FORM.match(filed) and not by_ticker:
            learned = filed  # a new listing the code list has no ticker for yet
        if learned is not None and row["ticker"] is None:
            con.execute("UPDATE issuers SET ticker = ? WHERE edinet_code = ? AND ticker IS NULL", (learned, row["edinet_code"]))
    ticker = filed
    best = learned or (row["ticker"] if row is not None else None)
    if best and match in ("edinet_code", "edinet_code_renamed", "name", "ticker") and best != filed:
        ticker = best
    if ticker != filed:
        rec["issuer_ticker_filed"] = filed
    else:
        rec.pop("issuer_ticker_filed", None)
    rec["issuer_ticker"] = ticker
    if match in ("ticker", "edinet_code") and ticker == filed:
        rec.pop("issuer_match", None)  # the ordinary case is not worth a field
    else:
        rec["issuer_match"] = match
    rec["issuer_name_en"] = row["name_en"] if row else None
    rec["issuer_edinet_code"] = row["edinet_code"] if row else None
    rec["issuer_corporate_number"] = row["corporate_number"] if row else None
    filer = con.execute("SELECT name_en FROM issuers WHERE edinet_code = ?", (rec.get("filer_edinet_code"),)).fetchone()
    rec["filer_name_en"] = filer["name_en"] if filer else None
    from . import largeholding
    warnings = largeholding.record_warnings(rec)
    if warnings:
        rec["data_warnings"] = warnings
    else:
        rec.pop("data_warnings", None)
    return rec


def upsert_large_holding(con, rec):
    link_issuer(con, rec)
    if rec.get("report_kind") == "amendment" and not rec.get("amends_doc_id"):
        try:  # EDINET names the document an amendment corrects (parentDocID)
            p = con.execute("SELECT parent_doc_id FROM filings WHERE doc_id = ?", (rec["doc_id"],)).fetchone()
        except sqlite3.OperationalError:  # a store without the filings table
            p = None
        if p and p[0]:
            rec["amends_doc_id"] = p[0]
    con.execute("INSERT OR REPLACE INTO large_holdings VALUES (?,?,?,?,?)",
                (rec["doc_id"], rec["issuer_ticker"], rec["filer_edinet_code"], rec["submit_date"],
                 json.dumps(rec, ensure_ascii=False)))
    con.commit()


def search_issuers(con, query, listed_only=True, limit=20):
    q = f"%{query}%"
    sql = "SELECT * FROM issuers WHERE (ticker = ? OR name_en LIKE ? OR name_ja LIKE ?)"
    if listed_only:
        sql += " AND listed = 1"
    sql += " ORDER BY ticker LIMIT ?"
    return [dict(r) for r in con.execute(sql, (query.upper(), q, q, limit))]


def get_issuer(con, ticker):
    r = con.execute("SELECT i.*, x.figi, x.composite_figi, x.share_class_figi FROM issuers i "
                    "LEFT JOIN identifiers x ON x.ticker = i.ticker WHERE i.ticker = ? AND i.listed = 1",
                    (ticker.upper(),)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["yahoo_symbol"] = f"{d['ticker']}.T"
    d["bloomberg_ticker"] = f"{d['ticker']} JP"
    return d


def large_holdings(con, ticker=None, filer_edinet_code=None, since=None, limit=50):
    sql, args = "SELECT record FROM large_holdings WHERE 1=1", []
    if ticker:
        sql += " AND issuer_ticker = ?"; args.append(ticker.upper())
    if filer_edinet_code:
        sql += " AND filer_edinet_code = ?"; args.append(filer_edinet_code)
    if since:
        sql += " AND submit_date >= ?"; args.append(since)
    sql += " ORDER BY submit_date DESC LIMIT ?"; args.append(limit)
    return [json.loads(r["record"]) for r in con.execute(sql, args)]


def run_once(con, name, sql):
    """Run a one-time repair, recorded so it never runs twice on the same database."""
    con.execute("CREATE TABLE IF NOT EXISTS repairs_done (name TEXT PRIMARY KEY, at TEXT)")
    if con.execute("SELECT 1 FROM repairs_done WHERE name = ?", (name,)).fetchone():
        return False
    con.execute(sql)
    con.execute("INSERT INTO repairs_done VALUES (?, datetime('now'))", (name,))
    con.commit()
    return True
