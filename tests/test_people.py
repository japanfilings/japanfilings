"""Natural persons' names are masked in every exported string (japanfilings/people.py, export_d1._export_row)."""
import json
import sqlite3

from japanfilings import export_d1, people


def _store():
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE issuers (edinet_code TEXT, ticker TEXT, name_ja TEXT, name_en TEXT, listed INTEGER);
        CREATE TABLE voting_results (doc_id TEXT, seq INTEGER, proposal_title_ja TEXT, candidate_ja TEXT);
        CREATE TABLE annual_reports (doc_id TEXT, record TEXT);
        CREATE TABLE large_holdings (doc_id TEXT, filer_edinet_code TEXT, record TEXT);
        CREATE TABLE holder_trades (doc_id TEXT, row_no INTEGER, holder TEXT, note_ja TEXT);
        CREATE TABLE tender_offers (doc_id TEXT, offeror_name_ja TEXT, record TEXT);
        CREATE TABLE ownership_events (event_id TEXT, record TEXT);
        CREATE TABLE investor_excluded_filers (edinet_code TEXT);
        CREATE TABLE filings (doc_id TEXT, edinet_code TEXT, filer_name_ja TEXT);
    """)
    con.executemany("INSERT INTO issuers VALUES (?,?,?,?,?)", [
        ("E1", "1001", "株式会社山田太郎商店", "YAMADA", 1),  # a listed name keeps every part of it
        ("E2", "1002", "トヨタ自動車株式会社", "TOYOTA MOTOR CORPORATION", 1),
        ("E3", None, "野村證券株式会社", "Nomura Securities", 0),
    ])
    con.execute("INSERT INTO voting_results VALUES ('V1', 1, '取締役選任の件', '甲野 一郎')")
    con.execute("INSERT INTO voting_results VALUES ('V1', 2, '定款一部変更の件', '定款一部変更の件')")  # parse junk
    con.execute("INSERT INTO annual_reports VALUES ('A1', ?)", (json.dumps({"company": {
        "representative": "代表取締役社長　社長執行役員　丙々美　勉",
        "officers": [{"title": "取締役", "name": "乙  川  次  郎"}, {"title": "取締役", "name": "George  Olcott"}],
        "major_shareholders": [{"name": "丙田 三子"}, {"name": "日本マスタートラスト信託銀行㈱"}]}}, ensure_ascii=False),))
    con.execute("INSERT INTO large_holdings VALUES ('L1', 'E9', ?)", (json.dumps({
        "holders": [{"name": "丁原 四郎", "holder_type": "individual"}, {"name": "試験アセット株式会社", "holder_type": "corporation"}]},
        ensure_ascii=False),))
    con.execute("INSERT INTO holder_trades VALUES ('L1', 1, 'WMパートナーズ株式会社、戊山守祥、野村證券', NULL)")
    con.execute("INSERT INTO ownership_events VALUES ('X', ?)", (json.dumps({
        "filer_is_individual": True, "filer_name_ja": "己島 五郎", "filer_name_en": "Goro Kishima"}, ensure_ascii=False),))
    return con


def test_dictionary_sources_and_filters():
    ja, en = people.collect(_store())
    for name in ("甲野一郎", "乙川次郎", "丙々美勉", "丙田三子", "丁原四郎", "戊山守祥", "己島五郎"):
        assert name in ja, name
    assert "山田太郎" not in ja  # inside a listed company's name
    assert "定款一部変更の件" not in ja and "野村證券" not in ja  # parse junk; an organisation without its legal form
    assert ("GORO", "KISHIMA") in en and ("GEORGE", "OLCOTT") in en


def test_mask_dictionary_spacing_and_longest_match():
    m = people.Masker({"甲野一郎", "甲野一", "乙川次郎"}, {("GORO", "KISHIMA")})
    assert m.mask("候補者は甲野　一郎です") == "候補者は[氏名省略]です"
    assert m.mask("乙  川  次  郎") == "[氏名省略]"
    assert m.mask('{"note": "甲野一郎へ譲渡"}') == '{"note": "[氏名省略]へ譲渡"}'
    assert m.mask("Filed by Goro Kishima and KISHIMA GORO") == "Filed by [氏名省略] and [氏名省略]"
    assert m.mask("GOROKISHIMAX") == "GOROKISHIMAX"  # never inside a longer word
    assert m.mask("doc_id S100ABCD") == "doc_id S100ABCD"


def test_pattern_rules():
    s = people.scrub
    assert s("取締役CFO兼CIO壬田康は、評価した。") == "取締役CFO兼CIO[氏名省略]は、評価した。"
    assert s("執行役財経担当戊川敬明は") == "執行役財経担当[氏名省略]は"
    assert s("リスクマネジメント担当丁関義徳は、") == "リスクマネジメント担当[氏名省略]は、"
    assert s("(社外)取締役乙木靖子解任の件") == "(社外)取締役[氏名省略]解任の件"
    assert s("筆頭株主の甲村けん牛氏") == "筆頭株主の[氏名省略]氏"
    assert s("代表取締役社長である辛田亨氏") == "代表取締役社長である[氏名省略]氏"
    assert s("取締役己井智明及び") == "取締役[氏名省略]及び"
    assert s("庚高規様株券貸借") == "[氏名省略]様株券貸借"
    assert s("癸山泰仁氏借株の返還") == "[氏名省略]氏借株の返還"


def test_ordinary_words_untouched():
    for t in ("取締役会は、決議した。", "代表取締役社長は、評価した。", "会長", "社長", "上記両氏", "同氏は", "当該氏名", "摂氏20度",
              "株主様", "皆様", "経理担当役員は", "取締役選任の件", "監査役1名選任の件", "KBセーレン株式会社長浜工場", "幌泉郡様似町",
              "鳥取統括支店", "取締役監査等委員は"):
        assert people.scrub(t) == t, t


def test_export_row_masks_every_string_and_keeps_listed_names():
    con = _store()
    m = people.Masker(*people.collect(con))
    cols = ["doc_id", "row_no", "holder", "note_ja"]
    row = export_d1._export_row("holder_trades", cols, ["L1", 1, "WMパートナーズ株式会社、戊山守祥", "丁原四郎45.68円"], m)
    assert row == ["L1", 1, "WMパートナーズ株式会社、[氏名省略]", "[氏名省略]45.68円"]
    rec = json.dumps({"issuer_name_ja": "株式会社山田太郎商店", "purpose_ja": "丙々美 勉氏との提携"}, ensure_ascii=False)
    out = export_d1._export_row("tender_offers", ["doc_id", "record"], ["T1", rec], m)
    assert json.loads(out[1]) == {"issuer_name_ja": "株式会社山田太郎商店", "purpose_ja": "[氏名省略]氏との提携"}
    big = json.dumps({"pad": "x" * 3000, "quote_ja": "取締役CFO兼CIO壬田康は、評価した。"}, ensure_ascii=False)
    assert "門田" not in export_d1._export_row("governance_events", ["doc_id", "record"], ["G1", big], m)[1]
    # Deterministic: a masked key column gives the same value every time (cross_holdings keys on holder).
    assert export_d1._export_row("holder_trades", cols, ["L1", 1, "戊山守祥", None], m) == \
        export_d1._export_row("holder_trades", cols, ["L1", 1, "戊山守祥", None], m)
