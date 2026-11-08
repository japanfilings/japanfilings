"""OpenFIGI mapping for Tokyo tickers. FIGIs are free to use and redistribute.

Anonymous access: 25 requests/min, 10 jobs/request. Set OPENFIGI_API_KEY for higher limits.
"""
import json
import os
import time
import urllib.error
import urllib.request

URL = "https://api.openfigi.com/v3/mapping"


def _post(jobs, attempts=8):
    headers = {"Content-Type": "application/json"}
    if os.environ.get("OPENFIGI_API_KEY"):
        headers["X-OPENFIGI-APIKEY"] = os.environ["OPENFIGI_API_KEY"]
    req = urllib.request.Request(URL, data=json.dumps(jobs).encode(), headers=headers)
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:  # network blips and 429s: back off and retry
            if i == attempts - 1:
                raise
            time.sleep(30 * (i + 1) if getattr(e, "code", None) != 429 else 60)


def _interval():
    return 0.25 if os.environ.get("OPENFIGI_API_KEY") else 2.5


def post_jobs(jobs):
    """One mapping request, paced to stay under the anonymous rate limit."""
    res = _post(jobs)
    time.sleep(_interval())
    return res


def map_tickers(tickers):
    """Yield (ticker, record or None) using the Japan composite exchange code 'JP'."""
    batch = 100 if os.environ.get("OPENFIGI_API_KEY") else 10
    for i in range(0, len(tickers), batch):
        chunk = tickers[i:i + batch]
        res = post_jobs([{"idType": "TICKER", "idValue": t, "exchCode": "JP"} for t in chunk])
        for t, r in zip(chunk, res):
            hits = [d for d in r.get("data", []) if d.get("securityType") in ("Common Stock", "REIT", "ETP", "Preference")] or r.get("data", [])
            yield t, (hits[0] if hits else None)
