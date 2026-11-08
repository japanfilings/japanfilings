import json
import sqlite3

from japanfilings import tender_offers


def facts(**values):
    return [{"element": "x:" + key, "label": key, "context": "FilingDateInstant", "unit": "", "value": value}
            for key, value in values.items()]


def test_parse_notification():
    parsed = tender_offers.parse(facts(
        FilerNameInJapaneseDEI="買付株式会社",
        NameOfSubjectCompanyTextBlock="対象株式会社 ",
        ClassesOfShareCertificatesEtcToAcquireByPurchaseEtcTextBlock="普通株式 ",
        PurposesOfPurchaseEtcTextBlock="(1) 本公開買付けの概要 対象者を完全子会社化することを目的とします。",
        OriginalPeriodAtFilingTextBlock="買付け等の期間 2026年4月7日から2026年5月22日まで",
        PriceOfPurchaseEtcTextBlock="株券 普通株式1株につき金1,900円 算定の基礎 2,100円",
        NumberOfShareCertificatesEtcIntendedToPurchaseTextBlock=(
            "買付予定数 買付予定数の下限 買付予定数の上限 "
            "1,485,897(株) 1,069,351(株) 1,485,897(株) (注1)")), "240")
    assert parsed["target_name_ja"] == "対象株式会社"
    assert parsed["price_jpy"] == 1900
    assert (parsed["period_start"], parsed["period_end"]) == ("2026-04-07", "2026-05-22")
    assert (parsed["planned_shares"], parsed["minimum_shares"], parsed["maximum_shares"]) == (1485897, 1069351, 1485897)
    assert parsed["parse_status"] == "parsed"
    assert len(parsed["purpose_quote_ja"]) < 200


def test_offeror_security_code_is_not_used_as_target_ticker():
    """S100Y4OS is filed by 5471 for target 5484; DEI's 54710 belongs to the offeror."""
    con = sqlite3.connect(":memory:")
    con.executescript(tender_offers.SCHEMA)
    parsed = tender_offers.parse(facts(
        FilerNameInJapaneseDEI="大同特殊鋼株式会社",
        SecurityCodeDEI="54710",
        NameOfSubjectCompanyTextBlock="1【対象者名】東北特殊鋼株式会社",
        PurposesOfPurchaseEtcTextBlock="対象者を完全子会社化することを目的とします。",
        OriginalPeriodAtFilingTextBlock="2026年5月18日から2026年6月29日まで",
        PriceOfPurchaseEtcTextBlock="普通株式1株につき金2,950円",
        NumberOfShareCertificatesEtcIntendedToPurchaseTextBlock="1,000株 500株 1,000株",
    ), "240")
    tender_offers.store(con, parsed, "S100Y4OS", "240", None, "2026-05-18 09:00", "E01239", "大同特殊鋼株式会社", "5484")
    ticker, record = con.execute("SELECT ticker, record FROM tender_offers").fetchone()
    assert ticker == "5484"
    assert json.loads(record)["target_name_ja"] == "東北特殊鋼株式会社"


def test_parse_result_and_never_derive_outcome():
    parsed = tender_offers.parse(facts(
        FilerNameInJapaneseDEI="買付株式会社",
        NameOfSubjectCompanyTextBlock="(1)【対象者名】対象株式会社",
        TenderOfferPeriodTextBlock="2026年4月27日から2026年5月28日まで",
        SuccessOrFailureOfTenderOfferTextBlock="応募数が下限以上となりましたので、応募株券等の全部の買付け等を行います。",
        NumberOfShareCertificatesEtcAcquiredByPurchaseEtcTextBlock=(
            "株券等の種類 株式に換算した応募数 株式に換算した買付数 "
            "株券929,650(株)624,100(株) 新株予約権証券―")), "270")
    assert parsed["target_name_ja"] == "対象株式会社"
    assert (parsed["tendered_shares"], parsed["purchased_shares"]) == (929650, 624100)
    assert "全部の買付け等を行います" in parsed["result_quote_ja"]
    assert "successful" not in parsed  # no model-created evaluation field


def test_parse_opinion_preserves_stated_words_and_null_limit():
    parsed = tender_offers.parse(facts(
        FilerNameInJapaneseDEI="対象株式会社",
        SecurityCodeDEI="12340",
        NameAndResidentialAddressOrLocationOfTenderOfferorTextBlock="名称 買付株式会社 所在地 東京都",
        ClassesOfShareCertificatesEtcForTenderOfferorToAcquireByPurchaseEtcTextBlock="普通株式",
        OverviewOfPurchaseEtcTextBlock=(
            "公開買付けの目的 完全子会社化 買付け等の期間2026年5月14日から2026年6月29日まで "
            "買付け等の価格 普通株式1株につき、金3,730円 買付予定数の下限13,465,700株 買付予定数の上限―株"),
        OpinionRegardingSaidTenderOfferAndBasisAndReasonsEtcTextBlock=(
            "(1)【意見の内容】当社は、本公開買付けに賛同の意見を表明するとともに、応募を推奨する旨を決議しました。"
            "(2)【意見の根拠及び理由】長い理由。")), "290")
    assert parsed["offeror_name_ja"] == "買付株式会社"
    assert parsed["target_ticker"] == "1234"
    assert parsed["price_jpy"] == 3730
    assert parsed["minimum_shares"] == 13465700
    assert parsed["maximum_shares"] is None
    assert "賛同の意見" in parsed["board_opinion_quote_ja"]
    assert "長い理由" not in parsed["board_opinion_quote_ja"]


def test_store_links_parent_and_includes_source():
    con = sqlite3.connect(":memory:")
    con.executescript(tender_offers.SCHEMA)
    parsed = tender_offers.parse(facts(
        FilerNameInJapaneseDEI="買付株式会社", NameOfSubjectCompanyTextBlock="対象株式会社",
        TenderOfferPeriodTextBlock="2026年4月1日から2026年5月1日まで",
        NumberOfShareCertificatesEtcAcquiredByPurchaseEtcTextBlock="株券100株100株 新株予約権証券―"), "270")
    tender_offers.store(con, parsed, "S200TEST", "270", "S100PARENT", "2026-05-02 09:00", "E00001", "買付株式会社", "1234")
    row = con.execute("SELECT related_offer_doc_id, source_url, record FROM tender_offers").fetchone()
    assert row[:2] == ("S100PARENT", "https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?S200TEST")
    assert json.loads(row[2])["doc_id"] == "S200TEST"


def test_deepseek_counterexamples():
    from japanfilings import tender_offers as t
    assert t._quote("2024年5月31日をもって終了").startswith("2024年")
    assert t._price("普通株式1株につき1,050円は終値であり、買付け等の価格は1株につき金1,200円である。") == 1200
    assert t._planned_numbers("普通株式 2,500,000株 買付予定数の下限 1,000,000株 買付予定数の上限 4,000,000株")[1:] == (1000000, 4000000)
    assert t._result_numbers("1株につき金1,200円 応募数(株) 2,500株 買付数(株) 2,000株") == (2500, 2000)
    assert t._offeror_from_opinion("公開買付者の名称及び所在地 名称 甲株式会社 所在地 東京") == "甲株式会社"


def _run_db():
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE issuers (edinet_code TEXT PRIMARY KEY, ticker TEXT, name_ja TEXT);
        CREATE TABLE filings (doc_id TEXT, doc_type_code TEXT, parent_doc_id TEXT, submit_datetime TEXT, edinet_code TEXT,
                              filer_name_ja TEXT, subject_edinet_code TEXT, withdrawn INTEGER, has_csv INTEGER, ticker TEXT);
        INSERT INTO issuers VALUES ('E01239', '5471', '大同特殊鋼'), ('E01245', '5484', '東北特殊鋼');
        -- 240 is filed by the offeror (E01239) about the target (E01245); the 290 is filed by the target about the offeror.
        INSERT INTO filings VALUES ('S100Y4OS', '240', NULL, '2026-05-18 09:00', 'E01239', '大同特殊鋼', 'E01245', 0, 1, NULL);
        INSERT INTO filings VALUES ('S100Y4SF', '290', 'S100Y4OS', '2026-05-19 09:00', 'E01245', '東北特殊鋼', 'E01239', 0, 1, NULL);
    """)
    return con


def test_run_resolves_target_ticker_for_opinion_filed_by_target(monkeypatch):
    """S100Y4SF (290) was stored as 5471, the offeror, because subject_edinet_code points at the offeror there."""
    fake = {
        "S100Y4OS": facts(FilerNameInJapaneseDEI="大同特殊鋼株式会社", NameOfSubjectCompanyTextBlock="東北特殊鋼株式会社"),
        "S100Y4SF": facts(FilerNameInJapaneseDEI="東北特殊鋼株式会社", SecurityCodeDEI="54840"),
    }
    monkeypatch.setattr(tender_offers, "_read_facts", lambda doc_id: fake[doc_id])
    con = _run_db()
    tender_offers.run(con)
    got = dict(con.execute("SELECT doc_id, ticker FROM tender_offers"))
    assert got == {"S100Y4OS": "5484", "S100Y4SF": "5484"}


def test_target_ticker_for_each_document_kind():
    assert tender_offers.target_ticker_for("240", "5484", "5471") == "5484"
    assert tender_offers.target_ticker_for("270", "5484", "5471") == "5484"
    assert tender_offers.target_ticker_for("290", "5471", "5484") == "5484"  # S100Y4SF
    assert tender_offers.target_ticker_for("290", "5471", None) is None  # never the offeror's


def test_270_without_target_block_keeps_filer_as_target_for_issuer_tender_offers():
    # S100Z0EY: the issuer buys its own shares, so the filer is the target (DeepSeek's "remove the fallback" made it null)
    facts = [{"element": "jptoi_cor:FilerNameInJapaneseDEI", "context": "x", "value": "アズワン株式会社", "label": ""}]
    assert tender_offers.parse(facts, "270")["target_name_ja"] == "アズワン株式会社"


def test_270_without_target_block_never_names_the_offeror_as_target():
    facts = [{"element": "jptoo-rto_cor:FilerNameInJapaneseDEI", "context": "x", "value": "買付株式会社", "label": ""}]
    parsed = tender_offers.parse(facts, "270")
    assert parsed["target_name_ja"] is None and "target_name_ja_not_found" in parsed["warnings"]


def test_issuer_notification_names_filer_as_target():
    """S100YVRY: an issuer tender offer (jptoi taxonomy) has no 対象者名 block."""
    f = [{"element": "jptoi_cor:" + k, "label": k, "context": "x", "value": v} for k, v in {
        "PurposesOfPurchaseEtcTextBlock": "自己株式の取得を目的とします。",
        "PeriodOfPurchaseEtcTextBlock": "2026年8月10日から2026年9月7日まで",
        "PriceOfPurchaseEtcTextBlock": "普通株式1株につき金2,151円"}.items()]
    f.append({"element": "jpdei_cor:FilerNameInJapaneseDEI", "label": "", "context": "x", "value": "アズワン株式会社"})
    parsed = tender_offers.parse(f, "240")
    assert parsed["issuer_tender_offer"] is True
    assert parsed["target_name_ja"] == parsed["offeror_name_ja"] == "アズワン株式会社"
    assert parsed["parse_status"] == "parsed"


def test_label_fallback_reads_renamed_elements():
    f = [{"element": "jptoo-ton_cor:OldName" + str(i), "label": label, "context": "x", "value": v}
         for i, (label, v) in enumerate({
             "対象者名 [テキストブロック]": "1【対象者名】対象株式会社",
             "買付け等の目的 [テキストブロック]": "完全子会社化を目的とします。",
             "買付け等の期間 [テキストブロック]": "2022年1月5日から2022年2月16日まで",
             "買付け等の価格 [テキストブロック]": "普通株式1株につき金800円"}.items())]
    f.append({"element": "jpdei_cor:FilerNameInJapaneseDEI", "label": "", "context": "x", "value": "買付株式会社"})
    parsed = tender_offers.parse(f, "240")
    assert (parsed["target_name_ja"], parsed["price_jpy"], parsed["period_end"]) == ("対象株式会社", 800, "2022-02-16")
    assert parsed["parse_status"] == "parsed"


def test_natural_person_offeror_is_flagged_for_masking():
    parsed = tender_offers.parse(facts(
        FilerNameInJapaneseDEI="甲木　剛志", NameOfSubjectCompanyTextBlock="対象株式会社",
        PurposesOfPurchaseEtcTextBlock="目的", OriginalPeriodAtFilingTextBlock="2025年1月6日から2025年2月3日まで",
        PriceOfPurchaseEtcTextBlock="普通株式1株につき金1,000円"), "240")
    assert parsed["offeror_name_ja"] == "甲木 剛志"  # kept (normalized) so the Worker can scrub every spelling
    assert parsed["offeror_is_individual"] is True and parsed["filer_is_individual"] is True
    opinion = tender_offers.parse(facts(
        FilerNameInJapaneseDEI="対象株式会社",
        NameAndResidentialAddressOrLocationOfTenderOfferorTextBlock="氏名 架 寛之 住所 東京都",
        OpinionRegardingSaidTenderOfferAndBasisAndReasonsEtcTextBlock="(1)【意見の内容】賛同します。"), "290")
    assert opinion["offeror_is_individual"] is True and opinion["filer_is_individual"] is False
    assert tender_offers.is_individual("株式会社甲、山田太郎") is True
    assert tender_offers.is_individual("堅果シナジー投資事業有限責任組合") is False


def test_withdrawal_is_a_row_linked_to_its_notification():
    parsed = tender_offers.parse([
        {"element": "jpdei_cor:FilerNameInJapaneseDEI", "label": "", "context": "x", "value": "ニデック株式会社"},
        {"element": "jptoo-wto_cor:X", "label": "対象者名 [テキストブロック]", "context": "x", "value": "対象株式会社"},
        {"element": "jptoo-wto_cor:Y", "label": "公開買付けの撤回等の理由 [テキストブロック]", "context": "x",
         "value": "本公開買付けを撤回することといたしました。"}], "260")
    assert parsed["document_kind"] == "withdrawal"
    assert "撤回" in parsed["withdrawal_quote_ja"]
    assert parsed["parse_status"] == "parsed"
    con = sqlite3.connect(":memory:")
    con.executescript(tender_offers.SCHEMA)
    tender_offers.store(con, parsed, "S100VPYE", "260", "S100VKMJ", "2025-05-09 09:04", "E01975", "ニデック株式会社", "6458")
    assert con.execute("SELECT related_offer_doc_id FROM tender_offers").fetchone() == ("S100VKMJ",)


def test_delisted_target_ticker_comes_from_its_own_filings(monkeypatch):
    con = _run_db()
    con.execute("UPDATE issuers SET ticker = NULL WHERE edinet_code = 'E01245'")  # delisted after the offer
    con.execute("INSERT INTO filings VALUES ('S100OLD', '120', NULL, '2025-06-20 09:00', 'E01245', '東北特殊鋼', NULL, 0, 1, '5484')")
    monkeypatch.setattr(tender_offers, "_read_facts", lambda doc_id: facts(FilerNameInJapaneseDEI="x株式会社"))
    tender_offers.run(con)
    got = dict(con.execute("SELECT doc_id, ticker FROM tender_offers"))
    assert got == {"S100Y4OS": "5484", "S100Y4SF": "5484"}


def test_coverage_counts_uncached_candidates(monkeypatch):
    con = _run_db()
    monkeypatch.setattr(tender_offers, "_cached", lambda doc_id: doc_id == "S100Y4OS")
    con.executescript(tender_offers.SCHEMA)
    cov = tender_offers.coverage(con)
    assert cov["240"] == {"with_csv": 1, "cached": 1, "not_cached": 0, "stored_parsed": 0, "stored_partial": 0}
    assert cov["290"]["not_cached"] == 1
    rows = tender_offers.candidates(con)
    assert {r[0] for r in rows} == {"S100Y4OS", "S100Y4SF"}


def test_planned_numbers_each_cell_independent_units_and_total_row():
    t = tender_offers
    # A dash in one limit keeps the other cells (labelled layout).
    assert t._planned_numbers("買付予定数 1,000株 買付予定数の下限 ―株 買付予定数の上限 800株") == (1000, None, 800)
    # REIT units (S100O1ZZ) and a box-drawing dash (S100TJQ8).
    assert t._planned_numbers("株券等の種類買付予定数買付予定数の下限買付予定数の上限投資口200,598(口)133,732(口)―(口)"
                              "合計200,598(口)133,732(口)―(口) (注1)") == (200598, 133732, None)
    assert t._planned_numbers("買付予定数買付予定数の下限買付予定数の上限21,557,194(株)14,372,200(株)─(株) (注1)") == (
        21557194, 14372200, None)
    # Several share classes: the limits are those of the 合計 row (S100UYAF).
    assert t._planned_numbers("株券等の種類買付予定数買付予定数の下限買付予定数の上限普通株式675,600(株)―(株)675,600(株)"
                              "新株予約権証券1,342,600(株)1,342,600(株)1,342,600(株)合計3,360,800(株)2,685,200(株)3,360,800(株)") == (
        3360800, 2685200, 3360800)
    assert t._result_numbers("株券等の種類投資口に換算した応募数投資口に換算した買付数投資証券146,962(口)146,962(口)"
                             "新投資口予約権証券――合計146,962146,962") == (146962, 146962)


def test_period_end_without_year():
    assert tender_offers._period("2024年6月10日(月曜日)から8月15日(木曜日)まで(47営業日)") == ("2024-06-10", "2024-08-15")
    assert tender_offers._period("2025年2月18日(火曜日)から同年4月15日(火曜日)まで") == ("2025-02-18", "2025-04-15")
    assert tender_offers._period("2025年12月18日から1月20日まで") == ("2025-12-18", "2026-01-20")


def test_old_format_result_period_element():
    parsed = tender_offers.parse(facts(FilerNameInJapaneseDEI="買付株式会社", NameOfSubjectCompanyTextBlock="対象株式会社",
                                       PeriodOfPurchaseEtcTextBlock="2020年1月6日から2020年2月3日まで"), "270")
    assert (parsed["period_start"], parsed["period_end"]) == ("2020-01-06", "2020-02-03")


def test_joint_offerors_from_cover_page_flag_a_person_without_keeping_name_or_address():
    f = facts(FilerNameInJapaneseDEI="甲株式会社", NameOfSubjectCompanyTextBlock="対象株式会社",
              FullNameOrNameOfFilerOfNotificationCoverPage="甲株式会社",
              FullNameOrNameOfFilerOfNotificationCoverPage2="乙投資事業有限責任組合無限責任組合員 丙株式会社代表取締役 山田 太郎",
              FullNameOrNameOfFilerOfNotificationCoverPage3="山田 花子",
              ResidentialAddressOrLocationOfFilerOfNotificationCoverPage3="東京都千代田区1-1")
    parsed = tender_offers.parse(f, "240")
    assert parsed["joint_offerors"] == [{"name_ja": "乙投資事業有限責任組合", "is_individual": False},
                                        {"name_ja": None, "is_individual": True}]
    assert parsed["offeror_is_individual"] is True
    dumped = json.dumps(parsed, ensure_ascii=False)
    assert "山田" not in dumped and "千代田" not in dumped


def test_amendments_are_linked_to_parent_with_warning(monkeypatch):
    con = _run_db()
    con.execute("INSERT INTO filings VALUES ('S100AMD1', '250', 'S100Y4OS', '2026-05-25 09:00', 'E01239', '大同特殊鋼', 'E01245', 0, 0, NULL)")
    con.execute("INSERT INTO filings VALUES ('S100AMD2', '300', 'S100Y4SF', '2026-05-26 09:00', 'E01245', '東北特殊鋼', 'E01239', 0, 0, NULL)")
    con.execute("INSERT INTO filings VALUES ('S100AMDX', '250', 'S100Y4OS', '2026-05-27 09:00', 'E01239', '大同特殊鋼', 'E01245', 1, 0, NULL)")
    monkeypatch.setattr(tender_offers, "_read_facts", lambda doc_id: facts(FilerNameInJapaneseDEI="x株式会社"))
    tender_offers.run(con)
    rec = {d: json.loads(r) for d, r in con.execute("SELECT doc_id, record FROM tender_offers")}
    assert [a["doc_id"] for a in rec["S100Y4OS"]["amendments"]] == ["S100AMD1"]
    assert [a["doc_id"] for a in rec["S100Y4SF"]["amendments"]] == ["S100AMD2"]
    assert "amended_later" in rec["S100Y4OS"]["warnings"]
    status = dict(con.execute("SELECT doc_id, parse_status FROM tender_offers"))
    tender_offers.link_amendments(con)  # idempotent
    assert json.loads(con.execute("SELECT record FROM tender_offers WHERE doc_id='S100Y4OS'").fetchone()[0])["warnings"].count("amended_later") == 1
    assert dict(con.execute("SELECT doc_id, parse_status FROM tender_offers")) == status


def test_reparse_reads_parsed_rows_again(monkeypatch):
    con = _run_db()
    monkeypatch.setattr(tender_offers, "_read_facts", lambda doc_id: facts(FilerNameInJapaneseDEI="x株式会社"))
    con.executescript(tender_offers.SCHEMA)
    con.execute("INSERT INTO tender_offers (doc_id, doc_type_code, parse_status, source_url, record) VALUES ('S100Y4OS', '240', 'parsed', '', '{}')")
    assert tender_offers.run(con)[0] == 1
    assert tender_offers.run(con, reparse=True)[0] == 2
