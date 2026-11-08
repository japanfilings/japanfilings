"""Resumable backfill of 5% reports (大量保有報告書 350, amendments 360) through the EDINET API.

Findings from sample requests on 2026-10-31 (nothing here is assumed beyond them):
  documents.json answers back to about ten years (2016-11-01 ok, 2014-11-04 status 404), but expired
  filings lose their metadata: 2021-10-28 and 2021-10-29 list no 350/360, 2021-11-01 lists 51, and
  2021-11-08 lists 88, all with csvFlag=1. That matches the five-year inspection period, so the start
  is expected to move forward one day per day. A filing we store before it expires stays ours.

Order: metadata for every day first, newest day first, then documents, newest first. `--oldest-first`
reverses the document phase when saving the filings about to expire matters more.

One request per filing: everything the PDF supplied is in the XBRL CSV, so the PDF is fetched only for a
filing without a CSV. `compare` measures the agreement against stored records.

History must not reach webhook subscribers, who are served by `ingested_at`. New events from this module
are stamped with their filing date, so they sit behind every subscriber's cursor.

GitHub Actions: run in the `ingest` concurrency group, restore and save the store with the existing cache:
  python -m japanfilings.backfill run --minutes 50 --no-csv-cache
The run stops by itself at the deadline, and every filing is committed as it arrives. Exit code 0 always
means "stopped cleanly"; the last line is JSON with `complete` true or false.

  python -m japanfilings.backfill run --minutes 50 [--max-docs N] [--interval 1.1] [--earliest 2021-11-01]
  python -m japanfilings.backfill status
  python -m japanfilings.backfill estimate
  python -m japanfilings.backfill compare            # CSV-only records against stored ones (no requests)
"""
import argparse
import html
import io
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from . import edinet, events, largeholding, store, trades, xbrlcsv
from .textutil import nfkc, parse_number

SCHEMA = """
CREATE TABLE IF NOT EXISTS backfill_days (
  day TEXT PRIMARY KEY,
  status TEXT NOT NULL,          -- listed | failed
  listed_filings INTEGER,
  targets INTEGER,
  fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS backfill_docs (
  doc_id TEXT PRIMARY KEY,
  day TEXT NOT NULL,
  doc_type_code TEXT,
  has_csv INTEGER,
  status TEXT NOT NULL,          -- pending | done | failed | gone
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  parsed_from TEXT,              -- csv | pdf
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS backfill_docs_todo ON backfill_docs(status, day);
"""

# Stored in `filings`: 5% reports and the tender-offer family, used by the company timeline. Other types
# are skipped by default because the `filings` table is copied to D1.
KEEP_TYPES = {"350", "360", "240", "250", "260", "270", "280", "290", "300", "310"}
EXPIRY_YEARS = 5
MAX_ATTEMPTS = 3
REFRESH_EVERY = 1000  # filings between event refreshes, so a killed run leaves few unstamped events
_NONE = ("", "－", "-", "―", "ー")

API = edinet.API


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def default_earliest(today=None):
    """Five years back plus a week of margin: listing a day that holds nothing costs one request."""
    t = today or date.today()
    return (t.replace(year=t.year - EXPIRY_YEARS) - timedelta(days=7)).isoformat()


def days_newest_first(start, end):
    d, last = date.fromisoformat(start), date.fromisoformat(end)
    while d >= last:
        yield d.isoformat()
        d -= timedelta(days=1)


class Gone(Exception):
    """EDINET no longer serves this document."""


class Client:
    """One process, one request per `interval` seconds. Short retries only: `edinet._get` waits minutes on
    an HTTP error, which is wrong for a document that has expired, and caches whatever body it receives."""

    def __init__(self, interval=1.1, opener=None, sleep=time.sleep, clock=time.monotonic):
        self.interval, self.sleep, self.clock = interval, sleep, clock
        self.opener = opener or (lambda url: urllib.request.urlopen(url, timeout=60))
        self.last = None
        self.requests = 0

    def get(self, path, params):
        key = os.environ.get("EDINET_API_KEY")
        if not key:
            raise RuntimeError("EDINET_API_KEY is not set.")
        url = f"{API}/{path}?{urllib.parse.urlencode({**params, 'Subscription-Key': key})}"
        for attempt in range(4):
            if self.last is not None:
                wait = self.interval - (self.clock() - self.last)
                if wait > 0:
                    self.sleep(wait)
            self.last = self.clock()
            self.requests += 1
            try:
                with self.opener(url) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code in (404, 410):
                    raise Gone(f"HTTP {e.code}") from None
                if e.code not in (429, 500, 502, 503, 504) or attempt == 3:
                    raise RuntimeError(f"HTTP {e.code}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt == 3:
                    raise RuntimeError(type(e).__name__) from None  # never the URL: it holds the key
            self.sleep(20 * (attempt + 1))
        raise RuntimeError("unreachable")

    def documents(self, day):
        data = json.loads(self.get("documents.json", {"date": day, "type": 2}))
        status = str(data.get("metadata", {}).get("status") or data.get("StatusCode"))
        if status == "404":
            raise Gone("date out of range")
        if status != "200":
            raise RuntimeError(f"EDINET status {status}")
        return data.get("results") or []

    def _document(self, doc_id, type_, magic):
        body = self.get(f"documents/{doc_id}", {"type": type_})
        if not body.startswith(magic):  # errors arrive as JSON with HTTP 200
            if b"404" in body[:300]:
                raise Gone("status 404")
            raise RuntimeError(f"unexpected body for type {type_}: {body[:80]!r}")
        return body

    def csv_zip(self, doc_id, cache=True):
        path = os.path.join(edinet.CSV_CACHE, f"{doc_id}.zip")
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()
        body = self._document(doc_id, 5, b"PK")
        if cache:
            os.makedirs(edinet.CSV_CACHE, exist_ok=True)
            with open(path, "wb") as f:
                f.write(body)
        return body

    def pdf(self, doc_id):
        return self._document(doc_id, 2, b"%PDF")


# ---------- Records from the CSV alone ----------

def _cover(facts, element):
    for f in facts:
        if f["element"] == element and f["value"] not in _NONE:
            return f["value"]
    return None


def _first_holder_value(facts, element):
    """The value on the first holder in filing order, which is what the PDF parse read."""
    best = None
    for f in facts:
        if f["element"] != element or f["value"] in _NONE:
            continue
        m = re.search(r"LargeVolumeHolder(\d+)Member$", f["context"])
        if m and (best is None or int(m.group(1)) < best[0]):
            best = (int(m.group(1)), f["value"])
    return best[1] if best else None


def _clean_text(v):
    return re.sub(r"\s+", "", nfkc(v)) if v else None


def record_from_facts(facts, doc_id, meta=None):
    """The same record shape as `cli._ingest_pdf_bytes`, built without the PDF."""
    meta = meta or {}
    # An amendment repeats the original's cover page, including its filing date and sometimes its title.
    amended = meta.get("docTypeCode") == "360" or _cover(facts, "jpdei_cor:AmendmentFlagDEI") == "true"
    title = _cover(facts, "jplvh_cor:DocumentTitleCoverPage") or meta.get("docDescription")
    purpose = _clean_text(_first_holder_value(facts, "jplvh_cor:PurposeOfHolding"))
    joint = parse_number(_cover(facts, "jplvh_cor:TotalNumberOfFilersAndJointHoldersCoverPage") or "")
    reason = _cover(facts, "jplvh_cor:ReasonForFilingChangeReportCoverPage")
    sec = (_cover(facts, "jplvh_cor:SecurityCodeOfIssuer") or "").strip()
    name = _cover(facts, "jpdei_cor:FilerNameInJapaneseDEI") or meta.get("filerName")
    rec = {
        "doc_id": doc_id,
        "report_kind": "amendment" if amended else largeholding._report_kind(nfkc(title) if title else None),
        "report_title_ja": nfkc(title) if title else None,
        "filer_name_ja": nfkc(name) if name else None,
        "filer_edinet_code": _cover(facts, "jpdei_cor:EDINETCodeDEI") or meta.get("edinetCode"),
        "obligation_date": _cover(facts, "jplvh_cor:DateWhenFilingRequirementAroseCoverPage"),
        "submit_date": (meta.get("submitDateTime") or "")[:10] or _cover(facts, "jplvh_cor:FilingDateCoverPage"),
        "holders_count": int(joint) if joint else None,
        "change_reason_ja": nfkc(reason) if reason else None,
        "issuer_name_ja": nfkc(_cover(facts, "jplvh_cor:NameOfIssuer") or "") or None,
        "issuer_ticker": nfkc(sec)[:4] or None,
        "exchange_ja": nfkc(_cover(facts, "jplvh_cor:StockListing") or "") or None,
        "purpose_ja": purpose,
        "purpose_tags": sorted({tag for kw, tag in largeholding.PURPOSE_TAGS if purpose and kw in purpose}),
        "source_url": largeholding.PDF_URL.format(doc_id=doc_id),
        "source": "EDINET (Financial Services Agency of Japan)",
        "pdf_ratio_pct": None,
        "ratio_source": "xbrl",
    }
    ratios = xbrlcsv.large_holding_ratios(facts)
    holders = xbrlcsv.large_holding_holders(facts)
    cur, prev = ratios["holding_ratio_pct"], ratios["previous_ratio_pct"]
    rec.update(holding_ratio_pct=cur, previous_ratio_pct=prev,
               ratio_change_pt=round(cur - prev, 2) if cur is not None and prev is not None else None,
               holders=holders["holders"], trades=trades.from_facts(facts, trades.holder_names(facts)))
    # The cover DEI names the filer. `holders["filer_name_en"]` is whichever joint holder came last.
    en = _cover(facts, "jpdei_cor:FilerNameInEnglishDEI")
    if en:
        rec["filer_name_en_filing"] = html.unescape(nfkc(en))
    return rec


def _record_from_pdf(pdf, doc_id):
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(pdf)
        f.flush()
        rec = largeholding.parse_text(largeholding.pdf_to_text(f.name), doc_id)
    rec.update(pdf_ratio_pct=rec["holding_ratio_pct"], ratio_source="pdf" if rec["holding_ratio_pct"] is not None else None,
               holders=[], trades=[])
    return rec


# ---------- Phases ----------

def init(con):
    con.executescript(SCHEMA + events.SCHEMA + trades.SCHEMA)
    con.commit()


def list_days(con, client, earliest, start, deadline, keep_all=False):
    """Phase 1. Returns True when every day from `start` back to `earliest` is listed."""
    done = {r[0] for r in con.execute("SELECT day FROM backfill_days WHERE status = 'listed'")}
    for day in days_newest_first(start, earliest):
        if day in done:
            continue
        if time.monotonic() >= deadline:
            return False
        try:
            docs = client.documents(day)
        except Gone:
            docs = []
        except Exception as e:
            print(day, "LIST FAILED", e, file=sys.stderr)
            con.execute("INSERT OR REPLACE INTO backfill_days VALUES (?,?,?,?,?)", (day, "failed", None, None, _now()))
            con.commit()
            continue
        kept = docs if keep_all else [d for d in docs if d.get("docTypeCode") in KEEP_TYPES]
        store.upsert_filings(con, kept)
        targets = [d for d in docs if d.get("docTypeCode") in edinet.LARGE_HOLDING_DOC_TYPES and d.get("withdrawalStatus") == "0"]
        for d in targets:
            status = "done" if store.has_large_holding(con, d["docID"]) else "pending"
            con.execute("INSERT OR IGNORE INTO backfill_docs (doc_id, day, doc_type_code, has_csv, status, updated_at) VALUES (?,?,?,?,?,?)",
                        (d["docID"], day, d.get("docTypeCode"), int(d.get("csvFlag") == "1"), status, _now()))
        con.execute("INSERT OR REPLACE INTO backfill_days VALUES (?,?,?,?,?)", (day, "listed", len(docs), len(targets), _now()))
        con.commit()
    return not con.execute("SELECT 1 FROM backfill_days WHERE status = 'failed' LIMIT 1").fetchone()


def _stamp_events(con, rec):
    """Store this filing's events behind every webhook cursor. `refresh_events` sets the final set."""
    stamp = f"{rec.get('submit_date') or '2000-01-01'}T00:00:00.000Z"
    for e in events.derive(rec, None):
        con.execute("INSERT OR IGNORE INTO ownership_events VALUES (?,?,?,?,?,?,?,?)",
                    (e["event_id"], e["type"], e["submit_date"], e["doc_id"], e["issuer_ticker"], e["filer_edinet_code"],
                     stamp, json.dumps(e, ensure_ascii=False)))


def fetch_doc(con, client, row, cache=True):
    doc_id = row["doc_id"]
    source = "csv"
    if row["has_csv"]:
        try:
            meta = {"docTypeCode": row["doc_type_code"], "submitDateTime": row["day"]}
            rec = record_from_facts(xbrlcsv.read_facts(client.csv_zip(doc_id, cache)), doc_id, meta)
        except zipfile.BadZipFile as e:
            raise RuntimeError(f"bad zip: {e}") from None
    else:
        rec = _record_from_pdf(client.pdf(doc_id), doc_id)
        source = "pdf"
    rec["submit_date"] = row["day"]
    store.link_issuer(con, rec)
    con.execute("INSERT OR REPLACE INTO large_holdings VALUES (?,?,?,?,?)",
                (doc_id, rec["issuer_ticker"], rec["filer_edinet_code"], rec["submit_date"], json.dumps(rec, ensure_ascii=False)))
    _stamp_events(con, rec)
    con.execute("UPDATE backfill_docs SET status = 'done', parsed_from = ?, error = NULL, updated_at = ? WHERE doc_id = ?", (source, _now(), doc_id))
    con.commit()


def fetch_docs(con, client, deadline, max_docs=None, oldest_first=False, cache=True, refresh_every=REFRESH_EVERY):
    """Phase 2. Returns the number of filings stored by this run."""
    order = "ASC" if oldest_first else "DESC"
    n = 0
    while True:
        rows = con.execute(f"SELECT doc_id, day, doc_type_code, has_csv FROM backfill_docs WHERE status = 'pending' "
                           f"ORDER BY day {order}, doc_id {order} LIMIT 200").fetchall()
        if not rows:
            return n
        for row in rows:
            if time.monotonic() >= deadline or (max_docs is not None and n >= max_docs):
                return n
            try:
                fetch_doc(con, client, row, cache)
                n += 1
                if refresh_every and n % refresh_every == 0:
                    refresh_events(con)
            except Gone as e:
                con.execute("UPDATE backfill_docs SET status = 'gone', error = ?, updated_at = ? WHERE doc_id = ?", (str(e), _now(), row["doc_id"]))
                con.commit()
            except Exception as e:  # keep going; the row stays pending until its attempts run out
                con.rollback()
                con.execute("UPDATE backfill_docs SET attempts = attempts + 1, error = ?, updated_at = ?, "
                            "status = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE 'pending' END WHERE doc_id = ?",
                            (str(e)[:300], _now(), MAX_ATTEMPTS, row["doc_id"]))
                con.commit()
                print(row["doc_id"], "FAILED", e, file=sys.stderr)


def refresh_events(con, chunk=None):
    """Recompute all events now that history has changed. An older filing changes what counts as a change on
    the next one, so some events disappear (first filing on record) and some appear. Events first seen here
    get their filing date as `ingested_at`; `events.rebuild` would stamp them now and send them to webhooks.
    Computed in memory; the write transaction is short, or with `chunk` many short ones. An event of a filing
    stored after the snapshot (by another process) is kept; one whose filing no longer exists is removed."""
    con.executescript(events.SCHEMA)
    last, rows, seen = {}, {}, set()
    for r in con.execute("SELECT record FROM large_holdings ORDER BY submit_date, doc_id"):
        rec = json.loads(r["record"])
        seen.add(rec["doc_id"])
        key = (rec.get("filer_edinet_code"), rec.get("issuer_ticker") or rec.get("issuer_name_ja"))
        for e in events.derive(rec, last.get(key)):
            rows[e["event_id"]] = e
        if rec.get("report_kind") != "amendment":
            last[key] = rec
    known = dict(con.execute("SELECT event_id, ingested_at FROM ownership_events").fetchall())
    owner = dict(con.execute("SELECT event_id, doc_id FROM ownership_events").fetchall())
    stale = [(k,) for k in known if k not in rows and (owner.get(k) in seen or not store.has_large_holding(con, owner.get(k)))]
    fresh = [(eid, e["type"], e["submit_date"], e["doc_id"], e["issuer_ticker"], e["filer_edinet_code"],
              known.get(eid) or f"{e.get('submit_date') or '2000-01-01'}T00:00:00.000Z", json.dumps(e, ensure_ascii=False))
             for eid, e in rows.items()]
    step = chunk or max(len(stale), len(fresh), 1)
    for i in range(0, len(stale), step):
        with con:
            con.executemany("DELETE FROM ownership_events WHERE event_id = ?", stale[i:i + step])
    for i in range(0, len(fresh), step):
        with con:
            con.executemany("INSERT OR REPLACE INTO ownership_events VALUES (?,?,?,?,?,?,?,?)", fresh[i:i + step])
    return {"events": len(fresh), "removed": len(stale)}


def status(con):
    days = dict(con.execute("SELECT status, COUNT(*) FROM backfill_days GROUP BY 1").fetchall())
    docs = dict(con.execute("SELECT status, COUNT(*) FROM backfill_docs GROUP BY 1").fetchall())
    span = con.execute("SELECT MIN(day), MAX(day) FROM backfill_docs WHERE status = 'done'").fetchone()
    listed = con.execute("SELECT MIN(day), MAX(day) FROM backfill_days WHERE status = 'listed'").fetchone()
    return {"days": days, "docs": docs, "listed_from": listed[0], "listed_to": listed[1], "stored_from": span[0], "stored_to": span[1],
            "metadata_complete": "failed" not in days and bool(days), "complete": bool(days) and "failed" not in days and not docs.get("pending")}


# ---------- Estimate ----------

def estimate(con, seconds_per_request=1.4, today=None):
    """From the filing counts already in `filings` and the bytes of the 5% records already stored."""
    today = today or date.today()
    rate = con.execute("SELECT COUNT(*), COUNT(DISTINCT substr(submit_datetime, 1, 10)) FROM filings WHERE doc_type_code IN ('350','360')").fetchone()
    per_day = rate[0] / rate[1] if rate[1] else 60
    business_days = EXPIRY_YEARS * 245
    calendar_days = EXPIRY_YEARS * 366
    stored = con.execute("SELECT COUNT(*), COALESCE(AVG(LENGTH(record)), 7300) FROM large_holdings").fetchone()
    filings = per_day * business_days
    todo = max(0, filings - stored[0])
    ev = con.execute("SELECT COUNT(*), COALESCE(AVG(LENGTH(record)), 670) FROM ownership_events").fetchone()
    tr = con.execute("SELECT COUNT(*) FROM holder_trades").fetchone()[0]
    ev_per, tr_per = (ev[0] / stored[0], tr / stored[0]) if stored[0] else (1.2, 14)
    hours = (calendar_days + todo) * seconds_per_request / 3600
    db_bytes = todo * (stored[1] + ev_per * ev[1] + tr_per * 160 + 400)
    new_rows = {"large_holdings": todo, "ownership_events": todo * ev_per, "holder_trades": todo * tr_per, "filings": filings * 1.05}
    # D1 bills a row written once per row and once per index on it.
    writes = todo * 3 + todo * ev_per * 4 + todo * tr_per * 3 + filings * 1.05 * 3
    return {
        "filings_per_business_day": round(per_day, 1), "filings_estimated": round(filings), "requests": round(calendar_days + todo),
        "hours_at_one_process": round(hours, 1), "days_continuous": round(hours / 24, 1),
        "actions_batches_of_50_min": round(hours / (50 / 60)),
        "csv_cache_gb": round(todo * 5816 / 1e9, 2), "sqlite_growth_gb": round(db_bytes / 1e9, 2),
        "d1_new_rows": {k: round(v) for k, v in new_rows.items()}, "d1_rows_written": round(writes),
    }


def compare(con):
    """Rebuild stored records from cached CSVs alone and count differences by field. No requests."""
    fields = ["report_kind", "filer_edinet_code", "obligation_date", "submit_date", "holders_count", "issuer_ticker",
              "exchange_ja", "holding_ratio_pct", "previous_ratio_pct", "change_reason_ja", "purpose_ja", "purpose_tags"]
    diff, seen, missing, examples = Counter(), Counter(), 0, {}
    for r in con.execute("SELECT doc_id, record FROM large_holdings").fetchall():
        path = os.path.join(edinet.CSV_CACHE, f"{r['doc_id']}.zip")
        if not os.path.exists(path):
            missing += 1
            continue
        old = json.loads(r["record"])
        with open(path, "rb") as f:
            new = record_from_facts(xbrlcsv.read_facts(f.read()), r["doc_id"], {"submitDateTime": old.get("submit_date"), "docTypeCode": "360" if old.get("report_kind") == "amendment" else "350"})
        for k in fields:
            a, b = old.get(k), new.get(k)
            if isinstance(a, str):
                a = _clean_text(a)
            if isinstance(b, str):
                b = _clean_text(b)
            seen[k] += 1
            if a != b and not (a in (None, "", []) and b in (None, "", [])):
                diff[k] += 1
                examples.setdefault(k, (r["doc_id"], str(a)[:50], str(b)[:50]))
    return {"compared": max(seen.values(), default=0), "no_cached_csv": missing, "differences": dict(diff), "examples": examples}


# ---------- CLI ----------

def run(con, client, minutes, earliest=None, start=None, max_docs=None, oldest_first=False, cache=True, keep_all=False):
    deadline = time.monotonic() + minutes * 60
    init(con)
    earliest = earliest or default_earliest()
    start = start or (date.today() - timedelta(days=1)).isoformat()
    metadata_done = list_days(con, client, earliest, start, deadline, keep_all)
    stored = 0
    if metadata_done:
        stored = fetch_docs(con, client, deadline, max_docs, oldest_first, cache)
    refreshed = refresh_events(con) if stored else None
    if stored:
        trades.rebuild(con)
    return {**status(con), "stored_this_run": stored, "requests_this_run": client.requests, "refreshed": refreshed}


def main(argv=None):
    p = argparse.ArgumentParser(prog="japanfilings.backfill", description="Backfill 5% reports from EDINET, newest first.")
    p.add_argument("command", choices=["run", "status", "estimate", "compare"])
    p.add_argument("--minutes", type=float, default=50)
    p.add_argument("--max-docs", type=int)
    p.add_argument("--interval", type=float, default=1.1, help="seconds between requests")
    p.add_argument("--earliest")
    p.add_argument("--start")
    p.add_argument("--oldest-first", action="store_true")
    p.add_argument("--no-csv-cache", action="store_true")
    p.add_argument("--all-filings", action="store_true", help="store every filing type's metadata, not only 5% and tender offers")
    a = p.parse_args(argv)
    con = store.connect()
    init(con)
    if a.command == "status":
        out = status(con)
    elif a.command == "estimate":
        out = estimate(con)
    elif a.command == "compare":
        out = compare(con)
    else:
        out = run(con, Client(a.interval), a.minutes, a.earliest, a.start, a.max_docs, a.oldest_first, not a.no_csv_cache, a.all_filings)
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
