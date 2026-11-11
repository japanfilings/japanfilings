"""EDINET API v2 client. Needs EDINET_API_KEY (free registration at the FSA)."""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.edinet-fsa.go.jp/api/v2"

# docTypeCode values for the 5% rule (large shareholding) family.
LARGE_HOLDING_DOC_TYPES = {"350": "initial_or_change_report", "360": "amendment"}

_ENV_FILE = os.path.join(os.path.dirname(__file__), "..", ".env")
if not os.environ.get("EDINET_API_KEY") and os.path.exists(_ENV_FILE):
    for line in open(_ENV_FILE):
        k, _, v = line.strip().partition("=")
        if k == "EDINET_API_KEY" and v:
            os.environ["EDINET_API_KEY"] = v

_MIN_INTERVAL_S = 1.0  # be polite to a public service
_last_call = 0.0


def _get(path, params, raw=False):
    global _last_call
    key = os.environ.get("EDINET_API_KEY")
    if not key:
        raise RuntimeError("EDINET_API_KEY is not set. Register for a free key at https://api.edinet-fsa.go.jp/")
    wait = _MIN_INTERVAL_S - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    url = f"{API}/{path}?{urllib.parse.urlencode({**params, 'Subscription-Key': key})}"
    for attempt in range(6):  # ride out short network outages (DNS, TLS timeouts)
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                body = r.read()
            break
        except urllib.error.HTTPError as e:  # a subclass of URLError: decide before the generic retry
            if e.code < 500 and e.code != 429:
                raise RuntimeError(f"EDINET HTTP {e.code} for {path}") from None
            if attempt == 5:
                raise
            time.sleep(30 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == 5:
                raise
            time.sleep(30 * (attempt + 1))
    _last_call = time.monotonic()
    if raw:
        return body
    data = json.loads(body)
    status = data.get("metadata", {}).get("status") or data.get("StatusCode")
    if str(status) not in ("200", "None"):
        raise RuntimeError(f"EDINET error: {data}")
    return data


def documents(day: str) -> list[dict]:
    """All filings submitted on `day` (YYYY-MM-DD), with metadata."""
    return _get("documents.json", {"date": day, "type": 2}).get("results", [])


def large_holding_documents(day: str) -> list[dict]:
    return [d for d in documents(day)
            if d.get("docTypeCode") in LARGE_HOLDING_DOC_TYPES and d.get("withdrawalStatus") == "0"]


def _expect(body: bytes, magic: bytes, doc_id: str) -> bytes:
    """EDINET reports some errors as JSON with HTTP 200; never treat that as the document."""
    if not body.startswith(magic):
        raise RuntimeError(f"EDINET returned no document for {doc_id}: {body[:200]!r}")
    return body


def download_pdf(doc_id: str) -> bytes:
    return _expect(_get(f"documents/{doc_id}", {"type": 2}, raw=True), b"%PDF", doc_id)


CSV_CACHE = os.path.join(os.path.dirname(__file__), "..", "data", "csv_cache")


def download_csv_zip(doc_id: str) -> bytes:
    """XBRL converted to CSV (only when the filing's csvFlag is '1'). Cached on disk so
    re-parsing never downloads the same filing twice."""
    path = os.path.join(CSV_CACHE, f"{doc_id}.zip")
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    body = _expect(_get(f"documents/{doc_id}", {"type": 5}, raw=True), b"PK", doc_id)
    os.makedirs(CSV_CACHE, exist_ok=True)
    with open(path, "wb") as f:
        f.write(body)
    return body
