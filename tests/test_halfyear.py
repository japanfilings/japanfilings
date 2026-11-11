import json
import sqlite3

from japanfilings import halfyear, refresh


def test_fiscal_year_end_month_is_the_fiscal_year_not_the_interim_period():
    f = halfyear.fiscal_year_end_month
    # S100ZA8O, Capcom 9697 (March year end): was stored as 2026-09.
    assert f("半期報告書－第48期(2026/04/01－2026/09/30)") == "2027-03"
    # S100Z87H 6183, S100Z7N0 3608, S100Z7FJ 2659 (February year end): were stored as 2026-08.
    for title in ("半期報告書－第10期(2026/03/01－2026/08/31)", "半期報告書－第40期(2026/03/01－2026/08/31)"):
        assert f(title) == "2027-02"
    assert f("訂正半期報告書－第5期(2026/07/01－2026/12/31)") == "2027-06"
    assert f("半期報告書－第5期(2026/07/16－2026/12/31)") == "2027-07"
    assert f("") is None and f(None) is None


def test_non_six_month_interim_keeps_its_stated_end():
    # Transition periods after a fiscal-year change: no year end can be inferred, so use the period's own end.
    assert halfyear.fiscal_year_end_month("半期報告書－第37期(2025/04/01－2026/03/31)") == "2026-03"
    assert halfyear.fiscal_year_end_month("半期報告書－第13期(2026/01/01－2026/09/30)") == "2026-09"


def test_refresh_stores_h1_under_the_fiscal_year_end(monkeypatch):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE earnings_releases (ticker, fiscal_year_end_month, period, release_date, url, record, PRIMARY KEY (ticker, fiscal_year_end_month, period))")
    monkeypatch.setattr(halfyear, "parse", lambda facts: {"results": {}})
    monkeypatch.setattr(refresh.edinet, "download_csv_zip", lambda doc_id: b"")
    import japanfilings.xbrlcsv as xbrlcsv
    monkeypatch.setattr(xbrlcsv, "read_facts", lambda data: [])
    refresh._read_half_year(con, {"doc_id": "S100ZA8O", "issuer_ticker": "9697", "submit_datetime": "2026-10-28 09:00",
                                  "description_ja": "半期報告書－第48期(2026/04/01－2026/09/30)"}, halfyear, xbrlcsv)
    row = con.execute("SELECT ticker, fiscal_year_end_month, period, record FROM earnings_releases").fetchone()
    assert row[:3] == ("9697", "2027-03", "H1")
    assert json.loads(row[3])["fiscal_year_end_month"] == "2027-03"


def test_ytd_and_quarter_contexts_are_read():
    # Some half-year reports label the period CurrentYTDDuration and its end CurrentQuarterInstant.
    from japanfilings import halfyear
    facts = [
        {"element": "jpcrp_cor:NetSalesSummaryOfBusinessResults", "context": "CurrentYTDDuration", "value": "10064909000"},
        {"element": "jpcrp_cor:NetSalesSummaryOfBusinessResults", "context": "Prior1YTDDuration", "value": "10135000000"},
        {"element": "jpcrp_cor:NetSalesSummaryOfBusinessResults", "context": "Prior1YearDuration", "value": "20000000000"},
        {"element": "jpcrp_cor:TotalAssetsSummaryOfBusinessResults", "context": "CurrentQuarterInstant", "value": "37557266000"},
    ]
    rec = halfyear.parse(facts)
    assert rec["results"]["revenue_mn_jpy"] == 10064.909
    assert rec["results"]["revenue_yoy_pct"] == -0.7
    assert rec["financial_position"]["total_assets"] == 37557266000


def test_latest_report_is_by_period_then_filing_time():
    from japanfilings import halfyear
    rows = [
        # A correction of last year's report, filed after this year's report, must not replace it.
        ("5021", "S100ZDRX", "2026-11-10 17:00", "訂正半期報告書－第11期(2025/04/01－2025/09/30)"),
        ("5021", "S100ZDE0", "2026-11-10 15:31", "半期報告書－第12期(2026/04/01－2026/09/30)"),
        # A title with no period is taken as this year's report.
        ("7011", "S100ZDE8", "2026-11-10 13:04", "半期報告書"),
        ("7011", "S100OLD1", "2025-11-12 13:00", "半期報告書－第100期(2025/04/01－2025/09/30)"),
    ]
    picked = {r[0]: r[1] for r in halfyear.latest_per_ticker(rows)}
    assert picked == {"5021": "S100ZDE0", "7011": "S100ZDE8"}


def test_fiscal_year_from_dei():
    from japanfilings import halfyear
    facts = [{"element": "jpdei_cor:CurrentFiscalYearEndDateDEI", "context": "FilingDateInstant", "value": "2027-03-31"}]
    assert halfyear.fiscal_year_from_facts(facts) == "2027-03"
    assert halfyear.fiscal_year_from_facts([]) is None
