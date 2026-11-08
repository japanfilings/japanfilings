"""Governance parsers against text shaped like real filings (wording taken from documents checked by hand). No network."""
import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from japanfilings import governance as g

NOT_EFFECTIVE = ("3【評価結果に関する事項】下記に記載した財務報告に係る内部統制の不備は、財務報告の適正性に重要な影響を及ぼすものであり"
                 "開示すべき重要な不備に該当すると判断いたしました。したがって、2026年3月31日時点において、当社の財務報告に係る内部統制は"
                 "有効でないと判断いたしました。 記 当社の2026年3月期の決算作業において、過年度の税効果会計の処理に誤りがあり、繰延税金資産の"
                 "計上が過大であったことが判明しました。")
HEADED = ("上記の評価の結果、当連結会計年度末日において、当社グループの財務報告に係る内部統制には以下のとおり開示すべき重要な不備が存在すると"
          "判断いたしました。したがって、当連結会計年度末日時点において、当社グループの財務報告に係る内部統制は有効ではないと判断いたしました。"
          " (1)開示すべき重要な不備の内容 当初公表した決算短信の数値に誤謬が判明しました。 (2)当事業年度末までに是正できなかった理由 人員が不足しておりました。")
COULD_NOT = ("上記評価の結果、継続企業の前提の評価に関する手続を除き、当社の財務報告に係る内部統制の評価手続は適切に完了し、不備は発見されなかった。"
             "しかしながら、上記のとおり、継続企業の前提の評価に関する内部統制について評価手続が実施できなかったため、その重要性に鑑み、"
             "当事業年度末日における当社の財務報告に係る内部統制の評価結果を表明できないと判断した。")
AMENDED = ("3【評価結果に関する事項】 (訂正前) 上記の評価の結果、当事業年度末日時点において、当社の財務報告に係る内部統制は有効であると判断しました。"
           " (訂正後) 下記に記載した不備は開示すべき重要な不備に該当すると判断しました。したがって、当事業年度末日時点において、当社の財務報告に係る内部統制は有効でないと判断しました。")
AUDITOR = ("2【報告内容】(1)当該異動に係る監査公認会計士等の名称①　選任する監査公認会計士等の名称史彩監査法人②　退任する監査公認会計士等の名称保森監査法人 "
           "(2)当該異動の年月日2026年６月23日（第23回定時株主総会開催予定日） (3)退任する監査公認会計士等が監査公認会計士等となった年月日2022年６月28日 "
           "(4)退任する監査公認会計士等が直近３年間に作成した監査報告書等における意見等に関する事項　該当事項はありません。 "
           "(5)当該異動の決定又は当該異動に至った理由及び経緯　現会計監査人である保森監査法人は、2026年６月23日開催予定の第23回定時株主総会の終結の時をもって任期満了となります。"
           "(6)上記(5)の理由及び経緯に対する意見①　退任する監査公認会計士等の意見特段の意見はない旨の回答を得ております。②　監査役会の意見妥当であると判断しております。 以　上")
INDIVIDUAL = ("(1)当該異動に係る監査公認会計士等の名称①選任する監査公認会計士等の名称城南監査法人②退任する監査公認会計士等の氏名山川一郎(山川公認会計士事務所)谷口次郎(谷口公認会計士事務所)"
              "(2)当該異動の年月日2024年9月24日 (5)当該異動の決定又は当該異動に至った理由及び経緯 当社の会計監査人である、山川公認会計士事務所 山川一郎及び谷口次郎の両氏は、任期満了となります。")


def _zip(rows):
    head = "要素ID\t項目名\tコンテキストID\t相対年度\t連結・個別\t期間・時点\tユニットID\t単位\t値\n"
    body = "".join(f"{e}\tl\tFilingDateInstant\t提出日時点\tその他\t時点\t－\t－\t{v}\n" for e, v in rows)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("XBRL_TO_CSV/jpctl010000-icr-001.csv", (head + body).encode("utf-16"))
    return buf.getvalue()


@pytest.mark.parametrize("text,want", [
    (NOT_EFFECTIVE, "not_effective"),
    (HEADED, "not_effective"),
    (COULD_NOT, "could_not_conclude"),
    (AMENDED, "not_effective"),
    ("上記の評価の結果、当事業年度末日時点において、当社の財務報告に係る内部統制は有効であると判断しました。", "effective"),
    ("上記の評価手続を実施した結果、当事業年度の末日 (2026年3月31日) 現在の当社の財務報告に係る内部統制は有効であると判断しました。", "effective"),
    ("前事業年度末日時点の内部統制は有効でないと判断しました。当事業年度末日時点において内部統制は有効であると判断しました。", "effective"),
    ("評価を実施した範囲において、当連結会計年度末日時点における当社の財務報告に係る内部統制は有効でないと判断しました。 記 調査の結果、評価手続を実施できなかったことにより、当連結会計年度末日時点における財務報告に係る内部統制の評価結果を表明できないと判断しました。", "could_not_conclude"),
    ("3【評価結果に関する事項】 該当事項はありません。", None),  # a real filing, later amended: nothing to report
])
def test_conclusion(text, want):
    assert g.classify_conclusion(text)[0] == want


def test_internal_control_fields():
    fields, quote = g.parse_internal_control({"ResultOfEvaluationTextBlock": NOT_EFFECTIVE, "SupplementaryInformationTextBlock": "4【付記事項】該当する事項はありません。"},
                                             "内部統制報告書－第79期(2025/04/01－2026/03/31)")
    assert fields["conclusion"] == "not_effective" and fields["material_weakness_stated"] is True
    assert fields["material_weakness_ja"].startswith("当社の2026年3月期の決算作業")
    assert (fields["fiscal_period"], fields["fiscal_year_start"], fields["fiscal_year_end"]) == ("第79期", "2025-04-01", "2026-03-31")
    assert fields["supplementary_note_ja"] is None and fields["is_amendment"] is False
    assert quote == "したがって、2026年3月31日時点において、当社の財務報告に係る内部統制は有効でないと判断いたしました。"


def test_weakness_section_stops_at_next_item():
    fields, _ = g.parse_internal_control({"ResultOfEvaluationTextBlock": HEADED})
    assert fields["material_weakness_ja"] == "当初公表した決算短信の数値に誤謬が判明しました。"


def test_effective_states_no_weakness_field():
    fields, _ = g.parse_internal_control({"ResultOfEvaluationTextBlock": "当事業年度末日時点において内部統制は有効であると判断しました。"})
    assert fields["conclusion"] == "effective" and fields["material_weakness_stated"] is None and fields["material_weakness_ja"] is None


def test_amendment_links_parent_and_uses_corrected_text():
    fields, quote = g.parse_internal_control({"ResultOfEvaluationTextBlock": AMENDED}, "訂正内部統制報告書－第75期(2021/04/01－2022/03/31)", "S100O8T7")
    assert fields["is_amendment"] and fields["amends_doc_id"] == "S100O8T7" and fields["conclusion"] == "not_effective"
    assert "有効でない" in quote


def test_fiscal_year_falls_back_to_assessment_date():
    fields, _ = g.parse_internal_control({"ResultOfEvaluationTextBlock": "内部統制は有効であると判断した。",
                                          "ScopeDateAndProceduresForEvaluationTextBlock": "当事業年度の末日である2025年8月31日を基準日として行われており"}, "内部統制報告書")
    assert fields["fiscal_year_end"] == "2025-08-31" and fields["fiscal_period"] is None


def test_auditor_change():
    fields, quote = g.parse_auditor_change(AUDITOR)
    assert fields["outgoing_auditor"] == "保森監査法人" and fields["incoming_auditor"] == "史彩監査法人"  # no stray '2' from a folded ②
    assert fields["change_date"] == "2026-06-23" and fields["change_date_scheduled"] is True
    assert fields["disagreement_stated"] is False and fields["prior_audit_opinion_matters_ja"] == "該当事項はありません。"
    assert quote.startswith("現会計監査人である保森監査法人は") and len(quote) < 200


def test_real_s100z04j_reason_marks_future_change_as_scheduled():
    cache = Path(__file__).parents[1] / "data" / "csv_cache" / "S100Z04J.zip"
    fields, _ = g.parse_auditor_change(g.text_blocks(cache.read_bytes()))
    assert fields["change_date"] == "2026-09-29" and fields["change_date_scheduled"] is True


@pytest.mark.parametrize("text,out_,in_,date", [
    ("(a) 当該異動に係る監査公認会計士等の名称ア.選任する監査公認会計士等の名称太陽有限責任監査法人イ.退任する監査公認会計士等の名称有限責任 あずさ監査法人 (b) 当該異動の年月日2024年12月24日",
     "有限責任 あずさ監査法人", "太陽有限責任監査法人", "2024-12-24"),
    ("1.当該異動に係る監査公認会計士等の名称(1) 選任する監査公認会計士等有限責任中部総合監査法人(2) 退任する監査公認会計士等EY新日本有限責任監査法人 2.当該異動の年月日2024年12月25日",
     "EY新日本有限責任監査法人", "有限責任中部総合監査法人", "2024-12-25"),
    ("(1)当該異動に係る監査公認会計士等の名称① 存続する監査公認会計士等の名称PwC Japan有限責任監査法人② 消滅する監査公認会計士等の名称PwC京都監査法人(2)当該異動の年月日2023年12月1日",
     "PwC京都監査法人", "PwC Japan有限責任監査法人", "2023-12-01"),
])
def test_auditor_layout_variants(text, out_, in_, date):
    fields, _ = g.parse_auditor_change(text)
    assert (fields["outgoing_auditor"], fields["incoming_auditor"], fields["change_date"]) == (out_, in_, date)


def test_individual_accountant_names_never_stored():
    fields, quote = g.parse_auditor_change(INDIVIDUAL)
    assert fields["outgoing_auditor"] is None and fields["outgoing_is_individual_accountant"] is True
    assert fields["incoming_auditor"] == "城南監査法人"
    assert not any(n in json.dumps([fields, quote], ensure_ascii=False) for n in ("山川", "谷口", "一郎", "次郎"))


@pytest.mark.parametrize("said,want", [
    ("特段の意見はない旨の回答を得ております。", False),
    ("特段意見はない旨の回答を得ております。", False),
    ("特段の意見は無い旨の回答を得ております。", False),
    ("見解の相違がある旨の回答を得ております。", True),
    ("次期以降の監査品質の維持に必要な工数の確保が難しいと判断した旨の回答を得ております。", None),
])
def test_disagreement_is_only_what_is_written(said, want):
    text = f"(1)当該異動に係る監査公認会計士等の名称(6)上記(5)の理由及び経緯に対する意見①　退任する監査公認会計士等の意見{said}②　監査役会の意見妥当であると判断しております。"
    fields, _ = g.parse_auditor_change(text)
    assert fields["disagreement_stated"] is want and fields["outgoing_auditor_statement_ja"] == said


def test_item_filter():
    assert g.is_auditor_change({"ChangeInIndependentAuditorsTextBlock": AUDITOR})
    assert g.is_auditor_change({"ReasonForFilingTextBlock": "企業内容等の開示に関する内閣府令第19条第２項第９号の４の規定に基づき"})
    assert not g.is_auditor_change({"ReasonForFilingTextBlock": "第19条第2項第9号の2の規定に基づき", "ResolutionOfShareholdersMeetingTextBlock": "決議事項"})
    assert not g.is_auditor_change({"ReasonForFilingTextBlock": "第19条第2項第4号の規定に基づき", "ChangesInMajorShareholderTextBlock": "主要株主の異動"})


def test_quote_limit_and_scrub():
    assert len(g._quote("あ" * 500)) < 200 and len(g._quote(("文章です。" * 100))) < 200
    assert "乙松周平" not in g.scrub("代表取締役社長乙松周平における新経営体制")
    assert "丙下直晃" not in g.scrub("当社の代表取締役であった丙下直晃氏が")
    assert g.scrub("監査公認会計士等の異動") == "監査公認会計士等の異動"


def test_text_blocks_reads_real_csv_shape():
    b = g.text_blocks(_zip([("jpctl_cor:ResultOfEvaluationTextBlock", "有効であると判断した。"), ("jpctl_cor:CompanyNameCoverPage", "会社")]))
    assert b == {"ResultOfEvaluationTextBlock": "有効であると判断した。"}


def test_pdf_blocks_undo_line_wraps():
    text = "3【評価結果に関する事項】 取締役会は、内部統制は安定しており、有効であ\nると考えている。 4【付記事項】 該当事項なし。"
    assert g.classify_conclusion(g.blocks_from_pdf_text(text)["ResultOfEvaluationTextBlock"])[0] == "effective"


@pytest.fixture
def db():
    con = sqlite3.connect(":memory:")
    con.executescript("""
    CREATE TABLE issuers (edinet_code TEXT PRIMARY KEY, ticker TEXT);
    CREATE TABLE filings (doc_id TEXT PRIMARY KEY, ticker TEXT, submit_datetime TEXT, edinet_code TEXT, filer_name_ja TEXT, doc_type_code TEXT,
      description_ja TEXT, parent_doc_id TEXT, withdrawn INTEGER, has_csv INTEGER);
    INSERT INTO issuers VALUES ('E1', '1111'), ('E2', NULL);
    INSERT INTO filings VALUES
      ('IC1', NULL, '2026-06-20 10:00', 'E1', '甲', '235', '内部統制報告書－第1期(2025/04/01－2026/03/31)', NULL, 0, 1),
      ('AN1', NULL, '2026-06-20 10:00', 'E1', '甲', '120', '有価証券報告書', NULL, 0, 1),
      ('AGM', NULL, '2026-06-25 10:00', 'E1', '甲', '180', '臨時報告書', NULL, 0, 1),
      ('AU1', NULL, '2026-05-20 10:00', 'E1', '甲', '180', '臨時報告書', NULL, 0, 1),
      ('OT1', NULL, '2026-02-20 10:00', 'E1', '甲', '180', '臨時報告書', NULL, 0, 1),
      ('FND', NULL, '2026-02-20 10:00', 'E1', '甲', '180', '臨時報告書（内国特定有価証券）', NULL, 0, 1),
      ('NOL', NULL, '2026-02-20 10:00', 'E2', '乙', '180', '臨時報告書', NULL, 0, 1),
      ('AM1', NULL, '2026-05-22 10:00', 'E1', '甲', '190', '訂正臨時報告書', 'AU1', 0, 1);
    """)
    con.executescript(g.SCHEMA)
    return con


def test_candidates_read_agm_window_and_skip_funds_and_unlisted(db):
    assert {m["doc_id"] for m in g.auditor_candidates(db)} == {"AU1", "OT1", "AGM"}
    assert {m["doc_id"] for m in g.auditor_candidates(db, include_agm_window=False)} == {"AU1", "OT1"}


def test_failed_reads_are_retried(db):
    db.executescript(g.SCHEMA)
    db.execute("INSERT INTO governance_read VALUES ('AU1', 'auditor_change', 'failed', 'network')")
    db.execute("INSERT INTO governance_read VALUES ('OT1', 'auditor_change', 'other_item', NULL)")
    assert {m["doc_id"] for m in g.auditor_candidates(db)} == {"AU1", "AGM"}


def test_run_end_to_end(db, monkeypatch):
    docs = {
        "IC1": [("jpctl_cor:ResultOfEvaluationTextBlock", NOT_EFFECTIVE)],
        "AU1": [("jpcrp-esr_cor:ReasonForFilingTextBlock", "第19条第2項第9号の4"), ("jpcrp-esr_cor:ChangeInIndependentAuditorsTextBlock", AUDITOR)],
        "OT1": [("jpcrp-esr_cor:ReasonForFilingTextBlock", "第19条第2項第4号"), ("jpcrp-esr_cor:ChangesInMajorShareholderTextBlock", "x")],
        "AM1": [("jpcrp-esr_cor:ChangeInIndependentAuditorsTextBlock", AUDITOR)],
        "AGM": [("jpcrp-esr_cor:ReasonForFilingTextBlock", "第19条第2項第9号の2")],
    }
    calls = []

    def fake(doc_id):
        calls.append(doc_id)
        return _zip(docs[doc_id])
    monkeypatch.setattr(g.edinet, "download_csv_zip", fake)
    counts = g.run(db)
    assert counts == {"internal_control:parsed": 1, "auditor_change:parsed": 2, "auditor_change:other_item": 2}
    rows = {r[0]: json.loads(r[1]) for r in db.execute("SELECT doc_id, record FROM governance_events")}
    assert set(rows) == {"IC1", "AU1", "AM1"}
    assert rows["AU1"]["type"] == "auditor_change" and rows["AU1"]["ticker"] == "1111" and rows["AU1"]["submit_date"] == "2026-05-20"
    assert rows["AU1"]["source_url"] == "https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?AU1"
    assert rows["AM1"]["fields"]["amends_doc_id"] == "AU1" and rows["AU1"]["fields"]["is_amendment"] is False
    assert not any(k in json.dumps(rows) for k in ("score", "risk", "red_flag"))
    n = len(calls)
    assert g.run(db) == {} and len(calls) == n  # resumes: nothing is opened twice


def test_run_limit_and_failure_do_not_stop(db, monkeypatch):
    def boom(doc_id):
        raise RuntimeError("bad zip")
    monkeypatch.setattr(g.edinet, "download_csv_zip", boom)
    assert g.run(db, limit=2) == {"internal_control:failed": 1, "auditor_change:failed": 1}


def test_auditor_label_without_the_word_public_is_not_part_of_the_name():
    """S100Y3JC (9643) wrote 退任する監査会計士等の名称 (no 公認); the label leaked into the outgoing auditor's name."""
    text = ("(1)異動に係る監査公認会計士等の名称①就任する監査公認会計士等の名称監査法人アンビシャス ②退任する監査会計士等の名称有限責任中部総合監査法人 "
            "(2)異動年月日令和８年６月24日（第93回定時株主総会開催予定日） (3)退任する監査公認会計士等が直近において監査公認会計士等となった年月日令和５年６月28日")
    fields, _ = g.parse_auditor_change(text)
    assert fields["outgoing_auditor"] == "有限責任中部総合監査法人"
    assert fields["incoming_auditor"] == "監査法人アンビシャス"


def test_deepseek_review_fixes_20261102():
    from japanfilings import governance as g
    assert g._norm("注記⑩に記載のとおり") == "注記⑩に記載のとおり"
    assert g.scrub("代表取締役 山田太郎。") == "代表取締役[氏名省略]。"
    assert g.classify_conclusion("当事業年度に係る内部統制は有効であると認められない。")[0] == "not_effective"
    assert g._weakness("当社の内部統制には、開示すべき重要な不備が存在しない。", "effective") == (False, None)
    assert g._disagreement("退任する監査公認会計士等の意見 意見の相違が生じた事実はありません。", "")[0] is False
    # no section on the outgoing auditor's view: unrelated words elsewhere (S100XFFT 不服申し立て等を行わない) must not set True
    assert g._disagreement(None, "不服申し立て等を行わない旨の決議をし")[0] is None
    assert g._names("選任する会計監査人の氏名又は名称及び所在地 △△監査法人 退任する会計監査人の氏名又は名称及び所在地 〇〇監査法人")[0][0].startswith("△△監査法人")


def test_scrub_officer_names_after_titles_20261105():
    assert g.scrub("上記の評価の結果、当社代表取締役会長甲井一及び当社取締役 CFO山田次郎は、") == \
        "上記の評価の結果、当社代表取締役会長[氏名省略]及び当社取締役 CFO[氏名省略]は、"
    assert g.scrub("代表取締役社長 丁川 誠及び取締役管理本部長 戊元 優太は、") == "代表取締役社長 [氏名省略]及び取締役管理本部長 [氏名省略]は、"
    assert g.scrub("当社の取締役会は、決議した。") == "当社の取締役会は、決議した。"
    assert g.scrub("代表取締役社長は、評価した。") == "代表取締役社長は、評価した。"
    assert g.scrub("当社取締役代表執行役社長CEO甲﨑成子及び当社執行役") == "当社取締役代表執行役社長CEO[氏名省略]及び当社執行役"
