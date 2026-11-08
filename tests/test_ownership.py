"""Cross-shareholdings, revenue fallbacks and ownership events. Annual-report CSVs are cached in .cache/."""
import json
import os

import pytest

from japanfilings import annual, crossholdings, events, xbrlcsv

CACHE = os.path.join(os.path.dirname(__file__), "..", ".cache")


def _annual(doc_id):
    path = os.path.join(CACHE, f"{doc_id}_csv.zip")
    if not os.path.exists(path):
        pytest.skip(f"cached annual report {doc_id} not present")
    return annual.parse(xbrlcsv.read_facts(open(path, "rb").read()), doc_id)


def test_revenue_from_filer_specific_and_statement_elements():
    toyota, sony = _annual("S100Y8NY"), _annual("S100YE2C")
    assert toyota["key_metrics"][0]["revenue"] == 50684952000000  # filer element OperatingRevenuesIFRSKeyFinancialData
    assert sony["key_metrics"][0]["revenue"] == 12479620000000  # SalesAndFinancialServicesRevenueIFRSKeyFinancialData
    assert len(toyota["key_metrics"]) == 5 and all(m["revenue"] for m in toyota["key_metrics"])


def test_toyota_cross_holdings():
    xh = _annual("S100Y8NY")["cross_holdings"]
    kddi = next(h for h in xh["holdings"] if h["name_ja"] == "ＫＤＤＩ㈱")
    assert (kddi["kind"], kddi["holder"], kddi["held_back"]) == ("specified", "reporting_company", True)
    assert kddi["shares"] == 363365900 and kddi["prior_shares"] == 203294600
    assert kddi["book_value_jpy"] == 989627000000 and kddi["purpose_ja"].startswith("自動車関連")
    assert sum(h["kind"] == "specified" for h in xh["holdings"]) == 37
    assert sum(h["kind"] == "deemed" for h in xh["holdings"]) == 5
    assert xh["summary"]["listed_issues"] == 34 and xh["summary"]["listed_sale_amount_jpy"] == 238784000000


def test_name_key_matches_filing_and_code_list_spellings():
    assert crossholdings.name_key("ＫＤＤＩ㈱") == crossholdings.name_key("KDDI株式会社") == "KDDI"
    assert crossholdings.name_key("㈱三菱ＵＦＪフィナンシャル・グループ") == crossholdings.name_key("株式会社三菱UFJフィナンシャル・グループ")


def _rec(**kw):
    base = dict(doc_id="S1", report_kind="change_report", submit_date="2026-10-22", issuer_ticker="4042",
                issuer_name_en="TOSOH CORPORATION", filer_name_en_filing="Silchester", filer_edinet_code="E1",
                purpose_tags=["pure_investment"], purpose_ja="純投資")
    return {**base, **kw}


def test_events_increase_threshold_and_activist_turn():
    prev = _rec(holding_ratio_pct=9.5, previous_ratio_pct=8.0)
    rec = _rec(doc_id="S2", holding_ratio_pct=10.6, previous_ratio_pct=9.5, purpose_tags=["may_make_proposals"],
               purpose_ja="重要提案行為等を行うことがある。")
    types = {e["type"]: e for e in events.derive(rec, prev)}
    assert set(types) == {"stake_increase", "threshold_crossed", "activist_purpose"}
    assert types["threshold_crossed"]["threshold_pct"] == 10 and types["threshold_crossed"]["direction"] == "up"
    assert types["stake_increase"]["summary_en"] == "Silchester raised holdings in TOSOH CORPORATION (4042) to 10.60% from 9.50%."
    assert types["stake_increase"]["event_id"] == "S2:stake_increase"


def test_events_negated_proposals_are_not_activist():
    rec = _rec(report_kind="initial_report", holding_ratio_pct=5.2, previous_ratio_pct=None, purpose_tags=["may_make_proposals"],
               purpose_ja="必要に応じて意見を表明する可能性がある。現時点で重要提案行為等を行う予定はない。")
    assert [e["type"] for e in events.derive(rec)] == ["new_stake"]


def test_events_exit_below_5pct():
    types = [e["type"] for e in events.derive(_rec(holding_ratio_pct=4.1, previous_ratio_pct=5.3))]
    assert types == ["exit_below_5pct"]


def test_new_stake_requires_no_previous_ratio_and_at_least_5pct():
    """S100VP2J (1.88%, previously 5.28%) was 'new_stake ... crossing the 5%'; so were S100VN93, S100VN9G, S100VQ2J, S100VQ6V."""
    cases = [  # doc id, now, previous, expected event types
        ("S100VP2J", 1.88, 5.28, ["exit_below_5pct"]),
        ("S100VN93", 7.97, 9.96, ["stake_decrease"]),
        ("S100VN9G", 5.28, 7.97, ["stake_decrease"]),
        ("S100VHJO", 6.13, 5.12, ["stake_increase"]),
        ("S100VKJG", 2.95, 7.33, ["exit_below_5pct"]),
    ]
    for doc, now, before, expected in cases:
        types = [e["type"] for e in events.derive(_rec(doc_id=doc, report_kind="initial_report", holding_ratio_pct=now, previous_ratio_pct=before))]
        assert "new_stake" not in types and types == expected, doc
    # Genuine first reports still produce it; below 5% with nothing before is not a crossing.
    first = events.derive(_rec(report_kind="initial_report", holding_ratio_pct=5.28, previous_ratio_pct=None))
    assert [e["type"] for e in first] == ["new_stake"]
    assert [e["type"] for e in events.derive(_rec(report_kind="initial_report", holding_ratio_pct=5.28, previous_ratio_pct=0.0))] == ["new_stake"]
    assert events.derive(_rec(report_kind="initial_report", holding_ratio_pct=4.0, previous_ratio_pct=None)) == []


def test_events_never_name_a_person_behind_an_organisation_filer():
    """DATA_CHECK_20261105 重大-1 shapes: 7050 filed by the issuer for a person, 2901 English field a person's name,
    Arcus with an officer's title and name after the company."""
    by_issuer = _rec(doc_id="S100ZA48", issuer_ticker="7050", issuer_edinet_code="E34657", filer_edinet_code="E34657",
                     filer_name_ja="株式会社フロンティアインターナショナル", filer_name_en="FRONTIER INTERNATIONAL INC.", filer_name_en_filing="Yasuhiro Otsumura",
                     holders=[{"name": "乙村 康宏", "holder_type": "individual"}], data_warnings=["filed_by_issuer"],
                     holding_ratio_pct=48.67, previous_ratio_pct=53.31)
    e = events.derive(by_issuer)[0]
    assert e["filer_is_individual"] is True and e["filed_by_issuer"] is True
    assert e["summary_en"].startswith("individual filer reduced holdings") and "Otsumura" not in e["summary_en"]
    trust = _rec(holding_ratio_pct=6.0, previous_ratio_pct=5.0, filer_name_ja="トラストアップ株式会社", filer_name_en=None, filer_name_en_filing="kasou tarou", holders=[{"name": "トラストアップ株式会社", "holder_type": "corporation"}])
    e = events.derive(trust)[0]
    assert (e["filer_name_en"], e["filer_name_ja"], e["filer_is_individual"]) == (None, "トラストアップ株式会社", False)
    assert e["summary_en"].startswith("トラストアップ株式会社 raised") and "kasou" not in json.dumps(e)
    arcus = _rec(holding_ratio_pct=6.0, previous_ratio_pct=5.0, filer_name_ja="アーカス・インベストメント・リミテッド", filer_name_en_filing="Arcus Investment Limited       Director/Executive Director Edward Placeholder",
                 holders=[{"name": "アーカス・インベストメント・リミテッド", "holder_type": "corporation"}])
    e = events.derive(arcus)[0]
    assert e["filer_name_en"] == "Arcus Investment Limited" and "Placeholder" not in json.dumps(e)
    rep = _rec(holding_ratio_pct=6.0, previous_ratio_pct=5.0, filer_name_ja="ブラックロック・ジャパン株式会社代表取締役社長 戊田 浩之", filer_name_en_filing=None, holders=[])
    e = events.derive(rep)[0]
    assert e["filer_name_ja"] == "ブラックロック・ジャパン株式会社" and "戊田" not in json.dumps(e, ensure_ascii=False)


def test_change_report_without_stated_previous_uses_previous_filing_20261105():
    from japanfilings import events as ev
    prev = {"doc_id": "A", "report_kind": "initial_report", "holding_ratio_pct": 8.0, "purpose_tags": []}
    up = ev.derive({"doc_id": "B", "report_kind": "change_report", "holding_ratio_pct": 12.5, "previous_ratio_pct": None,
                    "filer_name_ja": "株式会社X", "issuer_name_ja": "株式会社Y", "purpose_tags": []}, prev)
    inc = [e for e in up if e["type"] == "stake_increase"]
    assert inc and inc[0]["change_pt"] == 4.5 and inc[0]["compared_with"] == "previous_filing"
    assert "in its previous filing" in inc[0]["summary_en"]
    out = ev.derive({"doc_id": "C", "report_kind": "change_report", "holding_ratio_pct": 0.0, "previous_ratio_pct": None,
                     "filer_name_ja": "株式会社X", "issuer_name_ja": "株式会社Y", "purpose_tags": []}, prev)
    assert [e["type"] for e in out if e["type"] == "exit_below_5pct"] == ["exit_below_5pct"]
