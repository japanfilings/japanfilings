"""Monthly buyback reports (自己株券買付状況報告書). Block texts are copied from real filings, doc_id in each name."""
import csv
import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from japanfilings import buybacks, edinet, xbrlcsv

CACHE = Path(__file__).parents[1] / "data" / "csv_cache"

NIPPI_S100XW6K = ("2026年３月31日現在区分株式数(株)価額の総額(円)取締役会(2025年９月18日)での決議状況(取得期間2025年10月１日～2026年３月31日) "
                  "110,000(上限)1,000,000,000(上限)報告月における取得自己株式(取得日)３月２日5007,215,000 ３月３日5007,149,000 "
                  "３月23日4005,168,000計―8,000111,027,998告月末現在の累計取得自己株式 75,600999,437,979自己株式取得の進捗状況(％) 68.7399.94 "
                  "(注)１．取得期間及び取得自己株式(取得日)は、約定日基準で記載しております。")
COMMERCE_ONE_S100Y81E = ("　2026年５月31日現在区分株式数(株)価額の総額(円)取締役会(2025年11月14日)での決議状況(取得期間2026年５月１日～2026年５月14日)   "
                         "報告月における取得自己株式(取得日)５月１日1,200885,600 ５月14日－－計―3,3002,436,900報告月末現在の累計取得自己株式 50,00038,877,200"
                         "自己株式取得の進捗状況(％) 100.0086.39 （注）１．取得自己株式（取得日）は、約定ベースで記載しております。"
                         "３. 2026年５月13日の自己株式の取得をもちまして、2025年11月14日開催の取締役会における決議による自己株式の取得は終了いたしました。 "
                         "　2026年５月31日現在区分株式数(株)価額の総額(円)取締役会(2026年５月14日)での決議状況(取得期間2026年５月25日～2026年５月31日)   "
                         "報告月における取得自己株式(取得日)５月25日2,6001,814,100 ５月29日4,5003,156,300計―13,6009,520,400報告月末現在の累計取得自己株式 13,6009,520,400"
                         "自己株式取得の進捗状況(％) 27.2021.16 （注）１．取得自己株式（取得日）は、約定ベースで記載しております。")
TOYOTA_S100Y3HS = ("2026年４月30日現在区分株式数(株)価額の総額(円)取締役会(2025年６月３日、2026年１月14日および2026年３月30日)での決議状況取得期間(2026年３月31日～2026年６月30日) "
                   "1,192,331,020（上限）4,341,277,243,820（上限）報告月における取得自己株式(取得日)－月－日――計―――報告月末現在の累計取得自己株式――"
                   "自己株式取得の進捗状況(％)――  (注)  買付予定数　    ：　1,192,330,920株")
SRA_S100Z1EP = ("（２）【取締役会決議による取得の状況】2026年８月31日現在 区分株式数（株）価額の総額（円）取締役会（2026年７月17日）での決議状況（取得期間　2026年７月21日～2027年３月31日）"
                "960,0004,300,000,000報告月における取得自己株式（取得日）－－－計－－－報告月末現在の累計取得自己株式300,0001,356,000,000自己株式取得の進捗状況（％）31.2531.53 ")
MUFG_S100YAIW = ("2026年５月31日現在区分株式数(株)価額の総額(円)取締役会(2026年５月15日)での決議状況(取得期間　2026年５月18日～2026年６月30日) 45,000,000100,000,000,000"
                 "報告月における取得自己株式(取得日)５月20日1,384,2004,164,873,839計―10,720,00032,828,681,427報告月末現在の累積取得自己株式 10,720,00032,828,681,427自己株式取得の進捗状況(％) 23.832.8 (注) １．")
ITOCHU_S100XXER = ("2026年３月31日現在 区分株式数（株）価額の総額（円）取締役会（2026年２月６日）での決議状況（取得期間　2026年２月９日～2026年３月31日）13,000,00020,000,000,000"
                   "報告月における取得自己株式（取得日）３月２日270,000599,908,150計―6,876,60014,226,007,350報告月末現在の累計取得自己株式9,503,80019,999,850,300自己株式取得の進捗状況（％）73.11%100.00%（注１）約定日基準")
SMC_DISPOSAL_S100X4CW = ("2025年10月31日現在区分報告月における処分株式数(株)処分価額の総額(円)引き受ける者の募集を行った取得自己株式(処分日)   　月　日  計―――消却の処分を行った取得自己株式(消却日)   　月　日  計―――"
                         "合併、株式交換、株式交付、会社分割に係る移転を行った取得自己株式(移転日)   　月　日  計―――その他(単元未満株式の買増請求に基づき売り渡した取得自己株式)(処分日)   10月20日271,377,540計―271,377,540合計 271,377,540  ")
CANCELLED_S100XR1E = ("２【処理状況】 2026年２月28日現在区分報告月における処分株式総数（株）処分価額の総額（円）引き受ける者の募集を行った取得自己株式（処分日）月　日――計―――消却処分を行った取得自己株式（消却日）２月27日576,2002,739,831,000"
                      "計―576,2002,739,831,000合併、株式交換、株式交付、会社分割に係る移転を行った取得自己株式（移転日）月　日――計―――その他（　　　　　　　　　　　　　　　）（処分日）月　日――計―――合計576,2002,739,831,000  ")
TOKIO_S100Y3FS = ("その他（第三者割当により処分した株式）（処分日）  4月16日48,207,200287,411,326,400計－48,207,200287,411,326,400合計48,207,200287,411,326,400"
                  "（注）処分株式数：48,207,200株処分価額：1株につき5,962円")
ORIX_HOLD_S100Y89G = "３【保有状況】 2026年５月31日現在報告月末日における保有状況株式数（株）発行済株式総数（保有自己株式数を除く)1,099,924,922保有自己株式数24,181,702  "
RECRUIT_HOLD_S100Y9GX = ("2026年5月31日現在報告月末日における保有状況株式数(株)発行済株式総数1,472,504,149保有自己株式数35,531,585 (注1)　「保有自己株式数」には、単元未満株式の買取による株式数を含めています。"
                         "(注2)　当該株式を含めた当連結会計年度末における自己株式数は80,365,152株です。")


def one(text):
    entries, none = buybacks.parse_resolutions(text)
    assert not none and len(entries) == 1
    return entries[0]


def test_cells_run_together_and_arithmetic():
    r = one(NIPPI_S100XW6K)  # also: the filing's typo 告月末 for 報告月末
    assert (r["board_resolution_date"], r["acquisition_period_start"], r["acquisition_period_end"]) == ("2025-09-18", "2025-10-01", "2026-03-31")
    assert (r["authorized_max_shares"], r["authorized_max_yen"]) == (110000, 1000000000)
    assert (r["period_acquired_shares"], r["period_acquired_yen"]) == (8000, 111027998)
    assert (r["cumulative_acquired_shares"], r["cumulative_acquired_yen"]) == (75600, 999437979)
    assert (r["stated_progress_shares_pct"], r["stated_progress_yen_pct"]) == (68.73, 99.94)
    assert r["progress_shares_pct"] == pytest.approx(75600 / 110000 * 100, abs=1e-3) and r["progress_yen_pct"] == pytest.approx(99.9438, abs=1e-3)
    assert (r["remaining_shares"], r["remaining_yen"]) == (34400, 562021)
    assert r["avg_price_jpy"] == 13878.5 and r["warnings"] == []
    assert r["notes_ja"].startswith("(注)1.")


def test_two_resolutions_stay_separate_and_blank_limits_stay_null():
    entries, _ = buybacks.parse_resolutions(COMMERCE_ONE_S100Y81E)
    assert [e["board_resolution_date"] for e in entries] == ["2025-11-14", "2026-05-14"]
    assert [(e["period_acquired_shares"], e["period_acquired_yen"]) for e in entries] == [(3300, 2436900), (13600, 9520400)]
    assert [e["cumulative_acquired_shares"] for e in entries] == [50000, 13600]
    assert [(e["stated_progress_shares_pct"], e["stated_progress_yen_pct"]) for e in entries] == [(100.0, 86.39), (27.2, 21.16)]
    for e in entries:  # 50,000 at 100.00% is not turned into a limit
        assert e["authorized_max_shares"] is None and e["authorized_max_yen"] is None
        assert e["progress_shares_pct"] is None and e["remaining_shares"] is None


def test_stacked_resolutions_without_a_second_header():
    text = NIPPI_S100XW6K.split("(注)")[0] + "取締役会(2026年３月25日)での決議状況(取得期間2026年４月１日～2026年６月30日) 50,000500,000,000報告月における取得自己株式(取得日)－－－計－－－報告月末現在の累計取得自己株式－－自己株式取得の進捗状況(％)－－"
    a, b = buybacks.parse_resolutions(text)[0]  # second header absent
    assert a["board_resolution_date"] == "2025-09-18" and a["cumulative_acquired_shares"] == 75600
    assert b["board_resolution_date"] == "2026-03-25" and b["authorized_max_shares"] == 50000 and b["period_none_stated"]


def test_no_purchase_this_month_is_null_with_flag():
    r = one(SRA_S100Z1EP)
    assert r["period_acquired_shares"] is None and r["period_acquired_yen"] is None and r["period_none_stated"] and r["avg_price_jpy"] is None
    assert (r["cumulative_acquired_shares"], r["stated_progress_yen_pct"]) == (300000, 31.53)


def test_tender_offer_with_several_dates_and_no_cumulative():
    r = one(TOYOTA_S100Y3HS)
    assert r["resolution_dates"] == ["2025-06-03", "2026-01-14", "2026-03-30"] and r["board_resolution_date"] == "2026-03-30"
    assert r["authorized_max_shares"] == 1192331020 and r["authorized_max_yen"] == 4341277243820
    assert r["cumulative_acquired_shares"] is None and r["remaining_shares"] is None and r["progress_shares_pct"] is None


def test_label_and_percent_variants():
    assert one(MUFG_S100YAIW)["cumulative_acquired_yen"] == 32828681427  # 累積
    assert (one(MUFG_S100YAIW)["stated_progress_shares_pct"], one(MUFG_S100YAIW)["stated_progress_yen_pct"]) == (23.8, 32.8)
    assert (one(ITOCHU_S100XXER)["stated_progress_shares_pct"], one(ITOCHU_S100XXER)["stated_progress_yen_pct"]) == (73.11, 100.0)


def test_stated_progress_that_disagrees_is_flagged_not_replaced():
    r = one(SRA_S100Z1EP.replace("31.2531.53", "41.2531.53"))
    assert r["stated_progress_shares_pct"] == 41.25 and r["progress_shares_pct"] == 31.25
    assert "stated_progress_shares_differs_from_arithmetic" in r["warnings"]


def test_none_stated_block():
    assert buybacks.parse_resolutions("（１）【株主総会決議による取得の状況】　該当事項はありません。 ") == ([], True)
    assert buybacks.parse_resolutions("　当該事項はありません。 ") == ([], True)


def test_unit_price_settles_shares_jammed_against_yen_else_null():
    d = buybacks.parse_disposals(SMC_DISPOSAL_S100X4CW, ref_price=49911)  # 27 + 1,377,540, not 2 + 71,377,540
    assert (d["disposed_shares"], d["disposed_yen"]) == (27, 1377540) and d["categories"][0]["category"] == "other"
    d = buybacks.parse_disposals(SMC_DISPOSAL_S100X4CW)
    assert d["disposed_shares"] is None and d["disposed_yen"] is None and "disposal_total_unresolved" in d["warnings"]


def test_disposal_categories_and_footnote_numbers_ignored():
    d = buybacks.parse_disposals(CANCELLED_S100XR1E)
    assert d["categories"] == [{"category": "cancellation", "label_ja": None, "shares": 576200, "yen": 2739831000}]
    d = buybacks.parse_disposals(TOKIO_S100Y3FS)
    assert (d["disposed_shares"], d["disposed_yen"]) == (48207200, 287411326400) and d["categories"][0]["label_ja"] == "第三者割当により処分した株式"
    assert buybacks.parse_disposals("２【処理状況】　　　該当事項はありません。")["none_stated"] is True


def test_holdings_ignore_footnotes_and_note_the_basis():
    assert buybacks.parse_holdings(RECRUIT_HOLD_S100Y9GX) == {"treasury_shares_held": 35531585, "issued_shares": 1472504149, "issued_shares_excludes_treasury": False}
    assert buybacks.parse_holdings(ORIX_HOLD_S100Y89G) == {"treasury_shares_held": 24181702, "issued_shares": 1099924922, "issued_shares_excludes_treasury": True}


def test_unparseable_block_is_failed_not_guessed():
    p = buybacks.parse_facts([{"element": "x:AcquisitionsByResolutionOfBoardOfDirectorsMeetingTextBlock", "value": "別紙のとおり", "label": "", "context": "", "unit": ""}])
    assert p["parse_status"] == "failed" and p["resolutions"] == []


def test_real_s100z7bs_limits_require_stated_progress_confirmation():
    parsed = buybacks.parse_facts(xbrlcsv.read_facts((CACHE / "S100Z7BS.zip").read_bytes()))
    r = parsed["resolutions"][0]
    assert (r["authorized_max_shares"], r["authorized_max_yen"]) == (100_000_000, 60_000_000_000)
    assert (r["remaining_shares"], r["remaining_yen"]) == (53_138_800, 120_700)
    assert r["warnings"] == [] and parsed["parse_status"] == "parsed"


def _zip(period, board, held, amends="－", code="39990"):
    rows = [["要素ID", "項目名", "コンテキストID", "相対年度", "連結・個別", "期間・時点", "ユニットID", "単位", "値"]]
    for el, v in (("jpcrp-sbr_cor:ReportingPeriodCoverPage", period), ("jpdei_cor:SecurityCodeDEI", code), ("jpdei_cor:IdentificationOfDocumentSubjectToAmendmentDEI", amends),
                  ("jpcrp-sbr_cor:ClassesOfSharesTextBlock", "株式の種類　　　普通株式"),
                  ("jpcrp-sbr_cor:AcquisitionsByResolutionOfShareholdersMeetingTextBlock", "該当事項はありません。"),
                  ("jpcrp-sbr_cor:AcquisitionsByResolutionOfBoardOfDirectorsMeetingTextBlock", board),
                  ("jpcrp-sbr_cor:DisposalsOfTreasurySharesTextBlock", "該当事項はありません。"),
                  ("jpcrp-sbr_cor:HoldingOfTreasurySharesTextBlock", f"発行済株式総数15,240,000保有自己株式数{held}")):
        rows.append([el, "x", "FilingDateInstant", "", "", "", "", "", v])
    buf = io.StringIO()
    csv.writer(buf, delimiter="\t").writerows(rows)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("XBRL_TO_CSV/jpcrp170000-sbr-001.csv", buf.getvalue().encode("utf-16"))
    return out.getvalue()


@pytest.fixture
def con(monkeypatch):
    c = sqlite3.connect(":memory:")
    c.executescript("CREATE TABLE filings (doc_id TEXT, submit_datetime TEXT, edinet_code TEXT, filer_name_ja TEXT, doc_type_code TEXT, parent_doc_id TEXT, withdrawn INT, has_csv INT);"
                    "CREATE TABLE issuers (edinet_code TEXT, ticker TEXT);INSERT INTO issuers VALUES ('E1','3817');")
    p = "自　2026年８月１日　至　2026年８月31日"
    files = {"ORIG": _zip(p, SRA_S100Z1EP, "1,764,000"), "AMD1": _zip(p, SRA_S100Z1EP, "1,700,000", "ORIG"), "AMD2": _zip(p, SRA_S100Z1EP, "1,715,920", "ORIG"),
             "NOTK": _zip(p, SRA_S100Z1EP, "1", code="333A0"), "GONE": _zip(p, SRA_S100Z1EP, "2")}
    calls = []
    monkeypatch.setattr(edinet, "download_csv_zip", lambda d: calls.append(d) or files[d])
    c.executemany("INSERT INTO filings VALUES (?,?,?,?,?,?,?,1)", [
        ("ORIG", "2026-09-10 09:00", "E1", "SRA", "220", None, 0), ("AMD1", "2026-10-01 09:00", "E1", "SRA", "230", "ORIG", 0), ("AMD2", "2026-10-07 15:31", "E1", "SRA", "230", "ORIG", 0),
        ("NOTK", "2026-09-10 10:00", "E2", "X", "220", None, 0), ("GONE", "2026-09-10 11:00", "E1", "SRA", "220", None, 1), ("OTHR", "2026-09-10 12:00", "E1", "SRA", "120", None, 0)])
    return c, calls


def test_run_stores_and_amendment_replaces_parent(con):
    c, calls = con
    assert buybacks.run(c) == (4, 4)
    assert sorted(calls) == ["AMD1", "AMD2", "NOTK", "ORIG"]  # not withdrawn, not other doc types
    assert buybacks.run(c) == (0, 0) and len(calls) == 4  # nothing downloaded twice
    latest = buybacks.latest_by_ticker(c, "3817")
    assert [r["doc_id"] for r in latest] == ["AMD2"]
    assert latest[0]["treasury_shares_held"] == 1715920 and latest[0]["amends_doc_id"] == "ORIG" and latest[0]["doc_type"] == "amendment"
    every = buybacks.latest_by_ticker(c, "3817", include_superseded=True)
    assert {r["doc_id"]: r["superseded"] for r in every} == {"ORIG": True, "AMD1": True, "AMD2": False}


def test_record_shape_and_ticker_fallback(con):
    c, _ = con
    buybacks.run(c, limit=1)
    buybacks.run(c)
    rec = buybacks.latest_by_ticker(c, "333A")[0]  # issuers has no row: ticker from the filing's own code
    assert rec["ticker"] == "333A" and rec["reporting_period_end"] == "2026-08-31" and rec["share_class_ja"] == "普通株式"
    assert rec["computed_fields"]["avg_price_jpy"].startswith("period_acquired_yen")
    assert rec["parse_status"] == "parsed" and rec["source_url"].endswith("NOTK")
    row = c.execute("SELECT period_start, n_resolutions, family_doc_id, record FROM buyback_reports WHERE doc_id='NOTK'").fetchone()
    assert row[:3] == ("2026-08-01", 1, "NOTK") and json.loads(row[3])["doc_id"] == "NOTK"


def test_holdings_accept_colon_labels_and_stacked_parentheses():
    # real S100Y4U3 uses "label:value" cells
    real = buybacks.parse_holdings("3【保有状況】\t\t\t報告月末における保有状況:株式数(株)発行済株式総数:1,235,000株保有自己株式数:24,000株")
    assert (real["issued_shares"], real["treasury_shares_held"]) == (1235000, 24000)
    stacked = buybacks.parse_holdings("発行済株式総数（自己株式を除く）（2024年3月31日現在）1,000,000株")
    assert stacked["issued_shares"] == 1000000 and stacked["issued_shares_excludes_treasury"] is True


def test_yen_only_resolution_is_usable_and_class_colon_dropped():
    facts = [{"element": "x:AcquisitionsByResolutionOfBoardOfDirectorsMeetingTextBlock",
              "value": "取締役会(2024年5月9日)での決議状況 報告月末現在の累計取得自己株式 1,234,567,890円"},
             {"element": "x:ClassesOfSharesTextBlock", "value": "株式の種類：普通株式"}]
    out = buybacks.parse_facts(facts)
    assert out["share_class_ja"] == "普通株式"
    assert out["resolutions"][0]["cumulative_acquired_yen"] == 1234567890
    assert out["parse_status"] != "failed"
