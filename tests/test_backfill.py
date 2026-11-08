import io, json, sqlite3, urllib.error, zipfile
import pytest
from japanfilings import backfill, events, store, trades


def mem():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(store.SCHEMA)
    con.executescript(events.SCHEMA)
    con.executescript(trades.SCHEMA)
    backfill.init(con)
    return con


FACTS = [
    {"element": "jplvh_cor:DocumentTitleCoverPage", "label": "x", "context": "FilingDateInstant", "unit": "", "value": "大量保有報告書"},
    {"element": "jplvh_cor:FilingDateCoverPage", "label": "x", "context": "FilingDateInstant", "unit": "", "value": "2021-11-08"},
    {"element": "jpdei_cor:EDINETCodeDEI", "label": "x", "context": "FilingDateInstant", "unit": "", "value": "E31893"},
    {"element": "jpdei_cor:FilerNameInJapaneseDEI", "label": "x", "context": "FilingDateInstant", "unit": "", "value": "Ｍ＆Ｇ"},
    {"element": "jplvh_cor:SecurityCodeOfIssuer", "label": "x", "context": "FilingDateInstant", "unit": "", "value": "8253"},
    {"element": "jplvh_cor:NameOfIssuer", "label": "x", "context": "FilingDateInstant", "unit": "", "value": "株式会社クレディセゾン"},
    {"element": "jplvh_cor:HoldingRatioOfShareCertificatesEtc", "label": "株券等保有割合", "context": "FilingDateInstant_x-E31893-000FilerLargeVolumeHolder1Member", "unit": "", "value": "0.0501"},
    {"element": "jplvh_cor:PurposeOfHolding", "label": "x", "context": "FilingDateInstant_x-E31893-000FilerLargeVolumeHolder1Member", "unit": "", "value": "純投資"},
]


def test_record_from_csv_alone():
    r = backfill.record_from_facts(FACTS, "D1")
    assert (r["report_kind"], r["issuer_ticker"], r["holding_ratio_pct"], r["filer_edinet_code"]) == ("initial_report", "8253", 5.01, "E31893")
    assert r["purpose_tags"] == ["pure_investment"] and r["submit_date"] == "2021-11-08"


def test_amendment_uses_list_date_and_type():
    r = backfill.record_from_facts(FACTS, "D1", {"docTypeCode": "360", "submitDateTime": "2026-10-29 09:00"})
    assert r["report_kind"] == "amendment" and r["submit_date"] == "2026-10-29"


def test_days_newest_first_and_earliest():
    assert list(backfill.days_newest_first("2026-01-03", "2026-01-01")) == ["2026-01-03", "2026-01-02", "2026-01-01"]
    from datetime import date
    assert backfill.default_earliest(date(2026, 10, 31)) == "2021-10-24"


class Fake:
    """Answers like EDINET: a list per day and a zip per document."""
    def __init__(self, days, gone=()):
        self.days, self.gone, self.requests, self.calls = days, set(gone), 0, []
    def documents(self, day):
        self.calls.append(day); self.requests += 1
        return self.days.get(day, [])
    def csv_zip(self, doc_id, cache=True):
        self.calls.append(doc_id); self.requests += 1
        if doc_id in self.gone:
            raise backfill.Gone("status 404")
        return doc_id.encode()


def doc(i, type_="350"):
    return {"docID": i, "docTypeCode": type_, "withdrawalStatus": "0", "csvFlag": "1", "edinetCode": "E1", "filerName": "x", "submitDateTime": "2026-01-02 09:00"}


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(backfill.xbrlcsv, "read_facts", lambda b: FACTS)


def far():
    import time
    return time.monotonic() + 600


def test_metadata_first_newest_first_then_resume(patched):
    con = mem()
    f = Fake({"2026-01-02": [doc("A"), doc("B", "120")], "2026-01-01": [doc("C", "360")]})
    assert backfill.list_days(con, f, "2026-01-01", "2026-01-02", far()) is True
    assert f.calls == ["2026-01-02", "2026-01-01"]
    # Only 5% and tender filings go to `filings`.
    assert {r[0] for r in con.execute("SELECT doc_id FROM filings")} == {"A", "C"}
    assert backfill.fetch_docs(con, f, far(), max_docs=1, refresh_every=0) == 1
    assert f.calls[-1] == "A"  # newest day first
    f.calls.clear()
    backfill.list_days(con, f, "2026-01-01", "2026-01-02", far())
    assert f.calls == []  # listed days are not asked again
    assert backfill.fetch_docs(con, f, far(), refresh_every=0) == 1
    assert f.calls == ["C"]
    assert backfill.status(con)["complete"] is True


def test_deadline_stops_cleanly(patched):
    con = mem()
    import time
    f = Fake({"2026-01-02": [doc("A")]})
    assert backfill.list_days(con, f, "2026-01-02", "2026-01-02", time.monotonic() - 1) is False
    assert f.requests == 0


def test_gone_and_failed_documents(patched, monkeypatch):
    con = mem()
    f = Fake({"2026-01-02": [doc("A"), doc("Z")]}, gone={"A"})
    backfill.list_days(con, f, "2026-01-02", "2026-01-02", far())
    orig = f.csv_zip
    def bad(doc_id, cache=True):
        if doc_id == "Z":
            raise RuntimeError("boom")
        return orig(doc_id, cache)
    f.csv_zip = bad
    backfill.fetch_docs(con, f, far(), refresh_every=0)
    got = dict(con.execute("SELECT doc_id, status FROM backfill_docs").fetchall())
    assert got == {"A": "gone", "Z": "failed"}


def test_history_does_not_reach_webhooks_and_stale_events_go(patched):
    con = mem()
    f = Fake({"2026-01-02": [doc("A")]})
    backfill.list_days(con, f, "2026-01-02", "2026-01-02", far())
    backfill.fetch_docs(con, f, far(), refresh_every=0)
    backfill.refresh_events(con)
    stamps = [r[0] for r in con.execute("SELECT ingested_at FROM ownership_events")]
    assert stamps and all(s.startswith("2026-01-02T00") for s in stamps)
    con.execute("INSERT INTO ownership_events VALUES ('old:x','amendment','2020-01-01','old','1','E','2020','{}')")
    assert backfill.refresh_events(con)["removed"] == 1
    # An event already delivered keeps its first-seen time.
    con.execute("UPDATE ownership_events SET ingested_at = 'KEEP'")
    backfill.refresh_events(con)
    assert {r[0] for r in con.execute("SELECT ingested_at FROM ownership_events")} == {"KEEP"}


def test_client_error_never_contains_key_and_404_is_gone(monkeypatch):
    monkeypatch.setenv("EDINET_API_KEY", "SECRET")
    def nf(url):
        raise urllib.error.HTTPError("u", 404, "nf", {}, None)
    c = backfill.Client(0, opener=nf, sleep=lambda s: None)
    with pytest.raises(backfill.Gone):
        c.get("x", {})
    def down(url):
        raise urllib.error.URLError("dns " + url)
    c = backfill.Client(0, opener=down, sleep=lambda s: None)
    with pytest.raises(RuntimeError) as e:
        c.get("x", {})
    assert "SECRET" not in str(e.value)


def test_client_refuses_to_keep_a_json_error(tmp_path, monkeypatch):
    monkeypatch.setenv("EDINET_API_KEY", "k")
    monkeypatch.setattr(backfill.edinet, "CSV_CACHE", str(tmp_path))
    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass
    c = backfill.Client(0, opener=lambda u: R(b'{"metadata":{"status":"404"}}'), sleep=lambda s: None)
    with pytest.raises(backfill.Gone):
        c.csv_zip("D")
    assert not list(tmp_path.iterdir())


def test_estimate_runs_on_empty_store():
    assert backfill.estimate(mem())["requests"] > 1800
