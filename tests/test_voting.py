import sqlite3
from pathlib import Path

from japanfilings import voting, xbrlcsv

CACHE = Path(__file__).parents[1] / "data" / "csv_cache"


def fact(element, value):
    return {"element": "jpcrp_cor:" + element, "value": value}


def test_parse_rejects_other_extraordinary_report():
    assert voting.parse([fact("ReasonForFilingTextBlock", "主要株主に異動がありました。")]) == (False, None)


def test_parse_identifies_article_but_reports_missing_result_block():
    is_voting, parsed = voting.parse([
        fact("ReasonForFilingTextBlock", "企業内容等の開示に関する内閣府令第19条第2項第9号の2に基づくものです。")
    ])
    assert is_voting
    assert parsed == {"meeting_date": None, "rows": [], "warnings": ["block_missing"]}


def test_parse_numbered_and_candidate_rows_with_joined_cells():
    text = """
    (1) 株主総会が開催された年月日 2026年6月23日
    (2) 決議事項の内容
    第1号議案 剰余金の処分の件
    第2号議案 取締役2名選任の件
    (3) 決議事項に対する賛成、反対及び棄権の意思の表示に係る議決権の数
    決議事項 賛成数(個) 反対数(個) 棄権数(個) 可決要件 決議の結果及び賛成割合(%)
    第1号議案 剰余金の処分の件 39,515200 (注)1 可決97.26
    第2号議案 取締役2名選任の件 (注)2
    山田 太郎39,4131220 可決97.00
    鈴木 花子39,438970 可決97.07
    (4) 株主総会に出席した株主の議決権の数の一部を加算しなかった理由
    """
    parsed = voting.parse_text(text)
    assert parsed["meeting_date"] == "2026-06-23"
    assert parsed["warnings"] == []
    assert [(r["votes_for"], r["votes_against"], r["votes_abstain"]) for r in parsed["rows"]] == [
        (39515, 20, 0), (39413, 122, 0), (39438, 97, 0)]
    assert parsed["rows"][1]["proposal_no"] == 2
    assert parsed["rows"][1]["proposal_title_ja"] == "取締役2名選任の件"
    assert parsed["rows"][1]["candidate_ja"] == "山田 太郎"


def test_parse_failed_resolution_and_dashes_are_not_imputed_as_zero():
    text = """
    (1) 株主総会が開催された年月日2026年6月30日
    (2) 決議事項の内容 第1号議案 臨時配当の件
    (3) 決議事項に対する賛成、反対及び棄権の意思の表示に係る議決権の数
    決議事項 賛成(個) 反対(個) 棄権(個) 可決要件 決議の結果及び賛成割合(%)
    第1号議案 臨時配当の件 3,875 261,083 - (注)1 否決1.46
    (4) 議決権の数の一部を加算しなかった理由
    """
    row = voting.parse_text(text)["rows"][0]
    assert (row["votes_for"], row["votes_against"], row["votes_abstain"]) == (3875, 261083, None)
    assert row["stated_rate_pct"] == 1.46
    assert row["result"] == "failed"




def test_parse_uncommaed_four_digit_count_does_not_choose_wrong_unique_split():
    text = """
    (3) 決議事項に対する賛成、反対及び棄権の意思の表示に係る議決権の数
    決議事項 賛成数 反対数 棄権数 決議の結果
    第1号議案 テストの件 1000234 可決
    (4) 議決権の数の一部を加算しなかった理由
    """
    parsed = voting.parse_text(text)
    row = parsed["rows"][0]
    assert (row["votes_for"], row["votes_against"], row["votes_abstain"]) == (None, None, None)
    assert row["counts_method"] is None
    assert "counts_unresolved" in parsed["warnings"]


def test_parse_negated_result_is_unknown():
    base = """
    (3) 決議事項に対する賛成、反対及び棄権の意思の表示に係る議決権の数
    決議事項 賛成数 反対数 棄権数 決議の結果
    第1号議案 テストの件 10 20 0 {result}
    (4) 議決権の数の一部を加算しなかった理由
    """
    for result in ("可決できなかった", "否決できなかった"):
        row = voting.parse_text(base.format(result=result))["rows"][0]
        assert row["result"] is None


def test_parse_does_not_read_vote_counts_after_result_as_rate():
    text = """
    (3) 決議事項に対する賛成、反対及び棄権の意思の表示に係る議決権の数
    決議事項 賛成数 反対数 棄権数 決議の結果 賛成率
    第1号議案 テストの件 39,413 122 0 可決 39,413 122 0
    (4) 議決権の数の一部を加算しなかった理由
    """
    row = voting.parse_text(text)["rows"][0]
    assert row["stated_rate_pct"] is None
    assert row["rate_basis"] is None


def test_parse_integer_rate_before_result():
    text = """
    (3) 決議事項に対する賛成、反対及び棄権の意思の表示に係る議決権の数
    決議事項 賛成数 反対数 棄権数 賛成率 決議の結果
    第1号議案 テストの件 100 23 4 97% 可決
    (4) 議決権の数の一部を加算しなかった理由
    """
    row = voting.parse_text(text)["rows"][0]
    assert row["stated_rate_pct"] == 97.0


def test_parse_facts_preserves_amendment_id():
    is_voting, parsed = voting.parse([
        fact("ResolutionOfShareholdersMeetingTextBlock", "決議事項に対する賛成 可決"),
        fact("IdentificationOfDocumentSubjectToAmendmentDEI", "S100PARENT"),
    ])
    assert is_voting
    assert parsed["amends_doc_id"] == "S100PARENT"


def test_store_and_resumable_candidates():
    con = sqlite3.connect(":memory:")
    con.executescript("""
      CREATE TABLE filings (doc_id TEXT, submit_datetime TEXT, edinet_code TEXT, doc_type_code TEXT,
                            withdrawn INTEGER, has_csv INTEGER, parent_doc_id TEXT);
      CREATE TABLE issuers (edinet_code TEXT, ticker TEXT);
    """)
    con.executescript(voting.SCHEMA)
    parsed = {"meeting_date": "2026-06-30", "amends_doc_id": None, "warnings": [], "rows": [{
        "proposal_no": 1, "proposal_title_ja": "臨時配当の件", "candidate_ja": None,
        "votes_for": 3875, "votes_against": 261083, "votes_abstain": 0,
        "stated_rate_pct": 1.46, "rate_basis": "for", "result": "failed", "counts_method": "separated",
    }]}
    assert voting.store(con, "S100TEST", parsed, "2026-07-01 12:00", "E00001", "1234") == "parsed"
    stored = con.execute("SELECT doc_id, result, source_url FROM voting_results").fetchone()
    assert stored == ("S100TEST", "failed", voting.SOURCE_URL.format("S100TEST"))
    assert con.execute("SELECT status, n_rows FROM voting_read").fetchone() == ("parsed", 1)


def _real(doc_id):
    return voting.parse(xbrlcsv.read_facts((CACHE / f"{doc_id}.zip").read_bytes()))[1]


def test_real_s100z57n_extra_attendance_column_is_not_abstentions():
    parsed = _real("S100Z57N")
    first = parsed["rows"][0]
    assert (first["votes_for"], first["votes_against"], first["votes_abstain"]) == (92610, 354, 4)
    assert all((r["votes_abstain"] or 0) < 1000 for r in parsed["rows"])


def test_real_s100z5lm_note_number_is_not_a_vote_cell():
    row = _real("S100Z5LM")["rows"][0]
    assert (row["votes_for"], row["votes_against"], row["votes_abstain"]) == (157019, 225, None)


def test_real_s100z4jt_vertical_candidates_are_split_and_uncertain_counts_are_null():
    rows = _real("S100Z4JT")["rows"]
    directors = [r for r in rows if r["proposal_no"] == 3]
    auditors = [r for r in rows if r["proposal_no"] == 4]
    assert len(directors) == 8 and len(auditors) == 3
    assert [r["stated_rate_pct"] for r in auditors] == [86.58, 86.76, 86.35]
    assert all(r["votes_for"] is r["votes_against"] is r["votes_abstain"] is None for r in directors + auditors)


def test_second_table_with_against_heading_keeps_its_own_basis():
    """S100Z5I5 seq 12, 13: shareholder proposals headed 反対比率 were stored with rate_basis 'for'."""
    text = """
    (1) 株主総会が開催された年月日 2026年9月25日
    (2) 決議事項の内容
    第1号議案 剰余金の処分の件
    第5号議案 自己株式取得の件
    (3) 決議事項に対する賛成、反対の意思の表示に係る議決権の数、当該決議事項が可決されるための要件並びに当該決議の結果
    ＜会社提案＞ 決議事項 賛成(個) 反対(個) 棄権(個) 可決要件 決議結果 賛成比率(%)
    第1号議案 341,286 193 0 (注)1 可決 99.93
    ＜株主提案＞ 決議事項 賛成(個) 反対(個) 棄権(個) 可決要件 決議結果 反対比率(%)
    第5号議案 26,545 314,930 0 (注)1 否決 92.22
    (4) 株主総会に出席した株主の議決権の数の一部を加算しなかった理由
    """
    rows = voting.parse_text(text)["rows"]
    assert [(r["proposal_no"], r["rate_basis"], r["stated_rate_pct"], r["result"]) for r in rows] == [
        (1, "for", 99.93, "passed"), (5, "against", 92.22, "failed")]


def test_s100z5i5_shareholder_proposals_are_against_rates():
    path = CACHE / "S100Z5I5.zip"
    if not path.exists():
        import pytest
        pytest.skip("S100Z5I5 not cached")
    _, parsed = voting.parse(xbrlcsv.read_facts(path.read_bytes()))
    rows = parsed["rows"]
    assert {r["rate_basis"] for r in rows[:12]} == {"for"}
    for r, rate, votes in zip(rows[12:], (92.22, 90.09), ((26545, 314930), (33818, 307658))):
        assert (r["rate_basis"], r["stated_rate_pct"], (r["votes_for"], r["votes_against"]), r["result"]) == ("against", rate, votes, "failed")


def test_deepseek_review_fixes_20261102():
    from japanfilings import voting as v
    # header with only 反対率 used to raise AttributeError and abort the whole run
    t = ("開催された年月日:2025年6月27日 (2)決議事項に対する賛成、反対及び棄権の意思表示に係る議決権の数、当該決議事項が可決されるための要件並びに当該決議の結果 "
         "決議事項 賛成数 反対数 棄権数 反対率 決議結果 第1号議案 剰余金の処分の件 500 20 0 5.0 可決")
    v.parse_text(t)
    assert v._candidates("39,41,3 122 0", 3)[1] is False  # malformed comma groups are not a separated cell
    assert [m.group(2) for m in v._PROPOSAL.finditer("議案第1号 定款一部変更の件") if m.group(2)] == ["1"]
    assert v._candidate_lists("第2号議案 取締役2名選任の件 候補者として、山田太郎(1960年4月1日生、64歳)及び佐藤花子(1965年5月2日生、58歳)を選任する") == {2: ["山田太郎", "佐藤花子"]}
    facts = [{"element": "x:ResolutionOfShareholdersMeetingTextBlock", "value": " ", "context": "c", "label": ""},
             {"element": "x:IdentificationOfDocumentSubjectToAmendmentDEI", "value": "該当なし", "context": "c", "label": ""}]
    assert v.parse(facts) == (False, None)


# --- 2026-11-06: wider table layouts, without guessing -------------------------------------------------------------

def _table(header, rows, section2=""):
    return ("(1) 株主総会が開催された年月日 2026年6月26日 (2) 決議事項の内容 " + section2 +
            " (3) 決議事項に対する賛成、反対及び棄権の意思の表示に係る議決権の数、当該決議事項が可決されるための要件並びに当該決議の結果 "
            + header + " " + rows + " (4) 株主総会に出席した株主の議決権の数の一部を加算しなかった理由")


def _counts(rows):
    return [(r["votes_for"], r["votes_against"], r["votes_abstain"]) for r in rows]


def test_tail_scan_matches_old_regex_and_is_linear():
    import random
    import re
    import time
    old = re.compile(r"([\d,-](?:[\d,\s-]*[\d,-])?)\s*$")
    random.seed(0)
    for _ in range(20000):
        s = "".join(random.choice("0123456789,.- 　%()注円可\n") for _ in range(random.randint(0, 14)))
        m = old.search(s)
        expect = (m.start(), m.group(1)) if m else None
        if expect and expect[0] > 1 and s[expect[0] - 1] == "." and s[expect[0] - 2].isdigit():
            expect = None  # new rule: figures after a decimal point are a rate's
        assert voting._tail(s) == expect, s
    t = time.perf_counter()
    voting._tail(("1 2 3, " * 20000) + "円")
    voting._RATE_AFTER.match("　" * 20000 + "あ")
    voting._NOTE_END.search(" " * 20000 + "(注)1 x")
    assert time.perf_counter() - t < 0.5


def test_void_column_is_read_and_dropped():
    """S100YZ9P: 無効 used to become the abstain cell ('5,0723100' read as 5,072 / 310 / 0)."""
    parsed = voting.parse_text(_table(
        "決議事項 賛成(個) 反対(個) 棄権(個) 無効(個) 可決要件 決議の結果(賛成の割合)",
        "第1号議案5,0723100(注)1可決(99.39%) 第2号議案5,0802300(注)1可決(99.55%) 第3号議案5,0752800(注)1可決(99.45%)"))
    assert _counts(parsed["rows"]) == [(5072, 31, 0), (5080, 23, 0), (5075, 28, 0)]


def test_total_column_first_and_impossible_sum():
    """S100Y7N9: 行使数 comes first; a reading above the stated total is impossible."""
    rows = voting.parse_text(_table("決議事項 行使数(個) 賛成(個) 反対(個) 棄権(個) 可決要件 決議の結果(賛成割合)",
                                    "第1号議案 234,067210,77122,331238 可決(90.05%)"))["rows"]
    assert _counts(rows) == [(210771, 22331, 238)]


def test_ratio_heading_and_rate_after_note_number():
    """'賛成比率' is a rate heading (the row was read as 9 / 4 from '98.94'); '(注)198.487' is note 1, rate 98.487;
    '(注)93.7' is a rate of 93.7."""
    rows = voting.parse_text(_table("決議事項 賛成(個) 反対(個) 棄権(個) 決議の結果 賛成比率(%) 可否",
                                    "第1号議案 90,150,860 68,219 41,764 98.94可決"))["rows"]
    assert (_counts(rows), rows[0]["stated_rate_pct"]) == ([(90150860, 68219, 41764)], 98.94)
    rows = voting.parse_text(_table("決議事項 賛成数(個) 反対数(個) 棄権数(個) 可決要件 賛成率(%) 決議結果",
                                    "第1号議案 112,151 408 0 (注)198.487可決 第2号議案 92,655 19,904 0 (注)93.7可決"))["rows"]
    assert [r["stated_rate_pct"] for r in rows] == [98.487, 93.7]


def test_rate_settles_a_run_only_where_the_document_divides_by_the_counted_votes():
    """S100YMAX-like: '69,0998240' could be 69,099/824/0, 69,099/82/40 or 69,099/8/240; 98.82 fits only the first,
    and the settled rows show the rate is figured on for + against + abstain."""
    rows = voting.parse_text(_table("決議事項 賛成数(個) 反対数(個) 棄権数(個) 可決要件 決議の結果及び賛成割合(%)",
                                    "第1号議案 69,0998240 (注)1可決98.82 第2号議案 69,110 814 0 (注)1可決98.84"))["rows"]
    assert _counts(rows)[0] == (69099, 824, 0) and rows[0]["counts_method"] == "rate_consistency"
    # the same run where the rate is on all voting rights present (99.83 fits no reading): left open
    rows = voting.parse_text(_table("決議事項 賛成数(個) 反対数(個) 棄権数(個) 可決要件 決議の結果及び賛成割合(%)",
                                    "第1号議案 965,9361840 (注)1可決99.83 第2号議案 961,111 5,007 0 (注)1可決99.33"))["rows"]
    assert _counts(rows)[0] == (None, None, None)


def test_candidates_laid_out_by_column():
    """S100XUU8: names, then every for cell, every against cell, every abstain cell, one result word and the rates
    run together.  Accepted because only one reading gives all four the same total."""
    section2 = "第1号議案 取締役4名選任の件 取締役として、甲林泰平、乙部裕輔、丙井誠人、丁渡万希子の4名を選任する。"
    parsed = voting.parse_text(_table(
        "決議事項 賛成(個) 反対(個) 棄権(個) 可決要件 決議の結果及び賛成割合(%)",
        "第1号議案 取締役4名選任の件 甲林 泰平乙部 裕輔丙井 誠人丁渡 万希子 249,618253,437253,593253,433 "
        "11,0657,2467,0907,250 30303030(注) 可決 95.7497.2197.2797.21", section2))
    rows = parsed["rows"]
    assert [r["candidate_ja"] for r in rows] == ["甲林泰平", "乙部裕輔", "丙井誠人", "丁渡万希子"]
    assert _counts(rows) == [(249618, 11065, 30), (253437, 7246, 30), (253593, 7090, 30), (253433, 7250, 30)]
    assert [r["stated_rate_pct"] for r in rows] == [95.74, 97.21, 97.27, 97.21]
    assert {r["counts_method"] for r in rows} == {"column_sum_consistency"} and parsed["warnings"] == []


def test_column_layout_rate_first_and_ambiguous_cells_stay_null():
    # rates before the result words, the first rate run on to the last abstain cell (S100XCQ5)
    rows = voting.parse_text(_table(
        "決議事項 賛成数(個) 反対数(個) 棄権数(個) 賛成割合(%) 決議の結果",
        "第3号議案 監査役2名選任の件 戊嶋 秀行 己内 一英 564,633562,77629,23131,1161027494.47%94.16%可決可決"))["rows"]
    assert _counts(rows) == [(564633, 29231, 102), (562776, 31116, 74)]
    assert [r["stated_rate_pct"] for r in rows] == [94.47, 94.16]
    # S100XRDZ: '2020' as 20/20 leaves totals 3 votes apart, 202/0 leaves them 205 apart
    rows = voting.parse_text(_table(
        "決議事項 賛成数(個) 反対数(個) 棄権数(個) 賛成割合(%) 決議の結果",
        "第2号議案 庚松 正志 仮 秀樹 8,959,0908,379,709 603,5051,182,883 2020 93.3687.32 可決可決"))["rows"]
    assert _counts(rows) == [(8959090, 603505, 20), (8379709, 1182883, 20)]


def test_one_result_word_with_rate_and_reference_rate_is_one_row():
    """S100YF1G: '(97.45%)99.70%' is a rate and the advance-vote rate, not two candidates."""
    rows = voting.parse_text(_table("決議事項 賛成(個) 反対(個) 棄権(個) 決議の結果(賛成の割合)",
                                    "第1号議案1,698,9653,809482可決(97.45%)99.70% 第2号議案1,696,0016,741482可決(97.28%)99.50%"))["rows"]
    assert len(rows) == 2 and _counts(rows) == [(1698965, 3809, 482), (1696001, 6741, 482)]


def test_names_are_paired_only_when_item_2_states_them():
    assert voting._names("甲田 真明 壬村 正弥", None, 2, "取締役として、甲田真明、壬村正弥を選任") == ["甲田 真明", "壬村 正弥"]
    assert voting._names("甲松 英之逵 乙 隆小 丁 勝", None, 3, "甲松英之、逵乙隆、小丁勝を選任") is None
    assert voting._names("1.丙見 秀人2.丁原 正好", None, 2) == ["丙見 秀人", "丁原 正好"]


def test_column_search_is_bounded():
    import time
    t = time.perf_counter()
    reading, method = voting._column_readings("1" * 60 + " " + "2" * 60 + " " + "3" * 60, ["for", "against", "abstain"],
                                              14, [None] * 14, [None] * 14, ["passed"] * 14, "for")
    assert reading is None and time.perf_counter() - t < 5


def test_unit_suffixed_tables_20261106():
    from japanfilings import voting as v
    rows = v._unit_rows(v._norm("第1号議案 剰余金処分の件 251,760個1,370個0個98.72％可決 第2号議案 取締役選任の件 癸田 茂250,306個2,824個0個98.15％可　決"))
    assert [(r["proposal_no"], r["votes_for"], r["votes_against"], r["result"]) for r in rows] == [(1, 251760, 1370, "passed"), (2, 250306, 2824, "passed")]
    assert rows[1]["candidate_ja"] == "癸田 茂"
    # result before rate (S100XE7P)
    rows = v._unit_rows(v._norm("(注) 架田 謙118,802個5,485個0個 可決95.20%"))
    assert rows[0]["votes_for"] == 118802 and rows[0]["stated_rate_pct"] == 95.2
    # a rate that does not match the counts is dropped
    assert v._unit_rows(v._norm("A 100個900個0個 95.00% 可決")) == []
