import json, sqlite3
from japanfilings import investors, store

G = {"organization": [], "individual": [], "never_curate": [], "assign": [], "groups": [
    {"id": "nomura", "name_en": "Nomura Group", "kind": "financial_group", "patterns": ["^野村證券", "^NOMURAINTERNATIONAL"], "_rx": None},
    {"id": "custody", "name_en": "Custody", "kind": "custodian_nominee", "patterns": ["^日本カストディ銀行"]},
    {"id": "fmr", "name_en": "FMR", "kind": "asset_manager", "patterns": ["^FMR"]},
    {"id": "fil", "name_en": "FIL", "kind": "asset_manager", "patterns": ["^フィデリティ投信"]},
]}


def ov():
    import re
    o = json.loads(json.dumps(G))
    for g in o["groups"]:
        g["_rx"] = [re.compile(p) for p in g["patterns"]]
    return o


def lh(doc, code, name, ticker, ratio, date, kind="change_report", holders=None, en=None):
    return (doc, ticker, code, date, json.dumps({"doc_id": doc, "filer_edinet_code": code, "filer_name_ja": name, "issuer_ticker": ticker, "holding_ratio_pct": ratio,
            "submit_date": date, "report_kind": kind, "holders": holders or [], "filer_name_en_filing": en}, ensure_ascii=False))


def seed():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(store.SCHEMA)
    con.executemany("INSERT INTO large_holdings VALUES (?,?,?,?,?)", [
        lh("A1", "E1", "野村證券株式会社", "1000", 6.0, "2026-01-01", "initial_report", [{"name": "野村證券株式会社", "holder_type": "corporation"}, {"name": "ノムラ・プライベート株式会社", "holder_type": "corporation"}], "Nomura Securities"),
        lh("A2", "E1", "野村證券株式会社", "1000", 4.0, "2026-02-01"),
        lh("A3", "E1", "野村證券株式会社", "2000", 7.0, "2026-02-02"),
        lh("B1", "E2", "野村甲", "1000", 8.0, "2026-01-05", "initial_report", [{"name": "野村甲", "holder_type": "individual"}, {"name": "株式会社南青山", "holder_type": "corporation"}]),
        lh("C1", "E3", "山田太郎", "2000", 5.5, "2026-01-05", "initial_report"),
        lh("D1", "E4", "株式会社三井物産", "2000", 5.5, "2026-01-05", "initial_report"),
        lh("D2", "E5", "株式会社三井不動産", "2000", 5.5, "2026-01-05", "initial_report"),
        lh("F1", "E6", "ＦＭＲ　ＬＬＣ", "2000", 5.5, "2026-01-05", "initial_report"),
        lh("F2", "E7", "フィデリティ投信株式会社", "2000", 5.5, "2026-01-05", "initial_report"),
    ])
    con.executemany("INSERT INTO issuers (edinet_code, ticker, name_ja, name_en, listed) VALUES (?,?,?,?,?)",
                    [("E9", "1000", "株式会社甲", "Ko", 1), ("E10", "2000", "株式会社乙", "Otsu", 1)])
    company = {"major_shareholders": [{"name": "㈱日本カストディ銀行（信託口）", "ratio_pct": 9}, {"name": "仮野 一郎", "ratio_pct": 8},
                                      {"name": "野村證券株式会社 代表取締役 丁田健太郎", "ratio_pct": 3}]}
    con.execute("INSERT INTO annual_reports VALUES ('R','E9','1000','2026-03-31','2026-06-20',?)", (json.dumps({"doc_id": "R", "ticker": "1000", "fiscal_year_end": "2026-03-31", "company": company}, ensure_ascii=False),))
    return con


def groups(con):
    investors.rebuild(con, ov())
    return {r["group_id"]: r for r in con.execute("SELECT * FROM investor_groups")}


def test_normalization_treats_spellings_alike_and_strips_register_words():
    a = investors.split_name("㈱ＫＤＤＩ")[0]
    assert a == investors.split_name("KDDI株式会社")[0] == investors.split_name("KDDI(株)(常任代理人 みずほ銀行)")[0]
    assert investors.split_name("日本カストディ銀行(信託口4)")[0] == "日本カストディ銀行"


def test_organization_test_denies_person_like_names():
    assert not investors.looks_like_organization("仮野 一郎")
    assert not investors.looks_like_organization("KASOU HIROSHI(常任代理人 みずほ銀行)")
    assert investors.looks_like_organization("Effissimo Capital Management Pte Ltd")
    assert investors.looks_like_organization("光通信株式会社")


def test_individuals_never_published_or_searchable():
    con = seed()
    groups(con)
    blob = json.dumps([list(r) for t in investors.D1_TABLES for r in con.execute(f"SELECT * FROM {t}")], ensure_ascii=False)
    for name in ("野村甲", "山田太郎", "鈴木", "丁田健太郎"):
        assert name not in blob
    assert {r[0] for r in con.execute("SELECT edinet_code FROM investor_excluded_filers")} >= {"E2", "E3"}
    stats = dict(con.execute("SELECT key, value FROM investor_filter_stats").fetchall())
    assert stats["individual_stated"] >= 1 and stats["no_org_evidence"] >= 2


def test_person_surname_does_not_join_the_brand_and_uncertain_stays_apart():
    con = seed()
    g = groups(con)
    assert "nomura" in g and "ig_e2" not in g
    # Shared 三井 brand: not merged.
    assert "ig_e4" in g and "ig_e5" in g
    # Shared Fidelity name but different owners, each by own pattern.
    assert "fmr" in g and "fil" in g
    members = {r[0] for r in con.execute("SELECT name FROM investor_group_members WHERE group_id = 'nomura'")}
    assert not any("ノムラ・プライベート" in m for m in members)


def test_confidence_and_rule_are_published():
    con = seed()
    groups(con)
    r = con.execute("SELECT match_rule, confidence FROM investor_group_members WHERE group_id = 'nomura'").fetchone()
    assert tuple(r) == ("curated_pattern", "medium")
    assert con.execute("SELECT confidence FROM investor_groups WHERE group_id = 'ig_e4'").fetchone()[0] == "high"


def test_entries_and_exits_with_dates():
    con = seed()
    groups(con)
    p = con.execute("SELECT * FROM investor_positions WHERE group_id = 'nomura' AND ticker = '1000' AND source = 'filing_5pct'").fetchone()
    assert (p["status"], p["exit_date"], p["first_date"], p["first_kind"]) == ("below_5pct", "2026-02-01", "2026-01-01", "new_stake_reported")
    assert [h["event"] for h in json.loads(p["history"])] == ["new_stake_reported", "exit_below_5pct"]
    q = con.execute("SELECT * FROM investor_positions WHERE group_id = 'nomura' AND ticker = '2000'").fetchone()
    assert (q["status"], q["first_kind"]) == ("held", "first_filing_on_record")


def test_joint_only_as_filed_and_without_individuals():
    con = seed()
    groups(con)
    pairs = list(con.execute("SELECT group_a, group_b, ticker, latest_doc_id FROM investor_joint_filings"))
    assert all("B1" != r[3] for r in pairs)  # the other holder there is a person
    assert any(r[3] == "A1" for r in pairs) or not pairs


def test_custody_account_is_no_position():
    con = seed()
    groups(con)
    assert con.execute("SELECT COUNT(*) FROM investor_positions WHERE group_id = 'custody'").fetchone()[0] == 0


def test_deterministic():
    a, b = investors.build(seed(), ov()), investors.build(seed(), ov())
    assert a["groups"] == b["groups"] and a["members"] == b["members"]


def test_shipped_overrides_load():
    o = investors._load_overrides()
    ids = [g["id"] for g in o["groups"]]
    assert len(ids) == len(set(ids)) and "fmr" in ids and "fil" in ids
    fmr = next(g for g in o["groups"] if g["id"] == "fmr")
    assert any(r.search("FMR") for r in fmr["_rx"]) and not any(r.search("フィデリティ投信") for r in fmr["_rx"])


# Shapes from DATA_CHECK_20261105 重大-1: organisations whose English filer field is a person, filings an issuer made
# for a person, and organisation names followed by an officer's title and name.
_LEAK = ("kasou", "tarou", "Kouno Konosuke", "Otsumura", "Jinichi", "Placeholder", "HEIZAKI", "Kasoumoto", "戊田", "乙村", "康宏", "甲野 康之助")


def test_filer_names_cut_titles_and_people():
    for filed, want in [
        ("Arcus Investment Limited       Director/Executive Director Edward Placeholder", "Arcus Investment Limited"),
        ("JINYA-CONNECT Co.,Ltd. CEO TOMOKO HEIZAKI", "JINYA-CONNECT Co.,Ltd."),
        ("Kyosan Mizuho Kai Chairman Hirokazu Kasouda", "Kyosan Mizuho Kai"),
        ("KYOCERA CORPORATION Hideo Kasoumoto, Director and President", "KYOCERA CORPORATION"),
        ("RIZAP GROUP, Inc. Takeshi Kakuuno, Representative Director, President", "RIZAP GROUP, Inc."),
        ("Limited Society NSEC CEO Mineo Kasoumori", "Limited Society NSEC"),
        ("ブラックロック・ジャパン株式会社代表取締役社長 戊田 浩之", "ブラックロック・ジャパン株式会社"),
        ("株式会社 陣屋コネクト 代表取締役 丙﨑 知子", "株式会社 陣屋コネクト"),
        ("住友電気工業株式会社 社長 辛上 治", "住友電気工業株式会社"),
        ("株式会社長栄", "株式会社長栄"),
    ]:
        assert investors.clean_name(filed) == want, filed
    assert investors.public_filer_names({"filer_name_ja": "トラストアップ株式会社", "filer_name_en_filing": "kasou tarou", "holders": []}) == ("トラストアップ株式会社", None)
    assert investors.public_filer_names({"filer_name_ja": "トラストアップ株式会社", "filer_name_en_filing": "Trust Up Co., Ltd."})[1] == "Trust Up Co., Ltd."
    for en in ("kasou tarou", "Jiro Kakuu", "AKANE KAKUU", "Jinichi Chouno", "KAKUU TOSHIYUKI", "Kasou Honei"):
        assert investors.looks_like_romanized_person(en), en
    for en in ("Hikari Tsushin K.K.", "Goldman Sachs International", "YOUR TURN", "House Kousan Kabushikikaisya", "TAIYO FUDOUSAN", "Nippon Life"):
        assert not investors.looks_like_romanized_person(en), en


def test_filed_by_issuer_and_sole_individual_holder_are_persons():
    rec = {"filer_edinet_code": "E34657", "issuer_edinet_code": "E34657", "data_warnings": ["filed_by_issuer"], "holders": [{"name": "乙村 康宏", "holder_type": "individual"}]}
    assert investors.filer_is_person(rec)
    assert investors.filer_is_person({"holders": [{"name": "小倉クラッチ取引先持株会 理事長 架空嵐 誠", "holder_type": "individual"}]})
    assert not investors.filer_is_person({"filer_edinet_code": "E25629", "holders": [{"name": "庚井 康治", "holder_type": "individual"}, {"name": "有限会社 MASTERMIND", "holder_type": "corporation"}]})


def test_group_names_and_positions_never_carry_a_person():
    con = seed()
    con.executemany("INSERT INTO large_holdings VALUES (?,?,?,?,?)", [
        # 株式会社AIR-U (E38160): the issuer filed for 甲野 康之助; English field is his name.
        (d, t, c, dt, json.dumps({**json.loads(r), "data_warnings": ["filed_by_issuer"], "issuer_edinet_code": "E38160"}, ensure_ascii=False))
        for d, t, c, dt, r in [lh("G1", "E38160", "株式会社AIR-U", "1000", 30.0, "2026-01-05", "initial_report", [{"name": "甲野 康之助", "holder_type": "individual"}], "Kouno Konosuke")]
    ] + [
        lh("G2", "E39069", "トラストアップ株式会社", "2000", 6.0, "2026-01-05", "initial_report", [{"name": "トラストアップ株式会社", "holder_type": "corporation"}], "kasou tarou"),
        lh("G3", "E23032", "アーカス・インベストメント・リミテッド", "2000", 6.0, "2026-01-06", "initial_report", [{"name": "アーカス・インベストメント・リミテッド", "holder_type": "corporation"}],
           "Arcus Investment Limited       Director/Executive Director Edward Placeholder"),
    ])
    g = groups(con)
    assert g["ig_e38160"]["name_ja"] == "株式会社AIR-U" and g["ig_e38160"]["name_en"] is None
    assert g["ig_e39069"]["name_en"] is None and g["ig_e23032"]["name_en"] == "Arcus Investment Limited"
    assert con.execute("SELECT COUNT(*) FROM investor_positions WHERE group_id = 'ig_e38160'").fetchone()[0] == 0
    assert con.execute("SELECT holder_name FROM investor_positions WHERE group_id = 'ig_e23032'").fetchone()[0] == "Arcus Investment Limited"
    blob = json.dumps([list(r) for t in investors.D1_TABLES for r in con.execute(f"SELECT * FROM {t}")], ensure_ascii=False)
    for name in _LEAK:
        assert name.upper() not in blob.upper(), name


def test_clean_name_round_2_titles_brackets_leading_person_glued_ceo_agents():
    for filed, want in [
        ("株式会社〇〇 最高経営責任者 山田太郎", "株式会社〇〇"),
        ("株式会社〇〇 会計参与 山田太郎", "株式会社〇〇"),
        ("株式会社〇〇 清算人 山田太郎", "株式会社〇〇"),
        ("〇〇株式会社 破産管財人 山田太郎", "〇〇株式会社"),
        ("〇〇株式会社 支配人 山田太郎", "〇〇株式会社"),
        ("合同会社CYGNUS 代表社員 一般社団法人SPE 業務執行者 癸木洋介", "合同会社CYGNUS"),
        ("山田太郎 株式会社〇〇代表取締役", "株式会社〇〇"),
        ("株式会社〇〇(代表取締役 山田太郎)", "株式会社〇〇"),
        ("株式会社〇〇（代表取締役　山田太郎）", "株式会社〇〇"),
        ("PT ASIA PLASTIK VIKTORICEO FIKTIF", "PT ASIA PLASTIK VIKTORI"),
        ("Francis Capital IncCEO  Ann Fic Tion", "Francis Capital Inc"),
        ("ABC Co., Ltd. Japan President Taro Yamada", "ABC Co., Ltd. Japan"),
        ("弁護士 架 空 弘 典", ""),
        ("Akatsuki Capital Works 株式会社代表取締役 乙塚 郁人", "Akatsuki Capital Works 株式会社"),
    ]:
        assert investors.clean_name(filed) == want, filed
    for name in ("THECOO Inc.", "弁護士ドットコム株式会社", "弁護士法人〇〇", "ブラックロック・ジャパン 株式会社"):
        assert investors.clean_name(name) == name
    assert not investors.looks_like_organization("弁護士 架 空 弘 典")
    assert investors.public_filer_names({"filer_name_ja": "弁護士 架 空 弘 典", "filer_name_en": "First Eagle Investment Management, LLC", "holders": []}) == (None, "First Eagle Investment Management, LLC")


def test_katakana_foreign_legal_forms_are_organisations():
    for n in ("クナウフ・インタナショナル・ゲーエムベーハー", "クレディ・スイス・エイ・ジー", "ソシエテ・ジェネラル", "エネル・エスピーエー", "ピージージーエム フェルモーホンビヘーア ビーブイ",
              "ノムラ・ヨーロッパ・ファイナンス・エヌ・ブイ", "アライアンス・バーンスタイン・ルクセンブルグ・エス・エイ・アール・エル", "アムンディ・アイルランド・リミティッド", "〇〇・アクチェンゲゼルシャフト"):
        assert investors.looks_like_organization(n), n
    for n in ("ジョン・スミス", "カソウ・タロウ・ジロウ", "エイミー・ジョーンズ", "山田 太郎"):
        assert not investors.looks_like_organization(n), n
