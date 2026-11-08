"""Change-detected export, snapshots and the CI files, against a real SQLite store. No network."""
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import date

import pytest

from japanfilings import cli, export_d1, store

ROOT = os.path.join(os.path.dirname(__file__), "..")


def migrated():
    con = sqlite3.connect(":memory:")
    for f in sorted(glob.glob(os.path.join(ROOT, "worker", "migrations", "*.sql"))):
        con.executescript(open(f, encoding="utf-8").read())
    return con


@pytest.fixture
def env(tmp_path):
    con = store.connect(str(tmp_path / "japanfilings.db"))
    export_d1.ensure_schema(con)
    for i in range(5):
        con.execute("INSERT INTO filings (doc_id, submit_datetime, ticker) VALUES (?, '2026-10-01 09:00', '7203')", (f"S{i}",))
    con.execute("INSERT INTO cross_holdings (holder_ticker, doc_id, row_no, holder, name_ja) VALUES ('7203', 'S0', 1, 'a', 'x''y')")
    con.execute("INSERT INTO annual_reports VALUES ('A1', 'E1', '7203', '2026-03-31', '2026-06-20', ?)",
                (json.dumps({"keep": 1, "cross_holdings": [1], "text_indicators": {}}),))
    con.commit()
    return con, export_d1.open_state(str(tmp_path / "state.db"), init=True), str(tmp_path / "out"), migrated()


def apply_all(state, out, d1=None):
    for f in sorted(glob.glob(os.path.join(out, "*.sql"))):
        if d1 is not None:
            d1.executescript(open(f, encoding="utf-8").read())
        export_d1.commit_chunk(state, f[:-4] + ".json")


def test_every_exported_table_matches_the_d1_migrations(tmp_path):
    con = store.connect(str(tmp_path / "x.db"))
    export_d1.ensure_schema(con)
    d1 = migrated()
    stored = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for t in export_d1.TABLES:
        assert t in stored
        info = lambda c: [(r[1], r[5]) for r in c.execute(f"PRAGMA table_info({t})")]
        assert info(con) == info(d1), t
    # Tables outside the export are bookkeeping of the ingest, never meant for D1.
    assert stored - set(export_d1.TABLES) <= {"ratings_read", "governance_read", "filing_details_read", "voting_read", "sqlite_sequence"}
    # A table that some ingest module fills and D1 has must be exported, including ones added later.
    source = "".join(open(f, encoding="utf-8").read() for f in glob.glob(os.path.join(ROOT, "japanfilings", "*.py")))
    filled = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", source))
    in_d1 = {r[0] for r in d1.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert filled & in_d1 <= set(export_d1.TABLES)
    for t in ("api_keys", "users", "sessions", "usage", "webhooks", "login_tokens"):
        assert t not in export_d1.TABLES


def test_first_run_sends_all_second_run_nothing(env):
    con, state, out, d1 = env
    r = export_d1.delta(con, state, out)
    assert r["tables"]["filings"]["new"] == 5 and r["chunks"] >= 1
    apply_all(state, out, d1)
    assert d1.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 5
    assert d1.execute("SELECT name_ja FROM cross_holdings").fetchone()[0] == "x'y"
    assert json.loads(d1.execute("SELECT record FROM annual_reports").fetchone()[0]) == {"keep": 1}
    r = export_d1.delta(con, state, out)
    assert r["chunks"] == 0 and r["writes"] == 0


def test_changed_and_deleted_rows_only(env):
    con, state, out, d1 = env
    export_d1.delta(con, state, out)
    apply_all(state, out, d1)
    con.execute("UPDATE filings SET ticker = '9999' WHERE doc_id = 'S1'")
    con.execute("DELETE FROM filings WHERE doc_id = 'S2'")
    con.execute("UPDATE cross_holdings SET name_ja = 'z'")
    con.commit()
    r = export_d1.delta(con, state, out)
    assert r["tables"]["filings"]["changed"] == 1 and r["tables"]["filings"]["deleted"] == 1
    apply_all(state, out, d1)
    assert d1.execute("SELECT ticker FROM filings WHERE doc_id = 'S1'").fetchone()[0] == "9999"
    assert d1.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 4
    # no primary key in D1: replaced, not duplicated
    assert d1.execute("SELECT COUNT(*), MIN(name_ja) FROM cross_holdings").fetchone() == (1, "z")
    assert export_d1.delta(con, state, out)["chunks"] == 0


def test_empty_table_is_refused_and_leaves_no_chunks(env):
    con, state, out, _ = env
    export_d1.delta(con, state, out)
    apply_all(state, out)
    con.execute("DELETE FROM filings")
    con.commit()
    with pytest.raises(export_d1.ExportRefused):
        export_d1.delta(con, state, out)
    assert os.listdir(out) == []


def test_mass_delete_is_refused_unless_allowed(env):
    con, state, out, d1 = env
    con.executemany("INSERT INTO filings (doc_id) VALUES (?)", [(f"X{i}",) for i in range(300)])
    con.commit()
    export_d1.delta(con, state, out)
    apply_all(state, out, d1)
    con.execute("DELETE FROM filings WHERE doc_id LIKE 'X%' AND doc_id != 'X0'")
    con.commit()
    with pytest.raises(export_d1.ExportRefused):
        export_d1.delta(con, state, out)
    assert export_d1.delta(con, state, out, allow_mass_delete=True)["tables"]["filings"]["deleted"] == 299


def test_daily_cap_defers_and_later_run_finishes(env):
    con, state, out, d1 = env
    r = export_d1.delta(con, state, out, tables=["filings"], daily_cap=10)  # 5 estimated writes a row: the D1 indexes count
    assert r["writes"] == 10 and r["deferred"] == 3
    apply_all(state, out, d1)
    assert export_d1.delta(con, state, out, tables=["filings"], daily_cap=10)["writes"] == 0  # the day's budget is spent
    state.execute("DELETE FROM writes")  # the next UTC day
    r = export_d1.delta(con, state, out, tables=["filings"], daily_cap=100)
    assert r["writes"] == 15 and r["deferred"] == 0
    apply_all(state, out, d1)
    assert d1.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 5


def test_big_rows_split_into_short_statements_and_arrive_whole(env):
    con, state, out, d1 = env
    text = "日本語'" * 60_000
    con.execute("INSERT INTO us_fund_holdings VALUES ('7203', '2026q3', ?)", (text,))
    con.commit()
    export_d1.delta(con, state, out)
    for f in glob.glob(os.path.join(out, "*.sql")):
        assert max(len(line.encode()) for line in open(f, encoding="utf-8")) <= 100_000
    apply_all(state, out, d1)
    assert d1.execute("SELECT record FROM us_fund_holdings").fetchone()[0] == text


def test_chunks_respect_the_size_limit(env):
    con, state, out, _ = env
    assert export_d1.delta(con, state, out, tables=["filings"], chunk_writes=10)["chunks"] == 3


def test_push_stops_at_a_failure_and_resumes(env, tmp_path):
    con, state, out, d1 = env
    export_d1.delta(con, state, out, tables=["filings"], chunk_writes=10)
    fail_on = tmp_path / "fail"
    fail_on.write_text("0002")
    fake = tmp_path / "fake.py"
    fake.write_text(f"import sys\nsys.exit(1 if open({str(fail_on)!r}).read() in sys.argv[1] else 0)\n")
    with pytest.raises(RuntimeError):
        export_d1.push(state, out, f"{sys.executable} {fake} {{file}}", retries=2, wait=0)
    assert state.execute("SELECT COUNT(*) FROM row_hash").fetchone()[0] == 2  # chunk 1 only
    fail_on.write_text("nothing")
    r = export_d1.delta(con, state, out, tables=["filings"], chunk_writes=10)
    assert r["writes"] == 15  # three rows left, nothing already accepted is sent again
    assert export_d1.push(state, out, f"{sys.executable} {fake} {{file}}", retries=1, wait=0) == 2
    assert export_d1.delta(con, state, out, tables=["filings"])["writes"] == 0


def test_missing_state_is_refused(tmp_path):
    with pytest.raises(export_d1.ExportRefused):
        export_d1.open_state(str(tmp_path / "none.db"))


def test_baseline_sends_nothing_afterwards(env):
    con, state, out, _ = env
    assert export_d1.baseline(con, state) >= 7
    assert export_d1.delta(con, state, out)["chunks"] == 0


def _store_dir(tmp_path, name, filings):
    d = tmp_path / name
    con = store.connect(str(d / "japanfilings.db"))
    con.executemany("INSERT INTO filings (doc_id) VALUES (?)", [(f"S{i}",) for i in range(filings)])
    con.commit()
    con.close()
    return str(d)


def test_snapshot_round_trip_keeps_store_state_and_isin_map(tmp_path):
    src = _store_dir(tmp_path, "src", 1500)
    export_d1.open_state(os.path.join(src, "d1_state.db"), init=True).execute("INSERT INTO writes VALUES ('d', 1)").connection.commit()
    os.makedirs(os.path.join(src, "nport"))
    open(os.path.join(src, "nport", "isin_map.json"), "w").write("{}")
    archive = str(tmp_path / "s.tar.gz")
    cli.snapshot(archive, src)
    dst = str(tmp_path / "dst")
    assert cli.restore(archive, dst)["filings"] == 1500
    assert os.path.exists(os.path.join(dst, "d1_state.db")) and os.path.exists(os.path.join(dst, "nport", "isin_map.json"))
    with pytest.raises(ValueError):  # an existing store is never overwritten by accident
        cli.restore(archive, dst)


def test_restore_refuses_a_nearly_empty_snapshot_and_a_corrupt_file(tmp_path):
    src = _store_dir(tmp_path, "src", 3)
    archive = str(tmp_path / "s.tar.gz")
    cli.snapshot(archive, src)
    dst = tmp_path / "dst"
    with pytest.raises(ValueError):
        cli.restore(archive, str(dst))
    assert not (dst / "japanfilings.db").exists()
    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(b"not an archive")
    with pytest.raises(Exception):
        cli.restore(str(bad), str(dst))


def test_snapshot_refuses_a_store_that_shrank(tmp_path):
    src = _store_dir(tmp_path, "src", 1500)
    archive = str(tmp_path / "s.tar.gz")
    cli.snapshot(archive, src)
    dst = str(tmp_path / "dst")
    cli.restore(archive, dst)
    con = sqlite3.connect(os.path.join(dst, "japanfilings.db"))
    con.execute("DELETE FROM filings")
    con.commit()
    con.close()
    with pytest.raises(ValueError):
        cli.store_check(dst)
    with pytest.raises(ValueError):
        cli.snapshot(str(tmp_path / "t.tar.gz"), dst)
    assert not (tmp_path / "t.tar.gz").exists()


def test_freshness():
    today = "2026-11-10"
    assert cli.status_problem({"ok": True, "latest_large_holding_submit_date": "2026-11-09"}, today, 5) is None
    assert "days old" in cli.status_problem({"ok": True, "latest_large_holding_submit_date": "2026-11-01"}, today, 5)
    assert cli.status_problem({"ok": True, "latest_large_holding_submit_date": None}, today, 5)
    assert cli.status_problem(None, today, 5)


def test_files_ci_needs_are_committed_and_secrets_are_not():
    def ignored(path):
        return subprocess.run(["git", "check-ignore", "--no-index", "-q", path], cwd=tmp).returncode == 0
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
        open(os.path.join(tmp, ".gitignore"), "w").write(open(os.path.join(ROOT, ".gitignore")).read())
        assert not ignored("worker/package-lock.json")
        for path in ("data/japanfilings.db", "data/key_pro.txt", "data/d1_state.db", "data/nport/isin_map.json", "data/csv_cache/a", ".env"):
            assert ignored(path), path


def test_workflow_uses_every_scheduled_job_and_no_secret_in_script_text():
    text = open(os.path.join(ROOT, ".github", "workflows", "ingest.yml"), encoding="utf-8").read()
    for job in ("buybacks", "governance", "ratings", "indicators", "halfyear", "codelist", "figi"):
        assert f"japanfilings.cli {job}" in text, job
    for gone in ("releases-sweep", "cli calls", "cli releases"):  # company websites are not read
        assert gone not in text, gone
    for line in text.splitlines():  # secrets and inputs reach scripts through env, never pasted into shell text
        if "${{" in line and ("secrets." in line or "inputs." in line):
            assert re.match(r"\s*([A-Z_]+:|if:)", line), line
