import json
import sqlite3

from japanfilings import filing_details as fd


def fact(element, value, context="CurrentYearDuration", label=""):
    return {"element": element, "value": value, "context": context, "label": label, "unit": "JPY"}


def test_quote_masks_names_and_is_short():
    text = "代表取締役社長 山田 太郎は配当の安定を基本方針とします。" + "続きです。" * 80
    out = fd.quote(text)
    assert "山田" not in out and "太郎" not in out
    assert "[氏名省略]" in out
    assert len(out) <= fd.QUOTE_MAX


def test_segments_reads_reportable_members_and_single_segment():
    facts = []
    for member, sales, profit in (("jpcrp030000-asr_E00001SoftwareReportableSegmentsMember", "100", "20"),
                                  ("jpcrp030000-asr_E00001ServiceReportableSegmentsMember", "200", "30")):
        ctx = "CurrentYearDuration_" + member
        facts += [fact("jppfs_cor:RevenuesFromExternalCustomers", sales, ctx, "外部顧客への売上高"),
                  fact("jppfs_cor:SegmentProfitLoss", profit, ctx, "セグメント利益")]
    status, rec = fd.segments(facts)
    assert status == "found"
    assert [x["external_sales_jpy"] for x in rec["segments"]] == [100, 200]
    assert all(x["profit_jpy"] is not None for x in rec["segments"])

    status, rec = fd.segments([fact(
        "jpcrp_cor:DescriptionOfFactThatCompanysBusinessComprisesSingleSegment", "単一セグメントです")])
    assert status == "single_segment" and rec["segments"] == []


def test_sga_prefers_tagged_values_and_keeps_prior_year():
    facts = [
        fact("jppfs_cor:SalariesAndAllowancesSGA", "12000000", label="給料及び手当、販管費"),
        fact("jppfs_cor:AdvertisingExpensesSGA", "3000000", label="広告宣伝費、販管費"),
        fact("jppfs_cor:SalariesAndAllowancesSGA", "11000000", "Prior1YearDuration", "給料及び手当、販管費"),
    ]
    status, rec = fd.sga(facts)
    assert status == "found" and rec["method"] == "tagged"
    salaries = next(x for x in rec["items"] if x["element"] == "SalariesAndAllowancesSGA")
    assert salaries["current_jpy"] == 12_000_000 and salaries["prior_jpy"] == 11_000_000


def test_ambiguous_sga_digit_run_is_null_not_guessed():
    cells, ok = fd._sga_cells("1234")
    assert not ok and cells == []


def test_director_compensation_has_category_totals_only():
    ctx = "CurrentYearDuration_DirectorsExcludingOutsideDirectorsMember"
    facts = [
        fact("jpcrp_cor:TotalAmountOfRemunerationEtcByCategoryOfDirectorsAndOtherOfficers", "85000000", ctx, "報酬等の総額"),
        fact("jpcrp_cor:NumberOfDirectorsAndOtherOfficersByCategoryOfDirectorsAndOtherOfficers", "4", ctx, "対象となる役員の員数"),
        # Per-person remuneration elements deliberately do not match the category-total suffix.
        fact("jpcrp_cor:TotalAmountOfRemunerationEtcByIndividual", "120000000", "CurrentYearDuration_Individual1Member", "報酬等の総額"),
    ]
    status, rec = fd.director_compensation(facts)
    assert status == "found" and len(rec["categories"]) == 1
    assert rec["categories"][0]["total_jpy"] == 85_000_000
    assert "Individual" not in json.dumps(rec, ensure_ascii=False)


def test_customers_none_stated_and_a_simple_table():
    status, rows, warnings = fd.parse_customers("主要な顧客について、売上高の10%以上を占める顧客はありません。")
    assert status == "none_stated" and rows == [] and warnings == []

    text = "名称 売上高(百万円) 関連するセグメント名 株式会社顧客A 1,200 ソフトウェア"
    status, rows, warnings = fd.parse_customers(text, ["ソフトウェア"])
    assert status == "found" and not warnings
    assert rows == [{"name_ja": "株式会社顧客A", "sales_jpy": 1_200_000_000, "segment_ja": "ソフトウェア"}]


def test_affiliates_facilities_capex_and_dividend_policy():
    facts = [
        fact("jpcrp_cor:NumberOfConsolidatedSubsidiaries", "3", "CurrentYearInstant"),
        fact("jpcrp_cor:OverviewOfAffiliatedEntitiesTextBlock",
             "(連結子会社) 株式会社テスト 東京都 100 ソフトウェア事業 75.0", label="関係会社の状況"),
        fact("jpcrp_cor:CapitalExpendituresOverviewOfCapitalExpendituresEtc", "9000000"),
        fact("jpcrp_cor:OverviewOfCapitalExpendituresEtcTextBlock", "設備投資は生産設備を中心に実施しました。"),
        fact("jpcrp_cor:MajorFacilitiesTextBlock", "東京本社(東京都千代田区) ソフトウェア設備"),
        fact("jpcrp_cor:DividendPolicyTextBlock", "当社は安定的な配当を行うことを基本方針としております。その他の説明。"),
    ]
    status, rec = fd.affiliates(facts)
    assert status == "found" and rec["consolidated_subsidiaries"] == 3
    assert rec["rows"][0]["name_ja"] == "株式会社テスト"
    assert "東京都" not in rec["rows"][0]  # address is not retained
    assert fd.capex(facts)[1]["total_jpy"] == 9_000_000
    assert fd.major_facilities(facts)[1]["facilities"][0]["location_ja"] == "東京都千代田区"
    assert fd.dividend_policy(facts)[1]["quote_ja"] == "当社は安定的な配当を行うことを基本方針としております。"


def test_run_together_column_headings_are_not_part_of_names():
    rows = fd.parse_affiliate_rows("(連結子会社) 関係内容株式会社テスト 東京都 100 事業 75.0")
    assert rows[0]["name_ja"] == "株式会社テスト"
    rows = fd.parse_facilities("金額東京本社(東京都千代田区) 設備")
    assert rows[0]["name_ja"] == "東京本社"


def test_diversity_converts_tagged_fraction_to_percentage():
    facts = [
        fact("jpcrp_cor:RatioOfFemaleEmployeesInManagerialPositionsMetricsOfReportingCompany", "0.169"),
        fact("jpcrp_cor:AllEmployeesDifferencesInWagesBetweenMaleAndFemaleEmployeesMetricsOfReportingCompany", "0.721"),
    ]
    status, rec = fd.diversity(facts)
    assert status == "found"
    assert rec["reporting_company"] == {"female_manager_pct": 16.9, "pay_gap_all_pct": 72.1}


def test_parse_always_returns_all_items():
    parsed = fd.parse([])
    assert tuple(parsed) == fd.ITEMS
    assert all(status in {"not_found", "none_stated"} for status, _ in parsed.values())


def test_run_is_resumable_retries_failed_and_stores_provenance(tmp_path, monkeypatch):
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE filings(doc_id TEXT, submit_datetime TEXT, edinet_code TEXT, doc_type_code TEXT,
          withdrawn INTEGER, has_csv INTEGER, description_ja TEXT);
        CREATE TABLE issuers(edinet_code TEXT, ticker TEXT);
        INSERT INTO filings VALUES ('S100TEST','2026-06-30 09:00:00','E00001','120',0,1,'有価証券報告書');
        INSERT INTO issuers VALUES ('E00001','1234');
    """)
    con.executescript(fd.SCHEMA)
    con.execute("INSERT INTO filing_details_read VALUES ('S100TEST','failed','temporary')")
    (tmp_path / "S100TEST.zip").write_bytes(b"cached")
    monkeypatch.setattr(fd.edinet, "CSV_CACHE", str(tmp_path))
    monkeypatch.setattr(fd.edinet, "download_csv_zip", lambda _doc: b"zip")
    monkeypatch.setattr(fd.xbrlcsv, "read_facts", lambda _data: [
        fact("jpdei_cor:SecurityCodeDEI", "12340", "FilingDateInstant"),
        fact("jpdei_cor:CurrentFiscalYearEndDateDEI", "2026-03-31", "FilingDateInstant"),
    ])
    assert fd.run(con) == (1, 0)
    rows = con.execute("SELECT item, source_url, record FROM filing_details WHERE doc_id='S100TEST'").fetchall()
    assert len(rows) == len(fd.ITEMS)
    for item, url, raw in rows:
        rec = json.loads(raw)
        assert rec["doc_id"] == "S100TEST" and rec["item"] == item
        assert url == rec["source_url"] == fd.SOURCE_URL.format("S100TEST")
    assert fd.run(con) == (0, 0)


def test_coverage_groups_statuses():
    con = sqlite3.connect(":memory:")
    con.executescript(fd.SCHEMA)
    fd.store(con, "D1", {"segments": ("found", {})}, None, None, None, None)
    fd.store(con, "D2", {"segments": ("not_found", {})}, None, None, None, None)
    assert fd.coverage(con) == {"segments": {"found": 1, "not_found": 1}}


def test_compensation_scale_warnings():
    from japanfilings import filing_details as fd
    cat = lambda member, total, n: {"category_member": member, "total_jpy": total, "officers": n}
    # S100XVK0 (4777): 71.4 billion yen for 5 directors.  S100YTBE (3823): 24.9 billion yen for 5.
    assert fd.compensation_warnings([cat("DirectorsExcludingOutsideDirectorsMember", 71415505000.0, 5.0)]) == \
        ["scale_suspect_high:DirectorsExcludingOutsideDirectorsMember"]
    assert fd.compensation_warnings([cat("DirectorsExcludingOutsideDirectorsMember", 24900000000.0, 5.0)]) == \
        ["scale_suspect_high:DirectorsExcludingOutsideDirectorsMember"]
    # S100YLZQ (3175): 87,000 yen for 4 directors, filed in thousands of yen.
    assert fd.compensation_warnings([cat("DirectorsExcludingAuditAndSupervisoryCommitteeMembersAndOutsideDirectorsMember", 87000.0, 4.0)]) == \
        ["scale_suspect_low:DirectorsExcludingAuditAndSupervisoryCommitteeMembersAndOutsideDirectorsMember"]
    assert fd.compensation_warnings([cat("ExecutiveOfficersMember", 4602000000.0, 2.0), cat("X", None, 1.0), cat("Y", 50000000.0, 5.0)]) == []


def test_deepseek_review_fixes_20261102():
    # 第N facility names keep their ordinal (real S100YIVN, S100XTN7)
    rows = fd.parse_facilities("事業所名(所在地) 提出会社 第2センター(大阪市西成区)事務所 本社(東京都千代田区)")
    assert rows[0]["name_ja"] == "第2センター"
    # scrub: title kept, one-character surname masked, 摂氏 untouched
    assert fd.scrub("代表取締役山田太郎氏は説明した。") == "代表取締役[氏名省略]氏は説明した。"
    assert fd.scrub("林氏は出席した。") == "[氏名省略]氏は出席した。"
    assert fd.scrub("代表取締役山田太郎") == "代表取締役[氏名省略]"
    assert fd.scrub("摂氏20度") == "摂氏20度"
    # customers: ratio column glued to the amount (real S100YCH0), header 名称, single-segment preamble (S100YKKS)
    s, rows, _ = fd.parse_customers("(単位:千円)顧客の名称又は氏名売上高売上高合計に対する割合関連するセグメント名トヨタ(株)3,738,43412.8%自動車部品製造事業日産自動車(株)3,498,24612.0%自動車部品製造事業")
    assert [r["sales_jpy"] for r in rows] == [3738434000, 3498246000] and rows[0]["segment_ja"] == "自動車部品製造事業"
    s, rows, w = fd.parse_customers("当社グループは、セキュリティ事業のみの単一セグメントに従事しております。 (単位:百万円)顧客の名称又は氏名売上高ダイワボウ情報システム株式会社3,618SB C&S株式会社1,942")
    assert rows[0]["name_ja"] == "ダイワボウ情報システム株式会社" and not w
    # block(): current-year copy preferred over the prior-year one
    facts = [{"element": "x:Foo", "context": "Prior1YearInstant", "value": "5", "label": ""},
             {"element": "x:Foo", "context": "CurrentYearInstant", "value": "6", "label": ""}]
    assert fd._block(facts, "Foo") == "6"


# ---- segments on real cached filings (cache only: never downloads; skipped when the zip is not cached) -------------
import os

import pytest

from japanfilings import edinet, xbrlcsv


def _cached_facts(doc_id):
    path = os.path.join(edinet.CSV_CACHE, f"{doc_id}.zip")
    if not os.path.exists(path):
        pytest.skip(f"{doc_id} not in the CSV cache")
    with open(path, "rb") as f:
        return xbrlcsv.read_facts(f.read())


def _by_en(rec):
    return {x["name_en"]: x for x in rec["segments"]}


def test_segments_toyota_ifrs_external_vs_intersegment():
    status, rec = fd.segments(_cached_facts("S100Y8NY"))  # Toyota, IFRS, company-specific element names
    assert status == "found"
    seg = _by_en(rec)
    assert set(seg) == {"Automotive", "Financial Services", "Other"}
    assert seg["Automotive"]["external_sales_jpy"] == 45_201_924_000_000
    assert seg["Automotive"]["intersegment_sales_jpy"] == 215_779_000_000
    assert seg["Automotive"]["sales_jpy"] == 45_417_703_000_000
    assert seg["Financial Services"]["profit_jpy"] == 851_722_000_000 and seg["Financial Services"]["assets_jpy"] == 53_741_709_000_000
    assert seg["Other"]["kind"] == "other"
    # the elimination member is an adjustment, not a segment, and never gets a name
    assert not any("Elimination" in (x["member"] or "") for x in rec["segments"])
    assert rec["adjustments"]["sales_jpy"] == -1_241_278_000_000
    total_ext = sum(x["external_sales_jpy"] for x in rec["segments"])
    assert abs(total_ext - rec["consolidated_revenue_jpy"]) <= 1_000_000 and rec["revenue_check"] == "match"
    # Japanese names come only from the employee table, matched by headcount; the unmatched Other column gets none
    assert (seg["Automotive"]["name_ja"], seg["Financial Services"]["name_ja"], seg["Other"]["name_ja"]) == ("自動車事業", "金融事業", None)


def test_segments_sony_ifrs_sales_to_external_customers():
    status, rec = fd.segments(_cached_facts("S100YE2C"))  # Sony, IFRS: SalesToExternalCustomersIFRS / IntersegmentSalesIFRS
    assert status == "found"
    seg = _by_en(rec)
    assert seg["Game and Network Services"]["external_sales_jpy"] == 4_570_053_000_000
    assert seg["Game and Network Services"]["intersegment_sales_jpy"] == 115_598_000_000
    assert seg["Music"]["profit_jpy"] == 446_986_000_000
    assert seg["Other"]["kind"] == "other"
    # segment sales plus the reconciling item equal consolidated revenue
    assert sum(x["sales_jpy"] for x in rec["segments"]) + rec["adjustments"]["sales_jpy"] == 12_479_620_000_000
    assert rec["revenue_check"] == "match"


def test_segments_ifrs_total_revenue_only_and_single_segment_note():
    status, rec = fd.segments(_cached_facts("S100X6X6"))  # Fast Retailing: only RevenueIFRS / OperatingProfitLossIFRS tagged
    assert status == "found" and rec["revenue_check"] == "match"
    assert [x["sales_jpy"] for x in rec["segments"] if x["kind"] == "reportable"][:2] == [1_026_096_000_000, 1_910_289_000_000]
    # IFRS single-segment filers state it in the IFRS-named note block
    for doc_id in ("S100X6HK", "S100X7D0", "S100YARY"):
        status, rec = fd.segments(_cached_facts(doc_id))
        assert status == "single_segment" and rec["segments"] == []


def test_segments_japan_gaap_unchanged_and_adjustment_member_not_a_segment():
    status, rec = fd.segments(_cached_facts("S100X5JM"))  # Japan GAAP, Total-of-reportable-and-others member present
    assert status == "found" and rec["sum_check"] == "match" and rec["revenue_check"] == "match"
    assert [x["external_sales_jpy"] for x in rec["segments"]] == [6_045_035_000, 591_253_000]
    status, rec = fd.segments(_cached_facts("S100YEYZ"))
    assert not any(x["member"].endswith("AdjustmentMember") for x in rec["segments"])


def test_segments_us_gaap_without_tagged_table_stays_not_found():
    status, rec = fd.segments(_cached_facts("S100XTLJ"))  # US GAAP: segment table is text only
    assert status == "not_found" and rec["segments"] == [] and rec["warnings"] == ["us_gaap_segment_note_not_in_csv"]


def test_segment_japanese_names_have_no_header_or_merged_text():
    _s, rec = fd.segments(_cached_facts("S100YARD"))  # Sumitomo Corp: header cell glued to the first row, unit marks 人〕
    assert _by_en(rec)["Steel"]["name_ja"] == "鉄鋼" and _by_en(rec)["Automotive"]["name_ja"] == "自動車"
    _s, rec = fd.segments(_cached_facts("S100YB93"))  # Canon: two segments share one cell, so the merged text is dropped
    assert _by_en(rec)["Industry Business"]["name_ja"] is None
    _s, rec = fd.segments(_cached_facts("S100YGH5"))  # SoftBank Group: leading 報告セグメント header removed
    assert _by_en(rec)["Holding Company Investment Business"]["name_ja"] == "持株会社投資事業"


def test_toyota_english_names_all_marked_with_their_source():
    _s, rec = fd.segments(_cached_facts("S100Y8NY"))
    # every row has an English name for the English site; none is a translation guess
    assert {(x["name_en"], x["name_en_source"]) for x in rec["segments"]} == {
        ("Automotive", "member_id"), ("Financial Services", "member_id"), ("Other", "member_id")}


def test_name_en_fallback_is_marked_and_never_partial():
    member = "jpcrp030000-asr_E00541-000ReportableSegmentMember"  # no words in the member ID
    assert fd._name_en(member, "再生可能エネルギー", None, False) == ("Renewable Energy", "fallback")
    assert fd._name_en(member, "不動産事業", None, False) == ("Real Estate Business", "fallback")
    assert fd._name_en(member, "RV事業", None, False) == ("RV Business", "fallback")
    assert fd._name_en(member, "建設・不動産事業", None, False) == ("Construction & Real Estate Business", "fallback")
    assert fd._fallback_name_en("インダストリアル・プロダクツ") == "Industrial Products"  # one katakana term, not two
    # an unknown term anywhere means no English name at all, not a mixed one
    assert fd._name_en(member, "回転寿司事業", None, False) == (None, None)
    assert fd._name_en(member, None, None, False) == (None, None)
    assert fd._fallback_name_en("・事業") is None


def test_name_en_prefers_filer_label_then_member_id():
    member = "jpcrp030000-asr_E02144-000AutomotiveReportableSegmentMember"
    assert fd._name_en(member, "自動車事業", {member: "Automotive Business"}, False) == ("Automotive Business", "filer_label")
    assert fd._name_en(member, "自動車事業", {}, False) == ("Automotive", "member_id")
    assert fd._name_en("OtherReportableSegmentsMember", None, None, True) == ("Other", "member_id")


def test_placeholder_member_ids_are_not_names_and_digit_tokens_stay_whole():
    assert fd._member_name("jpcrp030000-asr_E00001-000Element001ReportableSegmentMember") is None
    assert fd._member_name("jpcrp030000-asr_E00001-000EREMENT02ReportableSegmentMember") is None
    assert fd._member_name("jpcrp030000-asr_E00001-000Test3ReportableSegmentMember") is None
    assert fd._name_en("jpcrp030000-asr_E00001-000Element001ReportableSegmentMember", "自動車関連事業", None, False) == \
        ("Automotive Related Business", "fallback")
    assert fd._member_name("jpcrp030000-asr_E00001-000D2COnlineShoppingBusinessReportableSegmentMember") == "D2C Online Shopping Business"
    assert fd._member_name("jpcrp030000-asr_E00001-000Web3SegmentReportableSegmentMember") == "Web3 Segment"
    assert fd._member_name("jpcrp030000-asr_E00001-000Industry40PropulsionBusinessReportableSegmentMember") == "Industry40 Propulsion Business"


def test_english_labels_read_from_label_linkbase():
    import io
    import zipfile
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase" xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:xml="http://www.w3.org/XML/1998/namespace">
 <link:labelLink xlink:type="extended" xlink:role="http://www.xbrl.org/2003/role/link">
  <link:loc xlink:type="locator" xlink:href="jpcrp030000-asr-001_E02144-000.xsd#jpcrp030000-asr_E02144-000_AutomotiveReportableSegmentMember" xlink:label="m1"/>
  <link:label xlink:type="resource" xlink:label="l1" xlink:role="http://www.xbrl.org/2003/role/verboseLabel" xml:lang="en">Automotive [member]</link:label>
  <link:label xlink:type="resource" xlink:label="l1" xlink:role="http://www.xbrl.org/2003/role/label" xml:lang="en">Automotive</link:label>
  <link:labelArc xlink:type="arc" xlink:from="m1" xlink:to="l1"/>
  <link:loc xlink:type="locator" xlink:href="x.xsd#jpcrp030000-asr_E02144-000_NetSales" xlink:label="m2"/>
  <link:label xlink:type="resource" xlink:label="l2" xlink:role="http://www.xbrl.org/2003/role/label" xml:lang="en">Net sales</link:label>
  <link:labelArc xlink:type="arc" xlink:from="m2" xlink:to="l2"/>
 </link:labelLink>
</link:linkbase>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("XBRL/PublicDoc/jpcrp030000-asr-001_E02144-000_lab-en.xml", xml)
        z.writestr("XBRL/PublicDoc/jpcrp030000-asr-001_E02144-000_lab.xml", xml.replace('xml:lang="en"', 'xml:lang="ja"'))
    labels = fd.english_labels(buf.getvalue())
    assert labels == {"jpcrp030000-asr_E02144-000AutomotiveReportableSegmentMember": "Automotive"}


def test_segment_total_rows_named_by_filer_are_not_segments():
    """S100Z2TG (4073) "CONSOLDATEDTOTALORCOMPANYWIDETOTAL" and S100YDV3 (8025) "Reportable Segments" are the totals."""
    for m in ("jpcrp030000-asr_E36666-000CONSOLDATEDTOTALORCOMPANYWIDETOTALMember",
              "jpcrp030000-asr_E02588-000ReportableSegmentsReportableSegmentsMember"):
        assert fd._member_role(m) == "total"
    assert fd._member_role("jpcrp030000-asr_E02588-000HomeFurnishingReportableSegmentsMember") == "segment"
    for name in ("Reportable Segments", "CONSOLDATEDTOTALORCOMPANYWIDETOTAL", "Consolidated total or company-wide total"):
        assert fd._not_a_segment({"name_en": name})
    assert not fd._not_a_segment({"name_en": "Total Pack Produce"})
    _s, rec = fd.segments(_cached_facts("S100Z2TG"))
    assert [x["name_en"] for x in rec["segments"]] == ["Payment Integration", "Payment Service"] and rec["sum_check"] == "match"
    _s, rec = fd.segments(_cached_facts("S100YDV3"))
    assert "Reportable Segments" not in [x["name_en"] for x in rec["segments"]] and rec["revenue_check"] == "match"


def _seg_facts(rows):
    facts = []
    for member, sales, external, profit, assets in rows:
        member = "jpcrp030000-asr_E00001-000" + member + "ReportableSegmentsMember"
        d, i = "CurrentYearDuration_" + member, "CurrentYearInstant_" + member
        facts += [fact("jppfs_cor:NetSales", sales, d, "売上高"),
                  fact("jppfs_cor:RevenuesFromExternalCustomers", external, d, "外部顧客への売上高"),
                  fact("jppfs_cor:SegmentProfitLoss", profit, d, "セグメント利益"),
                  fact("jppfs_cor:Assets", assets, i, "セグメント資産")]
    return facts


def test_subtotal_rows_move_to_segment_groups():
    m = "000000"
    facts = _seg_facts([("A", "100" + m, "90" + m, "10" + m, "50" + m), ("B", "201" + m, "200" + m, "-3" + m, "70" + m),
                        ("Group", "302" + m, "290" + m, "7" + m, "121" + m),  # rounding: off by one unit on two metrics
                        ("C", "40" + m, "40" + m, "4" + m, "9" + m)])
    status, rec = fd.segments(facts)
    assert [x["name_en"] for x in rec["segments"]] == ["A", "B", "C"]
    (g,) = rec["segment_groups"]
    assert g["name_en"] == "Group" and g["members"] == ["A", "B"] and g["sales_jpy"] == 302_000_000
    assert len(g["member_ids"]) == 2 and "kind" not in g


def test_subtotal_needs_two_agreeing_metrics_and_no_contradiction():
    m = "000000"
    # equal on sales only (other metrics differ): a coincidence, not a subtotal
    facts = _seg_facts([("A", "100" + m, "100" + m, "10" + m, "50" + m), ("B", "200" + m, "200" + m, "20" + m, "60" + m),
                        ("C", "300" + m, "300" + m, "99" + m, "999" + m)])
    assert fd.segments(facts)[1]["segment_groups"] == []
    # a zero row cannot turn a duplicate into a sum
    facts = _seg_facts([("A", "100" + m, "100" + m, "10" + m, "50" + m), ("Z", "0", "0", "0", "0"),
                        ("C", "100" + m, "100" + m, "10" + m, "50" + m)])
    _s, rec = fd.segments(facts)
    assert rec["segment_groups"] == [] and len(rec["segments"]) == 3


def test_cached_subtotal_segments_are_groups():
    _s, rec = fd.segments(_cached_facts("S100YHRI"))  # 7087: two 「フィールド」 subtotals
    assert {g["name_ja"]: g["members"] for g in rec["segment_groups"]} == {
        "人財系フィールド": ["製造請負・派遣事業", "技術者派遣事業"], "モノ・コトづくりフィールド": ["EMS事業", "社会サポート事業"]}
    assert rec["sum_check"] == "match" and rec["revenue_check"] == "match"
    _s, rec = fd.segments(_cached_facts("S100YHMT"))  # 1827: Construction precedes its two members
    assert [g["name_en"] for g in rec["segment_groups"]] == ["Construction", "Real Estate"] and rec["sum_check"] == "match"
    _s, rec = fd.segments(_cached_facts("S100YC72"))  # 5332: セラミック事業 sits between the members and the subtotal
    assert rec["segment_groups"][0]["members"] == ["日本住設事業", "米州事業", "アジア・オセアニア事業", "欧州事業", "中国大陸事業"]
    assert "セラミック事業" in [x["name_ja"] for x in rec["segments"]] and rec["sum_check"] == "match"
    _s, rec = fd.segments(_cached_facts("S100XUE5"))  # 8029
    assert [g["name_ja"] for g in rec["segment_groups"]] == ["アパレル関連事業計"] and rec["sum_check"] == "match"
    _s, rec = fd.segments(_cached_facts("S100Y8NY"))  # Toyota: nothing grouped
    assert rec["segment_groups"] == []


def _subtotal_case(rows):
    _s, rec = fd.segments(_seg_facts(rows))
    return {g["name_en"]: g["members"] for g in rec["segment_groups"]}


def test_subtotal_review_cases():
    m = "000000"
    # 1. coincidence on sales only (external = sales, nothing else stated), plain name: not enough to call it a subtotal
    assert _subtotal_case([("A", "100" + m, "100" + m, "", ""), ("B", "200" + m, "200" + m, "", ""),
                           ("C", "300" + m, "300" + m, "", ""), ("D", "50" + m, "50" + m, "", "")]) == {}
    # ...but the same figures under a grouping name are
    assert _subtotal_case([("A", "100" + m, "100" + m, "", ""), ("B", "200" + m, "200" + m, "", ""),
                           ("CTotal", "300" + m, "300" + m, "", ""), ("D", "50" + m, "50" + m, "", "")]) == {"C Total": ["A", "B"]}
    # 2. a row that is zero on every agreeing metric (assets only) is not a member
    assert _subtotal_case([("A", "100" + m, "90" + m, "10" + m, "50" + m), ("B", "200" + m, "190" + m, "20" + m, "60" + m),
                           ("Z", "0", "0", "0", "5" + m), ("C", "300" + m, "280" + m, "30" + m, "")]) == {"C": ["A", "B"]}
    # 3. a metric missing in a member: not counted, and present members exceeding the total rule the row out
    assert _subtotal_case([("A", "100" + m, "90" + m, "10" + m, "500" + m), ("B", "200" + m, "190" + m, "20" + m, ""),
                           ("C", "300" + m, "280" + m, "30" + m, "100" + m)]) == {}
    # 5. yen figures that happen to be multiples of 1,000: tiny sums within the tolerance are no evidence
    assert _subtotal_case([("A", "1000", "1000", "1000", "2000"), ("B", "2000", "1000", "1000", "1000"),
                           ("C", "4000", "3000", "1000", "2000")]) == {}
    # 4. rows with no figures inside the block are skipped; >= 2 rows with figures must remain
    assert _subtotal_case([("Group", "300" + m, "280" + m, "30" + m, "110" + m), ("A", "100" + m, "90" + m, "10" + m, "50" + m),
                           ("E", "", "", "", ""), ("B", "200" + m, "190" + m, "20" + m, "60" + m)]) == {"Group": ["A", "B"]}
    assert _subtotal_case([("A", "100" + m, "90" + m, "10" + m, "50" + m), ("E", "", "", "", ""),
                           ("C", "100" + m, "90" + m, "10" + m, "50" + m)]) == {}
    _s, rec = fd.segments(_cached_facts("S100YHHI"))  # 7745: America and Europe semiconductor rows are empty
    assert rec["segment_groups"][0]["members"] == ["Japan Semiconductor Related Business", "Asia and Oceania Semiconductor Related Business"]
    assert len(rec["segment_groups"]) == 3


def test_segment_names_swapped_back_by_note():
    def rows():
        return [{"member": "M", "kind": "reportable", "name_ja": "医療事業", "sales_jpy": 38_783_000_000},
                {"member": "D", "kind": "reportable", "name_ja": "計測事業", "sales_jpy": 47_176_000_000}]
    note = "NotesSegmentInformationEtcConsolidatedFinancialStatementsTextBlock"
    # one table per segment: 計測事業's stretch holds M's figure and the other way round, so the names were swapped
    per_segment = ("当連結会計年度(自 2025年4月1日 至 2026年3月31日) (単位:百万円) 計測事業 日本米州計 外部顧客への売上高"
                   "20,24118,54238,783 医療事業 日本米州計 外部顧客への売上高30,00017,17647,176 【関連情報】")
    r = rows()
    assert fd._swap_names_by_note([fact("jpcrp_cor:" + note, per_segment)], r) == [("M", "D")]
    assert [x["name_ja"] for x in r] == ["計測事業", "医療事業"]
    # all names in one header row: no evidence either way, nothing changes
    one_header = ("当連結会計年度(自 2025年4月1日 至 2026年3月31日) (単位:百万円) 医療事業計測事業計 外部顧客への売上高"
                  "38,78347,17685,959")
    r = rows()
    assert fd._swap_names_by_note([fact("jpcrp_cor:" + note, one_header)], r) == [] and r[0]["name_ja"] == "医療事業"
    _s, rec = fd.segments(_cached_facts("S100YHHI"))  # 7745: headcount tags on the wrong members
    groups = {g["name_en"]: g["name_ja"] for g in rec["segment_groups"]}
    assert groups["Measurement Weighing Instruments Businesses"] == "計測・計量機器事業"
    assert groups["Medical Healthcare Instruments Businesses"] == "医療・健康機器事業"
    assert "segment_names_ja_swapped_by_note" in rec["warnings"]
