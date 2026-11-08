"""Checks against real EDINET filings. Expected values were verified independently
(public_data_business/SOURCE_VERIFICATION.json). PDFs are fetched once through the
EDINET API (needs EDINET_API_KEY) into .cache/."""
import os

import pytest

from japanfilings import edinet, largeholding, store
from japanfilings.textutil import parse_jp_date

CACHE = os.path.join(os.path.dirname(__file__), "..", ".cache")

EXPECTED = {
    "S100Z9CV": dict(report_kind="initial_report", issuer_ticker="9409", holding_ratio_pct=5.06,
                     previous_ratio_pct=None, obligation_date="2026-10-15", submit_date="2026-10-22", holders_count=2),
    "S100Z9GY": dict(report_kind="change_report", issuer_ticker="4042", holding_ratio_pct=6.99,
                     previous_ratio_pct=5.98, ratio_change_pt=1.01, obligation_date="2026-10-21"),
    "S100Z9I7": dict(report_kind="amendment", issuer_ticker="648A", holding_ratio_pct=40.0),
}


def _text(doc_id):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{doc_id}.pdf")
    if not os.path.exists(path):
        if not os.environ.get("EDINET_API_KEY"):
            pytest.skip("EDINET_API_KEY not set")
        with open(path, "wb") as f:
            f.write(edinet.download_pdf(doc_id))
    return largeholding.pdf_to_text(path)


@pytest.mark.parametrize("doc_id", EXPECTED)
def test_parse_real_filing(doc_id):
    rec = largeholding.parse_text(_text(doc_id), doc_id)
    for k, v in EXPECTED[doc_id].items():
        assert rec[k] == v, k


def test_era_dates():
    assert parse_jp_date("令和8年10月15日") == "2026-10-15"
    assert parse_jp_date("令和元年5月1日") == "2019-05-01"
    assert parse_jp_date("２０２６年１０月２２日") == "2026-10-22"
    assert parse_jp_date("該当事項なし") is None


def test_link_new_listing_without_ticker(tmp_path):
    con = store.connect(str(tmp_path / "t.db"))
    store.upsert_issuers(con, [dict(edinet_code="E42147", ticker=None, corporate_number="1", name_ja="株式会社ルクレ",
                                    name_en="LECRE Inc.", listed=True, industry_ja="", fiscal_year_end="",
                                    capital_million_jpy=None, filer_type="")])
    rec = store.link_issuer(con, {"issuer_ticker": "648A", "issuer_name_ja": "株式会社ルクレ"})
    assert rec["issuer_name_en"] == "LECRE Inc."
    assert store.get_issuer(con, "648a")["edinet_code"] == "E42147"


def test_xbrl_prefers_joint_total_over_holders():
    from japanfilings.xbrlcsv import large_holding_ratios
    m = "FilingDateInstant_jplvh010000-lvh_E38633-000FilerLargeVolumeHolder{}Member"
    facts = [
        {"label": "株券等保有割合", "context": m.format(1), "value": "0.3033"},
        {"label": "直前の報告書に記載された株券等保有割合", "context": m.format(1), "value": "0.1956"},
        {"label": "株券等保有割合", "context": "FilingDateInstant", "value": "0.3823"},
        {"label": "直前の報告書に記載された株券等保有割合", "context": "FilingDateInstant", "value": "0.3822"},
        {"label": "株券等保有割合", "context": m.format(5), "value": "0.0506"},
        {"label": "欄外注記、株券等保有割合 [テキストブロック]", "context": "FilingDateInstant", "value": "－"},
    ]
    assert large_holding_ratios(facts) == {"holding_ratio_pct": 38.23, "previous_ratio_pct": 38.22}


def test_xbrl_single_holder_without_total_context():
    from japanfilings.xbrlcsv import large_holding_ratios
    facts = [{"label": "株券等保有割合", "context": "FilingDateInstant_x_FilerLargeVolumeHolder1Member", "value": "0.0699"}]
    assert large_holding_ratios(facts)["holding_ratio_pct"] == 6.99


def test_xbrl_english_filer_is_first_joint_holder_not_last():
    from japanfilings.xbrlcsv import large_holding_holders
    facts = [
        {"element": "jplvh_cor:FilerNameInEnglishDEI", "label": "", "context": "FilingDateInstant_x_FilerLargeVolumeHolder1Member", "value": "Submitter Ltd."},
        {"element": "jplvh_cor:FilerNameInEnglishDEI", "label": "", "context": "FilingDateInstant_x_FilerLargeVolumeHolder2Member", "value": "Joint Holder Ltd."},
    ]
    assert large_holding_holders(facts)["filer_name_en"] == "Submitter Ltd."


def test_byok_valuation_math():
    from japanfilings.byok import valuation
    report = {"fiscal_year_end": "2026-07-31",
              "key_metrics": [{"eps": 50.0, "bps": 500.0, "shares_issued": 1_000_000}],
              "company": {"issued_shares": 1_000_000, "treasury_shares": 100_000}}
    v = valuation(1000.0, report)
    assert v["market_cap_jpy"] == 900_000_000
    assert v["per"] == 20.0 and v["pbr"] == 2.0
    assert valuation(1000.0, {"key_metrics": [{"eps": -5.0, "bps": 0}], "company": {}})["per"] is None


def test_annual_report_parse_real_file():
    import os
    from japanfilings import annual, xbrlcsv
    path = os.path.join(os.path.dirname(__file__), "..", ".cache", "S100ZASH_csv.zip")
    if not os.path.exists(path):
        pytest.skip("cached annual report not present")
    r = annual.parse(xbrlcsv.read_facts(open(path, "rb").read()), "S100ZASH")
    latest = r["key_metrics"][0]
    assert (r["ticker"], r["fiscal_year_end"]) == ("3930", "2026-07-31")
    assert latest["revenue"] == 3647171000 and latest["employees"] == 213
    assert latest["roe"] == -42.8 and latest["equity_ratio"] == 71.6
    assert r["statements"]["income_statement"]["current"]["NetSales"] == 3647171000
    assert len(r["company"]["officers"]) == 8
    assert all("address" not in s for s in r["company"]["major_shareholders"])


def test_nport_summary_groups_by_ticker_and_ranks_by_value():
    from japanfilings.nport import summarize
    base = {"issuer_name": "x", "issuer_lei": None, "registrant": "R", "series_id": "S", "accession": "A", "ticker_reported": None}
    h = [dict(base, isin="JP3633400001", fund="Small", shares=10.0, value_usd=100.0, pct_of_fund=0.1, report_date="2026-06-30"),
         dict(base, isin="JP3633400001", fund="Big", shares=50.0, value_usd=900.0, pct_of_fund=0.5, report_date="2026-07-31"),
         dict(base, isin="JP0000000000", fund="Unmapped", shares=1.0, value_usd=1.0, pct_of_fund=0.0, report_date="2026-07-31")]
    s = summarize(h, {"JP3633400001": "7203", "JP0000000000": None}, "2026q3")
    assert list(s) == ["7203"]
    t = s["7203"]
    assert (t["fund_count"], t["total_shares"], t["total_value_usd"], t["latest_report_date"]) == (2, 60.0, 1000.0, "2026-07-31")
    assert [f["fund"] for f in t["top_funds"]] == ["Big", "Small"]


def test_xbrl_sums_holders_when_no_total_context():
    from japanfilings.xbrlcsv import large_holding_ratios
    m = "FilingDateInstant_x_E09096-000FilerLargeVolumeHolder{}Member"
    cur = [0.0131, 0, 0, 0.0002, 0.0008, 0, 0, 0, 0.0003, 0.0004]
    prev = [0.0134, 0.0037, 0.0014, 0.0012, 0.0011, 0.0075, 0.0064, 0.0211, 0.0103, 0.0014]
    facts = []
    for i, (c, p) in enumerate(zip(cur, prev), 1):
        facts.append({"label": "株券等保有割合", "context": m.format(i), "value": str(c)})
        facts.append({"label": "直前の報告書に記載された株券等保有割合", "context": m.format(i), "value": str(p)})
    assert large_holding_ratios(facts) == {"holding_ratio_pct": 1.48, "previous_ratio_pct": 6.75}


def _link_db():
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE issuers (edinet_code TEXT PRIMARY KEY, ticker TEXT, corporate_number TEXT, name_ja TEXT, name_en TEXT, listed INTEGER);
        CREATE TABLE filings (doc_id TEXT PRIMARY KEY, issuer_edinet_code TEXT, description_ja TEXT);
        INSERT INTO issuers VALUES ('E02848', '7501', NULL, '株式会社ティムコ', 'TIEMCO', 1), ('E37432', '9215', NULL, '株式会社ＣａＳｙ', 'CaSy', 1),
          ('E05673', '3037', NULL, '株式会社ＤＤグループ', 'DD Group', 1), ('E31575', '4450', NULL, '株式会社パワーソリューションズ', 'PS', 1),
          ('E35137', '6239', NULL, '株式会社ナガオカ', 'Nagaoka', 1), ('E01569', '6366', NULL, '千代田化工建設株式会社', 'Chiyoda', 1),
          ('E02529', '8058', NULL, '三菱商事株式会社', 'MC', 1), ('E38722', '387A', NULL, 'フラー株式会社', 'Fuller', 1);
        INSERT INTO filings VALUES ('S100Y1OM', 'E02848', '変更報告書'), ('S100WVR6', 'E37432', '変更報告書'), ('S100WORW', NULL, '変更報告書'),
          ('S100WSLD', 'E35137', '変更報告書'), ('S100Y05Q', 'E02529', '変更報告書'), ('S100WGRY', 'E38722', '変更報告書');
    """)
    return con


def _linked(con, doc, ticker, name, kind="change_report"):
    from japanfilings import store
    return store.link_issuer(con, {"doc_id": doc, "issuer_ticker": ticker, "issuer_name_ja": name, "report_kind": kind, "filer_edinet_code": "E9"})


def test_link_issuer_corrects_mistyped_ticker_from_edinet_code():
    con = _link_db()
    rec = _linked(con, "S100Y1OM", "7051", "株式会社ティムコ")  # also S100Y1IB, S100XXXN: 7051 typed for 7501
    assert (rec["issuer_ticker"], rec["issuer_ticker_filed"], rec["issuer_match"], rec["issuer_edinet_code"]) == ("7501", "7051", "edinet_code", "E02848")
    rec = _linked(con, "S100WVR6", "CaSy", "株式会社CaSy")
    assert (rec["issuer_ticker"], rec["issuer_ticker_filed"]) == ("9215", "CaSy")
    rec = _linked(con, "S100WGRY", "5583", "フラー株式会社")
    assert rec["issuer_ticker"] == "387A"
    # Same result without EDINET's code: the typed ticker is not in the code list, the name is.
    rec = _linked(con, "S100WORW", "3073", "株式会社DDグループ")
    assert (rec["issuer_ticker"], rec["issuer_match"]) == ("3037", "name")


def test_link_issuer_trusts_edinet_code_over_a_valid_but_wrong_ticker():
    rec = _linked(_link_db(), "S100WSLD", "4450", "株式会社ナガオカ")  # 4450 is a real company, the filer meant 6239
    assert (rec["issuer_ticker"], rec["issuer_ticker_filed"], rec["issuer_edinet_code"]) == ("6239", "4450", "E35137")


def test_link_issuer_marks_conflict_instead_of_guessing():
    # EDINET's code (三菱商事) agrees with neither the typed ticker nor the typed name (千代田化工建設): keep as filed.
    rec = _linked(_link_db(), "S100Y05Q", "6366", "千代田化工建設株式会社")
    assert rec["issuer_ticker"] == "6366" and "issuer_ticker_filed" not in rec
    assert rec["issuer_match"] == "metadata_conflict" and rec["issuer_edinet_code"] == "E01569"
    # Two different companies named by ticker and name, no EDINET code: unresolved, ticker left as filed.
    rec = _linked(_link_db(), "S100NONE", "6366", "株式会社ナガオカ")
    assert (rec["issuer_ticker"], rec["issuer_match"]) == ("6366", "ticker_name_mismatch")


def test_link_issuer_change_report_with_initial_cover_title():
    """111 documents whose cover reads 大量保有報告書 but which EDINET lists as 変更報告書 were stored as initial reports."""
    con = _link_db()
    con.execute("INSERT INTO filings VALUES ('S100VP2J', NULL, '変更報告書')")
    assert _linked(con, "S100VP2J", "7501", "株式会社ティムコ", kind="initial_report")["report_kind"] == "change_report"
    con.execute("INSERT INTO filings VALUES ('S100NEW1', NULL, '大量保有報告書')")
    assert _linked(con, "S100NEW1", "7501", "株式会社ティムコ", kind="initial_report")["report_kind"] == "initial_report"


def test_record_warnings_flag_filing_errors_without_changing_values():
    from japanfilings import largeholding as lh
    base = {"report_kind": "change_report", "submit_date": "2026-10-01", "obligation_date": "2026-09-28",
            "filer_edinet_code": "E1", "issuer_edinet_code": "E2", "holding_ratio_pct": 5.2, "pdf_ratio_pct": 5.2}
    assert lh.record_warnings(base) == []
    assert lh.record_warnings({**base, "obligation_date": "2027-10-01"}) == ["obligation_date_after_submit_date"]  # S100Z6EL
    assert lh.record_warnings({**base, "obligation_date": "1995-08-15"}) == ["obligation_date_implausibly_old"]  # S100Y7ZU
    assert lh.record_warnings({**base, "report_kind": "amendment", "obligation_date": "2019-01-04"}) == []  # a correction cites the original date
    assert lh.record_warnings({**base, "filer_edinet_code": "E2"}) == ["filed_by_issuer"]  # S100ZA48, S100XA9E
    assert lh.record_warnings({**base, "holding_ratio_pct": 1.48, "pdf_ratio_pct": 0.04}) == ["pdf_ratio_differs_from_xbrl"]  # S100Z90M
    assert lh.record_warnings({"obligation_date": None}) == []


# Real context shapes from S100VPZJ (3952): Toyota files, Nikkon Holdings is JointHolder1.
_VPZJ = "FilingDateInstant_jplvh010000-lvh_E02144-000{}Member"
_VPZJ_FACTS = [
    {"element": "jplvh_cor:FilerNameInEnglishDEI", "label": "", "context": _VPZJ.format("FilerLargeVolumeHolder1"), "value": "TOYOTA MOTOR CORPORATION"},
    {"element": "jplvh_cor:FilerNameInEnglishDEI", "label": "", "context": _VPZJ.format("JointHolder1"), "value": "NIKKON Holdings Co., Ltd."},
    {"element": "jplvh_cor:IndividualOrCorporation", "label": "個人・法人の別", "context": _VPZJ.format("FilerLargeVolumeHolder1"), "value": "法人（株式会社）"},
    {"element": "jplvh_cor:Name", "label": "氏名又は名称", "context": _VPZJ.format("FilerLargeVolumeHolder1"), "value": "トヨタ自動車株式会社"},
    {"element": "jplvh_cor:IndividualOrCorporation", "label": "個人・法人の別", "context": _VPZJ.format("JointHolder1"), "value": "法人（株式会社）"},
    {"element": "jplvh_cor:Name", "label": "氏名又は名称", "context": _VPZJ.format("JointHolder1"), "value": "ニッコンホールディングス株式会社"},
    {"element": "jplvh_cor:HoldingRatioOfShareCertificatesEtc", "label": "株券等保有割合", "context": _VPZJ.format("FilerLargeVolumeHolder1"), "value": "0.2297"},
    {"element": "jplvh_cor:HoldingRatioOfShareCertificatesEtc", "label": "株券等保有割合", "context": _VPZJ.format("JointHolder1"), "value": "0.6768"},
    {"element": "jplvh_cor:HoldingRatioOfShareCertificatesEtc", "label": "株券等保有割合", "context": "FilingDateInstant", "value": "0.9064"},
]


def test_xbrl_holders_include_joint_holders():
    from japanfilings.xbrlcsv import large_holding_holders
    out = large_holding_holders(_VPZJ_FACTS)
    assert [(h["name"], h["ratio_pct"], h["holder_type"], h["holder_role"]) for h in out["holders"]] == [
        ("ニッコンホールディングス株式会社", 67.68, "corporation", "joint_holder"),
        ("トヨタ自動車株式会社", 22.97, "corporation", "filer"),
    ]
    assert out["filer_name_en"] == "TOYOTA MOTOR CORPORATION"  # the joint holder's DEI copy is not the filer


def test_xbrl_holder_sum_counts_joint_holders_when_no_total():
    from japanfilings.xbrlcsv import large_holding_ratios
    facts = [f for f in _VPZJ_FACTS if f["context"] != "FilingDateInstant"]
    assert large_holding_ratios(facts)["holding_ratio_pct"] == 90.65  # 22.97 + 67.68


def test_xbrl_holders_two_filers_and_joint_holder():
    """S100Z9WG shape: FilerLargeVolumeHolder1/2 plus JointHolder1 (BCJ-110, 63.80%); filer 1 holds 0%."""
    from japanfilings.xbrlcsv import large_holding_holders
    c = "FilingDateInstant_jplvh010000-lvh_E34368-000{}Member"
    facts = []
    for member, name, kind, ratio in [("FilerLargeVolumeHolder1", "架空川　勝敏", "個人", "0.0000"),
                                      ("FilerLargeVolumeHolder2", "株式会社ＫＡＭ", "法人（株式会社）", "0.0705"),
                                      ("JointHolder1", "株式会社BCJ-110", "法人（株式会社）", "0.6380")]:
        facts += [{"element": "jplvh_cor:Name", "label": "", "context": c.format(member), "value": name},
                  {"element": "jplvh_cor:IndividualOrCorporation", "label": "", "context": c.format(member), "value": kind},
                  {"element": "jplvh_cor:HoldingRatioOfShareCertificatesEtc", "label": "", "context": c.format(member), "value": ratio}]
    hs = large_holding_holders(facts)["holders"]
    assert [(h["name"], h["ratio_pct"], h["holder_type"], h["holder_role"]) for h in hs] == [
        ("株式会社BCJ-110", 63.8, "corporation", "joint_holder"),
        ("株式会社KAM", 7.05, "corporation", "filer"),
        ("架空川 勝敏", 0.0, "individual", "filer"),
    ]


@pytest.mark.parametrize("doc_id,count,top", [("S100VPZJ", 2, ("ニッコンホールディングス株式会社", 67.68)),
                                              ("S100Z9WG", 3, ("株式会社BCJ-110", 63.8))])
def test_xbrl_joint_holders_real_cached_csv(doc_id, count, top):
    from japanfilings import xbrlcsv
    path = os.path.join(edinet.CSV_CACHE, f"{doc_id}.zip")
    if not os.path.exists(path):
        pytest.skip("cached 5% report CSV not present")
    hs = xbrlcsv.large_holding_holders(xbrlcsv.read_facts(open(path, "rb").read()))["holders"]
    assert len(hs) == count and (hs[0]["name"], hs[0]["ratio_pct"]) == top


def test_fix_script_restores_issuer_ticker_from_own_filings():
    """DD Group (E05673): code list has no ticker (delisted), a 5% report typo (3037) was learned; its own filings say 3073."""
    import importlib.util
    import sqlite3
    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "fix_data_check_20261102.py")
    spec = importlib.util.spec_from_file_location("fix_20261102", path)
    fix = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fix)
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE issuers (edinet_code TEXT PRIMARY KEY, ticker TEXT, listed INTEGER)")
    con.execute("CREATE TABLE filings (doc_id TEXT PRIMARY KEY, edinet_code TEXT, ticker TEXT)")
    con.executemany("INSERT INTO issuers VALUES (?,?,?)", [("E05673", "3037", 0), ("E42126", "646A", 1), ("E00001", "1111", 0)])
    con.executemany("INSERT INTO filings VALUES (?,?,?)", [("S100WD4P", "E05673", "3073"), ("S100WI2K", "E05673", "3073"),
                                                           ("A", "E42126", "0000"), ("B", "E00001", "1111"), ("C", "E00001", None)])
    assert fix.wrong_learned_tickers(con) == [("E05673", "3037", "3073")]
    assert fix.step_issuer_tickers(con) == (1, 0)
    assert con.execute("SELECT ticker FROM issuers WHERE edinet_code = 'E05673'").fetchone()[0] == "3073"
    assert fix.wrong_learned_tickers(con) == []


def _link_db2():
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE issuers (edinet_code TEXT PRIMARY KEY, ticker TEXT, corporate_number TEXT, name_ja TEXT, name_en TEXT, listed INTEGER);
        CREATE TABLE filings (doc_id TEXT PRIMARY KEY, edinet_code TEXT, ticker TEXT, issuer_edinet_code TEXT, description_ja TEXT);
        INSERT INTO issuers VALUES ('E05673', NULL, NULL, '株式会社ＤＤグループ', 'DD Group', 0),
          ('E02245', '3434', NULL, '株式会社アルファ', 'Alpha Corp', 1), ('E05083', '4760', NULL, '株式会社アルファ', 'Alpha', 1),
          ('E05181', '9446', NULL, '株式会社サカイホールディングス', 'Sakai HD', 1), ('E26689', NULL, NULL, '株式会社サカイ', NULL, 0),
          ('E31425', NULL, NULL, 'ＧＭＯデザインワン株式会社', 'GMO Design One', 0),
          ('E99001', NULL, NULL, '株式会社惻隠坊', NULL, 0), ('E42000', NULL, NULL, '株式会社ニュー', 'New', 1),
          ('E42001', NULL, NULL, '株式会社オールド', 'Old', 0);
        INSERT INTO filings VALUES ('OWN1', 'E05673', '3073', NULL, '意見表明報告書'), ('OWN2', 'E05673', '3073', NULL, '大量保有報告書'),
          ('A1', 'E9', NULL, 'E05083', '変更報告書'), ('S1', 'E9', NULL, 'E26689', '変更報告書'), ('R1', 'E9', NULL, 'E31425', '変更報告書'),
          ('F1', 'E99001', NULL, 'E99001', '大量保有報告書');
    """)
    return con


def test_link_issuer_learns_a_delisted_ticker_from_own_filings_and_uses_it_at_once():
    """DD Group (E05673): delisted, no ticker in the code list, its own filings carry 3073. UBS typed 3037."""
    con = _link_db2()
    rec = _linked(con, "X1", "3037", "株式会社DDグループ")
    assert (rec["issuer_ticker"], rec["issuer_ticker_filed"], rec["issuer_match"]) == ("3073", "3037", "name")
    assert con.execute("SELECT ticker FROM issuers WHERE edinet_code = 'E05673'").fetchone()[0] == "3073"
    rec = _linked(con, "X2", "3073", "株式会社DDグループ")
    assert rec["issuer_ticker"] == "3073" and "issuer_ticker_filed" not in rec


def test_link_issuer_never_learns_a_typed_ticker_for_a_delisted_company():
    con = _link_db2()
    rec = _linked(con, "X1", "9999", "株式会社オールド")
    assert rec["issuer_ticker"] == "9999" and rec["issuer_edinet_code"] == "E42001"
    assert con.execute("SELECT ticker FROM issuers WHERE edinet_code = 'E42001'").fetchone()[0] is None
    rec = _linked(con, "X2", "999A", "株式会社ニュー")  # a new listing the code list has no ticker for yet
    assert con.execute("SELECT ticker FROM issuers WHERE edinet_code = 'E42000'").fetchone()[0] == "999A"


def test_link_issuer_same_name_companies_and_subsidiary_codes():
    con = _link_db2()
    # EDINET's code names the other 株式会社アルファ (4760); the typed ticker 3434 fits the name as well: not replaced.
    rec = _linked(con, "A1", "3434", "株式会社アルファ")
    assert (rec["issuer_ticker"], rec["issuer_match"], rec["issuer_edinet_code"]) == ("3434", "metadata_conflict", "E02245")
    # No ticker, two listed companies of that name: no guess.
    rec = _linked(con, "NONE", None, "株式会社アルファ")
    assert rec["issuer_edinet_code"] is None and rec["issuer_match"] == "unresolved"
    # EDINET's code is the unlisted subsidiary 株式会社サカイ; ticker and exact name say the holding company.
    rec = _linked(con, "S1", "9446", "株式会社サカイホールディングス")
    assert rec["issuer_edinet_code"] == "E05181" and "issuer_match" not in rec


def test_link_issuer_takes_edinet_code_of_a_company_renamed_after_delisting_but_not_the_filers_own():
    con = _link_db2()
    rec = _linked(con, "R1", "6048", "株式会社デザインワン・ジャパン")
    assert (rec["issuer_ticker"], rec["issuer_match"], rec["issuer_edinet_code"]) == ("6048", "edinet_code_renamed", "E31425")
    from japanfilings import store
    rec = store.link_issuer(con, {"doc_id": "F1", "issuer_ticker": "638A", "issuer_name_ja": "ハイネスコーポレーション株式会社",
                                  "report_kind": "initial_report", "filer_edinet_code": "E99001"})
    assert rec["issuer_edinet_code"] is None and rec["issuer_match"] == "metadata_conflict"


def _link_db3():
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE issuers (edinet_code TEXT PRIMARY KEY, ticker TEXT, corporate_number TEXT, name_ja TEXT, name_en TEXT, listed INTEGER);
        CREATE TABLE filings (doc_id TEXT PRIMARY KEY, edinet_code TEXT, ticker TEXT, issuer_edinet_code TEXT, description_ja TEXT);
        INSERT INTO issuers VALUES ('E001', NULL, NULL, '株式会社エーワン', 'A', 0), ('E002', '2222', NULL, '株式会社ビーツー', 'B', 1),
          ('E010', '7501', NULL, '株式会社シーテン', 'C', 1), ('E011', NULL, NULL, '株式会社ディーイレブン', 'D', 0),
          ('E030', NULL, NULL, '株式会社イーサーティ', 'E', 0), ('E040', NULL, NULL, '株式会社エフフォーティ', 'F', 1);
        INSERT INTO filings VALUES ('M1', 'E9', NULL, 'E001', '変更報告書'), ('M2', 'E9', NULL, 'E010', '変更報告書'),
          ('O1', 'E030', '1111', NULL, '有価証券報告書'), ('O2', 'E030', '2222', NULL, '有価証券報告書');
    """)
    return con


def test_link_issuer_review_hypotheses():
    """Desk-review cases (2026-11): a missing typed ticker, conflicts, ambiguous own tickers, the 0000 placeholder."""
    con = _link_db3()
    # No ticker typed, and the typed name is another company's: EDINET's code is not taken on None == None.
    rec = _linked(con, "M1", None, "株式会社ビーツー")
    assert rec["issuer_edinet_code"] != "E001" and rec["issuer_match"] != "edinet_code"
    # EDINET's code agrees with nothing; the typed name is another company: nothing is learned for it.
    rec = _linked(con, "M2", "9999", "株式会社ディーイレブン")
    assert rec["issuer_match"] == "metadata_conflict" and rec["issuer_ticker"] == "9999"
    assert con.execute("SELECT ticker FROM issuers WHERE edinet_code = 'E011'").fetchone()[0] is None
    # Two tickers in the company's own filings: neither is learned.
    _linked(con, "X1", "2222", "株式会社イーサーティ")
    assert con.execute("SELECT ticker FROM issuers WHERE edinet_code = 'E030'").fetchone()[0] is None
    # 0000 is EDINET's placeholder, never a ticker.
    rec = _linked(con, "X2", "0000", "株式会社エフフォーティ")
    assert con.execute("SELECT ticker FROM issuers WHERE edinet_code = 'E040'").fetchone()[0] is None
