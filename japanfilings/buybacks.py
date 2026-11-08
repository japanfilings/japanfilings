"""Share buybacks as companies report them each month: 自己株券買付状況報告書 on EDINET.

docTypeCode 220 is the report and 230 its amendment (訂正). An amendment's XBRL CSV restates the whole
report (checked on six amendment/original pairs), so it replaces its parent: latest_by_ticker keeps the last
filing of each family. Parents can have several amendments.

The CSV keeps each table as one text block with the cells run together, e.g.
  取締役会(2025年9月18日)での決議状況(取得期間2025年10月1日~2026年3月31日) 110,000(上限)1,000,000,000(上限)
  報告月における取得自己株式(取得日)3月2日5007,215,000 ...計-8,000111,027,998
  報告月末現在の累計取得自己株式 75,600999,437,979自己株式取得の進捗状況(%) 68.7399.94
A block holds one table per resolution; each stays a separate entry in `resolutions`. For a shareholders'
meeting table (resolution_type), board_resolution_date holds the meeting date. Where one table names several
dates, resolution_dates lists them all and board_resolution_date is the last.

Numbers are what the filing states. A value the filing does not state is null, never derived from other cells.
`period_none_stated` / `none_stated` mean the filing shows dashes or "該当事項はありません" there.
Computed by arithmetic, and listed in COMPUTED_FIELDS: avg_price_jpy, progress_*_pct and remaining_*.
The filing's own progress figures are kept as stated_progress_*_pct.
"""
import json
import re

from . import edinet, xbrlcsv
from .textutil import nfkc, parse_jp_date

SCHEMA = """
CREATE TABLE IF NOT EXISTS buyback_reports (
  doc_id TEXT PRIMARY KEY,
  doc_type_code TEXT,
  amends_doc_id TEXT,
  family_doc_id TEXT,
  ticker TEXT,
  edinet_code TEXT,
  submit_datetime TEXT,
  submit_date TEXT,
  period_start TEXT,
  period_end TEXT,
  n_resolutions INTEGER,
  parse_status TEXT,
  record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bb_ticker ON buyback_reports(ticker, period_end);
CREATE INDEX IF NOT EXISTS bb_family ON buyback_reports(family_doc_id, submit_datetime);
"""

DOC_TYPES = {"220": "report", "230": "amendment"}
COMPUTED_FIELDS = {
    "avg_price_jpy": "period_acquired_yen / period_acquired_shares",
    "progress_shares_pct": "cumulative_acquired_shares / authorized_max_shares * 100",
    "progress_yen_pct": "cumulative_acquired_yen / authorized_max_yen * 100",
    "remaining_shares": "authorized_max_shares - cumulative_acquired_shares",
    "remaining_yen": "authorized_max_yen - cumulative_acquired_yen",
}

_DASH = "―—‐−–ー－-"
_DATE = re.compile(r"(?:令和|平成)?\s*(?:\d{1,4}|元)\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日")
_NONE = re.compile(r"該当事項はありません|該当事項なし|当該事項はありません|該当なし")
_WELL_FORMED = re.compile(r"^[1-9]\d{0,2}(?:,\d{3})*$|^0$")
_TOKEN = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(億円|百万円|千円|万円|円|千株|万株|株)?")
_UNITS = {"億円": 100_000_000, "百万円": 1_000_000, "千円": 1_000, "万円": 10_000, "円": 1, "千株": 1_000, "万株": 10_000, "株": 1}
_NOTE = re.compile(r"\(\s*注|※|注\s*\d")
_PERIOD_TOTAL_END = re.compile(r"報告月末|月末現在|累[計積]")
_CUMULATIVE = re.compile(r"累[計積](?:取得自己株式)?")
_DISPOSAL_KINDS = [("subscription", "募集"), ("cancellation", "消却"), ("merger_or_similar", "合併、株式交換"), ("other", r"その他")]
_DISPOSAL_LABEL = re.compile("|".join(p for _, p in _DISPOSAL_KINDS))


def _candidates(run):
    """Every way to read one digit run as two numbers. '271,377,540' -> (2, 71,377,540), (27, 1,377,540)."""
    out = []
    for i in range(1, len(run)):
        a, b = run[:i], run[i:]
        if _WELL_FORMED.match(a) and _WELL_FORMED.match(b):
            out.append((_int(a), _int(b)))
    return out


def _int(s):
    return int(s.replace(",", ""))


def _plausible(pairs, ref_price):
    return [(a, b) for a, b in pairs if a and ref_price and 0.5 <= (b / a) / ref_price <= 2]


def _pair(segment, ref_price=None, leading_cell=False):
    """(shares, yen, status) from one row's cells. status: 'ok', 'none' (blank or dashes) or 'unresolved'.
    `leading_cell` drops the dash a 計 row carries in its date column. Where a run can be read two ways
    (shares under 100 jammed against yen), only a unit price near `ref_price` settles it; otherwise null."""
    s = re.sub(r"\([^()]*\)", " ", segment)
    s = _DATE.sub(" ", s)
    s = re.sub(r"\d+(?:\.\d+)?\s*%", " ", s).replace("(上限)", " ")
    if leading_cell:
        s = re.sub(f"^[\\s{_DASH}]*?[{_DASH}]", " ", s, count=1)
    tokens = list(_TOKEN.finditer(s))
    if not tokens:
        return None, None, "none"
    if len(tokens) >= 2:
        shares, yen = _value(tokens[0], "株"), _value(tokens[1], "円")
        if shares is None and tokens[0].group(2) is None and yen is not None:
            return None, None, "unresolved"
        if len(tokens) > 2:  # a note's figures are mixed in; which pair is right cannot be told
            return None, None, "unresolved"
        return shares, yen, "ok" if shares is not None and yen is not None else "unresolved"
    tok = tokens[0]
    if (tok.group(2) or "").endswith("円"):
        return None, _value(tok, "円"), "ok"
    if (tok.group(2) or "").endswith("株"):
        return _value(tok, "株"), None, "ok"
    before, after = s[:tok.start()], s[tok.end():]
    if any(c in before for c in _DASH) and _WELL_FORMED.match(tok.group(1)):
        return None, _int(tok.group(1)), "ok"
    if any(c in after for c in _DASH) and _WELL_FORMED.match(tok.group(1)):
        return _int(tok.group(1)), None, "ok"
    pairs = _candidates(tok.group(1))
    if len(pairs) > 1:
        pairs = _plausible(pairs, ref_price)
    if len(pairs) == 1:
        return pairs[0][0], pairs[0][1], "ok"
    return None, None, "unresolved"


def _value(tok, default_unit):
    if "." in tok.group(1) or not _WELL_FORMED.match(tok.group(1)):
        return None
    return _int(tok.group(1)) * _UNITS[tok.group(2) or default_unit]


def _scale(value, factor):
    return None if value is None else value * factor


def _table_units(text):
    """Column units when the header is not plain 株 / 円."""
    shares = 1000 if re.search(r"株式数\s*\(\s*千株", text) else 1
    yen = 1_000_000 if re.search(r"総額\s*\(\s*百万円", text) else 1000 if re.search(r"総額\s*\(\s*千円", text) else 1
    return shares, yen


def _progress(segment, computed):
    """(shares %, yen %) as stated. '68.7399.94' is split using the computed values, or by equal decimals."""
    s = re.sub(r"\([^()]*\)", " ", _NOTE.split(segment, maxsplit=1)[0].replace("%", " "))
    parts = s.split()
    if len(parts) == 2 and all(re.fullmatch(r"\d+(?:\.\d+)?|[" + _DASH + "]+", p) for p in parts):
        return tuple(float(p) if p[0].isdigit() else None for p in parts)
    if not parts or all(not re.search(r"\d", p) for p in parts):
        return None, None
    run = "".join(c for c in s if c.isdigit() or c == ".")
    if run.count(".") != 2:
        return None, None
    splits = []
    first, mid, last = run.split(".")
    for dp in range(1, len(mid)):
        rest = mid[dp:]
        if rest and (rest == "0" or not rest.startswith("0")):
            splits.append((first + "." + mid[:dp], rest + "." + last))
    if computed[0] is not None and computed[1] is not None and splits:
        return tuple(float(x) for x in min(splits, key=lambda p: abs(float(p[0]) - computed[0]) + abs(float(p[1]) - computed[1])))
    even = [p for p in splits if len(p[0].split(".")[1]) == len(p[1].split(".")[1])]
    return tuple(float(x) for x in even[0]) if len(even) == 1 else (None, None)


def _resolution_dates(text):
    return [d for d in (parse_jp_date(m.group(0)) for m in _DATE.finditer(text)) if d]


def _parse_table(head, body, kind):
    warnings = []
    shares_unit, yen_unit = _table_units(head)
    term = re.search(r"取得期間", body)
    res_dates = _resolution_dates(head.rsplit("現在", 1)[-1])
    if not res_dates:
        res_dates = _resolution_dates(body[:term.start()] if term else body[:40])
    start, end, rest = None, None, body
    if term:
        dates = list(_DATE.finditer(body, term.end()))[:2]
        # A second date is the end of the term only when a range mark joins them; otherwise purchases took one day.
        if len(dates) == 2 and not re.fullmatch(r"[\s)]*(?:から|至|[~〜～―—–\-－ー])[\s(]*(?:至)?[\s(]*", body[dates[0].end():dates[1].start()]):
            dates = dates[:1]
        if dates:
            start = parse_jp_date(dates[0].group(0))
            end = parse_jp_date(dates[1].group(0)) if len(dates) > 1 else start
            rest = body[dates[-1].end():]
            rest = re.sub(r"^\s*\)", "", rest)
    elif ")" in body[:80]:
        rest = body[body.index(")") + 1:]

    month = re.search(r"報告月に?おける|取得自己株式\s*\(取得日\)", rest)
    limits_segment = rest[:month.start()] if month else ""
    max_shares, max_yen, st = _pair(limits_segment)
    if st == "unresolved":
        warnings.append("authorized_limits_unresolved")
    cum_m = _CUMULATIVE.search(rest)
    prog_m = re.search(r"進捗", rest)
    cum_seg = rest[cum_m.end():prog_m.start() if prog_m else len(rest)] if cum_m else ""
    if cum_m and not prog_m:
        cum_seg = _NOTE.split(cum_seg, maxsplit=1)[0]
    cum_shares, cum_yen, st = _pair(cum_seg)
    if st == "unresolved":
        warnings.append("cumulative_unresolved")
    ref = cum_yen * yen_unit / (cum_shares * shares_unit) if cum_shares and cum_yen else None

    period_shares, period_yen, period_none = None, None, False
    window = rest[month.start() if month else 0:cum_m.start() if cum_m else len(rest)]
    total = None
    for m in re.finditer(r"(?<!合)計", window):
        total = m
    if total:
        period_shares, period_yen, st = _pair(window[total.end():], ref, leading_cell=True)
        period_none = st == "none"
        if st == "unresolved":
            warnings.append("period_total_unresolved")
    else:
        warnings.append("period_total_not_found")

    max_shares, cum_shares, period_shares = (_scale(v, shares_unit) for v in (max_shares, cum_shares, period_shares))
    max_yen, cum_yen, period_yen = (_scale(v, yen_unit) for v in (max_yen, cum_yen, period_yen))

    prog_s = prog_y = None
    notes = None
    if prog_m:
        seg = re.sub(r"^(?:進捗)?(?:状況|率)?\s*(?:\([^()]*\))?", "", rest[prog_m.start():][2:].lstrip()).lstrip()
        seg = re.sub(r"^(?:状況|率)?\s*\([^()]*\)", "", seg)
        note = _NOTE.search(seg)
        notes = seg[note.start():].strip()[:1500] if note else None
        pseg = seg[:note.start()] if note else seg
        pseg = re.split(r"区分|株式数|20\d\d年|令和", pseg)[0]
        computed = (_pct(cum_shares, max_shares), _pct(cum_yen, max_yen))
        prog_s, prog_y = _progress(pseg, computed)
    comp_s, comp_y = _pct(cum_shares, max_shares), _pct(cum_yen, max_yen)
    for stated, comp, name in ((prog_s, comp_s, "shares"), (prog_y, comp_y, "yen")):
        if stated is not None and comp is not None and abs(stated - comp) > 0.11:
            warnings.append(f"stated_progress_{name}_differs_from_arithmetic")
    if cum_shares is not None and period_shares is not None and period_shares > cum_shares:
        warnings.append("period_shares_exceed_cumulative")
    if cum_yen is not None and period_yen is not None and period_yen > cum_yen:
        warnings.append("period_yen_exceed_cumulative")
    for cum, cap, name in ((cum_shares, max_shares, "shares"), (cum_yen, max_yen, "yen")):
        if cum is not None and cap is not None and cum > cap:
            warnings.append(f"cumulative_{name}_exceed_limit")

    # A jammed limits row is accepted only when the filing's own progress
    # percentages independently confirm the split.  This also recovers rows
    # whose acquisition-period note contains an unrelated number (for example
    # "10 business days") that made the first pass ambiguous.
    if "authorized_limits_unresolved" in warnings and prog_s is not None and prog_y is not None:
        cleaned = re.sub(r"\([^()]*\)", " ", limits_segment)
        runs = [m.group(1) for m in _TOKEN.finditer(cleaned) if m.group(2) is None]
        confirmed = []
        for run in runs:
            for shares, yen in _candidates(run):
                shares, yen = shares * shares_unit, yen * yen_unit
                if (_pct(cum_shares, shares) is not None and _pct(cum_yen, yen) is not None and
                        abs(_pct(cum_shares, shares) - prog_s) <= 0.11 and abs(_pct(cum_yen, yen) - prog_y) <= 0.11):
                    confirmed.append((shares, yen))
        if len(set(confirmed)) == 1:
            max_shares, max_yen = confirmed[0]
            warnings = [w for w in warnings if w not in {
                "authorized_limits_unresolved", "stated_progress_shares_differs_from_arithmetic",
                "stated_progress_yen_differs_from_arithmetic", "cumulative_shares_exceed_limit",
                "cumulative_yen_exceed_limit",
            }]
            comp_s, comp_y = _pct(cum_shares, max_shares), _pct(cum_yen, max_yen)

    unsafe_shares = "authorized_limits_unresolved" in warnings or "cumulative_shares_exceed_limit" in warnings
    unsafe_yen = "authorized_limits_unresolved" in warnings or "cumulative_yen_exceed_limit" in warnings

    return {
        "resolution_type": kind,
        "board_resolution_date": res_dates[-1] if res_dates else None,
        "resolution_dates": res_dates,
        "acquisition_period_start": start,
        "acquisition_period_end": end,
        "authorized_max_shares": None if unsafe_shares else max_shares,
        "authorized_max_yen": None if unsafe_yen else max_yen,
        "period_acquired_shares": period_shares,
        "period_acquired_yen": period_yen,
        "period_none_stated": period_none,
        "avg_price_jpy": round(period_yen / period_shares, 2) if period_shares and period_yen is not None else None,
        "cumulative_acquired_shares": cum_shares,
        "cumulative_acquired_yen": cum_yen,
        "progress_shares_pct": None if unsafe_shares else comp_s,
        "progress_yen_pct": None if unsafe_yen else comp_y,
        "stated_progress_shares_pct": prog_s,
        "stated_progress_yen_pct": prog_y,
        "remaining_shares": None if unsafe_shares else max_shares - cum_shares if max_shares is not None and cum_shares is not None else None,
        "remaining_yen": None if unsafe_yen else max_yen - cum_yen if max_yen is not None and cum_yen is not None else None,
        "notes_ja": notes,
        "warnings": warnings,
    }


def _pct(part, whole):
    return round(part / whole * 100, 4) if part is not None and whole else None


def parse_resolutions(text, kind="board"):
    """One entry per resolution table in a block. Returns (entries, none_stated)."""
    s = nfkc(text or "")
    anchors = list(re.finditer(r"決議状況", s))
    if not anchors:
        return [], bool(_NONE.search(s))
    out, previous = [], 0
    for i, m in enumerate(anchors):
        body_end = anchors[i + 1].start() if i + 1 < len(anchors) else len(s)
        cut = body_end
        if i + 1 < len(anchors):  # the next table's header cells sit in front of its anchor
            as_of = None
            for d in re.finditer(_DATE.pattern + r"\s*現在", s[m.end():body_end]):
                as_of = d
            if as_of:
                cut = m.end() + as_of.start()
            else:  # stacked in one table: only the "取締役会(date)" cell comes first
                organ = max(s.rfind("取締役会", m.end(), body_end), s.rfind("株主総会", m.end(), body_end))
                if organ > 0 and body_end - organ < 80:
                    cut = organ
        body = s[m.end():cut]
        out.append(_parse_table(s[previous:m.start()], body, kind))
        previous = m.end() + len(body)
    return out, False


def parse_disposals(text, ref_price=None):
    """Treasury shares disposed in the month: the 合計 row plus each category that shows a total."""
    s = nfkc(text or "")
    result = {"disposed_shares": None, "disposed_yen": None, "none_stated": False, "categories": [], "warnings": []}
    if not s.strip():
        result["none_stated"] = None
        return result
    if _NONE.search(s[:120]):
        result["none_stated"] = True
        return result
    s = _NOTE.split(s, maxsplit=1)[0]
    grand = re.search(r"合計", s)
    if grand:
        shares, yen, st = _pair(s[grand.end():], ref_price)
        result["disposed_shares"], result["disposed_yen"] = shares, yen
        result["none_stated"] = st == "none"
        if st == "unresolved":
            result["warnings"].append("disposal_total_unresolved")
    else:
        result["warnings"].append("disposal_total_not_found")
    table = s[:grand.start()] if grand else s
    labels = list(_DISPOSAL_LABEL.finditer(table))
    for i, m in enumerate(labels):
        piece = table[m.start():labels[i + 1].start() if i + 1 < len(labels) else len(table)]
        total = None
        for t in re.finditer("計", piece):
            total = t
        if not total:
            continue
        shares, yen, st = _pair(piece[total.end():], ref_price, leading_cell=True)
        if st == "ok":
            kind = next(k for k, p in _DISPOSAL_KINDS if re.match(p, m.group(0)))
            label = re.search(r"\(([^()]*)\)", piece) if kind == "other" else None
            result["categories"].append({"category": kind, "label_ja": label.group(1) if label else None, "shares": shares, "yen": yen})
        elif st == "unresolved":
            result["warnings"].append("disposal_category_unresolved")
    return result


def parse_holdings(text):
    s = _NOTE.split(nfkc(text or ""), maxsplit=1)[0]
    held = re.search(r"保有自己株式数\s*:?\s*([\d,]+)", s)
    issued = re.search(r"発行済株式総数\s*((?:\([^()]*\)\s*)*):?\s*([\d,]+)", s)
    return {
        "treasury_shares_held": _int(held.group(1)) if held and _WELL_FORMED.match(held.group(1)) else None,
        "issued_shares": _int(issued.group(2)) if issued and _WELL_FORMED.match(issued.group(2)) else None,
        "issued_shares_excludes_treasury": bool(issued.group(1) and "除く" in issued.group(1)) if issued else None,
    }


def _dei_ticker(code):
    code = (code or "").strip()
    return code[:4] if len(code) == 5 and code.endswith("0") else None


def parse_facts(facts):
    """One report from xbrlcsv.read_facts output. parse_status: 'parsed', 'no_resolution_stated' or 'failed'."""
    by = {f["element"].split(":")[-1]: f["value"] for f in facts}
    resolutions, none_flags = [], []
    for element, kind in (("AcquisitionsByResolutionOfShareholdersMeetingTextBlock", "shareholders_meeting"),
                          ("AcquisitionsByResolutionOfBoardOfDirectorsMeetingTextBlock", "board")):
        entries, none = parse_resolutions(by.get(element), kind)
        resolutions += entries
        none_flags.append(none)
    period = [d for d in (parse_jp_date(m.group(0)) for m in _DATE.finditer(nfkc(by.get("ReportingPeriodCoverPage", "")))) if d]
    refs = [r["cumulative_acquired_yen"] / r["cumulative_acquired_shares"] for r in resolutions
            if r["cumulative_acquired_yen"] and r["cumulative_acquired_shares"]]
    disposals = parse_disposals(by.get("DisposalsOfTreasurySharesTextBlock"), refs[0] if refs else None)
    holdings = parse_holdings(by.get("HoldingOfTreasurySharesTextBlock"))
    amends = by.get("IdentificationOfDocumentSubjectToAmendmentDEI")
    usable = any(r["authorized_max_shares"] is not None or r["authorized_max_yen"] is not None or r["cumulative_acquired_shares"] is not None
                 or r["period_acquired_shares"] is not None or r["period_none_stated"]
                 or r["cumulative_acquired_yen"] is not None or r["period_acquired_yen"] is not None for r in resolutions)
    has_warnings = bool(disposals["warnings"] or any(r["warnings"] for r in resolutions))
    status = "partial" if usable and has_warnings else "parsed" if usable else "no_resolution_stated" if not resolutions and all(none_flags) else "failed"
    classes = re.sub(r"^株式の種類:?", "", re.sub(r"\s", "", nfkc(by.get("ClassesOfSharesTextBlock", ""))))
    return {
        "filer_name_en": by.get("FilerNameInEnglishDEI") if by.get("FilerNameInEnglishDEI") not in (None, "", "－") else None,
        "dei_ticker": _dei_ticker(by.get("SecurityCodeDEI")),
        "amends_doc_id": amends if amends and amends != "－" else None,
        "reporting_period_start": period[0] if period else None,
        "reporting_period_end": period[1] if len(period) > 1 else None,
        "share_class_ja": classes or None,
        "resolutions": resolutions,
        **holdings,
        "treasury_disposed_shares": disposals["disposed_shares"],
        "treasury_disposed_yen": disposals["disposed_yen"],
        "treasury_disposal_none_stated": disposals["none_stated"],
        "treasury_disposal_categories": disposals["categories"],
        "warnings": disposals["warnings"],
        "parse_status": status,
        "computed_fields": COMPUTED_FIELDS,
    }


def run(con, limit=None):
    """Read every report and amendment not yet read. Returns (documents read, documents with parse_status 'parsed')."""
    con.executescript(SCHEMA)
    con.execute("DELETE FROM buyback_reports WHERE doc_id IN (SELECT doc_id FROM filings WHERE withdrawn = 1)")
    rows = con.execute(
        "SELECT f.doc_id, f.doc_type_code, f.submit_datetime, f.edinet_code, f.filer_name_ja, f.parent_doc_id, i.ticker "
        "FROM filings f LEFT JOIN issuers i ON i.edinet_code = f.edinet_code "
        "WHERE f.doc_type_code IN ('220', '230') AND f.withdrawn = 0 AND f.has_csv = 1 "
        "AND f.doc_id NOT IN (SELECT doc_id FROM buyback_reports) ORDER BY f.submit_datetime DESC").fetchall()
    read = parsed = 0
    for doc_id, code, submitted, edinet_code, name, parent, ticker in rows[:limit]:
        try:
            facts = xbrlcsv.read_facts(edinet.download_csv_zip(doc_id))
        except Exception as e:
            print(doc_id, "FAILED", e, flush=True)
            continue
        store(con, parse_facts(facts), doc_id, code, submitted, edinet_code, name, parent, ticker)
        con.commit()
        read += 1
        parsed += con.execute("SELECT parse_status = 'parsed' FROM buyback_reports WHERE doc_id = ?", (doc_id,)).fetchone()[0]
    return read, parsed


def store(con, parsed, doc_id, doc_type_code, submitted, edinet_code, name, parent, ticker):
    rec = dict(parsed)
    dei_ticker = rec.pop("dei_ticker")
    amends = None
    if doc_type_code == "230":
        amends = rec.get("amends_doc_id") or parent
    family = amends or doc_id
    rec.update({
        "doc_id": doc_id, "doc_type": DOC_TYPES.get(doc_type_code), "doc_type_code": doc_type_code, "amends_doc_id": amends,
        "ticker": ticker or dei_ticker, "edinet_code": edinet_code, "issuer_ja": name, "submit_date": submitted[:10],
        "source_url": f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{doc_id}",
    })
    con.execute(f"INSERT OR REPLACE INTO buyback_reports VALUES ({','.join('?' * 13)})", (
        doc_id, doc_type_code, amends, family, rec["ticker"], edinet_code, submitted, submitted[:10],
        rec["reporting_period_start"], rec["reporting_period_end"], len(rec["resolutions"]), rec["parse_status"],
        json.dumps(rec, ensure_ascii=False)))


def latest_by_ticker(con, ticker, limit=12, include_superseded=False):
    """A company's report records, newest reporting period first. A report replaced by a later amendment
    is left out unless `include_superseded`; then each record carries `superseded`."""
    rows = con.execute(
        "SELECT r.record, EXISTS (SELECT 1 FROM buyback_reports n WHERE n.family_doc_id = r.family_doc_id "
        "AND (n.submit_datetime, n.doc_id) > (r.submit_datetime, r.doc_id)) AS superseded "
        "FROM buyback_reports r WHERE r.ticker = ? ORDER BY r.period_end DESC, r.submit_datetime DESC, r.doc_id DESC", (ticker,)).fetchall()
    out = []
    for record, superseded in rows:
        if superseded and not include_superseded:
            continue
        out.append({**json.loads(record), "superseded": bool(superseded)})
    return out[:limit]
