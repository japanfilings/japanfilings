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
