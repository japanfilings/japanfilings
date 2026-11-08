"""Applies the corrections from DATA_CHECK_20261102_JA.md to rows already in the database.

The parsers were fixed for new rows; this brings existing rows in line, from the stored records and cached CSVs
only (no network).  Safe beside a running backfill: every write is a short transaction of at most CHUNK rows, and
record updates are compare-and-swap (`WHERE record = <the text that was read>`), so a row another process changed
in the meantime is skipped, not overwritten.  Re-run it after the backfill restarts to cover rows it stored with
the old code.  Idempotent.

  python3 scripts/fix_data_check_20261102.py [--db PATH] [--only step,step]
"""
import argparse
import copy
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from japanfilings import backfill, edinet, filing_details, governance, halfyear, indicators, investors, store, tender_offers, trades, voting, xbrlcsv  # noqa: E402

CHUNK = 200


def _cas(con, sql, batch):
    """Run (new..., key, old_record) updates in short transactions. Returns (applied, skipped)."""
    applied = skipped = 0
    for i in range(0, len(batch), CHUNK):
        with con:
            for args in batch[i:i + CHUNK]:
                if con.execute(sql, args).rowcount:
                    applied += 1
                else:
                    skipped += 1
    return applied, skipped


def step_tender_offers(con):
    batch = []
    rows = con.execute(
        "SELECT t.doc_id, t.doc_type_code, t.ticker, t.record, s.ticker AS subject_ticker, fl.ticker AS filer_ticker "
        "FROM tender_offers t JOIN filings f ON f.doc_id = t.doc_id LEFT JOIN issuers s ON s.edinet_code = f.subject_edinet_code "
        "LEFT JOIN issuers fl ON fl.edinet_code = f.edinet_code").fetchall()
    for r in rows:
        want = tender_offers.target_ticker_for(r["doc_type_code"], r["subject_ticker"], r["filer_ticker"])
        if want and want != r["ticker"]:
            rec = json.loads(r["record"])
            rec["ticker"] = want
            batch.append((want, json.dumps(rec, ensure_ascii=False), r["doc_id"], r["record"]))
            print("  tender_offers", r["doc_id"], r["ticker"], "->", want)
    return _cas(con, "UPDATE tender_offers SET ticker = ?, record = ? WHERE doc_id = ? AND record = ?", batch)


def wrong_learned_tickers(con):
    """[(edinet_code, stored, own)] for delisted issuers whose stored ticker disagrees with the one every filing of
    the company itself carries.  The code list has no ticker for a delisted company, so `store.link_issuer` learns
    one from a 5% report that names the issuer -- and a typo there sticks.  DD Group (E05673): the code list ticker
    is empty, UBS typed 3037 in S100WH1W, the name matched, so issuers got 3037; the company's own five EDINET
    filings (tender-offer opinion, 5% reports) all carry 3073 (DATA_CHECK_20261105 高-2).  This step's earlier run
    then 'corrected' six 3073 reports to 3037."""
    return [tuple(r) for r in con.execute(
        "SELECT i.edinet_code, i.ticker, MAX(f.ticker) FROM issuers i JOIN filings f ON f.edinet_code = i.edinet_code "
        "WHERE i.listed = 0 AND f.ticker IS NOT NULL AND f.ticker NOT IN ('', '0000') "
        "GROUP BY i.edinet_code HAVING COUNT(DISTINCT f.ticker) = 1 AND (i.ticker IS NULL OR MAX(f.ticker) <> i.ticker)")]


def step_issuer_tickers(con):
    """Put back the issuer's own ticker where a typo was learned (see wrong_learned_tickers).  Runs before
    tender_offers and large_holdings, which then re-link the affected rows (DD Group: 3037 -> 3073, filed 3037 kept)."""
    fixes = wrong_learned_tickers(con)
    for code, stored, own in fixes:
        print("  issuer", code, stored, "->", own)
    with con:
        con.executemany("UPDATE issuers SET ticker = ? WHERE edinet_code = ? AND ticker IS ?",
                        [(own, code, stored) for code, stored, own in fixes])
    return len(fixes), 0


def step_large_holdings(con):
    """Re-link issuers (typo'd tickers, change reports titled as initial), label bond prices, add data warnings."""
    changes, batch, kinds = Counter(), [], Counter()
    ht_ticker, ht_note = [], []  # holder_trades rows of changed filings, patched in place (see step_trades)
    last = ""
    while True:
        rows = con.execute("SELECT doc_id, record FROM large_holdings WHERE doc_id > ? ORDER BY doc_id LIMIT 500", (last,)).fetchall()
        if not rows:
            break
        last = rows[-1]["doc_id"]
        for r in rows:
            rec = json.loads(r["record"])
            old = copy.deepcopy(rec)
            store.link_issuer(con, rec)
            if rec.get("issuer_ticker_filed") == rec.get("issuer_ticker"):  # a reverted correction leaves no trace
                rec.pop("issuer_ticker_filed", None)
            for n, t in enumerate(rec.get("trades") or [], 1):
                if trades.label_bond_price(t) != old["trades"][n - 1]:
                    ht_note.append((t["note_ja"], r["doc_id"], n))
            if rec == old:
                continue
            for k in ("issuer_ticker", "issuer_edinet_code", "report_kind", "issuer_match", "data_warnings"):
                if rec.get(k) != old.get(k):
                    changes[k] += 1
            if rec.get("issuer_ticker") != old.get("issuer_ticker"):
                ht_ticker.append((rec.get("issuer_ticker"), r["doc_id"]))
                kinds[rec.get("issuer_match")] += 1
                print("  ticker", r["doc_id"], old.get("issuer_ticker"), "->", rec.get("issuer_ticker"), rec.get("issuer_match"))
            batch.append((rec.get("issuer_ticker"), json.dumps(rec, ensure_ascii=False), r["doc_id"], r["record"]))
        if len(batch) >= CHUNK:
            _cas(con, "UPDATE large_holdings SET issuer_ticker = ?, record = ? WHERE doc_id = ? AND record = ?", batch)
            batch = []
    if batch:
        _cas(con, "UPDATE large_holdings SET issuer_ticker = ?, record = ? WHERE doc_id = ? AND record = ?", batch)
    print("  fields changed:", dict(changes), "ticker corrections by:", dict(kinds))
    # holder_trades is rebuilt from these records by trades.rebuild (the backfill does that when it finishes, and
    # it would add every backfilled filing at once).  Here only the rows already there are corrected.
    with con:
        con.executemany("UPDATE holder_trades SET issuer_ticker = ? WHERE doc_id = ?", ht_ticker)
        con.executemany("UPDATE holder_trades SET unit_price_jpy = NULL, note_ja = ? WHERE doc_id = ? AND row_no = ?", ht_note)
    print("  holder_trades: tickers of", len(ht_ticker), "filings, bond price labels on", len(ht_note), "trades (where rows exist)")
    return sum(changes.values()), 0


def step_events(con):
    print("  events:", backfill.refresh_events(con, chunk=2000))
    return 0, 0


def step_trades_rebuild(con):
    """Not in the default run: rebuilds holder_trades from every stored filing, which adds the backfilled history."""
    print("  holder_trades rows:", trades.rebuild(con))
    return 0, 0


def step_indicators(con):
    n = indicators.refresh_ownership_activity(con)
    print("  companies with changed ownership_activity:", n)
    return n, 0


def step_investors(con):
    print("  investors:", investors.rebuild(con))
    return 0, 0


def step_half_year(con):
    batch = []
    rows = con.execute(
        "SELECT e.ticker, e.fiscal_year_end_month, e.url, e.record, f.description_ja FROM earnings_releases e "
        "JOIN filings f ON f.doc_id = substr(e.url, 8) WHERE e.period = 'H1' AND e.url LIKE 'edinet:%'").fetchall()
    taken = {(r[0], r[1]) for r in con.execute("SELECT ticker, fiscal_year_end_month FROM earnings_releases WHERE period = 'H1'")}
    applied = skipped = 0
    for r in rows:
        fy = halfyear.fiscal_year_end_month(r["description_ja"])
        if not fy or fy == r["fiscal_year_end_month"]:
            continue
        if (r["ticker"], fy) in taken:
            skipped += 1
            print("  H1 key already exists, left:", r["ticker"], fy)
            continue
        rec = json.loads(r["record"])
        rec["fiscal_year_end_month"] = fy
        with con:
            con.execute("DELETE FROM earnings_releases WHERE ticker = ? AND fiscal_year_end_month = ? AND period = 'H1'", (r["ticker"], r["fiscal_year_end_month"]))
            con.execute("INSERT INTO earnings_releases VALUES (?,?,?,?,?,?)",
                        (r["ticker"], fy, "H1", rec.get("release_date"), r["url"], json.dumps(rec, ensure_ascii=False)))
        taken.add((r["ticker"], fy))
        applied += 1
    return applied, skipped


def step_voting(con):
    """Re-read the cached CSV of each stored document; only rate_basis is updated, and only when rows line up exactly."""
    applied = skipped = 0
    for (doc_id,) in con.execute("SELECT DISTINCT doc_id FROM voting_results").fetchall():
        path = os.path.join(edinet.CSV_CACHE, doc_id + ".zip")
        if not os.path.exists(path):
            continue
        stored = con.execute("SELECT seq, proposal_no, candidate_ja, votes_for, votes_against, rate_basis FROM voting_results WHERE doc_id = ? ORDER BY seq", (doc_id,)).fetchall()
        with open(path, "rb") as fh:
            ok, parsed = voting.parse(xbrlcsv.read_facts(fh.read()))
        rows = parsed["rows"] if ok and parsed else []
        if len(rows) != len(stored):
            skipped += 1
            continue
        todo = []
        for s, n in zip(stored, rows):
            if (s["proposal_no"], s["candidate_ja"], s["votes_for"], s["votes_against"]) != (n["proposal_no"], n["candidate_ja"], n["votes_for"], n["votes_against"]):
                todo = None
                break
            if n["rate_basis"] != s["rate_basis"]:
                todo.append((n["rate_basis"], doc_id, s["seq"]))
        if todo is None:
            skipped += 1
            continue
        if todo:
            with con:
                con.executemany("UPDATE voting_results SET rate_basis = ? WHERE doc_id = ? AND seq = ?", todo)
            applied += len(todo)
            print("  voting", doc_id, [(t[2], t[0]) for t in todo])
    return applied, skipped


def step_compensation(con):
    batch = []
    for r in con.execute("SELECT doc_id, record FROM filing_details WHERE item = 'director_compensation'").fetchall():
        rec = json.loads(r["record"])
        kept = [w for w in rec.get("warnings") or [] if not w.startswith("scale_suspect")]
        new = kept + filing_details.compensation_warnings(rec.get("categories") or [])
        if new != (rec.get("warnings") or []):
            rec["warnings"] = new
            batch.append((json.dumps(rec, ensure_ascii=False), r["doc_id"], r["record"]))
    return _cas(con, "UPDATE filing_details SET record = ? WHERE doc_id = ? AND item = 'director_compensation' AND record = ?", batch)


def step_governance(con):
    """Re-read cached auditor-change text; only the two firm names are updated."""
    batch = []
    for r in con.execute("SELECT doc_id, record FROM governance_events WHERE type = 'auditor_change'").fetchall():
        path = os.path.join(edinet.CSV_CACHE, r["doc_id"] + ".zip")
        if not os.path.exists(path):
            continue
        with open(path, "rb") as fh:
            text = next((f["value"] for f in xbrlcsv.read_facts(fh.read()) if f["element"].endswith(":" + governance._E_AUDITOR)), None)
        if not text:
            continue
        fields, _ = governance.parse_auditor_change(text)
        rec = json.loads(r["record"])
        changed = False
        for k in ("outgoing_auditor", "incoming_auditor"):
            if fields.get(k) and fields[k] != rec["fields"].get(k) and rec["fields"].get(k):
                print("  governance", r["doc_id"], k, rec["fields"].get(k), "->", fields[k])
                rec["fields"][k] = fields[k]
                changed = True
        if changed:
            batch.append((json.dumps(rec, ensure_ascii=False), r["doc_id"], r["record"]))
    return _cas(con, "UPDATE governance_events SET record = ? WHERE doc_id = ? AND type = 'auditor_change' AND record = ?", batch)


STEPS = [("issuer_tickers", step_issuer_tickers), ("tender_offers", step_tender_offers), ("large_holdings", step_large_holdings), ("events", step_events),
         ("half_year", step_half_year), ("voting", step_voting), ("compensation", step_compensation),
         ("governance", step_governance), ("indicators", step_indicators),
         ]
OPTIONAL = [("trades_rebuild", step_trades_rebuild), ("investors", step_investors)]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=store.DEFAULT_DB)
    ap.add_argument("--only", default="")
    args = ap.parse_args(argv)
    only = set(filter(None, args.only.split(",")))
    con = store.connect(args.db)
    con.execute("PRAGMA busy_timeout = 60000")
    for name, fn in STEPS + OPTIONAL:
        if (only and name not in only) or (not only and (name, fn) in OPTIONAL):
            continue
        print(name, flush=True)
        applied, skipped = fn(con)
        print(f"  -> applied {applied}, skipped {skipped}", flush=True)


if __name__ == "__main__":
    main()
