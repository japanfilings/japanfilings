"""Ingestion CLI.

  python -m japanfilings.cli codelist                     # issuer universe, no key
  python -m japanfilings.cli figi                         # FIGIs for listed tickers, no key
  python -m japanfilings.cli pdf S100Z9GY ...             # parse filings by doc ID
  python -m japanfilings.cli range 2026-09-01 2026-09-30
                                                          # every filing's metadata + 5% reports + events + trades
  python -m japanfilings.cli annual 2025-11-01 2026-10-29 # annual reports: metrics, statements, items, cross-holdings
  python -m japanfilings.cli halfyear                     # half-year results of every listed company (EDINET)
  python -m japanfilings.cli buybacks                     # monthly share-buyback reports (EDINET)
  python -m japanfilings.cli governance                   # internal-control conclusions, auditor changes (EDINET)
  python -m japanfilings.cli filing-details [N] [--network] [--minutes M]  # annual-report detail tables (cached CSV by default)
  python -m japanfilings.cli voting [N]                   # shareholder votes (cached CSV by default)
  python -m japanfilings.cli tender-offers [N] [--reparse] # tender offers (cached CSV by default; --reparse re-reads parsed rows)
  python -m japanfilings.cli investors                    # rebuild organization-only investor groups
  python -m japanfilings.cli ratings                      # credit ratings stated in bond filings (EDINET)
  python -m japanfilings.cli text-reindex                 # recompute text indicators after a word-list change
  python -m japanfilings.cli indicators                   # percentiles, policy uncertainty, ownership activity
  python -m japanfilings.cli annual-reparse               # re-run the annual parser on stored reports
  python -m japanfilings.cli reparse                      # rebuild voting, buybacks and governance from cached CSV only
  python -m japanfilings.cli nport 2026q3                 # U.S. fund holdings of Japanese stocks (SEC)
  python -m japanfilings.cli recheck                      # re-derive ratios, holders and trades of stored 5% reports
  python -m japanfilings.cli report                       # large-holding parse quality
  python -m japanfilings.cli snapshot data/snapshot.tar.gz  # checked archive of the store + D1 hash state
  python -m japanfilings.cli restore data/snapshot.tar.gz   # unpack one (refuses to overwrite a store)
  python -m japanfilings.cli store-check                    # integrity, and no large loss of rows since the restore
  python -m japanfilings.cli check-freshness URL [--max-age-days 5]  # exit 1 when /v1/status is stale

`pdf` and `range` need EDINET_API_KEY. EDINET's terms (2.2) forbid mechanical downloads
from the website, so documents are fetched only through the API. Ratios come from the XBRL
CSV when the filing has one; the PDF parse is kept as `pdf_ratio_pct` for `report`.
"""
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

from . import annual, codelist, crossholdings, edinet, events, figi, indicators, largeholding, nport, store, textindex, trades, xbrlcsv


def _xbrl_for_large_holding(doc_id):
    facts = xbrlcsv.read_facts(edinet.download_csv_zip(doc_id))
    return {**xbrlcsv.large_holding_ratios(facts), **xbrlcsv.large_holding_holders(facts),
            "trades": trades.from_facts(facts, trades.holder_names(facts))}


def _ingest_pdf_bytes(con, doc_id, pdf, xbrl=None):
    """Text fields come from the PDF. Ratios come from XBRL when available: on 2026-10-22 the
    PDF parse missed 30 of 135 ratios that XBRL had, and agreed on all 105 it did find."""
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(pdf)
        f.flush()
        rec = largeholding.parse_text(largeholding.pdf_to_text(f.name), doc_id)
    if not rec["report_title_ja"] and not rec["filer_edinet_code"]:
        # Nothing was read: store nothing, so the filing stays missing and is retried and reported.
        raise RuntimeError("no text read from the PDF (is poppler-data installed?)")
    rec["pdf_ratio_pct"] = rec["holding_ratio_pct"]
    rec["ratio_source"] = "pdf" if rec["holding_ratio_pct"] is not None else None
    if xbrl:
        _apply_xbrl(rec, xbrl)
    cur, prev = rec["holding_ratio_pct"], rec["previous_ratio_pct"]
    rec["ratio_change_pt"] = round(cur - prev, 2) if cur is not None and prev is not None else None
    store.upsert_large_holding(con, rec)
    return rec


def _apply_xbrl(rec, xbrl):
    if xbrl.get("holding_ratio_pct") is not None:
        rec["holding_ratio_pct"] = xbrl["holding_ratio_pct"]
        if xbrl.get("previous_ratio_pct") is not None:
            rec["previous_ratio_pct"] = xbrl["previous_ratio_pct"]
        rec["ratio_source"] = "xbrl"
    rec["holders"] = xbrl.get("holders") or []
    rec["trades"] = xbrl.get("trades") or []
    if xbrl.get("filer_name_en"):
        rec["filer_name_en_filing"] = xbrl["filer_name_en"]
    rec.pop("xbrl_check", None)
    cur, prev = rec["holding_ratio_pct"], rec["previous_ratio_pct"]
    rec["ratio_change_pt"] = round(cur - prev, 2) if cur is not None and prev is not None else None


def _days(start, end):
    d, last = date.fromisoformat(start), date.fromisoformat(end)
    while d <= last:
        yield d.isoformat()
        d += timedelta(days=1)


def _range(con, start, end):
    for day in _days(start, end):
        docs = edinet.documents(day)
        store.upsert_filings(con, docs)
        targets = [d for d in docs if d.get("docTypeCode") in edinet.LARGE_HOLDING_DOC_TYPES
                   and d.get("withdrawalStatus") == "0" and not store.has_large_holding(con, d["docID"])]
        for d in targets:
            try:
                xbrl = _xbrl_for_large_holding(d["docID"]) if d.get("csvFlag") == "1" else None
                _ingest_pdf_bytes(con, d["docID"], edinet.download_pdf(d["docID"]), xbrl)
            except Exception as e:  # keep going; a later run retries docs that are still missing
                print(d["docID"], "FAILED", e, file=sys.stderr)
        print(day, f"filings={len(docs)} new_5pct={len(targets)}")
    print("ownership events:", events.rebuild(con))
    print("disclosed trades:", trades.rebuild(con))
    print("indicators (companies, index months):", indicators.rebuild(con))


ANNUAL_DOC_TYPES = {"120", "130"}  # annual securities report, and its amendment


def _annual(con, start, end):
    for day in _days(start, end):
        docs = edinet.documents(day)
        store.upsert_filings(con, docs)
        targets = [d for d in docs if d.get("docTypeCode") in ANNUAL_DOC_TYPES and d.get("csvFlag") == "1"
                   and d.get("withdrawalStatus") == "0" and d.get("secCode") and not store.has_annual_report(con, d["docID"])]
        for d in targets:
            try:
                rec = annual.parse(xbrlcsv.read_facts(edinet.download_csv_zip(d["docID"])), d["docID"])
                store.upsert_annual_report(con, rec, (d.get("submitDateTime") or "")[:10])
            except Exception as e:
                print(d["docID"], "FAILED", e, file=sys.stderr)
        print(day, f"filings={len(docs)} new_annual={len(targets)}", flush=True)
    print("cross-holdings (named, linked to ticker):", crossholdings.rebuild(con))
    print("indicators (companies, index months):", indicators.rebuild(con))


def _nport(con, quarter):
    import json, os
    holdings = nport.japan_holdings(nport.download(quarter))
    cache = os.path.join(nport.DIR, "isin_map.json")
    known = json.load(open(cache)) if os.path.exists(cache) else {}
    isin_map = nport.isin_to_ticker([h["isin"] for h in holdings], known,
                                    save=lambda m: json.dump(m, open(cache, "w")))
    summary = nport.summarize(holdings, isin_map, quarter)
    store.replace_us_fund_holdings(con, summary)
    mapped = sum(1 for h in holdings if isin_map.get(h["isin"]))
    print(f"positions={len(holdings)} mapped_to_ticker={mapped} tickers={len(summary)} "
          f"unmapped_isins={sum(1 for i, t in isin_map.items() if not t)}")


def _report(con):
    recs = store.large_holdings(con, limit=10**9)
    n = len(recs)
    if not n:
        print("no large-holding records")
        return
    def share(pred):
        return f"{sum(1 for r in recs if pred(r))}/{n}"
    print("records", n)
    print("ratio parsed      ", share(lambda r: r["holding_ratio_pct"] is not None))
    print("ratio in 0-100    ", share(lambda r: r["holding_ratio_pct"] is not None and 0 <= r["holding_ratio_pct"] <= 100))
    print("issuer linked     ", share(lambda r: r.get("issuer_edinet_code")))
    print("submit date parsed", share(lambda r: r["submit_date"]))
    print("change report has previous ratio",
          share(lambda r: r["report_kind"] != "change_report" or r["previous_ratio_pct"] is not None))
    print("ratio from XBRL   ", share(lambda r: r.get("ratio_source") == "xbrl"))
    both = [r for r in recs if r.get("ratio_source") == "xbrl" and r.get("pdf_ratio_pct") is not None]
    if both:
        agree = sum(1 for r in both if r["pdf_ratio_pct"] == r["holding_ratio_pct"])
        print(f"PDF vs XBRL ratio agreement (both parsed) {agree}/{len(both)}")
        for r in both:
            if r["pdf_ratio_pct"] != r["holding_ratio_pct"]:
                print("  mismatch", r["doc_id"], r["pdf_ratio_pct"], r["holding_ratio_pct"])


def _reparse_cached(con):
    """Rebuild the three audited derived datasets without network access.

    Each document is committed separately so a concurrent writer is blocked
    only for the brief delete/insert of that document.
    """
    from . import buybacks, governance, voting

    cache = edinet.CSV_CACHE
    changed = {"voting_results": 0, "buyback_reports": 0, "governance_events": 0}
    before = {
        "voting_results": con.execute("SELECT COUNT(*) FROM voting_results").fetchone()[0],
        "buyback_reports": con.execute("SELECT COUNT(*) FROM buyback_reports").fetchone()[0],
        "governance_events": con.execute("SELECT COUNT(*) FROM governance_events").fetchone()[0],
    }

    voting_docs = con.execute(
        "SELECT r.doc_id, f.submit_datetime, f.edinet_code, COALESCE(i.ticker, f.ticker), f.parent_doc_id "
        "FROM voting_read r JOIN filings f ON f.doc_id = r.doc_id LEFT JOIN issuers i ON i.edinet_code = f.edinet_code ORDER BY r.doc_id"
    ).fetchall()
    for doc_id, submitted, code, ticker, parent in voting_docs:
        path = os.path.join(cache, f"{doc_id}.zip")
        if not os.path.isfile(path):
            continue
        old = con.execute("SELECT * FROM voting_results WHERE doc_id = ? ORDER BY seq", (doc_id,)).fetchall()
        try:
            is_voting, parsed = voting.parse(xbrlcsv.read_facts(open(path, "rb").read()))
            if is_voting:
                voting.store(con, doc_id, parsed, submitted, code, ticker, parent)
            else:
                con.execute("DELETE FROM voting_results WHERE doc_id = ?", (doc_id,))
                con.execute("INSERT OR REPLACE INTO voting_read VALUES (?,?,?,?)", (doc_id, "not_voting", 0, None))
            new = con.execute("SELECT * FROM voting_results WHERE doc_id = ? ORDER BY seq", (doc_id,)).fetchall()
            changed["voting_results"] += old != new
            con.commit()
        except Exception:
            con.rollback()
            raise

    buyback_docs = con.execute(
        "SELECT r.doc_id, f.doc_type_code, f.submit_datetime, f.edinet_code, f.filer_name_ja, f.parent_doc_id, COALESCE(i.ticker, f.ticker) "
        "FROM buyback_reports r JOIN filings f ON f.doc_id = r.doc_id LEFT JOIN issuers i ON i.edinet_code = f.edinet_code ORDER BY r.doc_id"
    ).fetchall()
    for doc_id, doc_type, submitted, code, name, parent, ticker in buyback_docs:
        path = os.path.join(cache, f"{doc_id}.zip")
        if not os.path.isfile(path):
            continue
        old = con.execute("SELECT record, parse_status FROM buyback_reports WHERE doc_id = ?", (doc_id,)).fetchone()
        try:
            parsed = buybacks.parse_facts(xbrlcsv.read_facts(open(path, "rb").read()))
            buybacks.store(con, parsed, doc_id, doc_type, submitted, code, name, parent, ticker)
            new = con.execute("SELECT record, parse_status FROM buyback_reports WHERE doc_id = ?", (doc_id,)).fetchone()
            changed["buyback_reports"] += old != new
            con.commit()
        except Exception:
            con.rollback()
            raise

    governance_docs = con.execute(
        "SELECT e.doc_id, e.type, f.submit_datetime, f.edinet_code, f.filer_name_ja, f.description_ja, f.parent_doc_id, f.has_csv, COALESCE(i.ticker, f.ticker) "
        "FROM governance_events e JOIN filings f ON f.doc_id = e.doc_id LEFT JOIN issuers i ON i.edinet_code = f.edinet_code ORDER BY e.doc_id, e.type"
    ).fetchall()
    for doc_id, kind, submitted, code, name, description, parent, has_csv, ticker in governance_docs:
        path = os.path.join(cache, f"{doc_id}.zip")
        if not has_csv or not os.path.isfile(path):
            continue
        old = con.execute("SELECT record FROM governance_events WHERE doc_id = ? AND type = ?", (doc_id, kind)).fetchone()
        meta = {"doc_id": doc_id, "submit_date": (submitted or "")[:10], "edinet_code": code, "filer_name_ja": name,
                "description_ja": description, "parent_doc_id": parent, "has_csv": has_csv, "ticker": ticker}
        try:
            blocks = governance.text_blocks(open(path, "rb").read())
            if kind == "internal_control":
                fields, quote = governance.parse_internal_control(blocks, description, parent)
                status = "parsed" if fields["conclusion"] else "unparsed"
            else:
                fields, quote = governance.parse_auditor_change(blocks)
                fields["is_amendment"] = bool(parent)
                fields["amends_doc_id"] = parent
                if fields["change_date"] and meta["submit_date"] and fields["change_date"] > meta["submit_date"] and not fields["change_date_scheduled"]:
                    fields["warnings"] = ["future_change_date_not_marked_scheduled"]
                status = "parsed" if (fields["outgoing_auditor"] or fields["outgoing_is_individual_accountant"]) and fields["change_date"] else "unparsed"
                if status == "parsed" and fields.get("warnings"):
                    status = "partial"
            governance._store(con, governance._row(kind, meta, fields, quote))
            con.execute("INSERT OR REPLACE INTO governance_read VALUES (?,?,?,?)", (doc_id, kind, status, None))
            new = con.execute("SELECT record FROM governance_events WHERE doc_id = ? AND type = ?", (doc_id, kind)).fetchone()
            changed["governance_events"] += old != new
            con.commit()
        except Exception:
            con.rollback()
            raise

    after = {table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before}
    for table in before:
        print(f"{table}: rows {before[table]} -> {after[table]} ({after[table] - before[table]:+d}), changed_documents={changed[table]}")


# ---------- snapshots ----------

def _counts(db_path):
    if not os.path.isfile(db_path):
        raise ValueError(f"{db_path} does not exist")
    # query_only keeps this check read-only without relying on SQLite URI
    # parsing, which is unreliable for long macOS pytest temporary paths.
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA query_only = ON")
    try:
        if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError(f"{db_path} failed its integrity check")
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        return {n: con.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0] for n in names}
    finally:
        con.close()


def _copy_db(src, dst):
    a, b = sqlite3.connect(f"file:{src}?mode=ro", uri=True), sqlite3.connect(dst)
    a.backup(b)  # consistent even if another process is writing
    a.close()
    b.close()


def shrink_problems(before, after, share=0.2, least=100):
    return [f"{t}: {n} -> {after.get(t, 0)} rows" for t, n in before.items()
            if n - after.get(t, 0) > least and n - after.get(t, 0) > n * share]


def store_check(data_dir, allow_shrink=False):
    """Integrity of the live store, and no large loss of rows since it was restored."""
    counts = _counts(os.path.join(data_dir, "japanfilings.db"))
    restored = os.path.join(data_dir, "restored_manifest.json")
    if os.path.exists(restored) and not allow_shrink:
        problems = shrink_problems(json.load(open(restored))["counts"], counts)
        if problems:
            raise ValueError("store shrank since it was restored: " + "; ".join(problems))
    return counts


def snapshot(out, data_dir, allow_shrink=False):
    """Archive the store, the D1 hash state and the ISIN map together, so they can never disagree.
    Refuses if the store fails its check or lost many rows since it was restored."""
    db, state = os.path.join(data_dir, "japanfilings.db"), os.path.join(data_dir, "d1_state.db")
    isin = os.path.join(data_dir, "nport", "isin_map.json")
    with tempfile.TemporaryDirectory(dir=data_dir) as tmp:
        copy = os.path.join(tmp, "japanfilings.db")
        _copy_db(db, copy)
        counts = _counts(copy)
        store_check(data_dir, allow_shrink)
        files = {"japanfilings.db": copy}
        if os.path.exists(state):
            files["d1_state.db"] = os.path.join(tmp, "d1_state.db")
            _copy_db(state, files["d1_state.db"])
        if os.path.exists(isin):
            files["isin_map.json"] = isin
        manifest = os.path.join(tmp, "manifest.json")
        with open(manifest, "w") as f:
            json.dump({"created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "counts": counts}, f)
        files["manifest.json"] = manifest
        partial = out + ".partial"
        with tarfile.open(partial, "w:gz") as tar:
            for name, path in files.items():
                tar.add(path, arcname=name)
        os.replace(partial, out)
    return counts


def restore(archive, data_dir, force=False, min_filings=1000):
    """Unpack and check a snapshot. Refuses to overwrite an existing store unless forced."""
    db = os.path.join(data_dir, "japanfilings.db")
    if os.path.exists(db) and not force:
        raise ValueError(f"{db} exists; use --force to replace it")
    os.makedirs(data_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=data_dir) as tmp:
        with tarfile.open(archive, "r:gz") as tar:
            for name in ("japanfilings.db", "d1_state.db", "isin_map.json", "manifest.json"):
                try:
                    member = tar.getmember(name)
                except KeyError:
                    continue
                if not member.isfile():
                    raise ValueError(f"{name} in the archive is not a file")
                with tar.extractfile(member) as src, open(os.path.join(tmp, name), "wb") as dst:
                    shutil.copyfileobj(src, dst)
        counts = _counts(os.path.join(tmp, "japanfilings.db"))
        if counts.get("filings", 0) < min_filings:
            raise ValueError(f"snapshot has only {counts.get('filings', 0)} filings")
        for suffix in ("-wal", "-shm"):  # left over from the store being replaced
            if os.path.exists(db + suffix):
                os.remove(db + suffix)
        os.replace(os.path.join(tmp, "japanfilings.db"), db)
        if os.path.exists(os.path.join(tmp, "d1_state.db")):
            os.replace(os.path.join(tmp, "d1_state.db"), os.path.join(data_dir, "d1_state.db"))
        if os.path.exists(os.path.join(tmp, "isin_map.json")):
            os.makedirs(os.path.join(data_dir, "nport"), exist_ok=True)
            os.replace(os.path.join(tmp, "isin_map.json"), os.path.join(data_dir, "nport", "isin_map.json"))
        with open(os.path.join(data_dir, "restored_manifest.json"), "w") as f:
            json.dump({"counts": counts}, f)
    return counts


# ---------- freshness ----------

def status_problem(payload, today, max_age_days):
    """Why a /v1/status answer means stale data, or None. Holidays can reach the threshold: see docs/DEPLOY.md."""
    latest = (payload or {}).get("latest_large_holding_submit_date")
    if not (payload or {}).get("ok") or not latest:
        return f"status has no data: {payload}"
    age = (date.fromisoformat(today) - date.fromisoformat(latest[:10])).days
    return f"latest filing is {latest}, {age} days old (limit {max_age_days})" if age > max_age_days else None


def _check_freshness(url, max_age_days, tries=3, wait=30):
    error = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "japanstake-watchdog/0.1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                error = status_problem(json.load(r), (datetime.now(timezone.utc) + timedelta(hours=9)).date().isoformat(), max_age_days)
            break
        except Exception as e:  # network or 5xx: try again before raising the alarm
            error = f"{url}: {e}"
            if attempt + 1 < tries:
                time.sleep(wait)
    print(error or "fresh", file=sys.stderr if error else sys.stdout)
    return 1 if error else 0


def _ops(argv):
    """Commands that manage the store file itself, so they must not open it first."""
    cmd, args = argv[0], argv[1:]
    data_dir = os.path.dirname(os.path.abspath(store.DEFAULT_DB))
    try:
        if cmd == "snapshot":
            print("snapshot rows:", sum(snapshot(args[0], data_dir, "--allow-shrink" in args).values()))
        elif cmd == "restore":
            print("restored rows:", sum(restore(args[0], data_dir, "--force" in args).values()))
        elif cmd == "store-check":
            print("store rows:", sum(store_check(data_dir, "--allow-shrink" in args).values()))
        else:
            max_age = int(args[args.index("--max-age-days") + 1]) if "--max-age-days" in args else 5
            return _check_freshness(args[0], max_age)
    except (ValueError, OSError, tarfile.TarError, sqlite3.Error) as e:
        print(f"{cmd} FAILED: {e}", file=sys.stderr)
        return 1
    return 0


def main(argv):
    if argv[0] in ("snapshot", "restore", "store-check", "check-freshness"):
        return _ops(argv)
    con = store.connect()
    cmd, args = argv[0], argv[1:]
    if cmd == "codelist":
        issuers = codelist.parse(codelist.download())
        store.upsert_issuers(con, issuers)
        codelist.fill_missing_name_en(con)  # the code list leaves some English names blank
        # A delisted company has no ticker in the code list: take the one its own filings carry, when they agree.
        con.execute("UPDATE issuers SET ticker = (SELECT MAX(f.ticker) FROM filings f WHERE f.edinet_code = issuers.edinet_code "
                    "AND f.ticker NOT IN ('', '0000')) WHERE listed = 0 AND ticker IS NULL AND (SELECT COUNT(DISTINCT f.ticker) "
                    "FROM filings f WHERE f.edinet_code = issuers.edinet_code AND f.ticker NOT IN ('', '0000')) = 1")
        con.commit()
        listed = [i for i in issuers if i["listed"]]
        print(f"issuers={len(issuers)} listed={len(listed)} "
              f"listed_with_english_name={sum(1 for i in listed if i['name_en'])}")
    elif cmd == "figi":
        tickers = [r[0] for r in con.execute(
            "SELECT i.ticker FROM issuers i LEFT JOIN identifiers x ON x.ticker = i.ticker "
            "WHERE i.listed = 1 AND i.ticker IS NOT NULL AND x.ticker IS NULL ORDER BY i.ticker")]
        found = 0
        for t, rec in figi.map_tickers(tickers):
            if rec:
                store.upsert_identifiers(con, t, rec)
                found += 1
            con.commit()
        print(f"mapped {found}/{len(tickers)}")
    elif cmd == "pdf":
        for doc_id in args:
            rec = _ingest_pdf_bytes(con, doc_id, edinet.download_pdf(doc_id))
            print(doc_id, rec["report_kind"], rec["issuer_ticker"], rec["holding_ratio_pct"])
    elif cmd == "range":
        _range(con, args[0], args[1])
    elif cmd == "annual":
        _annual(con, args[0], args[1])
    elif cmd == "annual-reparse":  # re-run the parser on every stored annual report (cached CSVs, no new requests)
        rows = con.execute("SELECT doc_id, submit_date FROM annual_reports").fetchall()
        for doc_id, submit_date in rows:
            store.upsert_annual_report(con, annual.parse(xbrlcsv.read_facts(edinet.download_csv_zip(doc_id)), doc_id), submit_date)
        print("reparsed", len(rows))
        print("cross-holdings (named, linked to ticker):", crossholdings.rebuild(con))
        print("indicators (companies, index months):", indicators.rebuild(con))
    elif cmd == "reparse":
        _reparse_cached(con)
    elif cmd == "text-reindex":  # recompute only the text indicators of stored annual reports (cached CSVs)
        import json
        rows = con.execute("SELECT rowid, doc_id, record FROM annual_reports").fetchall()
        for rowid, doc_id, record in rows:
            rec = json.loads(record)
            rec["text_indicators"] = textindex.from_facts(xbrlcsv.read_facts(edinet.download_csv_zip(doc_id)))
            con.execute("UPDATE annual_reports SET record = ? WHERE rowid = ?", (json.dumps(rec, ensure_ascii=False), rowid))
        con.commit()
        print("text indicators recomputed", len(rows))
        print("indicators (companies, index months):", indicators.rebuild(con))
    elif cmd == "halfyear":  # half-year results of every listed company from EDINET: halfyear [N]
        from . import halfyear
        print("half-year reports stored:", halfyear.run(con, int(args[0]) if args else None))
    elif cmd == "buybacks":  # monthly share-buyback reports: buybacks [N]
        from . import buybacks
        print("buyback reports read, parsed:", buybacks.run(con, int(args[0]) if args else None))
    elif cmd == "governance":  # internal-control conclusions and auditor changes: governance [N]
        from . import governance
        print("governance filings:", governance.run(con, int(args[0]) if args else None))
    elif cmd == "filing-details":
        from . import filing_details
        network = "--network" in args
        minutes = int(args[args.index("--minutes") + 1]) if "--minutes" in args else None
        nums = [a for i, a in enumerate(args) if a.isdigit() and (i == 0 or args[i - 1] != "--minutes")]
        print("filing details read, failed:", filing_details.run(con, int(nums[0]) if nums else None, cached_only=not network, minutes=minutes))
    elif cmd == "voting":
        from . import voting
        network = "--network" in args
        all_reports = "--all" in args
        nums = [a for a in args if a.isdigit()]
        print("voting documents, reports, complete:", voting.run(con, int(nums[0]) if nums else None, agm_window_only=not all_reports, cached_only=not network))
    elif cmd == "tender-offers":
        from . import tender_offers
        network = "--network" in args
        nums = [a for a in args if a.isdigit()]
        print("tender-offer documents, complete:", tender_offers.run(
            con, int(nums[0]) if nums else None, cached_only=not network, reparse="--reparse" in args))
    elif cmd == "investors":
        import json
        from . import investors
        print("investor groups:", json.dumps(investors.rebuild(con), ensure_ascii=False))
    elif cmd == "ratings":  # credit ratings stated in bond filings on EDINET: ratings [N]
        from . import ratings
        print("bond filings read, ratings stored:", ratings.run(con, int(args[0]) if args else None))
    elif cmd == "indicators":
        print("indicators (companies, index months):", indicators.rebuild(con))
    elif cmd == "nport":
        _nport(con, args[0])
    elif cmd == "report":
        _report(con)
    elif cmd == "recheck":  # re-derive XBRL ratios, holders and issuer links for every stored 5% record
        changed = 0
        for rec in store.large_holdings(con, limit=10**9):
            before = (rec["holding_ratio_pct"], rec["previous_ratio_pct"])
            try:
                _apply_xbrl(rec, _xbrl_for_large_holding(rec["doc_id"]))
            except Exception as e:  # e.g. no CSV for this filing
                print(rec["doc_id"], "FAILED", e, file=sys.stderr)
                continue
            changed += before != (rec["holding_ratio_pct"], rec["previous_ratio_pct"])
            store.upsert_large_holding(con, rec)
        print("records whose ratios changed:", changed)
        print("disclosed trades:", trades.rebuild(con))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    from .refresh import tracked
    sys.exit(tracked(main, sys.argv[1:] or ["help"]))
