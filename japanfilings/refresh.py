"""Keeps every data product verifiably current: run records, reconciliation against EDINET's list, the refresh queue.

  python -m japanfilings.refresh gaps [--since YYYY-MM-DD]        # read-only: listed vs parsed, per doc type
  python -m japanfilings.refresh reconcile [--days 35] [--minutes 30] [--max-docs 300]
                                                                  # withdrawals, amendments, re-queue and read missed filings
  python -m japanfilings.refresh listings                         # companies listed or delisted, from the code list
  python -m japanfilings.refresh run [--minutes 10]               # claim D1's queue; EDINET documents, then the IR site
  python -m japanfilings.refresh publish [--sql-out FILE]         # after the D1 push: run records, stats, finished requests
  python -m japanfilings.refresh check URL [--ignore a,b]         # exit 1 when a dataset is late or failed

Only EDINET is read. Company websites are not read.
Publishing comes last, so "last successful run" means the data reached D1.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

from . import edinet, store
from .voting import AGM_WINDOW_DAYS as _AGM_WINDOW

ROOT = os.path.join(os.path.dirname(__file__), "..")
DATASETS = json.load(open(os.path.join(ROOT, "worker", "src", "datasets.json"), encoding="utf-8"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS dataset_runs (dataset TEXT PRIMARY KEY, last_attempt_at TEXT, last_success_at TEXT, last_error TEXT);
CREATE TABLE IF NOT EXISTS reconcile_queue (
  doc_id TEXT PRIMARY KEY, dataset TEXT, reason TEXT, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, queued_at TEXT, done_at TEXT);
CREATE TABLE IF NOT EXISTS refresh_work (request_id TEXT PRIMARY KEY, ticker TEXT, status TEXT, result TEXT, checked_at TEXT, published INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS listing_snapshot (edinet_code TEXT PRIMARY KEY, ticker TEXT, listed INTEGER);
CREATE TABLE IF NOT EXISTS issuer_changes (ticker TEXT, edinet_code TEXT, change TEXT, day TEXT, PRIMARY KEY (edinet_code, change, day));
CREATE TABLE IF NOT EXISTS publish_state (name TEXT PRIMARY KEY, value TEXT);
"""
MAX_ATTEMPTS = 5
# (table, column) holding rows derived from one filing; earnings_releases is keyed by url.
DERIVED = [("large_holdings", "doc_id"), ("ownership_events", "doc_id"), ("holder_trades", "doc_id"), ("annual_reports", "doc_id"), ("cross_holdings", "doc_id"),
           ("cross_holding_summary", "doc_id"), ("buyback_reports", "doc_id"), ("governance_events", "doc_id"), ("disclosed_ratings", "doc_id"), ("ratings_read", "doc_id"),
           ("filing_details", "doc_id"), ("filing_details_read", "doc_id"), ("voting_results", "doc_id"), ("voting_read", "doc_id"), ("tender_offers", "doc_id")]
# dataset: which filings should have been read, and the set of those that were.
RULES = {
    "large_holdings": ("f.doc_type_code IN ('350','360')", "SELECT doc_id FROM large_holdings WHERE NOT (json_extract(record, '$.report_title_ja') IS NULL AND filer_edinet_code IS NULL)"),
    "annual": ("f.doc_type_code IN ('120','130') AND f.has_csv = 1 AND f.ticker IS NOT NULL", "SELECT doc_id FROM annual_reports"),
    "buybacks": ("f.doc_type_code IN ('220','230') AND f.has_csv = 1", "SELECT doc_id FROM buyback_reports"),
    "ratings": ("f.doc_type_code = '100'", "SELECT doc_id FROM ratings_read"),
    "governance": ("(f.doc_type_code IN ('235','236') OR (f.doc_type_code = '180' AND f.description_ja = '臨時報告書' AND f.ticker IS NOT NULL))", "SELECT doc_id FROM governance_read WHERE status != 'failed'"),
    "tender_offers": ("f.doc_type_code IN ('240','270','290')", "SELECT doc_id FROM tender_offers"),
    "filing_details": ("f.doc_type_code = '120' AND f.has_csv = 1 AND f.ticker IS NOT NULL", "SELECT doc_id FROM filing_details_read WHERE status = 'read'"),
    # Only reports the voting job opens: those filed in the window around the company's annual report (voting.candidates).
    # Extraordinary reports outside it are other events, not meeting results, and are never read, so they are no gap.
    "voting": ("f.doc_type_code IN ('180','190') AND f.has_csv = 1 AND f.ticker IS NOT NULL AND EXISTS (SELECT 1 FROM filings a WHERE a.edinet_code = f.edinet_code "
               f"AND a.doc_type_code = '120' AND a.withdrawn = 0 AND julianday(substr(f.submit_datetime,1,10)) - julianday(substr(a.submit_datetime,1,10)) BETWEEN {_AGM_WINDOW[0]} AND {_AGM_WINDOW[1]})",
               "SELECT doc_id FROM voting_read"),
}
_HALF = ("SELECT f.doc_id, f.submit_datetime, f.edinet_code, i.ticker FROM filings f JOIN issuers i ON i.edinet_code = f.edinet_code "
         "WHERE f.doc_type_code IN ('160','170') AND i.listed = 1 AND i.ticker IS NOT NULL AND f.has_csv = 1 AND f.withdrawn = 0 ORDER BY f.submit_datetime DESC")
_REBUILD = {"large_holdings": ("events", "trades", "indicators"), "annual": ("crossholdings", "indicators")}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure(con):
    con.executescript(SCHEMA)


def _table_exists(con, t):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (t,)).fetchone() is not None


# ---------- run records ----------

def datasets_for(cmd):
    return [d["id"] for d in DATASETS if cmd in d.get("cmds", [])]


def record_run(con, cmd, error):
    ensure(con)
    at = _now()
    for ds in datasets_for(cmd):
        con.execute("INSERT INTO dataset_runs VALUES (?,?,?,?) ON CONFLICT(dataset) DO UPDATE SET last_attempt_at = excluded.last_attempt_at, "
                    "last_success_at = COALESCE(excluded.last_success_at, last_success_at), last_error = excluded.last_error",
                    (ds, at, None if error else at, error))
    con.commit()


def tracked(main, argv):
    """Runs the CLI and records the outcome per dataset. Only the error's class is kept: messages can hold request URLs."""
    if not datasets_for(argv[0]):
        return main(argv)
    try:
        rc = main(argv)
    except Exception as e:
        _safe_record(argv[0], type(e).__name__)
        raise
    _safe_record(argv[0], f"exit {rc}" if rc else None)
    return rc


def _safe_record(cmd, error):
    try:
        con = store.connect()
        record_run(con, cmd, error)
        con.close()
    except sqlite3.Error as e:
        print("could not record the run:", e, file=sys.stderr)


# ---------- gaps ----------

def _missing(con, ds, since=None):
    """Filings that should have been read for a dataset and were not, newest first."""
    if ds == "half_year":
        have = {r[0] for r in con.execute("SELECT url FROM earnings_releases WHERE url LIKE 'edinet:%'")} if _table_exists(con, "earnings_releases") else set()
        seen, out = set(), []
        for doc_id, sub, code, ticker in con.execute(_HALF):
            if ticker in seen:
                continue
            seen.add(ticker)
            if f"edinet:{doc_id}" not in have and (not since or sub[:10] >= since):
                out.append({"doc_id": doc_id, "submit_datetime": sub, "edinet_code": code})
        return out
    where, parsed = RULES[ds]
    table = re.search(r"FROM (\w+)", parsed).group(1)
    sql = f"SELECT f.doc_id, f.submit_datetime, f.edinet_code, f.issuer_edinet_code FROM filings f WHERE f.withdrawn = 0 AND {where}"
    args = ()
    if since:
        sql += " AND substr(f.submit_datetime, 1, 10) >= ?"
        args = (since,)
    if _table_exists(con, table):
        sql += f" AND f.doc_id NOT IN ({parsed})"
    return [dict(zip(("doc_id", "submit_datetime", "edinet_code", "issuer_edinet_code"), r)) for r in con.execute(sql + " ORDER BY f.submit_datetime DESC", args)]


def gap_report(con, since=None):
    rep = {"since": since, "datasets": {}}
    for ds in [*RULES, "half_year"]:
        m = _missing(con, ds, since)
        rep["datasets"][ds] = {"missing": len(m), "newest_missing": m[0]["submit_datetime"] if m else None, "oldest_missing": m[-1]["submit_datetime"] if m else None}
        if ds in RULES:
            where = RULES[ds][0]
            q = f"SELECT COUNT(*) FROM filings f WHERE f.withdrawn = 0 AND {where}" + (" AND substr(f.submit_datetime,1,10) >= ?" if since else "")
            rep["datasets"][ds]["listed"] = con.execute(q, (since,) if since else ()).fetchone()[0]
            rep["datasets"][ds]["parser_exists"] = _table_exists(con, re.search(r"FROM (\w+)", RULES[ds][1]).group(1))
    rep["withdrawn_with_derived_rows"] = len(withdrawn_derived(con))
    days = sorted({r[0] for r in con.execute("SELECT DISTINCT substr(submit_datetime,1,10) FROM filings WHERE submit_datetime IS NOT NULL")})
    rep["weekdays_without_filings"] = _weekday_holes(days)
    rep["last_filing_day"] = days[-1] if days else None
    rep["tickers_in_filings_not_issuers"] = con.execute("SELECT COUNT(DISTINCT ticker) FROM filings WHERE ticker IS NOT NULL AND ticker NOT IN (SELECT ticker FROM issuers WHERE ticker IS NOT NULL)").fetchone()[0]
    rep["amendments_read_as_superseding"] = con.execute("SELECT COUNT(*) FROM filings WHERE doc_type_code = '360' AND parent_doc_id IN (SELECT doc_id FROM large_holdings)").fetchone()[0]
    listed = con.execute("SELECT COUNT(DISTINCT ticker) FROM issuers WHERE ticker IS NOT NULL").fetchone()[0]
    rep["listed_companies"] = listed
    rep["coverage"] = {}
    for d in DATASETS:
        if d.get("company") and d["id"] != "issuers" and _table_exists(con, d["table"]):
            n = con.execute(f"SELECT COUNT(DISTINCT {d['company']}) FROM {d['table']}" + (f" WHERE {d['where']}" if d.get("where") else "")).fetchone()[0]
            rep["coverage"][d["id"]] = n
    return rep


def _weekday_holes(days):
    if not days:
        return []
    have, d, last, out = set(days), date.fromisoformat(days[0]), date.fromisoformat(days[-1]), []
    while d <= last:
        if d.weekday() < 5 and d.isoformat() not in have:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


# ---------- withdrawals, amendments, listings ----------

def withdrawn_derived(con):
    out = set()
    for t, col in DERIVED:
        if _table_exists(con, t):
            out |= {r[0] for r in con.execute(f"SELECT DISTINCT {col} FROM {t} WHERE {col} IN (SELECT doc_id FROM filings WHERE withdrawn = 1)")}
    if _table_exists(con, "earnings_releases"):
        out |= {r[0][7:] for r in con.execute("SELECT url FROM earnings_releases WHERE url LIKE 'edinet:%' AND substr(url, 8) IN (SELECT doc_id FROM filings WHERE withdrawn = 1)")}
    return out


def apply_withdrawals(con):
    """Rows derived from a withdrawn filing go; the delta export then deletes them from D1."""
    docs = withdrawn_derived(con)
    for doc in docs:
        for t, col in DERIVED:
            if _table_exists(con, t):
                con.execute(f"DELETE FROM {t} WHERE {col} = ?", (doc,))
        if _table_exists(con, "earnings_releases"):
            con.execute("DELETE FROM earnings_releases WHERE url = ?", (f"edinet:{doc}",))
    con.commit()
    return len(docs)


def apply_amendments(con):
    """A 5% report later amended keeps its row, as filed, and says which filing replaced it."""
    n = 0
    rows = con.execute("SELECT f.parent_doc_id, f.doc_id FROM filings f JOIN large_holdings l ON l.doc_id = f.parent_doc_id "
                       "WHERE f.doc_type_code = '360' AND f.withdrawn = 0 ORDER BY f.submit_datetime").fetchall()
    for parent, doc in rows:
        rec = json.loads(con.execute("SELECT record FROM large_holdings WHERE doc_id = ?", (parent,)).fetchone()[0])
        if rec.get("superseded_by") != doc:
            rec["superseded_by"] = doc
            con.execute("UPDATE large_holdings SET record = ? WHERE doc_id = ?", (json.dumps(rec, ensure_ascii=False), parent))
            n += 1
    con.commit()
    return n


def sync_listings(con, issuers, today=None):
    """Compares the current code list with the last one. A company gone from the list stops counting as listed."""
    ensure(con)
    today = today or date.today().isoformat()
    first = con.execute("SELECT COUNT(*) FROM listing_snapshot").fetchone()[0] == 0
    before = {r[0]: (r[1], r[2]) for r in con.execute("SELECT edinet_code, ticker, listed FROM listing_snapshot")}
    now = {i["edinet_code"]: (i["ticker"], 1 if i["listed"] else 0) for i in issuers}
    changes = []
    for code, (ticker, listed) in now.items():
        was = before.get(code)
        if listed and (was is None or not was[1]) and not first:
            changes.append((ticker, code, "listed", today))
        elif was and was[1] and not listed:
            changes.append((ticker or was[0], code, "delisted", today))
    for code, (ticker, listed) in before.items():
        if code not in now and listed:
            changes.append((ticker, code, "delisted", today))
            con.execute("UPDATE issuers SET listed = 0 WHERE edinet_code = ?", (code,))
    con.executemany("INSERT OR IGNORE INTO issuer_changes VALUES (?,?,?,?)", changes)
    con.execute("DELETE FROM listing_snapshot")
    con.executemany("INSERT INTO listing_snapshot VALUES (?,?,?)", [(c, t, l) for c, (t, l) in now.items()])
    con.commit()
    return changes


# ---------- reading one filing ----------

def _filing(con, doc_id):
    con.row_factory = sqlite3.Row
    return con.execute("SELECT f.*, i.ticker AS issuer_ticker FROM filings f LEFT JOIN issuers i ON i.edinet_code = f.edinet_code WHERE f.doc_id = ?", (doc_id,)).fetchone()


def read_doc(con, ds, doc_id):
    """One filing through the same parser its nightly job uses."""
    from . import annual, buybacks, cli, filing_details, governance, halfyear, ratings, voting, xbrlcsv
    f = _filing(con, doc_id)
    if ds == "large_holdings":
        xbrl = cli._xbrl_for_large_holding(doc_id) if f["has_csv"] else None
        cli._ingest_pdf_bytes(con, doc_id, edinet.download_pdf(doc_id), xbrl)
    elif ds == "annual":
        store.upsert_annual_report(con, annual.parse(xbrlcsv.read_facts(edinet.download_csv_zip(doc_id)), doc_id), (f["submit_datetime"] or "")[:10])
    elif ds == "buybacks":
        con.executescript(buybacks.SCHEMA)
        facts = xbrlcsv.read_facts(edinet.download_csv_zip(doc_id))
        buybacks.store(con, buybacks.parse_facts(facts), doc_id, f["doc_type_code"], f["submit_datetime"], f["edinet_code"], f["filer_name_ja"], f["parent_doc_id"], f["issuer_ticker"])
        con.commit()
    elif ds == "governance":
        con.executescript(governance.SCHEMA)
        meta = {"doc_id": doc_id, "submit_date": f["submit_datetime"][:10], "edinet_code": f["edinet_code"], "filer_name_ja": f["filer_name_ja"], "description_ja": f["description_ja"],
                "parent_doc_id": f["parent_doc_id"], "has_csv": f["has_csv"], "ticker": f["issuer_ticker"] or f["ticker"]}
        governance.process(con, meta, "internal_control" if f["doc_type_code"] in ("235", "236") else "auditor_change")
    elif ds == "ratings":
        _read_rating(con, f, ratings)
    elif ds == "half_year":
        _read_half_year(con, f, halfyear, xbrlcsv)
    elif ds == "filing_details":
        con.executescript(filing_details.SCHEMA)
        facts = xbrlcsv.read_facts(edinet.download_csv_zip(doc_id))
        filing_details.store(con, doc_id, filing_details.parse(facts), f["issuer_ticker"] or f["ticker"], f["edinet_code"], filing_details._dei(facts).get("CurrentFiscalYearEndDateDEI"), f["submit_datetime"][:10])
        con.execute("INSERT OR REPLACE INTO filing_details_read VALUES (?, 'read', NULL)", (doc_id,))
        con.commit()
    elif ds == "voting":
        con.executescript(voting.SCHEMA)
        parsed_report, parsed = voting.parse(xbrlcsv.read_facts(edinet.download_csv_zip(doc_id)))
        if parsed_report:
            voting.store(con, doc_id, parsed, f["submit_datetime"], f["edinet_code"], f["issuer_ticker"] or f["ticker"], f["parent_doc_id"])
        else:
            con.execute("INSERT OR REPLACE INTO voting_read VALUES (?, 'not_voting', 0, NULL)", (doc_id,))
        con.commit()
    else:
        raise ValueError(f"no parser for {ds}")


def _read_rating(con, f, ratings):
    con.executescript(ratings.SCHEMA)
    con.execute("CREATE TABLE IF NOT EXISTS ratings_read (doc_id TEXT PRIMARY KEY, found INTEGER)")
    text = ratings._pdf_text(edinet.download_pdf(f["doc_id"]))
    found = ratings.parse_text(text)
    bond = ratings._BOND.search(ratings._normal(text))
    for r in found:
        rec = {**r, "doc_id": f["doc_id"], "submit_date": f["submit_datetime"][:10], "edinet_code": f["edinet_code"], "ticker": f["issuer_ticker"], "issuer_ja": f["filer_name_ja"],
               "bond_ja": re.sub(r"\s+", "", bond.group(0)) if bond else None, "source_url": f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{f['doc_id']}"}
        con.execute("INSERT OR REPLACE INTO disclosed_ratings VALUES (?,?,?,?,?,?,?,?)", (f["doc_id"], r["agency"], r["rating"], r["rating_date"], f["submit_datetime"][:10], f["edinet_code"], f["issuer_ticker"], json.dumps(rec, ensure_ascii=False)))
    con.execute("INSERT OR REPLACE INTO ratings_read VALUES (?, ?)", (f["doc_id"], len(found)))
    con.commit()


def _read_half_year(con, f, halfyear, xbrlcsv):
    fy = halfyear.fiscal_year_end_month(f["description_ja"])
    rec = halfyear.parse(xbrlcsv.read_facts(edinet.download_csv_zip(f["doc_id"]))) if fy else None
    if not rec:
        raise ValueError("no summary table")
    doc = f["doc_id"]
    sub = f["submit_datetime"][:10]
    rec.update({"ticker": f["issuer_ticker"], "fiscal_year_end_month": fy, "period": "H1", "release_date": sub, "source": "edinet_half_year", "source_doc_id": doc, "source_url": f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{doc}"})
    con.execute("INSERT OR REPLACE INTO earnings_releases VALUES (?,?,?,?,?,?)", (f["issuer_ticker"], fy, "H1", sub, f"edinet:{doc}", json.dumps(rec, ensure_ascii=False)))
    con.commit()


def _rebuild(con, touched):
    from . import crossholdings, events, indicators, trades
    mods = {"events": events, "trades": trades, "crossholdings": crossholdings, "indicators": indicators}
    for name in dict.fromkeys(n for ds in touched for n in _REBUILD.get(ds, ())):
        mods[name].rebuild(con)


# ---------- reconcile ----------

def enqueue_gaps(con, since):
    ensure(con)
    n = 0
    for ds in [*RULES, "half_year"]:
        if ds in ("tender_offers", "voting"):
            continue  # read by its own job: reported by gaps, not queued
        for d in _missing(con, ds, since):
            n += con.execute("INSERT INTO reconcile_queue (doc_id, dataset, reason, queued_at) VALUES (?,?,'missing',?) ON CONFLICT(doc_id) DO UPDATE SET done_at = NULL WHERE attempts < ?",
                             (d["doc_id"], ds, _now(), MAX_ATTEMPTS)).rowcount
    con.commit()
    return n


def process_queue(con, minutes=30, max_docs=300, reader=read_doc):
    deadline, touched, done, failed = time.monotonic() + minutes * 60, set(), 0, 0
    rows = con.execute("SELECT q.doc_id, q.dataset FROM reconcile_queue q JOIN filings f ON f.doc_id = q.doc_id WHERE q.done_at IS NULL AND q.attempts < ? ORDER BY f.submit_datetime DESC LIMIT ?", (MAX_ATTEMPTS, max_docs)).fetchall()
    for doc_id, ds in rows:
        if time.monotonic() > deadline:
            break
        try:
            reader(con, ds, doc_id)
            con.execute("UPDATE reconcile_queue SET done_at = ?, attempts = attempts + 1, last_error = NULL WHERE doc_id = ?", (_now(), doc_id))
            touched.add(ds)
            done += 1
        except Exception as e:
            con.execute("UPDATE reconcile_queue SET attempts = attempts + 1, last_error = ? WHERE doc_id = ?", (type(e).__name__, doc_id))
            failed += 1
        con.commit()
    if touched:
        _rebuild(con, touched)
    return {"read": done, "failed": failed}


def reconcile(con, days=35, minutes=30, max_docs=300, today=None, list_holes=3):
    ensure(con)
    today = today or date.today().isoformat()
    since = (date.fromisoformat(today) - timedelta(days=days)).isoformat()
    filed = sorted({r[0] for r in con.execute("SELECT DISTINCT substr(submit_datetime,1,10) FROM filings WHERE submit_datetime >= ?", (since,))})
    refetched = 0
    for day in [d for d in _weekday_holes(filed + [today]) if d >= since][:list_holes]:  # a day never listed, or a holiday: asking settles it
        store.upsert_filings(con, edinet.documents(day))
        refetched += 1
    out = {"lists_refetched": refetched, "withdrawn_removed": apply_withdrawals(con), "amended": apply_amendments(con), "queued": enqueue_gaps(con, since)}
    out.update(process_queue(con, minutes, max_docs))
    return out


# ---------- D1 ----------

class D1Rest:
    """Cloudflare's official D1 query API; no bulk reads."""

    def __init__(self, account=None, database=None, token=None):
        self.account = account or os.environ["CLOUDFLARE_ACCOUNT_ID"]
        self.token = token or os.environ["CLOUDFLARE_API_TOKEN"]
        self.database = database or os.environ.get("D1_DATABASE_ID") or re.search(r'database_id\s*=\s*"([^"]+)"', open(os.path.join(ROOT, "worker", "wrangler.toml")).read()).group(1)

    def query(self, sql, params=()):
        req = urllib.request.Request(f"https://api.cloudflare.com/client/v4/accounts/{self.account}/d1/database/{self.database}/query",
                                     data=json.dumps({"sql": sql, "params": list(params)}).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            body = json.load(r)
        return (body.get("result") or [{}])[-1].get("results") or []


CLAIM = ("UPDATE refresh_requests SET status = 'running', started_at = ?, attempts = attempts + 1 WHERE id IN ("
         "SELECT id FROM refresh_requests WHERE status = 'queued' OR (status = 'running' AND started_at < ?) ORDER BY priority, requested_at LIMIT ?) RETURNING id, ticker, attempts")


def claim(d1, limit=30, now=None):
    now = now or datetime.now(timezone.utc)
    return d1.query(CLAIM, (now.isoformat(timespec="seconds"), (now - timedelta(hours=2)).isoformat(timespec="seconds"), limit))


def company_docs(con, ticker):
    """(dataset, doc_id) not yet read for one company, whether it filed them or is their subject."""
    codes = {r[0] for r in con.execute("SELECT edinet_code FROM issuers WHERE ticker = ?", (ticker,))}
    out = []
    for ds in [*RULES, "half_year"]:
        if ds == "tender_offers":
            continue
        for d in _missing(con, ds):
            if d["edinet_code"] in codes or d.get("issuer_edinet_code") in codes:
                out.append((ds, d["doc_id"]))
    return out


def refresh_company(con, ticker, sites=None, reader=read_doc, ir=None, max_docs=40):
    """Reads this company's unread EDINET documents. Company websites are not read."""
    res = {"documents_read": 0, "documents_failed": 0}
    touched = set()
    for ds, doc_id in company_docs(con, ticker)[:max_docs]:
        try:
            reader(con, ds, doc_id)
            res["documents_read"] += 1
            touched.add(ds)
        except Exception:
            res["documents_failed"] += 1
    if touched:
        _rebuild(con, touched)
    return res


def run_queue(con, d1, minutes=10, limit=30, reader=read_doc, ir=None, sites=None):
    ensure(con)
    deadline, n = time.monotonic() + minutes * 60, 0
    for r in claim(d1, limit):
        status, result = "done", None
        if time.monotonic() > deadline:  # left running: the next run claims it again after two hours
            continue
        if r["attempts"] > 3:
            status, result = "failed", {"error": "gave_up"}
        else:
            try:
                result = refresh_company(con, r["ticker"], sites, reader, ir)
            except Exception as e:
                status, result = "failed", {"error": type(e).__name__}
        con.execute("INSERT OR REPLACE INTO refresh_work VALUES (?,?,?,?,?,0)", (r["id"], r["ticker"], status, json.dumps(result), _now()))
        con.commit()
        n += 1
    return n


def _lit(v):
    if v is None:
        return "NULL"
    return repr(v) if isinstance(v, (int, float)) else "'" + str(v).replace("'", "''") + "'"


def _unsent(state_path, con, table):
    """Rows of a table the delta export has not sent yet (the daily write cap can defer them)."""
    if not state_path or not os.path.exists(state_path):
        return 0
    st = sqlite3.connect(f"file:{state_path}?mode=ro", uri=True)
    try:
        sent = st.execute("SELECT COUNT(*) FROM row_hash WHERE tbl = ?", (table,)).fetchone()[0]
        try:  # rows the export leaves out on purpose are never sent
            withheld = (st.execute("SELECT n FROM withheld_rows WHERE tbl = ?", (table,)).fetchone() or (0,))[0]
        except sqlite3.OperationalError:
            withheld = 0
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] - sent - withheld
    except sqlite3.Error:
        return 0
    finally:
        st.close()


def publish_sql(con, state_path=None, days=35, today=None):
    ensure(con)
    today = today or date.today().isoformat()
    since = (date.fromisoformat(today) - timedelta(days=days)).isoformat()
    at, out = _now(), []
    runs = {r[0]: r for r in con.execute("SELECT dataset, last_attempt_at, last_success_at, last_error FROM dataset_runs")}
    hopeless = {r[0] for r in con.execute("SELECT doc_id FROM reconcile_queue WHERE attempts >= ?", (MAX_ATTEMPTS,))}
    # A filing listed today is not a gap yet; give the ingest a day to read it.
    settled = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    gaps = {ds: sum(1 for d in _missing(con, ds, since) if d["doc_id"] not in hopeless and d["submit_datetime"][:10] <= settled) for ds in [*RULES, "half_year"] if ds != "tender_offers"}
    for d in DATASETS:
        if d.get("owner") == "worker":
            continue
        rows = newest = companies = None
        if _table_exists(con, d["table"]):
            w = f" WHERE {d['where']}" if d.get("where") else ""
            rows, newest, companies = con.execute(f"SELECT COUNT(*), {('MAX(' + d['date'] + ')') if d.get('date') else 'NULL'}, {('COUNT(DISTINCT ' + d['company'] + ')') if d.get('company') else 'NULL'} FROM {d['table']}{w}").fetchone()
        run = runs.get(d["id"], (None, None, None, None))
        success = None if _unsent(state_path, con, d["table"]) > 0 else run[2]  # not in D1 yet: the old time stands
        out.append("INSERT INTO dataset_status (dataset, last_attempt_at, last_success_at, last_error, newest_source_date, rows, companies, open_gap) VALUES "
                   f"({_lit(d['id'])},{_lit(run[1])},{_lit(success)},{_lit(run[3])},{_lit(newest)},{_lit(rows)},{_lit(companies)},{_lit(gaps.get(d['id'], 0))}) "
                   "ON CONFLICT(dataset) DO UPDATE SET last_attempt_at = excluded.last_attempt_at, last_success_at = COALESCE(excluded.last_success_at, last_success_at), last_error = excluded.last_error, "
                   "newest_source_date = excluded.newest_source_date, rows = excluded.rows, companies = excluded.companies, open_gap = excluded.open_gap;")
    for r in con.execute("SELECT ticker, edinet_code, change, day FROM issuer_changes WHERE day >= ?", (since,)):
        out.append(f"INSERT OR IGNORE INTO listing_changes VALUES ({','.join(_lit(v) for v in r)});")
    work = con.execute("SELECT request_id, status, result, checked_at FROM refresh_work WHERE published = 0").fetchall()
    for rid, status, result, checked in work:
        out.append(f"UPDATE refresh_requests SET status = {_lit(status)}, result = {_lit(result)}, checked_at = {_lit(checked)}, finished_at = {_lit(at)} WHERE id = {_lit(rid)};")
    return out, [w[0] for w in work]


def publish(con, d1=None, sql_out=None, state_path=None, chunk=200):
    stmts, work = publish_sql(con, state_path)
    if sql_out:
        with open(sql_out, "w", encoding="utf-8") as f:
            f.write("\n".join(stmts) + "\n")
    else:
        d1 = d1 or D1Rest()
        for i in range(0, len(stmts), chunk):
            d1.query("\n".join(stmts[i:i + chunk]))
    con.executemany("UPDATE refresh_work SET published = 1 WHERE request_id = ?", [(w,) for w in work])
    con.commit()
    return len(stmts)


# ---------- CI check ----------

def problems(payload, ignore=()):
    return [f"{d['dataset']}: {d['status']} ({', '.join(d.get('status_reasons') or [])}); last run {d.get('last_successful_run')}, newest source {d.get('newest_source_date')}"
            for d in payload.get("data", []) if d.get("status") != "fresh" and d.get("dataset") not in ignore]


def check(url, ignore=(), tries=3, wait=30):
    base = re.sub(r"/v1/(status|freshness)/?$", "", url.rstrip("/"))
    err = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(base + "/v1/freshness", headers={"User-Agent": "japanstake-watchdog/0.1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                bad = problems(json.load(r), ignore)
            for b in bad:
                print(b, file=sys.stderr)
            print(f"{len(bad)} dataset(s) late or failed" if bad else "all datasets fresh")
            return 1 if bad else 0
        except Exception as e:
            err = e
            if attempt + 1 < tries:
                time.sleep(wait)
    print(f"could not read freshness: {type(err).__name__}", file=sys.stderr)
    return 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="refresh")
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gaps"); g.add_argument("--since")
    r = sub.add_parser("reconcile"); r.add_argument("--days", type=int, default=35); r.add_argument("--minutes", type=int, default=30); r.add_argument("--max-docs", type=int, default=300)
    sub.add_parser("listings")
    u = sub.add_parser("run"); u.add_argument("--minutes", type=int, default=10)
    b = sub.add_parser("publish"); b.add_argument("--sql-out"); b.add_argument("--state", default="data/d1_state.db")
    c = sub.add_parser("check"); c.add_argument("url"); c.add_argument("--ignore", default="")
    k = sub.add_parser("mark"); k.add_argument("job")
    a = p.parse_args(argv)
    if a.cmd == "check":
        return check(a.url, tuple(x for x in a.ignore.split(",") if x))
    if a.cmd == "gaps":  # read-only, safe beside a running ingest
        con = sqlite3.connect(f"file:{os.path.abspath(store.DEFAULT_DB)}?mode=ro", uri=True, timeout=60)
        print(json.dumps(gap_report(con, a.since), ensure_ascii=False, indent=1))
        return 0
    con = store.connect()
    ensure(con)
    if a.cmd == "mark":
        record_run(con, a.job, None)
    elif a.cmd == "reconcile":
        print(reconcile(con, a.days, a.minutes, a.max_docs))
    elif a.cmd == "listings":
        from . import codelist
        issuers = codelist.parse(codelist.download())
        store.upsert_issuers(con, issuers)
        print("listing changes:", sync_listings(con, issuers))
    elif a.cmd == "run":
        print("refresh requests processed:", run_queue(con, D1Rest(), a.minutes))
    elif a.cmd == "publish":
        print("statements:", publish(con, sql_out=a.sql_out, state_path=a.state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
