"""US registered funds' holdings of Japanese stocks, from SEC Form N-PORT quarterly data sets.

SEC data is public; SEC asks for a contact email in the User-Agent (SEC_USER_AGENT in .env).
Only each fund series' latest report in the quarter is kept, and results are stored as one
summary row per Tokyo ticker.
"""
import csv
import io
import json
import os
import sys
import urllib.request
import zipfile
from datetime import datetime

from . import figi

URL = "https://www.sec.gov/files/dera/data/form-n-port-data-sets/{q}_nport.zip"
DIR = os.path.join(os.path.dirname(__file__), "..", "data", "nport")

csv.field_size_limit(sys.maxsize)


def _user_agent():
    ua = os.environ.get("SEC_USER_AGENT")
    if not ua:
        env = os.path.join(os.path.dirname(__file__), "..", ".env")
        for line in open(env) if os.path.exists(env) else []:
            k, _, v = line.strip().partition("=")
            if k == "SEC_USER_AGENT":
                ua = v
    if not ua:
        raise RuntimeError("Set SEC_USER_AGENT to 'name email' (SEC fair-access policy).")
    return ua


def download(quarter: str) -> str:
    path = os.path.join(DIR, f"{quarter}_nport.zip")
    if not os.path.exists(path):
        os.makedirs(DIR, exist_ok=True)
        req = urllib.request.Request(URL.format(q=quarter), headers={"User-Agent": _user_agent()})
        with urllib.request.urlopen(req, timeout=600) as r, open(path + ".part", "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        os.rename(path + ".part", path)
    return path


def _rows(z, name):
    with z.open(name) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", errors="replace"), delimiter="\t",
                                  quoting=csv.QUOTE_NONE)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def japan_holdings(zip_path: str) -> list[dict]:
    """Japanese common-stock positions from each fund series' latest report in the file."""
    with zipfile.ZipFile(zip_path) as z:
        subs = {r["ACCESSION_NUMBER"]: r for r in _rows(z, "SUBMISSION.tsv")}
        info = {r["ACCESSION_NUMBER"]: r for r in _rows(z, "FUND_REPORTED_INFO.tsv")}
        reg = {r["ACCESSION_NUMBER"]: r["REGISTRANT_NAME"] for r in _rows(z, "REGISTRANT.tsv")}
        # Latest report per series.
        latest = {}
        for acc, s in subs.items():
            series = (info.get(acc) or {}).get("SERIES_ID") or acc
            d = datetime.strptime(s["REPORT_DATE"], "%d-%b-%Y").date().isoformat()
            if series not in latest or d > latest[series][1]:
                latest[series] = (acc, d)
        keep = {acc: d for acc, d in latest.values()}
        holdings = {}
        for r in _rows(z, "FUND_REPORTED_HOLDING.tsv"):
            if r["INVESTMENT_COUNTRY"] != "JP" or r["ASSET_CAT"] != "EC" or r["ACCESSION_NUMBER"] not in keep:
                continue
            if r["PAYOFF_PROFILE"] and r["PAYOFF_PROFILE"] != "Long":
                continue
            value = _num(r["CURRENCY_VALUE"])
            acc = r["ACCESSION_NUMBER"]
            holdings[r["HOLDING_ID"]] = {
                "issuer_name": r["ISSUER_NAME"], "issuer_lei": r["ISSUER_LEI"] or None,
                "shares": _num(r["BALANCE"]) if r["UNIT"] == "NS" else None,
                # Checked on Vanguard Total International / Toyota (2026-07-31): CURRENCY_VALUE is the USD value
                # ($2.97bn for 157m shares) and PERCENTAGE is already a percent of net assets. EXCHANGE_RATE
                # conventions differ between filers, so no yen value is derived.
                "value_usd": value,
                "pct_of_fund": _num(r["PERCENTAGE"]),
                "fund": (info.get(acc) or {}).get("SERIES_NAME"), "series_id": (info.get(acc) or {}).get("SERIES_ID"),
                "registrant": reg.get(acc), "report_date": keep[acc], "accession": acc, "isin": None, "ticker_reported": None,
            }
        for r in _rows(z, "IDENTIFIERS.tsv"):
            h = holdings.get(r["HOLDING_ID"])
            if h:
                h["isin"] = h["isin"] or r["IDENTIFIER_ISIN"] or None
                h["ticker_reported"] = h["ticker_reported"] or r["IDENTIFIER_TICKER"] or None
    return list(holdings.values())


def isin_to_ticker(isins, known=None, save=None):
    """Map Japanese ISINs to Tokyo tickers with OpenFIGI. `save(mapping)` is called after every batch
    so an interrupted run resumes where it stopped."""
    known = dict(known or {})
    todo = sorted(i for i in set(isins) if i and i.startswith("JP") and i not in known)
    jobs = [{"idType": "ID_ISIN", "idValue": i, "exchCode": "JP"} for i in todo]
    batch = 100 if os.environ.get("OPENFIGI_API_KEY") else 10
    for start in range(0, len(jobs), batch):
        chunk = jobs[start:start + batch]
        for job, res in zip(chunk, figi.post_jobs(chunk)):
            data = res.get("data") or []
            known[job["idValue"]] = data[0]["ticker"] if data else None
        if save:
            save(known)
    return known


def summarize(holdings, isin_map, quarter):
    """One summary per ticker: number of funds, total shares and value, top 25 funds by value."""
    by_ticker = {}
    for h in holdings:
        t = isin_map.get(h["isin"]) if h["isin"] else None
        if not t:
            continue
        by_ticker.setdefault(t, []).append(h)
    out = {}
    for t, rows in by_ticker.items():
        rows.sort(key=lambda r: -(r["value_usd"] or 0))
        out[t] = {
            "ticker": t, "quarter": quarter,
            "fund_count": len(rows),
            "total_shares": sum(r["shares"] or 0 for r in rows),
            "total_value_usd": round(sum(r["value_usd"] or 0 for r in rows), 2),
            "latest_report_date": max(r["report_date"] for r in rows),
            "top_funds": [{k: r[k] for k in ("fund", "registrant", "series_id", "shares", "value_usd", "pct_of_fund", "report_date", "accession")}
                          for r in rows[:25]],
            "source": "SEC Form N-PORT data sets (U.S. Securities and Exchange Commission)",
        }
    return out


def export_json(summary):
    return {t: json.dumps(v, ensure_ascii=False) for t, v in summary.items()}
