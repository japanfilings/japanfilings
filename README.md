# JapanStake

Japanese listed-company data from official filings, in English, for AI agents (MCP) and developers.

Status: **prototype, tested locally.** Not deployed, not for sale yet.

## What works today

| Endpoint / MCP tool | Data | Source |
|---|---|---|
| `GET /v1/companies/search`, `search_companies` | 3,813 listed issuers, English names for 3,409 | EDINET code list (no key) |
| `GET /v1/companies/{ticker}`, `get_company` | English name, corporate number, industry, FIGI, `7203.T` / `7203 JP` symbols | EDINET code list, OpenFIGI |
| `GET /v1/companies/{ticker}/financials`, `get_financials` | 5-year key metrics; current/prior income statement, balance sheet, cash flow (Japan GAAP, IFRS) | Annual securities reports (XBRL) |
| `GET /v1/companies/{ticker}/details`, `get_company_details` | Officer titles (no names), workforce and salary, top shareholders, shareholder mix, auditor and fees | Annual securities reports (XBRL) |
| `GET /v1/large-holdings`, `get_large_holdings` | 5% rule filings: total stake (XBRL), change, purpose tags, each joint holder with purpose, activism flag and borrowings | EDINET API (XBRL + PDF) |
| `GET /v1/investors/{code}`, `get_investor` | An investor's latest stakes and joint holders (Pro) | EDINET API |
| `GET /v1/events`, `get_ownership_events` | Ownership-change events with one-line English summaries: new stake, increase, decrease, exit below 5%, crossing 10/20/33.4/50%, activist turn (negated "no proposals planned" wording excluded), purpose change, amendment. Free: last 7 days | Derived from 5% filings |
| `POST/GET/DELETE /v1/webhooks`, `POST /v1/webhooks/{id}/test` | Events pushed to the subscriber's HTTPS URL by the 5-minute cron, filtered by type and ticker, HMAC-signed, retried, replayable (Pro) | — |
| `GET /v1/companies/{ticker}/cross-holdings`, `get_cross_holdings` | Strategic shareholdings held (shares and book value vs. prior year, purpose, held back), listed companies holding it, mutual pairs confirmed by both reports, shares sold/bought in the year (Pro) | Annual securities reports (XBRL) |
| `GET /v1/cross-holdings/unwinding`, `get_cross_holding_unwinding` | Companies ranked by listed strategic shares sold in their latest year (Pro) | Annual securities reports (XBRL) |
| `GET /v1/companies/{ticker}/trades`, `GET /v1/investors/{code}/trades`, `get_disclosed_trades` | Every row of the 60-day trade tables in 5% reports: date, shares, % of outstanding, on/off market, buy/sell, unit price; per-holder net shares and average disclosed price; latest price stated in a filing. Lending, transfers and exercises flagged; overlapping windows kept once (Pro) | 5% reports (XBRL text block) |
| `GET /v1/companies/{ticker}/indicators`, `get_indicators` | Per-metric financial percentiles with formulas (`financial_percentiles`, `mean_percentile` as arithmetic only), policy-uncertainty counts by area, `results_wording_balance`, `ownership_activity` counts | Annual reports, 5% filings, cross-holdings |
| `GET /v1/indicators/sorted`, `sort_companies_by_metric` | Companies in the order of one figure or percentile, optional sector filter; no judgement added (Pro) | — |
| `GET /v1/indicators/policy-uncertainty`, `get_policy_uncertainty_index` | Japan corporate policy-uncertainty index by filing month, share of reports per policy area | Annual reports |
| `GET /v1/companies/{ticker}/earnings-releases`, `GET /v1/earnings-releases`, `get_earnings_releases` | Half-year results from EDINET half-year reports: period results with YoY %, link to the filing | EDINET half-year reports |
| `GET /v1/companies/{ticker}/brief`, `get_company_brief` | One company on one page: latest results, indicators, ownership changes, 50 bp interest arithmetic, each dated | All of the above |
| `GET /v1/companies/{ticker}/credit-ratings`, `GET /v1/credit-ratings`, `get_disclosed_credit_ratings` | Credit ratings as issuers state them in bond filings: agency, rating, date, bond, source. We do not rate anything, and agencies' websites are not read | EDINET bond supplements |
| `GET /v1/filings` | Metadata of every EDINET filing | EDINET API |
| `GET /v1/companies/{ticker}/us-funds`, `get_us_fund_holdings` | U.S. mutual funds/ETFs holding the stock: fund count, total shares and USD value, top 25 funds | SEC Form N-PORT data sets, ISIN→ticker via OpenFIGI |
| local MCP `get_prices`, `get_valuation` | Daily prices, market cap, P/E, P/B from the user's own J-Quants key; prices never reach our server | J-Quants (user's key) |

Website (same Worker, server-rendered, no client JavaScript): `/` landing with live ownership changes, `/events`, `/indicators` (monthly policy-uncertainty index and rankings), `/cross-holdings` (unwinding ranking), `/companies/{ticker}` (financial charts, ownership changes with 5%-rule threshold meters, cross-holdings, shareholders, U.S. funds), `/investors/{code}`, `/search`, `/docs`, `/pricing`, `/sources`, `/terms`, `/privacy`, email-link sign-in, `/dashboard` (key, usage, webhooks). Also `/sitemap.xml` (every company page), `/robots.txt`, `/favicon.svg`, security headers (CSP without scripts, HSTS, frame-ancestors none) and CORS on `/v1/*`.
| `GET /v1/status` | Data freshness (no key) | — |

Plans (all paid, every feature on both): Starter $9 with 20,000 requests a month and 60 a minute; Pro $39 with 300,000 and 600 a minute. An account without a plan gets HTTP 402. Keys are stored as SHA-256 hashes only.

Every record keeps the Japanese original (`*_ja`), a `source_url` and an attribution line. Factual data, not investment advice. No stock prices are distributed. Personal data is left out: no officer birthdates, no shareholder or holder addresses, phone numbers or birthdates.

Annual reports (filed 2026-06-19 to 10-29): 4,478 reports for 3,782 of 3,812 listed companies; revenue found for 4,457 (the rest are mostly insurers and pre-revenue drug developers). Cross-shareholdings: 35,905 named holdings, 33,280 (92.7%) matched to a ticker, 15,034 mutual holdings confirmed by both companies' reports.

Measured on real data (October 2026, 1,275 large-holding filings): XBRL holding ratio found for 1,275/1,275; PDF and XBRL agree on 1,045/1,049 where both exist, and the 4 differences are amended filings where XBRL is correct.

## Layout

```
japanfilings/        Python ingestion (EDINET, OpenFIGI) into SQLite; local stdio MCP server
worker/              Cloudflare Worker: REST API + remote MCP (Streamable HTTP) over D1
.github/workflows/   ingest.yml: every 10 min in JST business hours -> D1
```

## Local run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m japanfilings.cli codelist          # issuer universe, no key
.venv/bin/python -m japanfilings.cli figi              # FIGIs, no key (~16 min anonymously)
export EDINET_API_KEY=...                              # free; documents are fetched only via the API
.venv/bin/python -m japanfilings.cli range 2026-10-01 2026-10-29     # 5% filings
.venv/bin/python -m japanfilings.cli annual 2025-11-01 2026-10-29    # annual reports
.venv/bin/python -m japanfilings.cli nport 2026q3                     # U.S. fund holdings (needs SEC_USER_AGENT in .env)
.venv/bin/python -m japanfilings.cli report            # parse quality, PDF vs XBRL agreement

.venv/bin/python -m japanfilings.export_d1 data/d1_load.sql
.venv/bin/python -m japanfilings.export_d1 --new-key pro   # prints a key once + its SQL
cd worker && npm install && npm run db:local
npx wrangler d1 execute japanfilings --local --file ../data/d1_load.sql
echo DEV_SHOW_LINKS=1 > .dev.vars                      # local only: show sign-in links instead of emailing
npm run dev                                            # http://localhost:8788
FREE_KEY=... PRO_KEY=... BASE=http://localhost:8788 npm test
```

Claude Code against the remote MCP:

```bash
claude mcp add --transport http japanstake https://<your-worker>/mcp --header "Authorization: Bearer <key>"
```

## Deploy (owner steps, all free tiers)

1. EDINET API key (free) -> `EDINET_API_KEY`.
2. Cloudflare account -> `npx wrangler d1 create japanfilings`, put the ID in `worker/wrangler.toml`, `npx wrangler d1 migrations apply japanfilings --remote`, `npm run deploy`.
3. GitHub public repo -> the six secrets listed in `docs/DEPLOY.md` (including `LEXICON_JSON`). Full steps: `docs/DEPLOY.md`.
4. Sign-in email: Resend (free tier) -> `npx wrangler secret put RESEND_API_KEY`, and set `MAIL_FROM` in `wrangler.toml` `[vars]`.

## Before launch (owner)

Step-by-step commands, costs, failure runbook and update schedule: **[docs/DEPLOY.md](docs/DEPLOY.md)** (Japanese).

- Cloudflare: D1 database, R2 bucket `japanfilings-backups` with lifecycle rules, `wrangler deploy`. The first load is about 183,000 rows, 259 MB of SQL and an estimated 755,000 rows written (measured 2026-11-01 on a copy; collection was still running). That is 8 to 10 days on the free plan's 100,000 writes a day, or one run on Workers Paid ($5/month).
- Secrets for the Worker: `RESEND_API_KEY` and `MAIL_FROM` (sign-in email), `CONTACT_EMAIL` (where contact-form requests are forwarded; never shown on the site), `EDINET_API_KEY`. Do not set `DEV_SHOW_LINKS` in production.
- GitHub repository (public recommended: free Actions minutes). Secrets: `EDINET_API_KEY`, `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `SEC_USER_AGENT`, `BACKUP_PASSPHRASE`, `LEXICON_JSON`. Variables: `STATUS_URL`, `D1_DAILY_WRITE_CAP`.
- A domain routed to the Worker (`*.workers.dev` works for a trial; the live site turns it off so the zone rate limit covers every request).
- Billing: a merchant of record (Paddle or Lemon Squeezy) and a webhook that sets `users.plan` and `api_keys.plan`.

## Known limits

- Large-holding ratios come from the XBRL CSV (PDF text as fallback); text fields (names, purpose) come from the PDF. `report` shows coverage and PDF-vs-XBRL agreement.
- Three newly listed issuers have no ticker in the EDINET code list; tickers are learned from filings and kept across refreshes.
- No stock prices (licence needed: J-Quants forbids redistribution and relaying through our server; external distribution is J-Quants Pro, priced on request). Prices stated in 5% reports are served instead; they cover 369 companies and mostly off-market trades.
- Only public filing systems are read (EDINET, SEC, OpenFIGI). Company websites, TDnet and IR hosting services are not read.
- Text indicators use our own Japanese word lists; they are descriptive. Kanpo is not crawled (`kanpo.go.jp` robots.txt); gBizINFO is the planned route.
- Billing is built but closed and untested against Paddle; no plan can be bought until it opens (local test accounts start on Starter via `DEV_SHOW_LINKS=1`). Lemon Squeezy is not implemented (its webhook signature has no timestamp). A subscriber who wants to change plan is sent to Paddle's portal.
- Annual reports cover filings from 2026-06-19 onward; companies with other fiscal years appear as their reports are filed (the ingest workflow now picks up annual reports every run).
- N-PORT: only each fund's latest report in the quarter; values are USD as filed (no yen conversion, since exchange-rate conventions differ between filers). New quarter files appear about two months after quarter end.
- `get_prices` reads J-Quants v2 field names defensively; not yet run against a live key.
- Holder and shareholder names of individuals are shown as filed (Japanese).
- Cross-shareholding names are matched to tickers by Japanese name; companies renamed since the report (e.g. 第一生命ホールディングス -> 第一ライフグループ) and delisted ones stay unmatched. Share changes include stock splits. Three filers tag thousands of yen as yen; their summaries carry `data_warning` and are left out of the ranking.
- Cross-shareholding tables are replaced only by the weekly full export (to stay inside D1's daily write allowance); events go out with every incremental export.

Rate example: debt booked only under a filer's own tags (for example Honda and NTT financing liabilities) is not in the standard statement fields, so such companies have no example or an understated one; leases are left out.

## Personal data and legal switches

- All masking lives in `worker/src/privacy.ts` and fails closed: a filer's name is shown only if it is not excluded and is a known or recognisable organisation. `worker/test/masking.test.mjs` walks every OpenAPI route, CSV, MCP tool, page and webhook with planted names.
- `/contact` is a web form (no email address is published). Requests are stored in D1 `contact_messages` for a year and forwarded to `CONTACT_EMAIL` when Resend is set.
- `/legal` stays 404 until `LEGAL_PAGE_ENABLED=true` and `LEGAL_PROVIDER_NAME`, `LEGAL_START_OF_SERVICE`, `LEGAL_CANCELLATION`, `LEGAL_REFUNDS` and `CONTACT_EMAIL` are all set.
- Billing stays off until every billing setting is present.
