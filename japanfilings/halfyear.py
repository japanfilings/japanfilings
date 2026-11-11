"""Half-year results from the half-year securities report (半期報告書, docTypeCode 160) on EDINET.

Every listed company files one, through the API we may use, so this reaches companies whose websites refuse
crawlers or keep releases on hosts that disallow it. It comes later than the earnings release and has no
forecast. Rows go into earnings_releases with period H1 and source edinet_half_year, next to the release rows.
"""
import json
import re
from datetime import date, timedelta

from . import annual, edinet, tanshin, xbrlcsv

# Half-year reports use Interim* contexts; some (former quarterly filers) use YTDDuration for the
# half-year period and QuarterInstant for its end date instead. Both carry the same summary table.
_CTX = re.compile(r"^(?:Current|(Prior1))?(InterimDuration|InterimInstant|YTDDuration|QuarterInstant)(_NonConsolidatedMember)?$")
FIELDS = {"revenue": "revenue", "ordinary_income": "ordinary_income", "profit_before_tax": "profit_before_tax", "net_income": "net_income_parent"}


_PERIOD = re.compile(r"(\d{4})/(\d{2})/(\d{2})\s*[－\-]\s*(\d{4})/(\d{2})/(\d{2})\)")


def fiscal_year_end_month(description):
    """'YYYY-MM' of the fiscal year the half-year report belongs to, from its title.

    The title ends with the interim period, e.g. 半期報告書－第48期(2026/04/01－2026/09/30).  The key used by
    every other earnings_releases row is the month the *fiscal year* ends (2027-03), not the month the interim
    period ends (2026-09), so take the day before the first anniversary of the period's start.
    """
    m = _PERIOD.search(description or "")
    if not m:
        return None
    y, mo, d = (int(x) for x in m.group(1, 2, 3))
    ey, emo = int(m.group(4)), int(m.group(5))
    if not 5 <= (ey * 12 + emo) - (y * 12 + mo) + 1 <= 7:
        # Not a six-month interim (transition period after a fiscal-year change, first report after listing):
        # the period's own end is the only year-end the filing states.
        return f"{ey}-{emo:02d}"
    try:
        end = date(y + 1, mo, d) - timedelta(days=1)
    except ValueError:  # 29 February start
        end = date(y + 1, mo, 28)
    return f"{end.year}-{end.month:02d}"


def _pick(items, key):
    for p in annual.SUMMARY_KEYS[key]:
        hit = next((v for k, v in items.items() if re.fullmatch(p + r"(IFRS|USGAAP|JMIS)?", k)), None)
        if hit is not None:
            return hit
    return None


def parse(facts):
    """{(prior?, non_consolidated?): {element: value}} from the summary table, consolidated when there is one."""
    raw = {}
    for f in facts:
        prefix, _, name = f["element"].partition(":")
        m = _CTX.match(f["context"])
        v = annual._num(f["value"])
        if not m or v is None or not name.endswith("SummaryOfBusinessResults") or not prefix.startswith("jpcrp"):
            continue
        raw.setdefault((bool(m.group(1)), bool(m.group(3))), {})[name.replace("SummaryOfBusinessResults", "")] = v
    if not raw:
        return None
    non_consolidated = not any(not nc for _, nc in raw)
    cur, prior = raw.get((False, non_consolidated)) or {}, raw.get((True, non_consolidated)) or {}
    results = {}
    for key, out in FIELDS.items():
        v, p = _pick(cur, key), _pick(prior, key)
        if v is None:
            continue
        results[out + "_mn_jpy"] = round(v / 1e6, 3)
        results[out + "_yoy_pct"] = round(100 * (v - p) / abs(p), 1) if p and p > 0 else None
    if not results:
        return None
    return {"consolidated": not non_consolidated, "results": results,
            "financial_position": {k: _pick(cur, k) for k in ("total_assets", "net_assets")}}


def run(con, limit=None):
    """Latest half-year report of each listed company, from EDINET; returns the number stored."""
    con.executescript(tanshin.SCHEMA)
    rows = con.execute(
        "SELECT i.ticker, f.doc_id, f.submit_datetime, f.description_ja FROM filings f JOIN issuers i ON i.edinet_code = f.edinet_code "
        "WHERE f.doc_type_code IN ('160', '170') AND i.listed = 1 AND i.ticker IS NOT NULL AND f.has_csv = 1 AND f.withdrawn = 0 "
        "ORDER BY f.submit_datetime DESC").fetchall()
    have = {r[0] for r in con.execute("SELECT url FROM earnings_releases WHERE period = 'H1'")}
    seen, done = set(), 0
    for ticker, doc_id, submitted, desc in rows:
        if ticker in seen:
            continue
        seen.add(ticker)
        url = f"edinet:{doc_id}"
        if url in have:
            continue
        if limit is not None and done >= limit:
            break
        fy = fiscal_year_end_month(desc)
        if not fy:
            continue
        try:
            rec = parse(xbrlcsv.read_facts(edinet.download_csv_zip(doc_id)))
        except Exception as e:
            print(ticker, doc_id, "FAILED", e, flush=True)
            continue
        if not rec:
            print(ticker, doc_id, "no summary table", flush=True)
            continue
        rec.update({"ticker": ticker, "fiscal_year_end_month": fy, "period": "H1", "release_date": submitted[:10],
                    "source": "edinet_half_year", "source_doc_id": doc_id,
                    "source_url": f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{doc_id}"})
        con.execute("INSERT OR REPLACE INTO earnings_releases VALUES (?,?,?,?,?,?)",
                    (ticker, fy, "H1", submitted[:10], url, json.dumps(rec, ensure_ascii=False)))
        con.commit()
        done += 1
    return done
