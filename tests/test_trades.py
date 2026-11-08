"""Parsing the 60-day trade table of 5% reports, whose cells the XBRL CSV runs together."""
from japanfilings import trades

HEAD = "(5)【当該株券等の発行者の発行する株券等に関する最近60日間の取得又は処分の状況】年月日株券等の種類数量割合市場内外取引の別取得又は処分の別単価"


def test_cells_run_together_split_on_column_order():
    rows = trades.parse_block(HEAD + "2026年9月2日普通株式44,5000.01市場内取得2026年9月2日普通株式6,8000.00市場外取得2,706円"
                                     "2026年10月13日普通株式316,0000.97市場外処分1,735.60")
    assert [(r["trade_date"], r["shares"], r["pct_of_outstanding"], r["venue"], r["side"], r["unit_price_jpy"]) for r in rows] == [
        ("2026-09-02", 44500, 0.01, "on_market", "acquisition", None),
        ("2026-09-02", 6800, 0.0, "off_market", "acquisition", 2706),
        ("2026-10-13", 316000, 0.97, "off_market", "disposal", 1735.6),
    ]


def test_era_dates_negative_quantities_price_before_next_date_and_kinds():
    rows = trades.parse_block(HEAD + "令和8年7月27日普通株式-53,900-0.23市場外処分現物移管令和8年7月28日普通株式-3000.00市場外処分2307.03"
                                     "令和8年7月30日普通株式-8,200-0.01市場外処分2025令和8年8月1日普通株券1,9000.00市場外取得貸借"
                                     "2026年10月7日新株予約権1,411,00045.95市場外取得18.28(第三者割当による引受)")
    assert [(r["trade_date"], r["shares"], r["unit_price_jpy"], r["kind"]) for r in rows] == [
        ("2026-07-27", 53900, None, "transfer"), ("2026-07-28", 300, 2307.03, "trade"), ("2026-07-30", 8200, 2025, "trade"),
        ("2026-08-01", 1900, None, "lending"), ("2026-10-07", 1411000, 18.28, "trade"),
    ]
    assert rows[-1]["security_ja"] == "新株予約権" and rows[-1]["note_ja"] == "(第三者割当による引受)"


def test_one_decimal_ratio_and_empty_table():
    assert trades.parse_block(HEAD + "2026年8月17日普通株式2,999,7008.9市場外取得")[0]["shares"] == 2999700
    assert trades.parse_block(HEAD) == [] and trades.parse_block("－") == []


def test_convertible_bond_price_is_percent_of_par_not_yen():
    """S100Z92V, S100Z6EY, S100Z84S, S100Z6GI: 95.95 / 100.0 on 新株予約権付社債券 are % of face value, not yen per share."""
    rows = trades.parse_block(HEAD + "2026年10月1日新株予約権付社債券1,000,0002.35市場外取得95.95"
                                     "2026年10月2日普通株式3000.00市場外取得2,706円")
    bond, share = rows
    assert bond["unit_price_jpy"] is None and bond["price_pct_of_par"] == 95.95
    assert "額面100に対する割合" in bond["note_ja"] and "95.95" in bond["note_ja"]
    assert share["unit_price_jpy"] == 2706 and "price_pct_of_par" not in share
    assert trades.label_bond_price(dict(bond))["price_pct_of_par"] == 95.95  # idempotent


def test_trade_date_misprints_are_corrected_only_with_evidence():
    """S100P7FO 平成4年 for 令和4年 (30 years early), S100YTS8 2027 in a 2026 filing: corrected. S100XQ8N 1202-02-27 and
    S100RQG4 1900-07-05 fit no evident misprint: the date is dropped (date filters skip the row) and the printed one kept."""
    check = trades.check_trade_date
    assert check("1992-09-13", "2022-09-13", "2022-09-21") == ("2022-09-13", "data_warning=trade_date_corrected(printed 1992-09-13)")
    assert check("2027-06-22", "2026-08-01", "2026-08-07")[0] == "2026-06-22"
    assert check("2062-07-01", "2026-08-01", "2026-08-07")[0] == "2026-07-01"  # digits swapped
    assert check("1202-02-27", "2026-03-06", "2026-03-12") == (None, "data_warning=trade_date_implausible(printed 1202-02-27)")
    assert check("1900-07-05", "2023-08-17", "2023-08-21")[0] is None
    assert check("2026-05-01", "2026-04-20", "2026-04-25") == (None, "data_warning=trade_date_implausible(printed 2026-05-01)")


def test_plausible_dates_outside_the_window_are_kept_with_a_warning():
    check = trades.check_trade_date
    assert check("2026-09-01", "2026-10-01", "2026-10-07") == ("2026-09-01", None)
    assert check("2026-10-03", "2026-10-01", "2026-10-07") == ("2026-10-03", "data_warning=trade_date_after_obligation_date(2026-10-01)")
    assert check("2023-01-19", "2024-02-14", "2024-02-20") == ("2023-01-19", "data_warning=trade_date_before_60day_window(2024-02-14)")
    # a late filing: the obligation date is old but the trades fit it
    assert check("2012-12-17", "2013-01-15", "2021-12-24") == ("2012-12-17", None)
    # no obligation date: the submit date is the anchor
    assert check("2026-01-01", None, "2026-10-07")[1] == "data_warning=trade_date_before_60day_window(2026-10-07)"


def test_rebuild_keeps_misdated_rows_with_warning():
    import json
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE large_holdings (doc_id TEXT, submit_date TEXT, record TEXT)")
    rec = {"doc_id": "S1", "report_kind": "change_report", "obligation_date": "2026-03-06", "submit_date": "2026-03-12",
           "issuer_ticker": "1234", "filer_edinet_code": "E1",
           "trades": [{"trade_date": "1202-02-27", "shares": 100, "pct_of_outstanding": 0.1, "venue": "on_market",
                       "side": "acquisition", "note_ja": "x", "kind": "trade"}]}
    con.execute("INSERT INTO large_holdings VALUES ('S1', '2026-03-12', ?)", (json.dumps(rec),))
    assert trades.rebuild(con) == 1
    row = con.execute("SELECT trade_date, note_ja FROM holder_trades").fetchone()
    assert row["trade_date"] is None and row["note_ja"] == "x / data_warning=trade_date_implausible(printed 1202-02-27)"


def test_spaced_cells_units_percent_signs_and_blank_ratio():
    """Real layouts that were skipped: cells separated by spaces/tabs (S100RQAN), 株 and % printed (S100U67F, S100PFTE),
    an integer ratio (S100QKDZ), no ratio at all (S100WMRA)."""
    rows = trades.parse_block(HEAD + "                    2023年6月19日      普通株式      400,000      0.53      市場外      処分      2317.50"
                                     "令和6年6月19日      普通株式      100株      0.00%      市場外      処分      貸借取引              "
                                     "2022年9月13日普通株式3000.00%市場外取引取得2,550円"
                                     " 令和5年3月22日 株券(普通株式) 307,000 27% 市場外 処分 2,898"
                                     "2025年8月20日普通株式5,000市場内処分"
                                     "2024年1月26日\t\t\t普通株\t\t\t12000.01\t\t\t市場内\t\t\t取得")
    assert [(r["trade_date"], r["security_ja"], r["shares"], r["pct_of_outstanding"], r["side"], r["unit_price_jpy"], r["kind"])
            for r in rows] == [
        ("2023-06-19", "普通株式", 400000, 0.53, "disposal", 2317.5, "trade"),
        ("2024-06-19", "普通株式", 100, 0.0, "disposal", None, "lending"),
        ("2022-09-13", "普通株式", 300, 0.0, "acquisition", 2550, "trade"),
        ("2023-03-22", "株券(普通株式)", 307000, 27.0, "disposal", 2898, "trade"),
        ("2025-08-20", "普通株式", 5000, None, "disposal", None, "trade"),
        ("2024-01-26", "普通株", 1200, 0.01, "acquisition", None, "trade"),
    ]


def test_price_after_per_unit_wording_and_misprinted_separator():
    rows = trades.parse_block(HEAD + "2026年1月5日普通株式100市場外取得1株当たり150円(第三者割当)"
                                     "2026年1月6日新株予約権証券1000.01市場外処分1:10株式併合により減少"
                                     "2025年12月22日普通株式138,5000.23市場外取得1.869.65"
                                     "2026年1月7日株券1,6030.01市場外取得株券寄託取引")
    assert [(r["unit_price_jpy"], r["note_ja"], r["kind"]) for r in rows] == [
        (150, "(第三者割当)", "trade"), (None, "1:10株式併合により減少", "trade"), (1869.65, None, "trade"),
        (None, "株券寄託取引", "transfer")]


def _store(*recs):
    import json
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE large_holdings (doc_id TEXT, submit_date TEXT, record TEXT)")
    for rec in recs:
        con.execute("INSERT INTO large_holdings VALUES (?, ?, ?)", (rec["doc_id"], rec["submit_date"], json.dumps(rec)))
    return con


def _trade(**kw):
    return {"trade_date": "2026-09-01", "security_ja": "普通株式", "shares": 100, "pct_of_outstanding": 0.01,
            "venue": "on_market", "side": "acquisition", "unit_price_jpy": None, "note_ja": None, "kind": "trade",
            "holder": "マッコーリー バンク リミテッド (Macquarie Bank Limited)", **kw}


def test_overlapping_windows_dedup_across_printing_differences_but_keep_repeats():
    """The next report reprints the same rows with the price filled in, 株券(普通株式) for 普通株式, the holder without
    its English name and 貸借 noted; it also lists a third identical lot traded the same day, which is new."""
    base = {"report_kind": "change_report", "obligation_date": "2026-09-01", "issuer_ticker": "1234", "filer_edinet_code": "E1"}
    a = {**base, "doc_id": "S1", "submit_date": "2026-09-05", "trades": [_trade(), _trade()]}
    b = {**base, "doc_id": "S2", "submit_date": "2026-09-20", "obligation_date": "2026-09-15",
         "trades": [_trade(unit_price_jpy=2706, security_ja="株券(普通株式)", holder="マッコーリー バンク リミテッド",
                           note_ja="貸借", kind="lending"), _trade(), _trade()]}
    con = _store(a, b)
    assert trades.rebuild(con) == 3
    got = [tuple(r) for r in con.execute("SELECT doc_id, unit_price_jpy FROM holder_trades ORDER BY doc_id, row_no")]
    assert got == [("S1", 2706), ("S1", None), ("S2", None)]


def test_price_guard_covers_bare_common_share_labels_only():
    assert trades.is_common("株券(普通株式)") and trades.is_common("株券") and trades.is_common("普通")
    assert not trades.is_common("新株予約権証券") and not trades.is_common("投資証券") and not trades.is_common("株券(A種種類株式)")
    rec = {"doc_id": "S1", "submit_date": "2026-09-05", "report_kind": "change_report", "obligation_date": "2026-09-01",
           "issuer_ticker": "6740", "filer_edinet_code": "E1", "trades": [_trade(security_ja="株券", unit_price_jpy=86680000000)]}
    con = _store(rec)
    trades.rebuild(con)
    row = con.execute("SELECT unit_price_jpy, note_ja FROM holder_trades").fetchone()
    assert row[0] is None and row[1] == "data_warning=unit_price_implausible(printed 86680000000)"


def test_date_inside_a_note_parenthesis_stays_in_the_note():
    # S100NLSN, S100PU0K, S100WV5J: a date in the note's parenthesis cut the note at "(".
    from japanfilings.trades import parse_block
    rows = parse_block("令和3年12月29日普通株式5,884,7004.15市場外取得株式分割(効力発生日は令和4年1月1日)"
                       "令和4年1月5日普通株式1,0000.01市場内処分")
    assert [r["note_ja"] for r in rows] == ["株式分割(効力発生日は令和4年1月1日)", None]
    assert rows[0]["kind"] == "transfer" and rows[1]["side"] == "disposal"
    # A note left open before the next row (S100P7VV) does not swallow that row.
    rows = parse_block("令和4年8月4日普通株式1,300,0004.49市場外取得89(新株予約権の行使による取得"
                       "令和4年9月16日新株予約権657,7002.16市場外処分新株予約権の行使")
    assert len(rows) == 2 and rows[0]["note_ja"] == "(新株予約権の行使による取得"
