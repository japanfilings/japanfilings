"""Shareholder meeting voting results from extraordinary reports (臨時報告書, docTypeCode 180; amendments 190).

A filing under Article 19(2)(ix-2) of the Cabinet Office Ordinance carries ResolutionOfShareholdersMeetingTextBlock.
EDINET's metadata calls every such report "臨時報告書", so the document has to be opened; the element, or the article
named in ReasonForFilingTextBlock, identifies it. This module does not import governance.py.

One row per resolution, and per candidate where a proposal is voted person by person. Candidates' names are kept
because they identify what was voted on. `stated_rate_pct` is the filing's own figure. When the table heads it
賛成(反対)割合 a failed resolution's figure may be the share against; rate_basis records the heading.

The table's cells are run together: '39,4131220' is 39,413 / 122 / 0. A run is split only by these rules, recorded
in counts_method: separated (blanks or dashes already divide it), unique_split (one well-formed reading),
sum_consistency (one reading gives the for + against + abstain total shared by the document's other rows),
sum_and_abstain_consistency (as before, with a tie settled by the abstain figure all other rows show),
rate_consistency (one reading gives the stated rate to its decimals, where the document's settled rows show the rate is
figured on for + against + abstain). Candidates laid out by column (every for cell, then every against cell, ...) are
read as one group: column_sum_consistency (one reading gives every candidate the same total, or totals 3 votes apart,
or within 0.5%), column_rate_consistency (one reading gives every stated rate). Extra columns are read from the
'(個)' headings: 無効 and unconfirmed votes are dropped, a total column bounds the sum.
Otherwise the counts are null and the row is marked. A dash is stated as null, not zero. Nothing is scored.
"""
import json
import os
import re
import unicodedata
from collections import Counter
from functools import lru_cache
from itertools import product

from . import edinet, xbrlcsv
from .textutil import parse_jp_date

SCHEMA = """
CREATE TABLE IF NOT EXISTS voting_results (
  doc_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  ticker TEXT,
  edinet_code TEXT,
  meeting_date TEXT,
  submit_date TEXT,
  proposal_no INTEGER,
  proposal_title_ja TEXT,
  candidate_ja TEXT,
  votes_for INTEGER,
  votes_against INTEGER,
  votes_abstain INTEGER,
  stated_rate_pct REAL,
  rate_basis TEXT,
  result TEXT,
  counts_method TEXT,
  amends_doc_id TEXT,
  source_url TEXT NOT NULL,
  PRIMARY KEY (doc_id, seq)
);
CREATE INDEX IF NOT EXISTS vr_ticker ON voting_results(ticker, meeting_date);
CREATE TABLE IF NOT EXISTS voting_read (
  doc_id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  n_rows INTEGER,
  detail TEXT
);
"""

SOURCE_URL = "https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{}"
# The annual meeting is held around the annual report: documents filed in this window are opened first.
AGM_WINDOW_DAYS = (-2, 14)

_E_RESOLUTION = "ResolutionOfShareholdersMeetingTextBlock"
_E_REASON = "ReasonForFilingTextBlock"
_REASON = re.compile(r"第\s*19\s*条\s*第\s*2\s*項\s*第\s*9\s*号\s*の\s*2")
_DASHES = "―—‐−–－"
_WELL_FORMED = re.compile(r"^(?:0|[1-9]\d{0,8}|[1-9]\d{0,2}(?:,\d{3})+)$")
_RESULT = re.compile(
    r"(?:可決|否決)(?:できなかった|しなかった|せず)"
    r"|可決(?!要件|され|の|する|に|でき|しな|せず)"
    r"|否決(?!され|の|する|でき|しな|せず)"
)
# (?<!\s): start only at the head of a blank run; same matches, no rescanning from inside the run.
_NOTE_END = re.compile(r"(?<!\s)\s*\(注\s*\d*\)\s*[\d,、・]*\s*$")
# A row's trailing count cells: numbers or dashes, blanks between.  Found by a backward scan, not a regex: a
# nested-repeat regex backtracked for hours (S100Z3FN), and even a flat `...$` pattern under re.search rescans the
# run from every start, which took minutes on a 140k-character run.
_TAIL_EDGE = frozenset("0123456789,-")
_RATE_END = re.compile(r"(100(?:\.0+)?|\d{1,2}(?:\.\d+)?)\s*%?\s*$")
_RATE_AFTER = re.compile(r"\s*(?:\(\s*)?(\d{1,3}(?:\.\d+)?)(?![\d,])\s*(?:%\s*)?\)?")
_PROPOSAL = re.compile(r"第\s*(\d+)\s*号?\s*議案|(?<![号提])議案\s*(?:第\s*)?(\d*)(?:\s*号)?")
_TITLE_2 = re.compile(r"(?:第\s*(\d+)\s*号?\s*議案|(?<![号提])議案)\s*(.{1,80}?の件)")


def _norm(text):
    s = unicodedata.normalize("NFKC", text or "")
    s = re.sub(f"[{_DASHES}]", "-", s)
    s = re.sub(r"(可|否)\s+決", r"\1決", s)  # 「可　決」 (S100XVT3, S100YM1E)
    s = re.sub(r"(?<=[\d\s])ー(?=[\d\s(]|可決|否決)", "-", s)
    return re.sub(r"\s+", " ", s).strip()


def _tail(seg):
    """(start, text) of the trailing run of numbers, commas, dashes and blanks, edges not blank; None if there is none.
    Linear in len(seg)."""
    end = len(seg)
    while end and seg[end - 1].isspace():
        end -= 1
    if not end or seg[end - 1] not in _TAIL_EDGE:
        return None
    i = end
    while i and (seg[i - 1] in _TAIL_EDGE or seg[i - 1].isspace()):
        i -= 1
    while seg[i] not in _TAIL_EDGE:
        i += 1
    if i > 1 and seg[i - 1] == "." and seg[i - 2].isdigit():  # '98.94': figures after a decimal point (not 'Jr.')
        return None
    return i, seg[i:end]


def _int(s):
    v = int(s.replace(",", ""))
    return v if v < 2**63 else None  # joined cells read as one huge number are unknown, not a vote count


@lru_cache(maxsize=4096)
def _splits(run, k):
    """Every way to cut a digit run into k well-formed numbers."""
    if k == 1:
        return ((run,),) if _WELL_FORMED.match(run) else ()
    out = []
    for i in range(1, len(run)):
        head = run[:i]
        if _WELL_FORMED.match(head):
            out += [(head,) + rest for rest in _splits(run[i:], k - 1)]
    return tuple(out)


def _candidates(tail, n_cells=3):
    """Readings of a row's count cells as (for, against, abstain).

    ``n_cells`` includes any extra numeric table column (most often total
    voting rights represented).  Extra columns are used to find the cell
    boundaries and then discarded; they must never become abstentions.
    """
    tokens = re.findall(r"[\d,]+|-", tail)
    # Only the full header-defined cell count proves separation: two tokens can
    # be joined cells such as "213002 118022".
    if len(tokens) == n_cells and all(t == "-" or _WELL_FORMED.match(t) for t in tokens):
        cells = tuple(None if t == "-" else _int(t) for t in tokens)
        return [cells], True
    readings = set()
    number_positions = [i for i, token in enumerate(tokens) if token != "-"]
    totals = (n_cells, n_cells - 1) if n_cells == 3 else (n_cells,)
    for total in totals:  # the abstain cell can be blank only in a three-column table
        nums = [tokens[i] for i in number_positions]
        need = total - (len(tokens) - len(nums))
        if need < len(nums) or not nums:
            continue
        for shape in product(*[range(1, need + 1)] * len(nums)):
            if sum(shape) != need:
                continue
            for cut in product(*[_splits(n, c) for n, c in zip(nums, shape)]):
                cells, number_i = [], 0
                for token in tokens:
                    if token == "-":
                        cells.append(None)
                    else:
                        cells.extend(_int(part) for part in cut[number_i])
                        number_i += 1
                if len(cells) == total:
                    cells += [None] * (n_cells - len(cells))
                    readings.add(tuple(cells))
    return sorted(readings, key=str), False


def _sum(r):
    return sum(x or 0 for x in r[:3])


def _plausible(readings, rate, passed):
    """A reading whose approval share is far from a stated rate of a passed resolution cannot be right."""
    if rate is None or not passed:
        return readings
    kept = [r for r in readings if r[0] is not None and (r[3] if len(r) > 3 else _sum(r)) and
            abs(r[0] / (r[3] if len(r) > 3 else _sum(r)) * 100 - rate) <= (0.02 if len(r) > 3 else 10)]
    return kept or readings


def _resolve(rows):
    """Settle each row's counts. Rows sharing one total settle each other. 99/0 and 9/90 have the same total, so a tie
    goes to the abstain figure that every settled row of the document shows, if they all show one."""
    totals = Counter()
    for r in rows:
        for s in {_sum(c) for c in r["_readings"]}:
            totals[s] += 1
    for r in rows:
        readings, method, top, best = r["_readings"], None, [], 0
        if r.get("_method"):
            chosen, method = (readings[0], r["_method"]) if readings else (None, None)
        elif r["_separated"]:
            chosen, method = readings[0], "separated"
        elif len(readings) == 1:
            chosen, method = readings[0], "unique_split"
        else:
            best = max((totals[_sum(c)] for c in readings), default=0)
            top = [c for c in readings if totals[_sum(c)] == best]
            # The shared total counts only if other rows hold it too.
            if best >= 2 and len(top) == 1:
                chosen, method = top[0], "sum_consistency"
            else:
                chosen = None
        r["votes_for"], r["votes_against"], r["votes_abstain"] = chosen[:3] if chosen else (None, None, None)
        r["counts_method"] = method
        r["_top"] = top if method is None and not r["_separated"] and len(readings) > 1 and best >= 2 else None
    abstains = {r["votes_abstain"] for r in rows if r["counts_method"]}
    for r in rows:
        if r["_top"] and len(abstains) == 1:
            hit = [c for c in r["_top"] if c[2] in abstains]
            if len(hit) == 1:
                r["votes_for"], r["votes_against"], r["votes_abstain"] = hit[0][:3]
                r["counts_method"] = "sum_and_abstain_consistency"
    _resolve_by_rate(rows)
    for r in rows:
        for k in ("_readings", "_separated", "_top", "_unit", "_method"):
            r.pop(k, None)


def _rate_share(c, r):
    """The stated rate's share of c, in percent, when the heading says which share it is; else None."""
    basis = r.get("rate_basis")
    which = 0 if basis == "for" or (basis == "for_or_against_by_heading" and r["result"] == "passed") else \
        1 if basis == "against" else None
    total = _sum(c)
    if which is None or len(c) > 3 or not total or c[which] is None:
        return None
    return c[which] / total * 100


def _rate_fits(c, r):
    """The stated rate is c's share rounded or truncated to the stated decimals."""
    v = _rate_share(c, r)
    rate, unit = r["stated_rate_pct"], r["_unit"]
    return v is not None and rate - unit / 2 - 1e-9 <= v < rate + unit - 1e-9


def _resolve_by_rate(rows):
    """Settle a row left open when exactly one of its readings gives the stated rate to the stated decimals.

    This holds only where the rate is figured on for + against + abstain.  Many filings divide by every voting right
    present, so the rule needs the document's own evidence: each row already settled, if it states a rate, gives it
    exactly (and at least one does); or, with no row settled, at least two open rows each fit exactly one reading."""
    def open_row(r):
        return r["counts_method"] is None and not r.get("_method") and len(r["_readings"]) > 1 and r["stated_rate_pct"] is not None and r["_unit"]
    settled = [r for r in rows if r["counts_method"] and r["stated_rate_pct"] is not None and r["_unit"]]
    fits = {}
    for i, r in enumerate(rows):
        if open_row(r):
            hit = [c for c in r["_readings"] if _rate_fits(c, r)]
            if len(hit) == 1:
                fits[i] = hit[0]
    if not fits:
        return
    checked = [r for r in settled if _rate_share((r["votes_for"], r["votes_against"], r["votes_abstain"]), r) is not None]
    if checked:
        if not all(_rate_fits((r["votes_for"], r["votes_against"], r["votes_abstain"]), r) for r in checked):
            return
    elif len(fits) < 2:
        return
    for i, c in fits.items():
        rows[i]["votes_for"], rows[i]["votes_against"], rows[i]["votes_abstain"] = c[:3]
        rows[i]["counts_method"] = "rate_consistency"


_COLUMN_KINDS = (
    (re.compile(r"賛成(?:数|議決権の個数)?$"), "for"),
    (re.compile(r"反対(?:数|議決権の個数)?$"), "against"),
    (re.compile(r"棄権(?:数|議決権の個数)?(?:・無効数?)?$"), "abstain"),
    (re.compile(r"無効(?:数|等)?$|確認できなかった|不行使"), "void"),
    (re.compile(r"出席|行使|総数|有効"), "total"),
)


def _columns(header_zone):
    """Kinds of the count columns, in order, from headings each followed by '(個)': for, against, abstain, void (無効,
    unconfirmed: read and dropped) or total (voting rights present or exercised).  None unless the headings show
    exactly one for, against and abstain column, at most one total, and more than those three."""
    labels = re.split(r"\(\s*個\s*\)", header_zone)[:-1]
    kinds = []
    for label in labels:
        label = re.sub(r"\([^()]*\)|\s", "", re.sub(r"\(\s*注\s*\d*\s*\)\s*\d*", "", label))
        kinds.append(next((k for pat, k in _COLUMN_KINDS if pat.search(label)), None))
    if len(kinds) <= 3 or None in kinds or kinds.count("total") > 1 or \
            any(kinds.count(k) != 1 for k in ("for", "against", "abstain")):
        return None
    return kinds


def _arrange(readings, kinds):
    """Readings of the table's cells as (for, against, abstain[, total]); a sum above the stated total is impossible."""
    out = set()
    for c in readings:
        cell = dict(zip(kinds, c))  # one each of for/against/abstain/total; voids are dropped
        r = (cell["for"], cell["against"], cell["abstain"]) + ((cell["total"],) if "total" in kinds else ())
        if len(r) > 3 and r[3] is not None and _sum(r) > r[3]:
            continue
        out.add(r)
    return sorted(out, key=str)


_GAP = re.compile(r"[\d.\s()%]*")


def _rate_runs(text):
    """(run, end offset) of the decimal runs starting text ('97.46', '95.7497.21'), up to the first figure without a
    decimal point."""
    runs = []
    for m in re.finditer(r"[\d.]+", text):
        if "." not in m.group(0):
            break
        runs.append((m.group(0), m.end()))
    return runs


def _split_rates(run, n=None):
    """'95.7497.2197.27' -> ['95.74', '97.21', '97.27']: rates run together share their decimals; none exceeds 100."""
    if run.count(".") == 1:
        return [run] if re.fullmatch(r"(?:100|\d{1,2})\.\d+", run) else None
    for d in (2, 1, 3):
        unit = rf"(?:100|\d{{1,2}})\.\d{{{d}}}"
        if re.fullmatch(rf"(?:{unit})+", run):
            parts = re.findall(unit, run)
            if n is None or len(parts) == n:
                return parts
    return None


def _group_end(body, hits, i):
    """Index of the last result word run together with hits[i], with nothing but decimal rates between them."""
    j = i
    while j + 1 < len(hits):
        gap = body[hits[j].end():hits[j + 1].start()]
        if not _GAP.fullmatch(gap) or re.search(r"(?<![\d.])\d+(?![\d.])", gap):
            break
        j += 1
    return j


# Search steps one document may spend cutting column-laid-out cells (a second or so); past it, groups stay unresolved.
_DOC_STEPS = 400000


def _column_cells(tokens, n, kinds, check, budget, cap=2000, max_steps=100000):
    """Readings of n candidates' cells laid out by column, as one tuple of cells per candidate.

    Cells are cut one at a time, each a well-formed number of at most 13 characters or a dash, so the search is bounded
    by its step cap rather than by the number of ways to cut a long run.  `check(i, cells)` sees a candidate's cells cut
    so far, from its second column on, and rejects it as early as it can.  None when a cap is hit: too many readings to settle anything."""
    k, total = len(kinds), n * len(kinds)
    out, steps = [], [0]
    flat = []
    memo = {}

    def cuts(t, off):
        """(value, width) of each well-formed cell that can start at tokens[t][off:]."""
        key = (t, off)
        if key not in memo:
            if tokens[t] == "-":
                memo[key] = ((None, 1),)
            else:
                rest = tokens[t][off:]
                memo[key] = tuple((_int(rest[:w]), w) for w in range(1, min(13, len(rest)) + 1)
                                  if rest[w:w + 1] != "," and _WELL_FORMED.match(rest[:w]))
        return memo[key]

    suffix_chars = [0] * (len(tokens) + 1)
    for t in range(len(tokens) - 1, -1, -1):
        suffix_chars[t] = suffix_chars[t + 1] + len(tokens[t])

    def walk(t, off):
        steps[0] += 1
        if steps[0] > max_steps or len(out) > cap:
            return
        if t < len(tokens) and off == len(tokens[t]):
            t, off = t + 1, 0
        left = total - len(flat)
        if t == len(tokens):
            if left == 0:
                out.append(tuple(tuple(flat[col * n + i] for col in range(k)) for i in range(n)))
            return
        chars = len(tokens[t]) - off + suffix_chars[t + 1]
        # Every token still ahead takes one cell at least, and no cell is longer than 13 characters.
        if left <= 0 or chars < left or chars > 13 * left or len(tokens) - t > left:
            return
        p = len(flat)
        ahead = len(tokens) - t - 1  # cells the later tokens need, beyond this one
        for value, w in cuts(t, off):
            if w < len(tokens[t]) - off and left - 1 - ahead < 1:
                continue
            flat.append(value)
            if p < n or check(p % n, tuple(flat[col * n + p % n] for col in range(p // n + 1)), flat):
                walk(t, off + w)
            flat.pop()

    max_steps = min(max_steps, budget[0])
    walk(0, 0)
    budget[0] -= min(steps[0], max_steps)
    return None if steps[0] > max_steps or len(out) > cap else out


def _cell_ok(c, rate, passed, basis):
    if passed and c[0] is not None and c[1] is not None and c[0] < c[1]:
        return False
    if len(c) > 3 and c[3] is not None and _sum(c) > c[3]:
        return False
    if rate is not None and passed and basis == "for" and c[0] is not None:
        total = c[3] if len(c) > 3 and c[3] else _sum(c)
        if not total or abs(c[0] / total * 100 - rate) > (0.02 if len(c) > 3 and c[3] else 10):
            return False
    return True


def _column_readings(tail, kinds, n, rates, units, results, basis, budget=None):
    """Counts of n candidates laid out by column (n for cells, then n against, ...) and the method that settles them.

    A reading stands only if it is the one whose for + against + abstain totals are equal for every candidate, else
    the one whose totals are at most 3 votes apart, else the one whose totals agree within 0.5%, else the one giving every stated rate to its decimals; cells already
    divided by blanks stand as they are."""
    budget = budget if budget is not None else [_DOC_STEPS]
    tokens = re.findall(r"[\d,]+|-", tail)
    k = len(kinds)
    separated = len(tokens) == n * k and all(t == "-" or _WELL_FORMED.match(t) for t in tokens)

    def exact(j, c):
        return rates[j] is not None and units[j] and _rate_fits(
            c, {"rate_basis": basis, "result": results[j], "stated_rate_pct": rates[j], "_unit": units[j]})

    def check(i, cells, flat):
        if len(cells) < k:  # partial: for and against known once both columns are cut
            if len(cells) <= max(at_for, at_against) or results[i] != "passed":
                return True
            f, a = cells[at_for], cells[at_against]
            if f is None or a is None:
                return True
            if f < a:
                return False
            # A passed for-share of r% leaves against at most for * (100 / (r - 10) - 1), the rate test's slack.
            r = rates[i] if basis == "for" else None
            return r is None or r <= 10 or a <= f * (100 / (r - 10) - 1) + 1
        arranged = _arrange([cells], kinds)
        if not arranged or not _cell_ok(arranged[0], rates[i], results[i] == "passed", basis):
            return False
        # Only a reading whose totals agree, or whose every rate fits, can be accepted: prune the rest early.  The
        # first pass keeps totals at most 3 votes apart; only if none exists does a second pass widen to 0.5% or
        # every rate fitting.  (0.5% of ten million votes admits any split of small cells, which is slow.)
        # Candidates complete in order along the search path, so totals[:i] belong to the path being tried.
        totals[i], fits[i] = _sum(arranged[0]), bool(exact(i, arranged[0]))
        if separated or i == 0:
            return True
        lo, hi = min(totals[:i + 1]), max(totals[:i + 1])
        if mode[0] == "tight":
            return hi - lo <= 3
        return bool(hi and hi - lo <= 0.005 * hi) or all(fits[:i + 1])

    totals, fits = [0] * n, [False] * n
    at_for, at_against = kinds.index("for"), kinds.index("against")

    mode = ["tight"]

    if separated:  # blanks already divide the cells: one reading, checked like any other
        flat = [None if t == "-" else _int(t) for t in tokens]
        cells = [tuple(flat[col * n + i] for col in range(k)) for i in range(n)]
        if all(check(i, c, flat) for i, c in enumerate(cells)):
            return tuple(_arrange([c], kinds)[0] for c in cells), "separated"
        return None, None

    def sums(rd):
        return [_sum(c) for c in rd]

    def readings_of(found):
        return list(dict.fromkeys(tuple(_arrange([c], kinds)[0] for c in rd) for rd in found))

    found = _column_cells(tokens, n, kinds, check, budget)
    if found is None:
        return None, None
    if found:
        readings = readings_of(found)
        equal = [rd for rd in readings if len(set(sums(rd))) == 1]
        # Totals a few votes apart (votes the count left out, '593,968' against '593,969') are as good as equal; a
        # cell cut in the wrong place moves a total by tens at least.
        tiers = (equal, readings)
    else:
        mode[0] = "wide"
        found = _column_cells(tokens, n, kinds, check, budget, max_steps=30000)
        if not found:
            return None, None
        readings = readings_of(found)
        near = [rd for rd in readings if max(sums(rd)) and (max(sums(rd)) - min(sums(rd))) <= 0.005 * max(sums(rd))]
        exact_all = [rd for rd in readings if all(exact(i, c) for i, c in enumerate(rd))]
        tiers = (near, exact_all)
    for got, method in zip(tiers, ("column_sum_consistency",) * 2 if found and mode[0] == "tight" else
                           ("column_sum_consistency", "column_rate_consistency")):
        if len(got) == 1:
            return got[0], method
        if len(got) > 1:
            break
    return None, None


def _read_group(body, hits, i, seg, rate_first):
    """(last hit index, seg without its rates, n rate strings, end of the group) when hits[i] starts the results of
    several candidates laid out by column; else None.  The rates follow each result word, follow them all run
    together, or (rate-first tables) end seg."""
    j = _group_end(body, hits, i)
    if rate_first:
        k = len(seg)
        while k and (seg[k - 1].isdigit() or seg[k - 1] in ". %()" or seg[k - 1].isspace()):
            k -= 1
        runs = _rate_runs_back(seg[k:])
        if not runs:
            return None
        cut = seg.rindex(runs[0], k)
        # The first run can carry the last count cell in front: '...4646 93.39%' written '...464693.39%'.
        lead = next((x for x in range(len(runs[0])) if _split_rates(runs[0][x:])), None)
        if lead is None:
            return None
        cut, runs[0] = cut + lead, runs[0][lead:]
        after = hits[j].end()
        rest_seg = seg[:cut]
    else:
        runs, after = [], hits[j].end()
        for x in range(i, j + 1):
            stop = hits[x + 1].start() if x < j else min(hits[x].end() + 200, len(body))
            found = _rate_runs(_GAP.match(body, hits[x].end(), stop).group(0))
            runs += [run for run, _ in found]
            if x == j and found:  # stop after the last rate: the next row's figures may follow a blank
                after = hits[j].end() + found[-1][1]
        rest_seg = seg
    n_words = j - i + 1
    rates = []
    for run in runs:
        parts = _split_rates(run)
        if parts is None:
            return None
        rates += parts
    # One result word stands for several candidates only when their rates are run together in one figure: two
    # figures after one word are a rate and its reference ('(97.45%)99.70%', '94.89(0.42)').
    if n_words == 1 and (len(runs) != 1 or len(rates) < 2):
        return None
    if n_words > 1 and not rates:
        rates = [None] * n_words
    if n_words > 1 and len(rates) != n_words:
        return None
    return j, rest_seg, rates, after


def _rate_runs_back(text):
    """Decimal runs ending text, back to the first figure without a decimal point."""
    runs = re.findall(r"[\d.]+", text)
    out = []
    for run in reversed(runs):
        if "." not in run:
            break
        out.insert(0, run)
    return out


def _names(candidate, listed, n, section2=None):
    """n candidate names, in table order, from item (2)'s list or the row's words."""
    compact = re.sub(r"\s", "", candidate or "")
    if listed and len(listed) == n:
        at = [compact.find(re.sub(r"\s", "", x)) for x in listed]
        if all(p >= 0 for p in at) and at == sorted(at):
            return list(listed)
    numbered = [w.strip() for w in re.split(r"(?:^|(?<=\D))\d{1,2}\.\s*", candidate or "") if w.strip()]
    if len(numbered) == n and re.match(r"\s*1\.", candidate or ""):  # '1.丙見 秀人2.丁原 正好'
        return numbered
    words = [w for w in re.sub(r"\([^()]*\)", " ", candidate or "").split() if w]
    if len(words) == n and all(len(w) >= 2 for w in words):
        return words
    # Surname and given name in pairs, kept only when item (2) states every name so paired: words run together
    # across names ('甲松 英之逵 乙 隆小 丁 勝' for 甲松英之, 逵乙隆, 小丁勝) pair wrongly.
    if len(words) == 2 * n:
        pairs = [words[2 * i] + " " + words[2 * i + 1] for i in range(n)]
        stated = re.sub(r"\s", "", section2 or "")
        if stated and all(p.replace(" ", "") in stated for p in pairs):
            return pairs
    return None


def _titles(section2):
    return {int(m.group(1) or 0): re.sub(r"^[\s:]+", "", m.group(2)) for m in _TITLE_2.finditer(section2)}


def _candidate_lists(section2):
    """Candidate names stated in item (2), keyed by proposal number."""
    out = {}
    marks = list(_PROPOSAL.finditer(section2))
    for i, m in enumerate(marks):
        no = int(m.group(1) or m.group(2) or 0)
        piece = section2[m.end():marks[i + 1].start() if i + 1 < len(marks) else len(section2)]
        title = re.search(r"(?:取締役|監査役|補欠監査役)\s*(\d+)\s*名選任", piece)
        names = re.search(r"として、(.{2,200}?)を選任", piece)
        if not no or not title or not names:
            continue
        stated = re.sub(r"の\s*\d+\s*名$", "", names.group(1))  # '…、丁渡万希子の4名を選任'
        parts = [p.strip() for p in re.split(r"、|及び|および", re.sub(r"\([^()]*\)", "", stated)) if p.strip()]  # '山田太郎(1960年4月1日生、64歳)' 
        if len(parts) == int(title.group(1)):
            out[no] = parts
    return out


def _proposal_results(body, proposal_no):
    """Result/rate cells for one proposal in a vertically laid-out table."""
    marks = list(re.finditer(rf"第\s*{proposal_no}\s*号?\s*議案", body))
    if not marks:
        return []
    start = marks[-1].end()
    nxt = re.search(r"第\s*\d+\s*号?\s*議案", body[start:])
    piece = body[start:start + nxt.start()] if nxt else body[start:]
    out = []
    for hit in _RESULT.finditer(piece):
        rm = _RATE_AFTER.match(piece, hit.end())
        word = hit.group(0)
        result = None if any(x in word for x in ("できなかった", "しなかった", "せず")) else "passed" if word == "可決" else "failed"
        out.append((result, float(rm.group(1)) if rm else None))
    return out


def parse_text(text):
    """{meeting_date, rows, warnings} from the resolution block. rows is [] when no result table can be read."""
    s = _norm(text)
    head = re.search(r"決議事項に対する賛成|賛成、反対及び棄権|賛成、反対および棄権", s)
    meeting = re.search(r"開催された年月日\s*[::]?\s*((?:令和|平成)?\s*(?:\d{1,4}|元)\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)", s)
    out = {"meeting_date": parse_jp_date(meeting.group(1)) if meeting else None, "rows": [], "warnings": []}
    if not head:
        out["warnings"].append("result_table_not_found")
        return out
    section2 = s[:head.start()]
    titles = _titles(section2)
    candidate_lists = _candidate_lists(section2)
    end = re.search(r"(?:\(\s*4\s*\)|4\s*[..]\s)\s*[^ ]{0,40}(?:加算|理由)", s[head.end():])
    body = s[head.end():head.end() + end.start()] if end else s[head.end():]
    hits = list(_RESULT.finditer(body))
    if not hits:
        out["warnings"].append("no_result_words")
        return out
    first = body[:hits[0].start()]
    table_at = max(first.rfind("決議事項"), 0)
    header_zone = first[table_at:]
    heads = list(re.finditer(r"割合\s*(?:\(\s*%?\s*\))?\)?|賛成(?:比)?率\s*(?:\(\s*%?\s*\))?|決議(?:の)?結果(?:\s*\([^)]*\))?", header_zone))
    if not heads:
        out["warnings"].append("table_header_not_found")
        return out
    rate_at = re.search(r"賛成(?:比)?率|割合", header_zone)
    result_at = list(re.finditer(r"決議(?:の)?結果", header_zone))
    rate_first = bool(rate_at and result_at and rate_at.start() < result_at[-1].start())
    basis = "for_or_against_by_heading" if re.search(r"賛成\s*\(\s*反対\s*\)", header_zone) else \
        "against" if re.search(r"反対(?:の)?(?:割合|比?率)", header_zone) and not re.search(r"賛成(?:の)?(?:割合|比?率)", header_zone) \
        and (rate_at is None or "賛成" not in header_zone[max(rate_at.start() - 6, 0):rate_at.start()]) else "for"
    # One block can hold several tables, each with its own heading: company proposals with 賛成比率, then
    # shareholder proposals with 反対比率 (S100Z5I5).  A heading right after 決議結果 sets the basis from there on.
    marks = [(m.start(), "against" if m.group(1) == "反対" else "for")
             for m in re.finditer(r"決議(?:の)?結果\s*(?:\([^)]*\))?\s*(賛成|反対)(?:の)?(?:比率|割合|率)", body)]
    kinds = _columns(header_zone)
    if not kinds:
        kinds = ["for", "against", "abstain"] + \
            (["total"] if re.search(r"棄権.{0,80}出席した議決権|棄権.{0,80}出席株主", header_zone) else [])
    count_cells = len(kinds)
    prev = table_at + heads[-1].end()
    budget = [_DOC_STEPS]
    proposal_no, title = None, None
    rows = []
    i = 0
    while i < len(hits):
        hit = hits[i]
        seg = body[prev:hit.start()]
        rate_m = None if rate_first else _RATE_AFTER.match(body, hit.end())
        seg = re.sub(r"^[\s)%]+", "", seg)
        seg = _NOTE_END.sub("", seg)
        # '(注)198.487可決' is note 1 then the rate 98.487 (no rate exceeds 100); '(注)93.7' is a rate of 93.7.
        seg = re.sub(r"\(\s*注\s*\)\s*\d(?=(?:100|\d{2})\.\d)", " ", seg)
        seg = re.sub(r"\(\s*注\s*\)\s*(?:\d+(?!\d|\.\d)\.?)?|\(\s*注\s*\d*\s*\)", " ", seg)
        group = _read_group(body, hits, i, seg, rate_first)
        if group:
            j, seg, rates, after = group
            prev = after
        else:
            j = i
            prev = rate_m.end() if rate_m else hit.end()
        rate = float(rate_m.group(1)) if rate_m else None
        rate_text = rate_m.group(1) if rate_m else None
        # A decimal figure right before the result word is the row's rate, whatever the heading order says.
        if not group and (rate_first or (rate_m is None and re.search(r"\d\.\d+\s*%?\s*$", seg))):
            rm = _RATE_END.search(seg)
            if rm:
                rate, rate_text, seg = float(rm.group(1)), rm.group(1), seg[:rm.start()]
        first_i, i = i, j + 1
        # The rate's last stated decimal; whole-number rates are too coarse to settle a reading.
        unit = 10.0 ** -len(rate_text.split(".")[1]) if rate_text and "." in rate_text else None
        tail = _tail(seg)
        if not tail or not re.search(r"\d", tail[1]):
            continue
        label = seg[:tail[0]]
        proposal = None
        for proposal in _PROPOSAL.finditer(label):
            pass
        rest = label
        if proposal:
            proposal_no = int(proposal.group(1) or proposal.group(2) or 0) or None
            rest = label[proposal.end():]
            title = titles.get(proposal_no or 0)
        rest = re.sub(r"(?:\(注\)\s*\d*|\(注\s*\d*\))\s*[\d、,]*", " ", rest)
        rest = re.sub(r"(?:^|\s)-(?=\s|$)", " ", rest)
        rest = re.sub(r"\s+", " ", rest).strip(" -.")
        candidate = None
        if "の件" in rest:
            cut = rest.rindex("の件") + 2
            title = rest[:cut].strip()
            candidate = rest[cut:].strip() or None
            if candidate and candidate.startswith("("):  # '剰余金の処分の件(臨時配当の実施)'
                title, candidate = title + candidate, None
        elif rest:
            # With a proposal heading and no 件, the words are its title unless (2) says this proposal elects people.
            elects = bool(title and "選任" in title)
            if proposal and not elects and len(rest) > 12:
                title = rest
            else:
                candidate = rest
        row_basis = basis
        if basis != "for_or_against_by_heading":
            row_basis = next((b for at, b in reversed(marks) if at < hit.start()), basis)
        words = [h.group(0) for h in hits[first_i:j + 1]]
        if group:
            n = len(rates)
            words = words * n if len(words) == 1 else words
            results = [None if any(w in x for w in ("できなかった", "しなかった", "せず")) else
                       "passed" if x == "可決" else "failed" for x in words]
            names = _names(candidate, candidate_lists.get(proposal_no), n, section2)
            units = [10.0 ** -len(x.split(".")[1]) if x and "." in x else None for x in rates]
            values = [float(x) if x else None for x in rates]
            reading, method = _column_readings(tail[1], kinds, n, values, units, results, row_basis, budget)
            for k in range(n):
                rows.append({"proposal_no": proposal_no, "proposal_title_ja": title,
                             "candidate_ja": names[k] if names else None, "stated_rate_pct": values[k],
                             "rate_basis": row_basis if values[k] is not None else None, "result": results[k],
                             "_readings": [reading[k]] if reading else [], "_separated": False, "_unit": units[k],
                             "_method": method or "unresolved", "_unnamed": not names})
            continue
        result_word = hit.group(0)
        negated = any(word in result_word for word in ("できなかった", "しなかった", "せず"))
        passed = result_word == "可決"
        readings, separated = _candidates(tail[1], count_cells)
        readings = _arrange(readings, kinds)
        separated = separated and len(readings) == 1
        rows.append({"proposal_no": proposal_no, "proposal_title_ja": title, "candidate_ja": candidate, "stated_rate_pct": rate,
                     "rate_basis": row_basis if rate is not None else None,
                     "result": None if negated else "passed" if passed else "failed",
                     "_readings": _plausible(readings, rate if row_basis == "for" else None, passed), "_separated": separated,
                     "_unit": unit})
    _resolve(rows)
    expanded = []
    for row in rows:
        names = candidate_lists.get(row["proposal_no"])
        compact = re.sub(r"\s", "", row.get("candidate_ja") or "")
        if names and len(names) > 1 and all(re.sub(r"\s", "", n) in compact for n in names):
            results = _proposal_results(body, row["proposal_no"])
            for i, name in enumerate(names):
                item = dict(row)
                item["candidate_ja"] = name
                item["votes_for"] = item["votes_against"] = item["votes_abstain"] = None
                item["counts_method"] = None
                if i < len(results):
                    item["result"], item["stated_rate_pct"] = results[i]
                expanded.append(item)
            out["warnings"].append("multi_candidate_counts_unresolved")
        else:
            expanded.append(row)
    rows = expanded
    for row in rows:
        if row.pop("_unnamed", False):
            out["warnings"].append("candidate_unresolved")
        counts = (row["votes_for"], row["votes_against"], row["votes_abstain"])
        bad_counts = (row["result"] == "passed" and row["votes_for"] is not None and row["votes_against"] is not None and
                      row["votes_for"] < row["votes_against"]) or any(v is not None and v > 1_000_000_000 for v in counts)
        candidate = row.get("candidate_ja") or ""
        compact = re.sub(r"\s", "", candidate)
        single_alias = bool(re.fullmatch(r"[^()]{2,20}\s*\(戸籍上の氏名:[^()]{2,20}\)", candidate))
        bad_candidate = (bool(re.search(r"\d", candidate)) or
                         (len(compact) > 12 and bool(re.search(r"[一-龥々ぁ-んァ-ヶ]", candidate)) and not single_alias))
        if bad_counts or bad_candidate:
            row["votes_for"] = row["votes_against"] = row["votes_abstain"] = None
            row["counts_method"] = None
        if bad_counts:
            out["warnings"].append("inconsistent_counts_rejected")
        if bad_candidate:
            row["candidate_ja"] = None
            out["warnings"].append("candidate_unresolved")
    if not rows:
        rows = _unit_rows(_norm(text))
        if rows:
            out["warnings"].append("read_by_unit_suffix")
    if not rows:
        out["warnings"].append("no_rows_read")
    if any(r["votes_for"] is None for r in rows):
        out["warnings"].append("counts_unresolved")
    out["warnings"] = list(dict.fromkeys(out["warnings"]))
    out["rows"] = rows
    return out


# A table whose counts end in 個 (units): "251,760個1,370個0個98.72%可決" (S100YLR8, S100XE7P). Each row is three counts,
# a rate and a result in either order; what precedes it since the last row is its label. No nested repeats, so it stays linear.
_UNIT_ROW = re.compile(r"(\d{1,3}(?:,\d{3})*|\d+)個 ?(\d{1,3}(?:,\d{3})*|\d+)個 ?(\d{1,3}(?:,\d{3})*|\d+)個"
                       r"(?: ?\(注 ?\d*\))? ?(?:(\d{1,3}(?:\.\d+)?) ?[%％]? ?(可決|否決)|(可決|否決) ?(\d{1,3}(?:\.\d+)?) ?[%％]?)")


def _unit_rows(text):
    """Rows of a 個-suffixed table, kept only when the stated rate agrees with for/(for+against+abstain) within 1 point."""
    rows, prev = [], 0
    for m in _UNIT_ROW.finditer(text):
        f, a, b = (int(x.replace(",", "")) for x in m.group(1, 2, 3))
        rate = float(m.group(4) or m.group(7))  # rate then result, or result then rate (S100XE7P)
        word = m.group(5) or m.group(6)
        label = text[prev:m.start()].strip(" -.,、")
        prev = m.end()
        if not f + a + b or abs(100 * f / (f + a + b) - rate) > 1.0:
            continue
        label = label[-120:]
        no = re.search(r"第\s*(\d+)\s*号", label)
        name = re.sub(r".*(?:の件|号議案|議案)\s*", "", label).strip()
        name = re.sub(r"^.*?(?:割合|結果|比率|要件|\(注\d*\))\s*", "", name).strip()
        rows.append({"proposal_no": int(no.group(1)) if no else None, "proposal_title_ja": None,
                     "candidate_ja": name if 2 <= len(name) <= 20 and not re.search(r"\d|件|議案|賛成|反対", name) else None,
                     "votes_for": f, "votes_against": a, "votes_abstain": b, "stated_rate_pct": rate, "rate_basis": "for",
                     "result": "passed" if word == "可決" else "failed", "counts_method": "unit_suffix"})
    # carry the proposal number down to candidate rows that follow it
    last = None
    for r in rows:
        last = r["proposal_no"] = r["proposal_no"] or last
    return rows


def parse(facts):
    """(is_voting_report, parsed or None) from xbrlcsv.read_facts output."""
    by = {}
    for f in facts:
        name = f["element"].split(":")[-1]
        by[name] = (by[name] + " " + f["value"]) if name in by and name.endswith("TextBlock") else f["value"]
    has_block = bool(_norm(by.get(_E_RESOLUTION, "")))
    if not has_block and not _REASON.search(_norm(by.get(_E_REASON, ""))):
        return False, None
    if not has_block:
        return True, {"meeting_date": None, "rows": [], "warnings": ["block_missing"]}
    amends = by.get("IdentificationOfDocumentSubjectToAmendmentDEI")
    parsed = parse_text(by[_E_RESOLUTION])
    amends = (amends or "").strip()
    parsed["amends_doc_id"] = amends if amends and amends not in ("－", "-", "―", "—", "該当なし", "なし") else None
    return True, parsed


# Kept for callers of the initial implementation.  New code should use the
# module-wide public entry point `parse`, like the other extraction modules.
parse_facts = parse


def candidates(con, agm_window_only=True, cached_only=False):
    """Documents to open, newest first. The default takes extraordinary reports of listed filers filed in the window
    around their annual report, so results of extraordinary meetings outside it are not reached."""
    sql = ("SELECT f.doc_id, f.submit_datetime, f.edinet_code, i.ticker, f.parent_doc_id FROM filings f "
           "LEFT JOIN issuers i ON i.edinet_code = f.edinet_code "
           "WHERE f.doc_type_code IN ('180','190') AND f.withdrawn = 0 AND f.has_csv = 1 "
           "AND f.doc_id NOT IN (SELECT doc_id FROM voting_read) ")
    if agm_window_only:
        sql += ("AND i.ticker IS NOT NULL AND EXISTS (SELECT 1 FROM filings a WHERE a.edinet_code = f.edinet_code AND a.doc_type_code = '120' AND a.withdrawn = 0 "
                f"AND julianday(substr(f.submit_datetime,1,10)) - julianday(substr(a.submit_datetime,1,10)) BETWEEN {AGM_WINDOW_DAYS[0]} AND {AGM_WINDOW_DAYS[1]}) ")
    rows = con.execute(sql + "ORDER BY f.submit_datetime DESC").fetchall()
    if cached_only:
        rows = [r for r in rows if os.path.exists(os.path.join(edinet.CSV_CACHE, f"{r[0]}.zip"))]
    return rows


def run(con, limit=None, agm_window_only=True, cached_only=False):
    """Open documents not yet read. Returns (opened, voting reports, reports with every row's counts read)."""
    con.executescript(SCHEMA)
    opened = voting = complete = 0
    for doc_id, submitted, code, ticker, parent in candidates(con, agm_window_only, cached_only)[:limit]:
        try:
            facts = xbrlcsv.read_facts(edinet.download_csv_zip(doc_id))
        except Exception as e:
            print(doc_id, "FAILED", e, flush=True)
            continue
        opened += 1
        is_voting, parsed = parse(facts)
        if not is_voting:
            con.execute("INSERT OR REPLACE INTO voting_read VALUES (?,?,?,?)", (doc_id, "not_voting", 0, None))
            con.commit()
            continue
        voting += 1
        status = store(con, doc_id, parsed, submitted, code, ticker, parent)
        complete += status == "parsed"
        con.commit()
    return opened, voting, complete


def store(con, doc_id, parsed, submitted, code, ticker, parent=None):
    rows = parsed["rows"]
    status = "failed" if not rows else "partial" if parsed["warnings"] else "parsed"
    amends = parsed.get("amends_doc_id") or parent
    con.execute("DELETE FROM voting_results WHERE doc_id = ?", (doc_id,))
    for seq, r in enumerate(rows):
        con.execute("INSERT INTO voting_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            doc_id, seq, ticker, code, parsed["meeting_date"], (submitted or "")[:10], r["proposal_no"], r["proposal_title_ja"], r["candidate_ja"],
            r["votes_for"], r["votes_against"], r["votes_abstain"], r["stated_rate_pct"], r["rate_basis"], r["result"], r["counts_method"],
            amends, SOURCE_URL.format(doc_id)))
    con.execute("INSERT OR REPLACE INTO voting_read VALUES (?,?,?,?)", (doc_id, status, len(rows), json.dumps(parsed["warnings"], ensure_ascii=False)))
    return status
