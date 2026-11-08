"""Company indicators built from what we already hold, one row per ticker, plus a monthly policy-uncertainty index.

  policy uncertainty      counts in the text of the latest annual report (see textindex.py)
  financial percentiles   for each of ROE, ordinary margin, equity ratio, revenue growth, net income growth and operating
                          cash flow / total assets: the filed or computed value and its percentile across listed companies.
                          mean_percentile is plain arithmetic over them, with no grade or label. Banks and insurers sit low
                          on equity ratio by the nature of their balance sheets.
  ownership activity      counts from 5% filings and the share held by other listed companies. No score or weighting.
"""
import json
from bisect import bisect_left, bisect_right
from datetime import date, timedelta

from .textutil import nfkc

SCHEMA = """
CREATE TABLE IF NOT EXISTS company_indicators (
  ticker TEXT PRIMARY KEY,
  fiscal_year_end TEXT,
  doc_id TEXT,
  policy_uncertainty REAL,
  record TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_uncertainty_monthly (
  month TEXT PRIMARY KEY,
  record TEXT NOT NULL
);
"""

METRIC_FORMULAS = {
    "roe": "Return on equity as stated in the annual report, %.",
    "ordinary_margin": "Ordinary income (or profit before tax) / revenue.",
    "equity_ratio": "Equity ratio as stated in the annual report, %.",
    "revenue_growth": "Revenue / prior-year revenue - 1.",
    "net_income_growth": "(Net income - prior-year net income) / |prior-year net income|.",
    "ocf_to_assets": "Operating cash flow / total assets.",
}
PERCENTILE_FORMULA = "Percent of listed companies with a filed value at or below this one, ties counted half."
MEAN_FORMULA = "Arithmetic mean of the available metric percentiles; null when fewer than three are available. It is not a rating or an evaluation."
ACTIVITY_NOTE = "Counts from 5% filings and annual reports, as filed. No score or weighting is applied."
WORDING_NOTE = ("(positive - negative results words) / total in the MD&A, from our own short Japanese word lists. It counts words only: "
                "it does not read context or negation and says nothing about results or prospects.")
STRENGTH_PARTS = ["roe", "ordinary_margin", "equity_ratio", "revenue_growth", "net_income_growth", "ocf_to_assets"]
RATE_SHOCK_BP = 50
RATE_LENS_NOTE = (f"Arithmetic example: reported interest-bearing debt x {RATE_SHOCK_BP} basis points for one year. It is not a forecast. Fixed-rate debt, maturities, "
                  "hedges and lease liabilities are not taken into account, and interest income on cash is left out. Debt of financing subsidiaries is included where the filer consolidates it. One year of operating cash flow can include one-off items.")
# Sum of these balance-sheet lines. An IFRS "...Liabilities" total stands in only when the filer has no current/non-current split.
DEBT_LINES = ["ShortTermLoansPayable", "CurrentPortionOfLongTermLoansPayable", "LongTermLoansPayable", "ShortTermBondsPayable", "BondsPayable", "CurrentPortionOfBonds",
              "CommercialPapersLiabilities", "ConvertibleBondTypeBondsWithSubscriptionRightsToShares", "BondsWithSubscriptionRightsToSharesNCL", "ConvertibleBonds",
              "BondsAndBorrowingsCLIFRS", "BondsAndBorrowingsNCLIFRS", "BorrowingsCLIFRS", "BorrowingsNCLIFRS", "InterestBearingLiabilitiesCLIFRS", "InterestBearingLiabilitiesNCLIFRS",
              "BondsPayableCLIFRS", "BondsPayableNCLIFRS", "CurrentPortionOfLongTermBorrowingsCLIFRS", "LongTermDebtNCLIFRS"]
DEBT_TOTALS = ["BondsAndBorrowingsLiabilitiesIFRS", "BorrowingsLiabilitiesIFRS", "BondsPayableLiabilitiesIFRS", "InterestBearingLiabilitiesLiabilitiesIFRS"]
INTEREST_LINES = ["InterestExpensesNOE", "InterestExpensesIFRS", "FinancialLiabilitiesMeasuredAtAmortizedCostInterestExpensesIFRS"]
FINANCIAL_SECTORS = {"銀行業", "保険業", "証券、商品先物取引業", "その他金融業"}  # debt is their raw material, so the example does not apply
AREAS = ["trade", "fiscal_tax", "monetary", "regulation", "politics"]


def _ratio(a, b):
    return a / b if a is not None and b not in (None, 0) else None


def _fin_parts(km):
    m0 = km[0] if km else {}
    m1 = km[1] if len(km) > 1 else {}
    ni1 = m1.get("net_income")
    return {
        "roe": m0.get("roe"),
        "ordinary_margin": _ratio(m0.get("ordinary_income") or m0.get("profit_before_tax"), m0.get("revenue")),
        "equity_ratio": m0.get("equity_ratio"),
        "revenue_growth": (_ratio(m0.get("revenue"), m1.get("revenue")) or 0) - 1 if m0.get("revenue") and m1.get("revenue") else None,
        "net_income_growth": _ratio((m0.get("net_income") or 0) - ni1, abs(ni1)) if m0.get("net_income") is not None and ni1 else None,
        "ocf_to_assets": _ratio(m0.get("operating_cf"), m0.get("total_assets")),
    }


def rate_lens(rec, industry):
    """Extra yearly interest if every yen of reported debt repriced by RATE_SHOCK_BP; None where it does not apply."""
    if industry in FINANCIAL_SECTORS:
        return None
    st = rec.get("statements") or {}
    bs = (st.get("balance_sheet") or {}).get("current") or {}
    lines = {k: bs[k] for k in DEBT_LINES if isinstance(bs.get(k), (int, float))}
    if not lines:
        lines = {k: bs[k] for k in DEBT_TOTALS if isinstance(bs.get(k), (int, float))}
    if not lines:
        return None
    debt = sum(lines.values())
    extra = debt * RATE_SHOCK_BP / 10000
    km = (rec.get("key_metrics") or [{}])[0]
    ocf, cash = km.get("operating_cf"), km.get("cash")
    pl = (st.get("income_statement") or {}).get("current") or {}
    interest = next((pl[k] for k in INTEREST_LINES if isinstance(pl.get(k), (int, float))), None)
    implied = 100 * interest / debt if interest and debt > 0 else None
    return {
        # A rate far outside any loan market means the lines we matched miss part of the debt, or interest covers more than they do.
        "data_warning": "Reported interest does not fit the debt lines found; debt may be incomplete." if implied is not None and not 0.1 <= implied <= 15 else None,
        "shock_bp": RATE_SHOCK_BP, "fiscal_year_end": rec.get("fiscal_year_end"), "interest_bearing_debt_jpy": debt, "debt_lines": lines, "cash_jpy": cash,
        "extra_annual_interest_jpy": extra, "reported_interest_expense_jpy": interest,
        "extra_interest_pct_of_operating_cf": round(100 * extra / ocf, 2) if ocf and ocf > 0 else None,
        "implied_interest_rate_pct": round(implied, 2) if implied is not None else None,
        "note": RATE_LENS_NOTE,
    }


def _pctile(sorted_vals, v):
    """Share of companies at or below v, ties counted half."""
    lo, hi = bisect_left(sorted_vals, v), bisect_right(sorted_vals, v)
    return (lo + hi) / 2 / len(sorted_vals)


def ownership_activity_counts(con, today=None):
    """The three ownership_events counts and the cross-holding shares behind `ownership_activity`."""
    today = today or date.today()
    ago = lambda days: (today - timedelta(days=days)).isoformat()
    proposals = dict(con.execute("SELECT issuer_ticker, COUNT(DISTINCT filer_edinet_code) FROM ownership_events WHERE type = 'activist_purpose' AND submit_date >= ? GROUP BY 1", (ago(365),)).fetchall())
    new_holders = dict(con.execute("SELECT issuer_ticker, COUNT(DISTINCT filer_edinet_code) FROM ownership_events WHERE type = 'new_stake' AND submit_date >= ? GROUP BY 1", (ago(180),)).fetchall())
    net_change = dict(con.execute(
        "SELECT issuer_ticker, SUM(json_extract(record, '$.change_pt')) FROM ownership_events "
        "WHERE type IN ('stake_increase', 'stake_decrease', 'exit_below_5pct') AND submit_date >= ? GROUP BY 1", (ago(90),)).fetchall())
    return proposals, new_holders, net_change


def refresh_ownership_activity(con, today=None, chunk=200):
    """Recompute only `ownership_activity` in company_indicators from the current ownership_events, in short
    transactions of `chunk` rows.  The full `rebuild` also does this but holds one write transaction throughout.
    Returns the number of companies whose figures changed."""
    proposals, new_holders, net_change = ownership_activity_counts(con, today)
    changed, batch = 0, []
    for t, rec in con.execute("SELECT ticker, record FROM company_indicators").fetchall():
        record = json.loads(rec)
        act = record.get("ownership_activity") or {}
        new = {"filers_stating_may_make_proposals_365d": proposals.get(t) or 0, "new_5pct_holders_180d": new_holders.get(t) or 0,
               "net_stake_change_pt_90d": round(net_change.get(t) or 0.0, 2)}
        if any(act.get(k) != v for k, v in new.items()):
            record["ownership_activity"] = {**act, **new}
            batch.append((json.dumps(record, ensure_ascii=False), t, rec))
            changed += 1
        if len(batch) >= chunk:
            with con:
                con.executemany("UPDATE company_indicators SET record = ? WHERE ticker = ? AND record = ?", batch)
            batch = []
    if batch:
        with con:
            con.executemany("UPDATE company_indicators SET record = ? WHERE ticker = ? AND record = ?", batch)
    return changed


def rebuild(con, today=None):
    con.executescript(SCHEMA)
    today = today or date.today()
    latest = con.execute(
        "SELECT a.ticker, a.doc_id, a.submit_date, a.record, i.industry_ja FROM annual_reports a "
        "LEFT JOIN issuers i ON i.ticker = a.ticker AND i.listed = 1 WHERE a.ticker IS NOT NULL AND a.rowid = ("
        "SELECT b.rowid FROM annual_reports b WHERE b.ticker = a.ticker ORDER BY b.fiscal_year_end DESC, b.submit_date DESC LIMIT 1)").fetchall()
    recs = {r["ticker"]: (r, json.loads(r["record"])) for r in latest}
    parts = {t: _fin_parts(rec.get("key_metrics") or []) for t, (_, rec) in recs.items()}
    dist = {p: sorted(v[p] for v in parts.values() if v[p] is not None) for p in STRENGTH_PARTS}
    industries = {}
    for t, (row, _) in recs.items():
        industries.setdefault(row["industry_ja"], []).append(t)

    def ago(days):
        return (today - timedelta(days=days)).isoformat()

    proposals, new_holders, net_change = ownership_activity_counts(con, today)
    cross_shares = dict(con.execute("SELECT held_ticker, SUM(shares) FROM cross_holdings WHERE held_ticker IS NOT NULL GROUP BY 1").fetchall())

    pu_dist = sorted(((r.get("text_indicators") or {}).get("policy_uncertainty_per_100")) for _, r in recs.values()
                     if (r.get("text_indicators") or {}).get("policy_uncertainty_per_100") is not None)
    # An older store has score columns; the table is derived, so it is rebuilt with the current shape.
    if "strength_score" in [r[1] for r in con.execute("PRAGMA table_info(company_indicators)")]:
        con.execute("DROP TABLE company_indicators")
        con.executescript(SCHEMA)
    con.execute("DELETE FROM company_indicators")
    for t, (row, rec) in recs.items():
        p = parts[t]
        ranks = {k: round(_pctile(dist[k], v), 3) for k, v in p.items() if v is not None and dist[k]}
        mean_pct = round(100 * sum(ranks.values()) / len(ranks), 1) if len(ranks) >= 3 else None
        peers = [parts[x] for x in industries.get(row["industry_ja"], []) if row["industry_ja"]]
        industry_rank = None
        if mean_pct is not None and len(peers) >= 10:
            peer_scores = []
            for q in peers:
                r = [_pctile(dist[k], v) for k, v in q.items() if v is not None and dist[k]]
                if len(r) >= 3:
                    peer_scores.append(sum(r) / len(r))
            peer_scores.sort()
            industry_rank = round(100 * _pctile(peer_scores, mean_pct / 100), 1)

        km = (rec.get("key_metrics") or [{}])[0]
        issued = km.get("shares_issued") or (rec.get("company") or {}).get("issued_shares")
        cross_pct = round(100 * cross_shares[t] / issued, 2) if cross_shares.get(t) and issued else 0.0
        accum = net_change.get(t) or 0.0
        ti = rec.get("text_indicators") or {}
        record = {
            "ticker": t, "name_en_filing": nfkc((rec.get("company") or {}).get("english_name") or "") or None,
            "fiscal_year_end": rec.get("fiscal_year_end"), "source_doc_id": rec.get("doc_id"),
            "sector_ja": row["industry_ja"],
            "financial_percentiles": {
                "metrics": {k: {"value": round(v, 4) if isinstance(v, float) else v, "percentile": round(100 * ranks[k], 1) if k in ranks else None,
                                "formula": METRIC_FORMULAS[k]} for k, v in p.items()},
                "mean_percentile": mean_pct, "mean_percentile_within_sector": industry_rank,
                "percentile_formula": PERCENTILE_FORMULA, "mean_formula": MEAN_FORMULA,
            },
            "policy_uncertainty": {k: ti.get(k) for k in ("policy_uncertainty_per_100", "policy_uncertainty_mentions", "policy_uncertainty_by_area",
                                                          "uncertainty_per_100", "sentences", "examples")}
            | {"percentile": round(100 * _pctile(pu_dist, ti["policy_uncertainty_per_100"]), 1)} if ti.get("sentences") else None,
            "results_wording_balance": {"balance": ti.get("results_tone"), "words": ti.get("results_tone_words"), "note": WORDING_NOTE} if ti else None,
            "rate_lens": rate_lens(rec, row["industry_ja"]),
            "ownership_activity": {
                "filers_stating_may_make_proposals_365d": proposals.get(t) or 0, "new_5pct_holders_180d": new_holders.get(t) or 0,
                "net_stake_change_pt_90d": round(accum, 2), "held_by_listed_companies_pct": cross_pct, "note": ACTIVITY_NOTE,
            },
        }
        con.execute("INSERT INTO company_indicators VALUES (?,?,?,?,?)",
                    (t, rec.get("fiscal_year_end"), rec.get("doc_id"), ti.get("policy_uncertainty_per_100"), json.dumps(record, ensure_ascii=False)))
    con.commit()  # the monthly pass below reads every report; no write transaction stays open meanwhile

    # Monthly index over every annual report (not only the latest), by filing month.
    con.execute("DELETE FROM policy_uncertainty_monthly")
    months = {}
    for r in con.execute("SELECT submit_date, json_extract(record, '$.text_indicators') ti FROM annual_reports WHERE ti IS NOT NULL"):
        ti = json.loads(r["ti"])
        if not ti.get("sentences") or not r["submit_date"]:
            continue
        months.setdefault(r["submit_date"][:7], []).append(ti)
    for month, items in sorted(months.items()):
        n = len(items)
        rec = {
            "month": month, "reports": n,
            "mean_per_100_sentences": round(sum(x["policy_uncertainty_per_100"] for x in items) / n, 3),
            "share_of_reports_mentioning": round(sum(1 for x in items if x["policy_uncertainty_mentions"]) / n, 3),
            "share_by_area": {a: round(sum(1 for x in items if x["policy_uncertainty_by_area"].get(a)) / n, 3) for a in AREAS},
        }
        con.execute("INSERT INTO policy_uncertainty_monthly VALUES (?, ?)", (month, json.dumps(rec)))
    con.commit()
    return len(recs), len(months)
