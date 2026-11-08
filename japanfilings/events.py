"""Ownership-change events derived from 5% rule filings, with a one-line English summary each.

Types:
  new_stake           initial report with no previous ratio and a ratio of at least 5%: the filer crossed 5%.
                      An "initial" report that states a previous ratio is a change from it, not a new stake
  stake_increase      change report with a higher ratio
  stake_decrease      change report with a lower ratio (still at or above 5%)
  exit_below_5pct     the ratio fell below 5%
  threshold_crossed   the ratio crossed 10, 20, 33.4 (veto over special resolutions) or 50%
  activist_purpose    the stated purpose now includes making proposals to management, and the filer's
                      previous filing on the same company did not (or this is the first filing)
  purpose_changed     the purpose tags differ from the filer's previous filing on the same company
  amendment           a correction to an earlier filing

`ingested_at` is set when an event is first seen and never changes; webhooks deliver by it.
"""
import json
import re
from datetime import datetime, timezone

from .investors import filer_is_person, public_filer_names

INDIVIDUAL = "individual filer"  # same words as INDIVIDUAL in worker/src/privacy.ts

SCHEMA = """
CREATE TABLE IF NOT EXISTS ownership_events (
  event_id TEXT PRIMARY KEY,
  type TEXT,
  submit_date TEXT,
  doc_id TEXT,
  issuer_ticker TEXT,
  filer_edinet_code TEXT,
  ingested_at TEXT,
  record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ev_date ON ownership_events(submit_date);
CREATE INDEX IF NOT EXISTS ev_ingested ON ownership_events(ingested_at);
CREATE INDEX IF NOT EXISTS ev_ticker ON ownership_events(issuer_ticker, submit_date);
"""

THRESHOLDS = [10.0, 20.0, 33.4, 50.0]

# "現時点で重要提案行為等を行う予定はない" and similar: proposals are mentioned but ruled out.
_NO_PROPOSALS = re.compile(
    r"提案[^。]{0,30}(予定は?(ない|無い|なく|ありません|ございません|有りません)|行わない|行いません|行う意図は?(ない|ありません)"
    r"|ことはない|ことはありません|目的とせず|目的としない|目的としておりません|考えておりません)")


def is_activist(rec):
    return "may_make_proposals" in (rec.get("purpose_tags") or []) and not _NO_PROPOSALS.search(rec.get("purpose_ja") or "")


def _names(rec):
    """The filer as summaries name it: an organisation by its cleaned name (no officer titles or the people after
    them, no English field that is only a personal name), a person never."""
    if filer_is_person(rec):
        filer = INDIVIDUAL
    else:
        ja, en = public_filer_names(rec, joint_filing_name=True)
        filer = en or ja or INDIVIDUAL
    issuer = rec.get("issuer_name_en") or rec.get("issuer_name_ja")
    if rec.get("issuer_ticker"):
        issuer = f"{issuer} ({rec['issuer_ticker']})"
    return filer, issuer


def _pct(v):
    return f"{v:.2f}%" if v is not None else "n/a"


def derive(rec, previous=None):
    """Events for one 5% filing. `previous` is the same filer's prior filing on the same company, if any."""
    kind = rec.get("report_kind")
    now, before = rec.get("holding_ratio_pct"), rec.get("previous_ratio_pct")
    filer, issuer = _names(rec)
    out = []

    def add(type_, summary, **extra):
        out.append({"type": type_, "summary_en": summary, **extra})

    activist, was_activist = is_activist(rec), previous is not None and is_activist(previous)
    ref = before if before is not None else (previous or {}).get("holding_ratio_pct") if kind != "initial_report" else None
    if kind == "amendment":
        add("amendment", f"{filer} filed a correction to a 5% report on {issuer}.", amends_doc_id=rec.get("amends_doc_id"))
    elif kind == "initial_report" and not before:
        # A first report with nothing before it.  Below 5% it did not cross the threshold, so no event
        # (the old code called a 1.88% holding "crossing the 5%").
        if now is None or now >= 5:
            add("new_stake", f"{filer} disclosed a {_pct(now)} stake in {issuer}, crossing the 5% reporting threshold.")
    elif now is not None and ref is not None:
        # Against the stated previous ratio, or else the filer's previous filing on record (said so in the summary).
        was = _pct(ref) + ("" if before is not None else " in its previous filing")
        if now < 5 <= ref:
            add("exit_below_5pct", f"{filer} cut holdings in {issuer} to {_pct(now)} from {was}, below the 5% threshold.")
        elif now > ref:
            add("stake_increase", f"{filer} raised holdings in {issuer} to {_pct(now)} from {was}.")
        elif now < ref:
            add("stake_decrease", f"{filer} reduced holdings in {issuer} to {_pct(now)} from {was}.")
    if now is not None and kind != "amendment":
        # Only a first report starts from 0. A change report without a stated previous ratio is compared with the
        # filer's previous filing on record; with none, the move is unknown and no threshold event is made.
        base, label = before, None
        if base is None and kind == "initial_report":
            base, label = 0.0, "new"
        elif base is None and previous is not None and previous.get("holding_ratio_pct") is not None:
            base, label = previous["holding_ratio_pct"], "previous filing"
        for t in (THRESHOLDS if base is not None else ()):
            if base < t <= now or now < t <= base:
                up = now >= t
                add("threshold_crossed", f"Holdings of {filer} in {issuer} moved {'above' if up else 'below'} {t:g}% "
                    f"({'new' if label == 'new' else _pct(base) + (' in previous filing' if label else '')} -> {_pct(now)}).",
                    threshold_pct=t, direction="up" if up else "down")
    if kind == "amendment":
        pass
    elif activist and not was_activist:
        add("activist_purpose", f"{filer} states it may make proposals to the management of {issuer} "
            f"({'new compared with its previous filing' if previous is not None else 'first filing' if kind == 'initial_report' else 'first filing on record'}).")
    elif previous is not None and rec.get("purpose_tags") and sorted(previous.get("purpose_tags") or []) != sorted(rec["purpose_tags"]):
        add("purpose_changed", f"{filer} changed its stated purpose for holding {issuer}: "
            f"{', '.join(previous.get('purpose_tags') or ['none'])} -> {', '.join(rec.get('purpose_tags') or ['none'])}.",
            previous_purpose_tags=previous.get("purpose_tags") or [])
    person = filer_is_person(rec)
    ja, en = public_filer_names(rec, joint_filing_name=True)
    for e in out:
        e.update({
            "doc_id": rec["doc_id"], "submit_date": rec.get("submit_date"), "issuer_ticker": rec.get("issuer_ticker"),
            "issuer_name_en": rec.get("issuer_name_en"), "issuer_name_ja": rec.get("issuer_name_ja"),
            "filer_edinet_code": rec.get("filer_edinet_code"),
            # A person keeps the names as filed so the public layer can find and remove every spelling; it is flagged.
            "filer_name_en": (rec.get("filer_name_en_filing") or rec.get("filer_name_en")) if person else en,
            "filer_name_ja": rec.get("filer_name_ja") if person else ja, "filer_is_individual": person,
            "filed_by_issuer": "filed_by_issuer" in (rec.get("data_warnings") or []), "holding_ratio_pct": now, "previous_ratio_pct": before,
            "change_pt": round(now - ref, 2) if now is not None and ref is not None else None,
            "compared_with": None if ref is None else "stated_previous_ratio" if before is not None else "previous_filing",
            "purpose_tags": rec.get("purpose_tags") or [], "activist": activist, "source_url": rec.get("source_url"),
        })
        e["event_id"] = f"{rec['doc_id']}:{e['type']}" + (f":{e['threshold_pct']:g}" if "threshold_pct" in e else "")
    return out


def rebuild(con):
    """Derive events for every stored 5% filing, keeping the first-seen time of events already stored."""
    con.executescript(SCHEMA)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")  # same form as JavaScript toISOString()
    last = {}  # (filer, issuer) -> previous non-amendment filing
    # One key per company however a report names it: EDINET code, else ticker or name looked up in the master.
    by_ticker, by_name = {}, {}
    for code, ticker, name in con.execute("SELECT edinet_code, ticker, name_ja FROM issuers").fetchall():
        if ticker:
            by_ticker.setdefault(ticker, code)
        if name:
            by_name.setdefault(name, code)
    n, seen = 0, set()
    for r in con.execute("SELECT record FROM large_holdings ORDER BY submit_date, doc_id").fetchall():
        rec = json.loads(r["record"])
        t, nm = rec.get("issuer_ticker"), rec.get("issuer_name_ja")
        key = (rec.get("filer_edinet_code"), rec.get("issuer_edinet_code") or by_ticker.get(t) or by_name.get(nm) or t or nm)
        events = derive(rec, last.get(key))
        if rec.get("report_kind") != "amendment":
            last[key] = rec
        elif key in last and rec.get("holding_ratio_pct") is not None:  # a correction restates the ratio to compare with
            last[key] = {**last[key], "holding_ratio_pct": rec["holding_ratio_pct"]}
        for e in events:
            con.execute(
                "INSERT INTO ownership_events VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET "
                "type = excluded.type, submit_date = excluded.submit_date, issuer_ticker = excluded.issuer_ticker, record = excluded.record",
                (e["event_id"], e["type"], e["submit_date"], e["doc_id"], e["issuer_ticker"], e["filer_edinet_code"], now,
                 json.dumps(e, ensure_ascii=False)))
            n += 1
            seen.add(e["event_id"])
    # Events no longer derived (a parser or rule fix) are removed, so the table matches the filings.
    stale = [r[0] for r in con.execute("SELECT event_id FROM ownership_events").fetchall() if r[0] not in seen]
    con.executemany("DELETE FROM ownership_events WHERE event_id = ?", [(x,) for x in stale])
    con.commit()
    return n
