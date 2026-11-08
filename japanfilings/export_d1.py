"""Export the local SQLite store as SQL for Cloudflare D1, and issue API keys.

Change-detected export (what the ingest workflow uses). A state file holds one content hash per row
already in D1; only new, changed and removed rows are sent, in chunks, and a chunk's hashes are recorded
only after D1 accepted it, so an interrupted push resumes where it stopped:

  python -m japanfilings.export_d1 delta --out-dir data/d1_delta --state data/d1_state.db [--profile fast|all]
                                         [--init] [--daily-cap N] [--allow-mass-delete]
  python -m japanfilings.export_d1 push --dir data/d1_delta --state data/d1_state.db
  python -m japanfilings.export_d1 baseline --state data/d1_state.db   # D1 already holds this store: record hashes only

Whole-table SQL for a local database:

  python -m japanfilings.export_d1 data/d1_load.sql [--since YYYY-MM-DD]   # --since: recent filings only
  npx wrangler d1 execute japanfilings --local --file ../data/d1_load.sql   (from worker/)

  python -m japanfilings.export_d1 --new-key pro [customer_ref]   # prints the key once and its SQL
"""
import argparse
import hashlib
import json
import os
import secrets
import shlex
import sqlite3
import subprocess
import sys
import re
import time
from datetime import datetime, timezone

from . import (buybacks, lexicon, crossholdings, events, filing_details, governance,
               indicators, investors, people, ratings, store, tanshin, tender_offers, trades, voting)

TABLES = ["issuers", "identifiers", "large_holdings", "filings", "annual_reports", "us_fund_holdings",
          "ownership_events", "cross_holdings", "cross_holding_summary", "holder_trades",
          "company_indicators", "policy_uncertainty_monthly", "earnings_releases", "disclosed_ratings", "buyback_reports", "governance_events",
          "filing_details", "voting_results", "tender_offers", *investors.D1_TABLES]
# company_facts and sector_benchmarks are not here: the Worker builds them inside D1 once a day (screener.ts).


def _lit(v):
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


# Incremental exports only send recent filings, to stay inside D1's daily write allowance.
RECENT_FILTER = {"large_holdings": "submit_date >= ?", "filings": "substr(submit_datetime, 1, 10) >= ?", "annual_reports": "submit_date >= ?",
                 "ownership_events": "submit_date >= ?", "holder_trades": "submit_date >= ?",
                 "earnings_releases": "release_date >= ?", "disclosed_ratings": "submit_date >= ?", "buyback_reports": "submit_date >= ?", "governance_events": "submit_date >= ?",
                 "filing_details": "submit_date >= ?", "voting_results": "submit_date >= ?", "tender_offers": "substr(submit_datetime, 1, 10) >= ?"}
# Rebuilt as a whole from annual reports; sent only with full exports (weekly), cleared first so stale rows go.
REPLACE_WHOLE = {"cross_holdings", "cross_holding_summary", "company_indicators", "policy_uncertainty_monthly"}


def ensure_schema(con):
    """Every table the export reads, so a store where a job has not run yet still exports."""
    con.executescript(events.SCHEMA + crossholdings.SCHEMA + trades.SCHEMA + indicators.SCHEMA + tanshin.SCHEMA
                      + ratings.SCHEMA + buybacks.SCHEMA + governance.SCHEMA)
    con.executescript(filing_details.SCHEMA + voting.SCHEMA + tender_offers.SCHEMA + investors.SCHEMA)


_WITHHELD = []


def _withheld():
    """Names never published, from the private word lists (section "withheld"). Missing lists stop the export."""
    if not _WITHHELD:
        _WITHHELD.append(re.compile("|".join(lexicon.load("withheld"))))
    return _WITHHELD[0]


def _drop_withheld_examples(rec, rx):
    for v in rec.values():
        if isinstance(v, dict) and isinstance(v.get("examples"), list):
            v["examples"] = [e for e in v["examples"] if not rx.search(json.dumps(e, ensure_ascii=False))]


_TITLE_NAME = [(re.compile(r"(選任)\s+(?!の件)[^\s、。]{1,4}\s*[^\s、。]{0,4}$"), r"\1"),
               (re.compile(r"候補者\s*[^\s、。をにがはの]{1,10}?氏"), "候補者")]


def scrub_proposal_title(title):
    """Candidate names never leave the store: a name stuck to a proposal title is cut."""
    if not title:
        return title
    for rx, rep in _TITLE_NAME:
        title = rx.sub(rep, title)
    return title


def _export_row(t, cols, row, masker=None):
    """Cross-holdings and trades are served from their own tables; keeps rows under D1's statement limit.
    Returns None for a row that names a withheld name; quoted example sentences that do are dropped instead.
    Last, every string (JSON records as text) goes through people.Masker: natural persons' names become [氏名省略].
    `masker` is people.masker(con) for the store being exported; without it the default store's is used."""
    row = list(row)
    if t == "voting_results":  # officer and candidate names are not published
        row[cols.index("candidate_ja")] = None
        i = cols.index("proposal_title_ja")
        row[i] = scrub_proposal_title(row[i])
    rx = _withheld()
    if "record" in cols and isinstance(row[cols.index("record")], str) and rx.search(row[cols.index("record")]):
        i = cols.index("record")
        rec = json.loads(row[i])
        _drop_withheld_examples(rec, rx)
        row[i] = json.dumps(rec, ensure_ascii=False)
    if t in ("annual_reports", "large_holdings"):
        i = cols.index("record")
        rec = json.loads(row[i])
        rec.pop("cross_holdings", None)
        rec.pop("trades", None)
        rec.pop("text_indicators", None)
        row[i] = json.dumps(rec, ensure_ascii=False)
    if any(isinstance(v, str) and rx.search(v) for v in row):
        return None
    m = masker or people.masker()
    return [m.mask(v) if isinstance(v, str) else v for v in row]


def export(con, out, since=None):
    m = people.masker(con)
    for t in TABLES:
        if since and t not in RECENT_FILTER:
            continue
        if t in REPLACE_WHOLE:
            out.write(f"DELETE FROM {t};\n")
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
        where, args = (f" WHERE {RECENT_FILTER[t]}", (since,)) if since else ("", ())
        for row in con.execute(f"SELECT {','.join(cols)} FROM {t}{where}", args):
            row = _export_row(t, cols, row, m)
            if row is None:
                continue
            out.write(f"INSERT OR REPLACE INTO {t} ({','.join(cols)}) VALUES ({','.join(_lit(v) for v in row)});\n")


# ---------- change-detected export ----------

# Sent by the 10-minute run. The others change as a whole or only overnight, so they go out once a day.
FAST_TABLES = ["filings", "large_holdings", "annual_reports", "ownership_events", "holder_trades"]
# Tables with no primary key in D1: a changed row is deleted and inserted again by this key.
NO_PK_KEYS = {"cross_holdings": ["doc_id", "row_no", "holder"]}
MAX_STATEMENT_BYTES = 95_000  # D1 allows 100 KB per statement
CHUNK_BYTES = 5_000_000
CHUNK_WRITES = 25_000
FREE_DAILY_WRITES = 100_000  # D1 free plan; the cap below leaves room for the Worker's own writes
DEFAULT_DAILY_CAP = 80_000
MASS_DELETE_SHARE = 0.2
MASS_DELETE_MIN = 100


class ExportRefused(Exception):
    """The store looks wrong: nothing may be sent to D1."""


def open_state(path, init=False):
    if not init and not os.path.exists(path):
        raise ExportRefused(f"{path} is missing. If D1 already holds this store run `baseline`; "
                            "if D1 is empty run `delta --init`. See docs/DEPLOY.md.")
    con = sqlite3.connect(path, timeout=60)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS row_hash (tbl TEXT, key TEXT, hash TEXT, PRIMARY KEY (tbl, key)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS writes (day TEXT PRIMARY KEY, n INTEGER NOT NULL);
    """)
    return con


def _today_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")  # D1's free daily limits reset at 00:00 UTC


_d1_indexes = {}


def _d1_index_count(t):
    """Indexes on the table in D1, which can have more than the store (read from the migrations)."""
    if not _d1_indexes:
        d1 = sqlite3.connect(":memory:")
        folder = os.path.join(os.path.dirname(__file__), "..", "worker", "migrations")
        for name in sorted(os.listdir(folder)):
            if name.endswith(".sql"):
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    d1.executescript(f.read())
        for (name,) in d1.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
            _d1_indexes[name] = len(d1.execute(f"PRAGMA index_list({name})").fetchall())
    return _d1_indexes.get(t, 0)


def _table_meta(con, t):
    info = con.execute(f"PRAGMA table_info({t})").fetchall()
    cols = [r[1] for r in info]
    pk = [r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5])]
    keys = pk or NO_PK_KEYS[t]
    # Cloudflare counts the table row plus one row per index. Automatic primary-key indexes are counted
    # too, which may overstate: the budget must err on the safe side.
    writes = 1 + max(len(con.execute(f"PRAGMA index_list({t})").fetchall()), _d1_index_count(t))
    return cols, keys, bool(pk), writes


def _hash(values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:24]


def _where(keys, values):
    return " AND ".join(f"{k} IS {_lit(v)}" for k, v in zip(keys, values))


def _upsert_sql(t, cols, keys, has_pk, row):
    """Statements for one row, each under D1's statement limit."""
    key_vals = [row[cols.index(k)] for k in keys]
    insert_cols = ",".join(cols)
    # NULL in a primary key never conflicts, so such a row is replaced by hand like a table without a key.
    by_hand = not has_pk or any(v is None for v in key_vals)
    tail, prefix = "", []
    if by_hand:
        prefix = [f"DELETE FROM {t} WHERE {_where(keys, key_vals)};"]
    else:
        others = [c for c in cols if c not in keys]
        action = "DO UPDATE SET " + ",".join(f"{c}=excluded.{c}" for c in others) if others else "DO NOTHING"
        tail = f" ON CONFLICT({','.join(keys)}) {action}"
    sql = f"INSERT INTO {t} ({insert_cols}) VALUES ({','.join(_lit(v) for v in row)}){tail};"
    if len(sql.encode()) <= MAX_STATEMENT_BYTES:
        return prefix + [sql]
    # Oversized text: insert it empty, then append in pieces.
    big = max((i for i, v in enumerate(row) if isinstance(v, str)), key=lambda i: len(row[i].encode()))
    empty = list(row)
    empty[big] = ""
    out = prefix + _upsert_sql(t, cols, keys, has_pk, empty)
    text, step = row[big], MAX_STATEMENT_BYTES // 8  # at most 4 bytes a character, doubled when a quote is escaped
    for start in range(0, len(text), step):
        out.append(f"UPDATE {t} SET {cols[big]} = {cols[big]} || {_lit(text[start:start + step])} WHERE {_where(keys, key_vals)};")
    return out


class _Chunks:
    """Writes NNNN.sql and NNNN.json (the hashes to record once D1 has accepted the file)."""

    def __init__(self, out_dir, chunk_bytes, chunk_writes):
        self.dir, self.max_bytes, self.max_writes, self.n = out_dir, chunk_bytes, chunk_writes, 0
        self.lines, self.rows, self.bytes, self.writes = [], [], 0, 0

    def add(self, statements, state_row, writes):
        size = sum(len(x.encode()) + 1 for x in statements)
        if self.rows and (self.bytes + size > self.max_bytes or self.writes + writes > self.max_writes):
            self.flush()
        self.lines += statements
        self.rows.append(state_row)
        self.bytes += size
        self.writes += writes

    def flush(self):
        if not self.rows:
            return
        self.n += 1
        base = os.path.join(self.dir, f"{self.n:04d}")
        with open(base + ".sql", "w", encoding="utf-8") as f:
            f.write("\n".join(self.lines) + "\n")
        with open(base + ".json", "w", encoding="utf-8") as f:
            json.dump({"writes": self.writes, "rows": self.rows}, f, ensure_ascii=False)
        self.lines, self.rows, self.bytes, self.writes = [], [], 0, 0


def delta(con, state, out_dir, tables=None, daily_cap=DEFAULT_DAILY_CAP, allow_mass_delete=False,
          chunk_bytes=CHUNK_BYTES, chunk_writes=CHUNK_WRITES):
    """Write chunks for rows that differ from the state. Rows over the day's write budget stay unsent and
    go out on a later run. Raises ExportRefused, leaving no chunks, if the store looks damaged."""
    os.makedirs(out_dir, exist_ok=True)
    for name in os.listdir(out_dir):
        os.remove(os.path.join(out_dir, name))
    used = (state.execute("SELECT n FROM writes WHERE day = ?", (_today_utc(),)).fetchone() or (0,))[0]
    budget = max(0, daily_cap - used)
    chunks = _Chunks(out_dir, chunk_bytes, chunk_writes)
    summary, refused = {}, []
    m = people.masker(con)
    for t in tables or TABLES:
        cols, keys, has_pk, per_row = _table_meta(con, t)
        known = dict(state.execute("SELECT key, hash FROM row_hash WHERE tbl = ?", (t,)))
        before = len(known)
        s = summary[t] = {"rows": 0, "new": 0, "changed": 0, "deleted": 0, "deferred": 0, "writes": 0}
        for raw in con.execute(f"SELECT {','.join(cols)} FROM {t}"):
            row = _export_row(t, cols, raw, m)
            if row is None:  # stays in `known`, so a copy already in D1 is deleted
                continue
            s["rows"] += 1
            key = json.dumps([row[cols.index(k)] for k in keys], ensure_ascii=False)
            h = _hash(row)
            old = known.pop(key, None)
            if old == h:
                continue
            if per_row > budget:
                s["deferred"] += 1
                continue
            s["new" if old is None else "changed"] += 1
            budget -= per_row
            s["writes"] += per_row
            chunks.add(_upsert_sql(t, cols, keys, has_pk, row), [t, key, h], per_row)
        gone = len(known)
        if before and not s["rows"]:
            refused.append(f"{t}: the store has no rows but D1 has {before}")
        elif gone > MASS_DELETE_MIN and gone > before * MASS_DELETE_SHARE and not allow_mass_delete:
            refused.append(f"{t}: {gone} of {before} rows would be deleted")
        for key in known:
            if per_row > budget:
                s["deferred"] += 1
                continue
            s["deleted"] += 1
            budget -= per_row
            s["writes"] += per_row
            chunks.add([f"DELETE FROM {t} WHERE {_where(keys, json.loads(key))};"], [t, key, None], per_row)
    if refused and not allow_mass_delete:
        for name in os.listdir(out_dir):
            os.remove(os.path.join(out_dir, name))
        raise ExportRefused("; ".join(refused) + ". Check the store (docs/DEPLOY.md), then use --allow-mass-delete if this is intended.")
    chunks.flush()
    return {"chunks": chunks.n, "tables": summary, "writes": sum(x["writes"] for x in summary.values()),
            "deferred": sum(x["deferred"] for x in summary.values())}


def commit_chunk(state, json_path):
    with open(json_path, encoding="utf-8") as f:
        c = json.load(f)
    with state:
        state.executemany("INSERT OR REPLACE INTO row_hash VALUES (?,?,?)", [r for r in c["rows"] if r[2] is not None])
        state.executemany("DELETE FROM row_hash WHERE tbl = ? AND key = ?", [r[:2] for r in c["rows"] if r[2] is None])
        state.execute("INSERT INTO writes VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET n = n + excluded.n", (_today_utc(), c["writes"]))
    return c["writes"]


def push(state, directory, command, cwd=None, retries=3, wait=30):
    """Send each chunk in order, recording its hashes after it succeeds. Sending again is harmless, so a
    failure leaves the remaining rows for the next run. Returns the number of chunks sent."""
    names = sorted(n[:-4] for n in os.listdir(directory) if n.endswith(".sql"))
    for sent, name in enumerate(names):
        path = os.path.abspath(os.path.join(directory, name + ".sql"))
        for attempt in range(1, retries + 1):
            # Only the exit code is used: wrangler's output could carry filing text into public logs at length.
            r = subprocess.run([a.replace("{file}", path) for a in shlex.split(command)], cwd=cwd,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            if r.returncode == 0:
                break
            print(f"chunk {name} attempt {attempt}/{retries} failed: {r.stderr[-500:]}", file=sys.stderr, flush=True)
            if attempt == retries:
                raise RuntimeError(f"chunk {name} was not accepted; {sent} of {len(names)} sent, the rest go out on the next run")
            time.sleep(wait * attempt)
        print(f"chunk {name}: sent, {commit_chunk(state, path[:-4] + '.json')} rows written (estimate)", flush=True)
    return len(names)


def baseline(con, state, tables=None):
    """Record the store's current rows as already in D1, without sending anything."""
    total = 0
    m = people.masker(con)
    with state:
        for t in tables or TABLES:
            cols, keys, _, _ = _table_meta(con, t)
            state.execute("DELETE FROM row_hash WHERE tbl = ?", (t,))
            for raw in con.execute(f"SELECT {','.join(cols)} FROM {t}"):
                row = _export_row(t, cols, raw, m)
                if row is None:
                    continue
                state.execute("INSERT OR REPLACE INTO row_hash VALUES (?,?,?)",
                              (t, json.dumps([row[cols.index(k)] for k in keys], ensure_ascii=False), _hash(row)))
                total += 1
    return total


def _delta_main(argv):
    p = argparse.ArgumentParser(prog="export_d1")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("delta")
    d.add_argument("--out-dir", required=True)
    d.add_argument("--state", required=True)
    d.add_argument("--profile", choices=["fast", "all"], default="all")
    d.add_argument("--init", action="store_true", help="no state yet and D1 is empty: send everything")
    d.add_argument("--daily-cap", type=int, default=int(os.environ.get("D1_DAILY_WRITE_CAP") or DEFAULT_DAILY_CAP))
    d.add_argument("--allow-mass-delete", action="store_true")
    d.add_argument("--chunk-writes", type=int, default=CHUNK_WRITES)
    b = sub.add_parser("baseline")
    b.add_argument("--state", required=True)
    u = sub.add_parser("push")
    u.add_argument("--dir", required=True)
    u.add_argument("--state", required=True)
    u.add_argument("--command", default="npx wrangler d1 execute japanfilings --remote --yes --file {file}")
    u.add_argument("--cwd", default="worker")
    a = p.parse_args(argv)
    try:
        if a.cmd == "push":
            state = open_state(a.state)
            print("chunks sent:", push(state, a.dir, a.command, a.cwd))
            return 0
        state = open_state(a.state, init=a.cmd == "baseline" or a.init)
        con = store.connect()
        ensure_schema(con)
        if a.cmd == "baseline":
            print("rows recorded as already in D1:", baseline(con, state))
            return 0
        r = delta(con, state, a.out_dir, FAST_TABLES if a.profile == "fast" else None, a.daily_cap, a.allow_mass_delete,
                  chunk_writes=a.chunk_writes)
    except ExportRefused as e:
        print("REFUSED:", e, file=sys.stderr)
        return 3
    except RuntimeError as e:
        print("FAILED:", e, file=sys.stderr)
        return 1
    for t, s in r["tables"].items():
        print(t, s)
    print(f"chunks={r['chunks']} estimated_rows_written={r['writes']} deferred_rows={r['deferred']}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"chunks={r['chunks']}\n")
    return 0


def new_key(plan, customer_ref=None):
    assert plan in ("free", "starter", "pro", "business"), plan
    key = "jf_" + secrets.token_urlsafe(32)
    h = hashlib.sha256(key.encode()).hexdigest()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sql = (f"INSERT INTO api_keys (key_hash, plan, customer_ref, created_at) "
           f"VALUES ({_lit(h)}, {_lit(plan)}, {_lit(customer_ref)}, {_lit(now)});")
    return key, sql


if __name__ == "__main__":
    if sys.argv[1] in ("delta", "push", "baseline"):
        sys.exit(_delta_main(sys.argv[1:]))
    if sys.argv[1] == "--new-key":
        key, sql = new_key(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
        print(key)
        print(sql)
    else:
        since = sys.argv[sys.argv.index("--since") + 1] if "--since" in sys.argv else None
        with open(sys.argv[1], "w") as f:
            con = store.connect()
            ensure_schema(con)
            export(con, f, since)
