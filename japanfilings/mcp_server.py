"""English MCP server (stdio) that runs on the user's machine.

Filing data comes from the hosted API when JAPANFILINGS_API_URL and JAPANFILINGS_API_KEY are set,
otherwise from the local store. Prices come only from the user's own J-Quants key (JQUANTS_API_KEY).

Claude Code:  claude mcp add japanfilings -- /path/.venv/bin/python -m japanfilings.mcp_server
"""
import json
import os
import urllib.request
from datetime import date, timedelta

from mcp.server.mcpserver import MCPServer

from . import byok, store

server = MCPServer(
    name="japanfilings",
    instructions=(
        "Japanese listed-company data from official filings (EDINET, Financial Services Agency of Japan). "
        "Company names are given in English when the filer registered one; Japanese originals are kept "
        "in *_ja fields. Every record has a source_url to the original filing. Prices come from the user's own "
        "J-Quants key. This is factual data, not investment advice."
    ),
)


def _con():
    return store.connect()


def _remote(path):
    url, key = os.environ.get("JAPANFILINGS_API_URL"), os.environ.get("JAPANFILINGS_API_KEY")
    if not (url and key):
        return None
    req = urllib.request.Request(url.rstrip("/") + path, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def _annual_report(ticker):
    if os.environ.get("JAPANFILINGS_API_URL"):
        fin = _remote(f"/v1/companies/{ticker}/financials")
        det = _remote(f"/v1/companies/{ticker}/details")
        return {**fin, "company": det} if fin else None
    return store.latest_annual_report(_con(), ticker)


@server.tool()
def search_companies(query: str, limit: int = 20) -> list[dict]:
    """Find listed Japanese companies by ticker (e.g. '7203', '648A') or by English/Japanese name."""
    return store.search_issuers(_con(), query, limit=limit)


@server.tool()
def get_company(ticker: str) -> dict:
    """Company profile by Tokyo ticker: English name, corporate number, industry, FIGI, Yahoo/Bloomberg symbols."""
    return store.get_issuer(_con(), ticker) or {"error": f"no listed company with ticker {ticker}"}


@server.tool()
def get_financials(ticker: str) -> dict:
    """Five years of headline figures and current/prior financial statements from the latest annual report."""
    r = _annual_report(ticker)
    return {k: r[k] for k in ("ticker", "fiscal_year_end", "accounting_standard", "key_metrics", "statements") if k in r} if r else {
        "error": f"no annual report for {ticker}"}


@server.tool()
def get_large_holdings(ticker: str = "", filer_edinet_code: str = "", since: str = "", limit: int = 50) -> list[dict]:
    """Large-shareholding (5% rule) filings: who holds how much, change vs. previous report, stated purpose
    (activist proposals, pure investment, ...), joint holders. Filter by target ticker, by filer EDINET code
    (investor reverse lookup), and by submit date (YYYY-MM-DD)."""
    return store.large_holdings(_con(), ticker or None, filer_edinet_code or None, since or None, limit)


@server.tool()
def get_us_fund_holdings(ticker: str) -> dict:
    """U.S. mutual funds and ETFs holding a Tokyo-listed stock, from the latest SEC N-PORT quarter."""
    if os.environ.get("JAPANFILINGS_API_URL"):
        return _remote(f"/v1/companies/{ticker}/us-funds")
    return store.us_fund_holdings(_con(), ticker) or {"error": f"no U.S. fund holdings for {ticker}"}


@server.tool()
def get_prices(ticker: str, date_from: str = "", date_to: str = "") -> list[dict]:
    """Daily prices from the user's own J-Quants account (JQUANTS_API_KEY). Dates YYYY-MM-DD; default last 30 days.
    The free J-Quants plan is delayed by about 12 weeks."""
    to = date_to or date.today().isoformat()
    frm = date_from or (date.fromisoformat(to) - timedelta(days=30)).isoformat()
    return byok.daily_bars(ticker, frm, to)


@server.tool()
def get_valuation(ticker: str) -> dict:
    """Market cap, trailing P/E and P/B: latest close from the user's own J-Quants key combined with the
    latest annual report."""
    report = _annual_report(ticker)
    if not report:
        return {"error": f"no annual report for {ticker}"}
    bars = byok.daily_bars(ticker, (date.today() - timedelta(days=120)).isoformat(), date.today().isoformat())
    closes = [(b.get("Date"), byok.close_of(b)) for b in bars if byok.close_of(b) is not None]
    if not closes:
        return {"error": "no price returned by J-Quants for this ticker and window"}
    as_of, price = closes[-1]
    return {"ticker": ticker.upper(), "price_date": as_of, **byok.valuation(price, report)}


if __name__ == "__main__":
    server.run()
