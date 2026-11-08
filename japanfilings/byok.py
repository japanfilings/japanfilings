"""Bring-your-own-key prices: runs on the user's machine with the user's J-Quants API key.

We never receive or redistribute price data; this code only calls J-Quants on the user's behalf
from their own computer and combines the result with filing data.

J-Quants v2: GET https://api.jquants.com/v2/equities/bars/daily?code=72030&from=...&to=...,
header x-api-key. The response field names are not verified against a live key yet, so the
reader below accepts both the v2 short names and the v1 long names.
"""
import json
import os
import urllib.parse
import urllib.request

JQUANTS = "https://api.jquants.com/v2/equities/bars/daily"
_CLOSE_KEYS = ("AdjC", "C", "AdjustmentClose", "Close")


def daily_bars(ticker: str, date_from: str, date_to: str) -> list[dict]:
    key = os.environ.get("JQUANTS_API_KEY")
    if not key:
        raise RuntimeError("Set JQUANTS_API_KEY (your own key from jpx-jquants.com, free plan available).")
    code = ticker.upper() + "0" if len(ticker) == 4 else ticker.upper()  # J-Quants uses 5-character codes
    q = urllib.parse.urlencode({"code": code, "from": date_from.replace("-", ""), "to": date_to.replace("-", "")})
    req = urllib.request.Request(f"{JQUANTS}?{q}", headers={"x-api-key": key})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = json.loads(r.read())
    return body.get("data") or body.get("daily_quotes") or []


def close_of(bar: dict):
    return next((bar[k] for k in _CLOSE_KEYS if bar.get(k) is not None), None)


def valuation(price: float, report: dict) -> dict:
    """Market cap, P/E and P/B from a price and the latest annual report's figures."""
    km = (report.get("key_metrics") or [{}])[0]
    company = report.get("company") or {}
    shares = company.get("issued_shares") or km.get("shares_issued")
    treasury = company.get("treasury_shares") or 0
    out = {"price": price, "fiscal_year_end": report.get("fiscal_year_end"), "eps": km.get("eps"), "bps": km.get("bps")}
    out["market_cap_jpy"] = round(price * (shares - treasury)) if shares else None
    out["per"] = round(price / km["eps"], 2) if km.get("eps") and km["eps"] > 0 else None
    out["pbr"] = round(price / km["bps"], 2) if km.get("bps") and km["bps"] > 0 else None
    out["note"] = "Computed locally from your own price data and the latest annual report; trailing, not forward."
    return out
