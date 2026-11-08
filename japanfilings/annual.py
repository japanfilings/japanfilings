"""Annual securities report (有価証券報告書, docTypeCode 120) from the XBRL CSV.

Element IDs are English (e.g. jppfs_cor:NetSales), so they double as English field names.
Personal data is left out on purpose: no officer birthdates, no shareholder addresses.
"""
import re
from datetime import date

from . import textindex
from .textutil import parse_number

# Context prefix -> years before the current fiscal year.
_YEAR_OFFSET = {"CurrentYear": 0, "Prior1Year": 1, "Prior2Year": 2, "Prior3Year": 3, "Prior4Year": 4}
_CTX_RE = re.compile(r"^(CurrentYear|Prior[1-4]Year)(Duration|Instant)(_NonConsolidatedMember)?$")

# Standard key -> element-name patterns tried in order (Japan GAAP, IFRS, US GAAP variants).
SUMMARY_KEYS = {
    # OrdinaryIncome is 経常収益, banks' and insurers' revenue (OrdinaryIncomeLoss is 経常利益).
    "revenue": ["NetSales", "Revenue", "OperatingRevenues?\\d?", "TotalRevenues?", "TotalNetRevenues?", "OrdinaryIncome",
                "NetSalesOfCompletedConstructionContracts", r"(?!Cost)\w*(Revenues?|Sales)", "StatementRevenue"],
    "ordinary_income": ["OrdinaryIncomeLoss"],
    "profit_before_tax": ["ProfitLossBeforeTax", "IncomeBeforeIncomeTaxes"],
    "net_income": ["ProfitLossAttributableToOwnersOfParent", "NetIncomeLoss", "ProfitLossAttributableToOwnersOfParentIFRS"],
    "total_assets": ["TotalAssets"],
    "net_assets": ["NetAssets", "EquityAttributableToOwnersOfParent", "TotalEquity"],
    # Under IFRS, EquityToAssetRatioIFRS is equity per share (Toyota 2026: 3,062.82 yen); the ratio is RatioOfOwnersEquityToGrossAssets.
    "equity_ratio": ["RatioOfOwnersEquityToGrossAssets", "EquityToAssetRatio(?!IFRS)"],
    "roe": ["RateOfReturnOnEquity"],
    "eps": ["BasicEarningsLossPerShare"],
    "bps": ["NetAssetsPerShare", "EquityToAssetRatioPerShare", "EquityAttributableToOwnersOfParentPerShare", "EquityToAssetRatio(?=IFRS)"],
    "dividend_per_share": ["DividendPaidPerShare"],
    "payout_ratio": ["PayoutRatio"],
    "operating_cf": ["NetCashProvidedByUsedInOperatingActivities", "CashFlowsFromUsedInOperatingActivities"],
    "investing_cf": ["NetCashProvidedByUsedInInvestingActivities", "CashFlowsFromUsedInInvestingActivities"],
    "financing_cf": ["NetCashProvidedByUsedInFinancingActivities", "CashFlowsFromUsedInFinancingActivities"],
    "cash": ["CashAndCashEquivalents"],
    "shares_issued": ["TotalNumberOfIssuedShares"],
    "employees": ["NumberOfEmployees"],
}
_PCT_KEYS = {"equity_ratio", "roe", "payout_ratio"}


def _num(v):
    return parse_number(v) if v and v not in ("－", "-") else None


def _years_back(iso, n):
    d = date.fromisoformat(iso)
    try:
        return d.replace(year=d.year - n).isoformat()
    except ValueError:  # Feb 29
        return d.replace(year=d.year - n, day=28).isoformat()


def _dei(facts):
    return {f["element"].split(":")[1]: f["value"] for f in facts if f["element"].startswith("jpdei_cor:")}


def key_metrics(facts, fy_end):
    """Up to five fiscal years of headline figures, consolidated when available."""
    raw, extra = {}, {}  # (offset, basis) -> {base element name: value}; extra = filer-specific and statement fallbacks
    for f in facts:
        el = f["element"]
        prefix, _, name = el.partition(":")
        m = _CTX_RE.match(f["context"])
        v = _num(f["value"])
        if not m or v is None:
            continue
        slot = (_YEAR_OFFSET[m.group(1)], "non_consolidated" if m.group(3) else "consolidated")
        if prefix == "jpcrp_cor" and ("SummaryOfBusinessResults" in name or name == "NumberOfEmployees"):
            raw.setdefault(slot, {})[name.replace("SummaryOfBusinessResults", "")] = v
        elif prefix.startswith("jpcrp030000-asr") and re.search(r"(SummaryOfBusinessResults|KeyFinancialData)$", name):
            # Filers such as Toyota and Sony put headline revenue in their own summary element.
            extra.setdefault(slot, {}).setdefault(re.sub(r"(SummaryOfBusinessResults|KeyFinancialData)$", "", name), v)
        elif prefix in ("jppfs_cor", "jpigp_cor") and re.fullmatch(r"(NetSales|Revenues?|OperatingRevenues?\d?|TotalRevenues?)(IFRS|USGAAP)?", name):
            extra.setdefault(slot, {}).setdefault("StatementRevenue", v)
    for slot, items in extra.items():
        if slot in raw:
            raw[slot] = {**items, **raw[slot]}
    out = []
    bases = {b for _, b in raw}
    basis = "consolidated" if "consolidated" in bases else "non_consolidated"
    for offset in sorted({o for o, b in raw if b == basis}):
        items = raw[(offset, basis)]
        if basis == "consolidated":
            # Dividends and share counts belong to the parent company and are reported only on that basis.
            parent = raw.get((offset, "non_consolidated"), {})
            items = {**{k: v for k, v in parent.items() if re.match(r"(DividendPaidPerShare|PayoutRatio|TotalNumberOfIssuedShares)", k)}, **items}
        row = {"fiscal_year_end": _years_back(fy_end, offset) if fy_end else None, "basis": basis}
        for key, patterns in SUMMARY_KEYS.items():
            row[key] = None
            for p in patterns:
                hit = next((v for k, v in items.items() if re.fullmatch(p + r"(IFRS|USGAAP|JMIS)?", k)), None)
                if hit is not None:
                    row[key] = round(hit * 100, 2) if key in _PCT_KEYS else hit
                    break
        out.append(row)
    return out


def _statement(element):
    name = element.split(":")[1]
    if re.search(r"(OpeCF|InvCF|FinCF)", name) or re.search(r"NetCashProvidedByUsedIn|CashFlowsFromUsedIn|CashAndCashEquivalents", name):
        return "cash_flow"
    return None


def statements(facts):
    """Current and prior year financial statements as {statement: {period: {element: value}}}.
    Balance sheet = instants; income statement = durations not tagged as cash flow."""
    out = {"income_statement": {}, "balance_sheet": {}, "cash_flow": {}}
    consolidated = any(f["element"].startswith(("jppfs_cor:", "jpigp_cor:")) and _CTX_RE.match(f["context"])
                       and not f["context"].endswith("_NonConsolidatedMember") for f in facts)
    for f in facts:
        if not f["element"].startswith(("jppfs_cor:", "jpigp_cor:")):
            continue
        m = _CTX_RE.match(f["context"])
        v = _num(f["value"])
        if not m or v is None or m.group(1) not in ("CurrentYear", "Prior1Year") or bool(m.group(3)) == consolidated:
            continue
        kind = _statement(f["element"]) or ("balance_sheet" if m.group(2) == "Instant" else "income_statement")
        period = "current" if m.group(1) == "CurrentYear" else "prior"
        out[kind].setdefault(period, {})[f["element"].split(":")[1]] = v
    out["basis"] = "consolidated" if consolidated else "non_consolidated"
    return out


def _first(facts, element, context_prefix=""):
    for f in facts:
        if f["element"] == element and f["context"].startswith(context_prefix):
            return f["value"]
    return None


def _rows(facts, prefix_elements, member_re):
    """Group facts by table row member (e.g. No1MajorShareholdersMember), in filing order."""
    rows = {}
    for f in facts:
        m = re.search(member_re, f["context"])
        if m and f["element"] in prefix_elements:
            rows.setdefault(m.group(1), {})[prefix_elements[f["element"]]] = f["value"]
    return list(rows.values())


def company_items(facts):
    c = "jpcrp_cor:"
    employees = {
        "employees": _num(_first(facts, c + "NumberOfEmployees", "CurrentYearInstant")),
        "average_age": _num(_first(facts, c + "AverageAgeYearsInformationAboutReportingCompanyInformationAboutEmployees")),
        "average_tenure_years": _num(_first(facts, c + "AverageLengthOfServiceYearsInformationAboutReportingCompanyInformationAboutEmployees")),
        "average_annual_salary_jpy": _num(_first(facts, c + "AverageAnnualSalaryInformationAboutReportingCompanyInformationAboutEmployees")),
        "female_manager_ratio_pct": _pct(_first(facts, c + "RatioOfFemaleEmployeesInManagerialPositionsMetricsOfReportingCompany")),
    }
    major = _rows(facts, {
        c + "NameMajorShareholders": "name",
        c + "NumberOfSharesHeld": "shares",
        c + "ShareholdingRatio": "ratio",
    }, r"(No\d+MajorShareholdersMember)")
    for r in major:
        r["shares"] = _num(r.get("shares"))
        r["ratio_pct"] = _pct(r.pop("ratio", None))
    officers = _rows(facts, {
        c + "NameInformationAboutDirectorsAndCorporateAuditors": "name",
        c + "OfficialTitleOrPositionInformationAboutDirectorsAndCorporateAuditors": "title",
        c + "NumberOfSharesHeldOrdinarySharesInformationAboutDirectorsAndCorporateAuditors": "shares",
    }, r"_([^_]+Member)$")
    officers = [dict(o, shares=_num(o.get("shares"))) for o in officers if o.get("name")]
    categories = {}
    for f in facts:
        m = re.fullmatch(r"jpcrp_cor:PercentageOfShareholdings(\w+)", f["element"])
        if m and f["context"].startswith("CurrentYearInstant"):
            categories[_snake(m.group(1))] = _pct(f["value"])
    return {
        "english_name": _first(facts, c + "CompanyNameInEnglishCoverPage"),
        "representative": _first(facts, c + "TitleAndNameOfRepresentativeCoverPage"),
        "headquarters": _first(facts, c + "AddressOfRegisteredHeadquarterCoverPage"),
        "auditor": _first(facts, c + "AuditFirm1NonConsolidated") or _first(facts, c + "AuditFirm1Consolidated"),
        "audit_fees_jpy": _num(_first(facts, c + "AuditFeesReportingCompany", "CurrentYear")),
        "non_audit_fees_jpy": _num(_first(facts, c + "NonAuditFeesReportingCompany", "CurrentYear")),
        "capex_jpy": _num(_first(facts, c + "CapitalExpendituresOverviewOfCapitalExpendituresEtc")),
        "total_shareholder_return_5y": _num(_first(facts, c + "TotalShareholderReturn", "CurrentYear")),
        "issued_shares": _num(_first(facts, c + "NumberOfIssuedSharesAsOfFiscalYearEndIssuedSharesTotalNumberOfSharesEtc")),
        "treasury_shares": _num(_first(facts, c + "TotalNumberOfSharesHeldTreasurySharesEtc")),
        "female_officer_ratio_pct": _pct(_first(facts, c + "RatioOfFemaleDirectorsAndOtherOfficers")),
        "workforce": employees,
        "major_shareholders": major,
        "officers": officers,
        "shareholder_categories_pct": categories,
    }


# Strategic shareholdings table (特定投資株式 / みなし保有株式) in section "株式の保有状況".
# Element names carry the holder: the reporting company, or for holding companies its largest subsidiary.
_XH_FIELD = {"NameOfSecurities": "name_ja", "NumberOfSharesHeld": "shares", "BookValue": "book_value_jpy",
             "PurposeOfShareholding": "purpose_ja", "WhetherIssuerOfAforementionedSharesHoldsReportingCompanysShares": "held_back_ja"}
_XH_RE = re.compile(r"^(" + "|".join(_XH_FIELD) + r")\w*?(SpecifiedInvestment|DeemedHoldings)\w*PureInvestment(\w+)$")
_XH_CTX = re.compile(r"^(CurrentYear|Prior1Year)Instant_(Row\d+Member)$")
_XH_SUMMARY = {  # listed shares ("other than not listed") held for purposes other than pure investment
    "NumberOfIssuesSharesOtherThanThoseNotListed": "listed_issues",
    "CarryingAmountSharesOtherThanThoseNotListed": "listed_carrying_amount_jpy",
    "NumberOfIssuesWhoseNumberOfSharesIncreasedSharesOtherThanThoseNotListed": "listed_issues_increased",
    "NumberOfIssuesWhoseNumberOfSharesDecreasedSharesOtherThanThoseNotListed": "listed_issues_decreased",
    "TotalAcquisitionCostForIncreasedSharesSharesOtherThanThoseNotListed": "listed_acquisition_cost_jpy",
    "TotalSaleAmountForDecreasedSharesSharesOtherThanThoseNotListed": "listed_sale_amount_jpy",
}


def cross_holdings(facts):
    """Named strategic shareholdings with current/prior shares and book value, and whether the investee
    holds the reporting company back (mutual holding, as stated by the filer)."""
    rows = {}
    summary = {}
    for f in facts:
        el = f["element"].split(":")[1]
        key = next((k for k in _XH_SUMMARY if el == k + "InvestmentSharesHeldForPurposesOtherThanPureInvestmentReportingCompany"), None)
        if key:
            if f["context"] in ("CurrentYearInstant", "CurrentYearDuration"):
                summary[_XH_SUMMARY[key]] = _num(f["value"])
            continue
        m, c = _XH_RE.match(el), _XH_CTX.match(f["context"])
        if not (m and c):
            continue
        holder = _snake(m.group(3))  # reporting_company, largest_holding_company, ...
        row = rows.setdefault((holder, m.group(2), c.group(2)), {
            "holder": holder, "kind": "deemed" if m.group(2) == "DeemedHoldings" else "specified"})
        field = _XH_FIELD[m.group(1)]
        if field in ("shares", "book_value_jpy"):
            row[field if c.group(1) == "CurrentYear" else "prior_" + field] = _num(f["value"])
        else:
            row[field] = f["value"].strip()
    out = []
    for r in rows.values():
        if not r.get("name_ja"):
            continue
        hb = r.get("held_back_ja") or ""
        r["held_back"] = True if hb.startswith("有") else False if hb.startswith("無") else None
        out.append(r)
    return {"holdings": out, "summary": summary}


def _pct(v):
    n = _num(v)
    return round(n * 100, 2) if n is not None and n <= 1 else n


def _snake(s):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()


def parse(facts, doc_id):
    dei = _dei(facts)
    fy_end = dei.get("CurrentFiscalYearEndDateDEI")
    sec = (dei.get("SecurityCodeDEI") or "").strip()
    return {
        "doc_id": doc_id,
        "edinet_code": dei.get("EDINETCodeDEI"),
        "ticker": sec[:4] if sec and sec not in ("－", "-") else None,
        "accounting_standard": dei.get("AccountingStandardsDEI"),
        "fiscal_year_start": dei.get("CurrentFiscalYearStartDateDEI"),
        "fiscal_year_end": fy_end,
        "amendment": dei.get("AmendmentFlagDEI") == "true",
        "key_metrics": key_metrics(facts, fy_end),
        "statements": statements(facts),
        "company": company_items(facts),
        "cross_holdings": cross_holdings(facts),
        "text_indicators": textindex.from_facts(facts),
        "source": "EDINET (Financial Services Agency of Japan)",
    }
