import json
import sqlite3

import pytest

from japanfilings import refresh, store


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.executescript(store.SCHEMA + refresh.SCHEMA + "CREATE TABLE ownership_events (doc_id TEXT, submit_date TEXT, issuer_ticker TEXT); CREATE TABLE holder_trades (doc_id TEXT, submit_date TEXT, issuer_ticker TEXT);"
                    "CREATE TABLE earnings_releases (ticker TEXT, fiscal_year_end_month TEXT, period TEXT, release_date TEXT, url TEXT, record TEXT, PRIMARY KEY (ticker, fiscal_year_end_month, period));")
    c.execute("INSERT INTO issuers (edinet_code, ticker, listed) VALUES ('E1', '7203', 1)")
    return c


def filing(c, doc, typ, withdrawn=0, day="2026-10-20", parent=None, code="E1", issuer=None, csv=1, ticker="7203"):
    c.execute("INSERT OR REPLACE INTO filings (doc_id, submit_datetime, edinet_code, ticker, doc_type_code, description_ja, issuer_edinet_code, parent_doc_id, withdrawn, has_csv) VALUES (?,?,?,?,?,?,?,?,?,?)",
              (doc, day + " 09:00", code, ticker, typ, "半期報告書－第1期(2026/04/01－2027/03/31)", issuer, parent, withdrawn, csv))


def test_gap_counts_listed_vs_parsed_per_type(con):
    filing(con, "A", "350"); filing(con, "B", "350"); filing(con, "C", "120")
    con.execute("INSERT INTO large_holdings VALUES ('A','7203','E1','2026-10-20','{}')")
    rep = refresh.gap_report(con)["datasets"]
    assert (rep["large_holdings"]["listed"], rep["large_holdings"]["missing"]) == (2, 1)
    assert rep["annual"]["missing"] == 1


def test_withdrawn_filing_removes_derived_and_is_not_requeued(con):
    filing(con, "A", "350", withdrawn=1)
    con.execute("INSERT INTO large_holdings VALUES ('A','7203','E1','2026-10-20','{}')")
    con.execute("INSERT INTO ownership_events (doc_id) VALUES ('A')")
    con.execute("INSERT INTO earnings_releases VALUES ('7203','2027-03','H1','2026-10-20','edinet:A','{}')")
    assert refresh.apply_withdrawals(con) == 1
    assert con.execute("SELECT COUNT(*) FROM large_holdings").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM ownership_events").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM earnings_releases").fetchone()[0] == 0
    assert refresh.enqueue_gaps(con, "2020-01-01") == 0


def test_amendment_marks_the_original(con):
    filing(con, "A", "350"); filing(con, "B", "360", parent="A", day="2026-10-21")
    con.execute("INSERT INTO large_holdings VALUES ('A','7203','E1','2026-10-20','{}')")
    assert refresh.apply_amendments(con) == 1
    assert json.loads(con.execute("SELECT record FROM large_holdings WHERE doc_id='A'").fetchone()[0])["superseded_by"] == "B"


def test_failed_documents_retry_then_stop(con):
    filing(con, "A", "350")
    def boom(*_):
        raise RuntimeError("secret url")
    for _ in range(refresh.MAX_ATTEMPTS + 2):
        refresh.enqueue_gaps(con, "2020-01-01")
        refresh.process_queue(con, reader=boom)
    attempts, err = con.execute("SELECT attempts, last_error FROM reconcile_queue").fetchone()
    assert attempts == refresh.MAX_ATTEMPTS and err == "RuntimeError"


def test_requeued_document_read_clears_queue(con):
    filing(con, "A", "350")
    refresh.enqueue_gaps(con, "2020-01-01")
    seen = []
    def ok(c, ds, doc):
        seen.append((ds, doc))
    refresh._rebuild = lambda *_: None
    assert refresh.process_queue(con, reader=ok)["read"] == 1
    assert seen == [("large_holdings", "A")]


def test_listings_detect_new_and_gone(con):
    base = [{"edinet_code": "E1", "ticker": "7203", "listed": True}, {"edinet_code": "E2", "ticker": "1111", "listed": True}]
    assert refresh.sync_listings(con, base, "2026-10-01") == []
    con.execute("INSERT INTO issuers (edinet_code, ticker, listed) VALUES ('E2','1111',1)")
    ch = refresh.sync_listings(con, [base[0], {"edinet_code": "E3", "ticker": "2222", "listed": True}], "2026-10-08")
    assert ("2222", "E3", "listed", "2026-10-08") in ch and ("1111", "E2", "delisted", "2026-10-08") in ch
    assert con.execute("SELECT listed FROM issuers WHERE edinet_code='E2'").fetchone()[0] == 0


def test_run_records_success_and_failure_with_class_only(con):
    refresh.record_run(con, "buybacks", None)
    ok = con.execute("SELECT last_success_at, last_error FROM dataset_runs WHERE dataset='buybacks'").fetchone()
    assert ok[0] and ok[1] is None
    refresh.record_run(con, "buybacks", "URLError")
    row = con.execute("SELECT last_success_at, last_error FROM dataset_runs WHERE dataset='buybacks'").fetchone()
    assert row == (ok[0], "URLError")


def test_publish_keeps_old_success_while_rows_are_unsent(con, tmp_path):
    refresh.record_run(con, "range", None)
    con.execute("INSERT INTO large_holdings VALUES ('A','7203','E1','2026-10-20','{}')")
    state = tmp_path / "s.db"
    s = sqlite3.connect(state); s.execute("CREATE TABLE row_hash (tbl TEXT, key TEXT, hash TEXT)"); s.commit(); s.close()
    stmts, _ = refresh.publish_sql(con, str(state), today="2026-10-21")
    line = next(x for x in stmts if "'large_holdings'" in x)
    assert line.split("VALUES (")[1].split(",")[2] == "NULL"  # last_success_at


def test_publish_ignores_rows_withheld_on_purpose(con, tmp_path):
    refresh.record_run(con, "range", None)
    con.execute("INSERT INTO large_holdings VALUES ('A','7203','E1','2026-10-20','{}')")
    state = tmp_path / "s.db"
    s = sqlite3.connect(state)
    s.execute("CREATE TABLE row_hash (tbl TEXT, key TEXT, hash TEXT)")
    s.execute("CREATE TABLE withheld_rows (tbl TEXT PRIMARY KEY, n INTEGER NOT NULL)")
    s.execute("INSERT INTO withheld_rows VALUES ('large_holdings', 1)"); s.commit(); s.close()
    stmts, _ = refresh.publish_sql(con, str(state), today="2026-10-21")
    line = next(x for x in stmts if "'large_holdings'" in x)
    assert line.split("VALUES (")[1].split(",")[2] != "NULL"


def test_ci_check_fails_on_late():
    p = {"data": [{"dataset": "a", "status": "fresh"}, {"dataset": "b", "status": "late", "status_reasons": ["run_overdue"]}, {"dataset": "c", "status": "failed"}]}
    assert len(refresh.problems(p)) == 2
    assert len(refresh.problems(p, ("b", "c"))) == 0


def test_registry_commands_are_real_targets():
    ids = {d["id"] for d in refresh.DATASETS}
    assert {"large_holdings", "annual", "half_year", "buybacks", "governance", "ratings", "tender_offers", "voting", "nport", "indicators", "trades", "cross_holdings", "issuers", "figi"} <= ids


class _FakeD1:
    def __init__(self, rows): self.rows, self.sql = rows, []
    def query(self, sql, params=()):
        self.sql.append(sql)
        return self.rows


def test_queue_reads_edinet_only_and_result_waits_for_publish(con):
    filing(con, "A", "350", code="E9", issuer="E1", ticker=None)
    order = []
    refresh._rebuild = lambda *_: None
    n = refresh.run_queue(con, _FakeD1([{"id": "rf_1", "ticker": "7203", "attempts": 1}]), reader=lambda *a: order.append("doc"))
    assert n == 1
    rid, status, res, pub = con.execute("SELECT request_id, status, result, published FROM refresh_work").fetchone()
    assert (rid, status, pub) == ("rf_1", "done", 0) and "ir_outcome" not in json.loads(res)
    d1 = _FakeD1([])
    refresh.publish(con, d1)
    assert "UPDATE refresh_requests SET status = 'done'" in d1.sql[0]
    assert con.execute("SELECT published FROM refresh_work").fetchone()[0] == 1




def test_a_parser_failing_most_new_filings_shows_as_a_gap(con):
    # Every filing is "read" (status failed), so no gap by the usual rule; a broken parser must still show.
    con.execute("CREATE TABLE voting_read (doc_id TEXT PRIMARY KEY, status TEXT NOT NULL, n_rows INTEGER, detail TEXT)")
    filing(con, "AR", "120", day="2026-10-01")
    for i in range(6):
        filing(con, f"V{i}", "180", day="2026-10-15")
        con.execute("INSERT INTO voting_read VALUES (?, 'failed', 0, NULL)", (f"V{i}",))
    assert refresh._failing(con, "voting", "2026-10-20", "2026-10-19", set()) == 6
    # A few odd failures among many good reads are not a broken parser.
    for i in range(6, 20):
        filing(con, f"V{i}", "180", day="2026-10-15")
        con.execute("INSERT INTO voting_read VALUES (?, 'read', 3, NULL)", (f"V{i}",))
    assert refresh._failing(con, "voting", "2026-10-20", "2026-10-19", set()) == 0
