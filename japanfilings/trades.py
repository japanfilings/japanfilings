"""Trades disclosed in 5% reports: the table "最近60日間の取得又は処分の状況".

Each report lists, per holder, every acquisition and disposal of the issuer's shares in the 60 days
before the reporting date: date, kind of security, quantity, % of shares outstanding, on/off market,
acquisition/disposal, and a unit price (usually given only for off-market trades). The XBRL CSV
keeps the table as one text block with the cells run together, e.g.
  2026年9月2日普通株式6,8000.00市場外取得2,706円2026年9月25日...
so rows are split on dates and cells on the fixed column order.
"""
import json
import re

from .textutil import nfkc, parse_jp_date
from .xbrlcsv import holder_member

SCHEMA = """
CREATE TABLE IF NOT EXISTS holder_trades (
  doc_id TEXT,
  row_no INTEGER,
  trade_date TEXT,
  submit_date TEXT,
  issuer_ticker TEXT,
  filer_edinet_code TEXT,
  holder TEXT,
  security_ja TEXT,
  shares REAL,
  pct_of_outstanding REAL,
  venue TEXT,
  side TEXT,
  unit_price_jpy REAL,
  note_ja TEXT,
  kind TEXT,
  PRIMARY KEY (doc_id, row_no)
);
CREATE INDEX IF NOT EXISTS ht_ticker ON holder_trades(issuer_ticker, trade_date);
CREATE INDEX IF NOT EXISTS ht_filer ON holder_trades(filer_edinet_code, trade_date);
"""

_ELEMENT = "DuringLast60Days"
_HEADER = re.compile(r"^.*?単価")
_DATE = re.compile(r"(?:令和|平成)?\s*(?:\d{1,4}|元)\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日")
# security, quantity (thousands separators, sometimes negative for disposals; some filers print 株 and %, or separate
# the cells with spaces and tabs), % of outstanding (left blank by a few filers), venue, side, rest. Run together,
# "3000.00" is 300 shares at 0.00%: the quantity takes every digit it can that still leaves a ratio behind it.
_ROW = re.compile(r"^(?P<sec>.*?)\s*(?P<qty>-?(?:\d{1,3}(?:,\d{3})+|\d+))\s*(?:株|口|個)?\s*"
                  r"(?:(?P<pct>-?\d{1,3}\.\d{1,4})\s*%?|\s(?P<pcti>\d{1,3})\s*%)?\s*(?P<venue>市場内|市場外)\s*"
                  r"(?:取引)?\s*(?P<side>取得|処分)(?P<rest>.*)$", re.S)
# Rows that move shares without a purchase or sale: lending, custody transfers, option exercises.
_SIDE = re.compile(r"市場[内外]\s*(?:取引)?\s*(?:取得|処分)")
_KINDS = [("lending", re.compile(r"貸借|借株|貸株|借入|返還|返却")), ("transfer", re.compile(
    r"移管|受管|振替|相続|贈与|寄付|寄附|現物分配|現物配当|現物出資|株式分割|委任|寄託|引出|合併|会社分割|吸収分割|新設分割|"
    r"株式交換|株式移転|株式交付|代物弁済|無償|投資一任|運用受託|組織変更|信託譲渡|承継|譲渡制限付株式")),
          ("exercise", re.compile(r"行使|転換|オプション"))]
_NUM = r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{1,3}\.\d{3}\.\d+|\d+(?:\.\d+)?)"  # middle one: 1.869.65 for 1,869.65
# "1株当たり150円" / "1株510円": the price follows the unit; a leading "1" there is not the price, nor in "1:10株式併合".
_PER_UNIT = re.compile(r"^[\s.\-－―]*(?:単価)?\s*1\s*(?:株|口|個)\s*(?:当たり|あたり|につき)?\s*" + _NUM + r"\s*円")
_PRICE = re.compile(r"^[\s.\-－―]*(?:単価)?\s*" + _NUM + r"(?!\s*[:：株口個対]|[\d,])\s*円?")


_PAR_LABEL = "額面100に対する割合(%)"


def is_bond(security_ja):
    """Convertible bonds and other 社債: their 単価 is a percentage of par (95.95 = 95.95% of face value), not yen."""
    return "社債" in (security_ja or "")


def label_bond_price(t):
    """Move a bond's price out of `unit_price_jpy` (which readers take to be yen per share) into `price_pct_of_par`
    and say so in the note, because the holder_trades table has no column for it. Idempotent."""
    if is_bond(t.get("security_ja")) and t.get("unit_price_jpy") is not None:
        t["price_pct_of_par"] = t["unit_price_jpy"]
        t["unit_price_jpy"] = None
        label = f"単価{t['price_pct_of_par']:g}は{_PAR_LABEL}"
        t["note_ja"] = f"{t['note_ja']} / {label}" if t.get("note_ja") else label
    return t


def parse_block(text: str) -> list[dict]:
    """Rows of one holder's 60-day table. Rows that do not fit the column order are skipped."""
    s = nfkc(text or "")
    s = _HEADER.sub("", s, count=1) if "単価" in s[:200] else s
    raw = list(_DATE.finditer(s))
    starts = []
    for j, m in enumerate(raw):
        # A date inside a parenthesis the note opened belongs to that note when what follows is no row:
        # 株式分割(効力発生日は令和4年1月1日), 報告義務発生日(令和7年9月1日)の... Filers also leave a note's "(" open
        # before the next row, so a following row always starts a row.
        side = _SIDE.search(s, starts[-1].end(), m.start()) if starts else None
        if side and (note := s[side.end():m.start()]).count("(") > note.count(")") \
                and not _ROW.match(s[m.end(): raw[j + 1].start() if j + 1 < len(raw) else len(s)].strip()):
            continue
        starts.append(m)
    out = []
    for i, m in enumerate(starts):
        cell = s[m.end(): starts[i + 1].start() if i + 1 < len(starts) else len(s)]
        r = _ROW.match(cell.strip())
        if not r:
            continue
        rest = r.group("rest").strip()
        price = None
        p = _PER_UNIT.match(rest) or _PRICE.match(rest)
        if p:
            num = p.group(1)
            num = num.replace(".", ",", 1) if num.count(".") == 2 else num
            price = float(num.replace(",", ""))
            rest = rest[p.end():]
        note = re.sub(r"^[\s.\-－―円]+|[\s.\-－―]+$", "", rest) or None
        out.append(label_bond_price({
            "trade_date": parse_jp_date(m.group(0)),
            "security_ja": re.sub(r"\s+", " ", r.group("sec")).strip(" -－") or None,
            "shares": abs(float(r.group("qty").replace(",", ""))),
            "pct_of_outstanding": abs(float(pct)) if (pct := r.group("pct") or r.group("pcti")) else None,
            "venue": "on_market" if r.group("venue") == "市場内" else "off_market",
            "side": "acquisition" if r.group("side") == "取得" else "disposal",
            "unit_price_jpy": price if price and price > 0 else None,
            "note_ja": note,
            "kind": next((k for k, rx in _KINDS if note and rx.search(note)), "trade"),
        }))
    return out


def from_facts(facts, holders=None) -> list[dict]:
    """All holders' trades in one report. `holders` maps a holder member (filer or joint holder) to a name."""
    trades = []
    for f in facts:
        if _ELEMENT not in f["element"] or "テキストブロック" not in f["label"]:
            continue
        member = holder_member(f["context"])
        holder = (holders or {}).get(member) if member else None
        for t in parse_block(f["value"]):
            trades.append({"holder": holder, **t})
    return trades


def holder_names(facts) -> dict:
    return {m: nfkc(f["value"]) for f in facts if f["element"] == "jplvh_cor:Name"
            and (m := holder_member(f["context"])) and f["value"] not in ("", "－")}


WINDOW_DAYS = 70  # the table covers the 60 days up to the obligation date; a few days' slack for holidays
_WARN_SEP = " / "


def _year_typo(raw_year: int, fixed_year: int) -> bool:
    """True when `fixed_year` is an evident misprint of `raw_year`: the 平成/令和 era slip (平成3年 for 令和3年 is exactly
    30 years early), two digits swapped (2062 for 2026), or one digit wrong (2027 for 2026)."""
    a, b = f"{raw_year:04d}", f"{fixed_year:04d}"
    diff = [i for i in range(4) if a[i] != b[i]]
    swapped = len(diff) == 2 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]
    return fixed_year - raw_year == 30 or swapped or len(diff) == 1


def check_trade_date(trade_date, obligation_date, submit_date):
    """Validate one row's date against its report. Returns (date to store, warning or None).

    The anchor is the obligation date (報告義務発生日), or the submit date when that is missing or later than the
    submission. A date that cannot be right (before 2000, or after the submission) is corrected only when one year
    puts it inside the 60-day window and that year is an evident misprint of the printed one (`_year_typo`);
    otherwise the date is dropped (NULL, so date filters and sorts skip the row) and the printed date kept in the
    warning. Plausible dates outside the window are kept as printed, with a warning."""
    from datetime import date, timedelta
    if not trade_date:
        return None, None
    try:
        d = date.fromisoformat(trade_date)
        sub = date.fromisoformat(submit_date) if submit_date else None
        ob = date.fromisoformat(obligation_date) if obligation_date else None
    except ValueError:
        return trade_date, None
    anchor = ob if ob and (not sub or ob <= sub) else sub
    impossible = d.year < 2000 or (sub is not None and d > sub)
    if not impossible:
        if anchor and anchor == ob and d > ob:
            return trade_date, f"data_warning=trade_date_after_obligation_date({ob.isoformat()})"
        if anchor and (anchor - d).days > WINDOW_DAYS:
            return trade_date, f"data_warning=trade_date_before_60day_window({anchor.isoformat()})"
        return trade_date, None
    fixes = []
    if anchor:
        for y in (anchor.year, anchor.year - 1):
            try:
                c = d.replace(year=y)
            except ValueError:  # 29 February
                continue
            if 0 <= (anchor - c).days <= WINDOW_DAYS and (sub is None or c <= sub) and _year_typo(d.year, y):
                fixes.append(c)
    if len(fixes) == 1:
        return fixes[0].isoformat(), f"data_warning=trade_date_corrected(printed {trade_date})"
    return None, f"data_warning=trade_date_implausible(printed {trade_date})"


def _with_warning(note, warning):
    if not warning:
        return note
    return f"{note}{_WARN_SEP}{warning}" if note else warning


MAX_COMMON_PRICE = 1_000_000  # yen per common share
_NOT_COMMON = re.compile(r"予約権|社債|種類|優先|投資|受益|新株")


def is_common(security_ja):
    """普通株式, 普通株券, 株券(普通株式), and the bare 株券 / 株式 / 普通 that most filers write for common shares."""
    s = re.sub(r"\s+", "", security_ja or "")
    return bool(s) and not _NOT_COMMON.search(s) and ("普通" in s or s in ("株券", "株式", "株券等", "株式等"))


def _holder_key(holder):
    """One holder's name as different reports print it: 'マッコーリー バンク リミテッド (Macquarie Bank Limited)' and
    'マッコーリー バンク リミテッド', '有限会社和貴' and '有限会社 和貴'."""
    s = re.sub(r"\s+", "", nfkc(holder or ""))
    return re.sub(r"\([^()]*[A-Za-z][^()]*\)$", "", s) or s


def _security_key(security_ja):
    return "common" if is_common(security_ja) else re.sub(r"[\s()（）]", "", security_ja or "")


def rebuild(con):
    """Recreate holder_trades from the `trades` stored in each 5% report record.

    Consecutive reports' 60-day windows overlap, so a trade is listed again by each later report. A row is kept from
    the first report that lists it. Reports print the same row differently (the unit price given in one and left
    blank in the next, 普通株式 / 株券(普通株式), the holder with or without an English name, 貸借 noted or not), so
    rows are matched without those cells; and a holder can make the same trade twice in a day, so a later report adds
    a row only for each repeat beyond what earlier reports listed. A unit price missing from the kept row is taken
    from the later report."""
    con.executescript(SCHEMA)
    rows = []
    kept = {}  # match key -> indexes into rows, in order
    for r in con.execute("SELECT record FROM large_holdings ORDER BY submit_date, doc_id"):
        rec = json.loads(r["record"])
        if rec.get("report_kind") == "amendment":
            continue  # corrections repeat the original's table
        seen_here = {}
        for n, t in enumerate(rec.get("trades") or [], 1):
            if not t["trade_date"]:
                continue
            # Misprinted dates (平成 for 令和, 1202, 2027 in a 2026 filing): corrected when evident, otherwise kept
            # with no date and a data_warning in the note (holder_trades has no warnings column; see check_trade_date).
            trade_date, warning = check_trade_date(t["trade_date"], rec.get("obligation_date"), rec.get("submit_date"))
            price = t.get("unit_price_jpy")
            if price is not None and price > MAX_COMMON_PRICE and is_common(t.get("security_ja")):
                # No common share on the exchange trades near ¥1m; a figure above it is a total or a misprint.
                t = {**t, "unit_price_jpy": None}
                warning = "; ".join(x for x in (warning, f"data_warning=unit_price_implausible(printed {price:.12g})") if x)
            key = (rec.get("filer_edinet_code"), rec.get("issuer_ticker"), _holder_key(t.get("holder")),
                   trade_date or t["trade_date"], _security_key(t.get("security_ja")), t["shares"], t["venue"], t["side"])
            i = seen_here[key] = seen_here.get(key, 0) + 1
            earlier = kept.setdefault(key, [])
            if i <= len(earlier) and earlier[i - 1][0] != rec["doc_id"]:
                row = rows[earlier[i - 1][1]]
                if row[12] is None and t.get("unit_price_jpy") is not None:
                    rows[earlier[i - 1][1]] = row[:12] + (t["unit_price_jpy"],) + row[13:]
                continue
            earlier.append((rec["doc_id"], len(rows)))
            rows.append((rec["doc_id"], n, trade_date, rec.get("submit_date"), rec.get("issuer_ticker"),
                         rec.get("filer_edinet_code"), t.get("holder"), t.get("security_ja"), t["shares"],
                         t["pct_of_outstanding"], t["venue"], t["side"], t.get("unit_price_jpy"),
                         _with_warning(t.get("note_ja"), warning), t.get("kind")))
    # Everything is computed above; the write transaction is only the swap, so it stays short.
    with con:
        con.execute("DELETE FROM holder_trades")
        con.executemany(f"INSERT OR REPLACE INTO holder_trades VALUES ({','.join('?' * 15)})", rows)
    return len(rows)
