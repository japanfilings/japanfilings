"""Tender offers as stated in EDINET filings.

The document type codes handled here are 240 (tender-offer notification), 260
(withdrawal), 270 (tender-offer result) and 290 (the target board's opinion).  A
row is one source document.  ``related_offer_doc_id`` is EDINET's parent document
id, so a withdrawal, result and opinion can be joined to the notification without
guessing from company names or dates.

CSV packages are read from ``edinet.CSV_CACHE``; no other job downloads these
document types, so a full build needs ``tender-offers --network`` once.

All numbers and excerpts are statements in the filing.  Missing or ambiguous
cells stay null; this module does not infer a price, limit, outcome or opinion.
"""
import datetime
import json
import os
import re
import unicodedata

from . import edinet, xbrlcsv
from .textutil import parse_jp_date


SCHEMA = """
CREATE TABLE IF NOT EXISTS tender_offers (
  doc_id TEXT PRIMARY KEY,
  doc_type_code TEXT NOT NULL,
  related_offer_doc_id TEXT,
  ticker TEXT,
  edinet_code TEXT,
  submit_datetime TEXT,
  target_name_ja TEXT,
  offeror_name_ja TEXT,
  price_jpy INTEGER,
  period_start TEXT,
  period_end TEXT,
  minimum_shares INTEGER,
  maximum_shares INTEGER,
  tendered_shares INTEGER,
  purchased_shares INTEGER,
  parse_status TEXT NOT NULL,
  source_url TEXT NOT NULL,
  record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS to_target ON tender_offers(target_name_ja, submit_datetime);
CREATE INDEX IF NOT EXISTS to_family ON tender_offers(related_offer_doc_id, submit_datetime);
"""

DOC_TYPES = {"240": "notification", "260": "withdrawal", "270": "result", "290": "opinion"}
# Amendments (250 訂正公開買付届出書, 280 訂正公開買付報告書, 300 訂正意見表明報告書) restate
# only the corrected passages as text; they are not rows of their own here.
AMENDMENT_DOC_TYPES = ("250", "280", "300")
SOURCE_URL = "https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{}"
QUOTE_MAX = 199
_DATE = re.compile(r"(?:令和|平成|昭和)?\s*(?:\d{1,4}|元)\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日")
_DASH = "-―—‐−–ー－─━"
# Share counts are in 株; REIT tender offers state 投資口 in 口.
_UNIT = r"(?:\(\s*[株口]\s*\)|[株口])"


def _norm(value):
    value = unicodedata.normalize("NFKC", value or "")
    return re.sub(r"\s+", " ", value).strip()


def _by_element(facts):
    """Last nonblank fact for each local element name (the filing-date context).

    Each value is also filed under ``label:<Japanese item name>`` so a taxonomy
    year that renames an element can still be read by the printed item name.
    """
    out = {}
    for fact in facts:
        value = fact.get("value")
        if value not in (None, "", "－"):
            out[fact["element"].split(":")[-1]] = value
            label = _label_key(fact.get("label"))
            if label:
                out["label:" + label] = value
    return out


def _label_key(label):
    s = _norm(label)
    s = re.sub(r"\s*\[テキストブロック\]\s*$", "", s)
    return s.split("、")[0].strip()


def _pick(by, elements, labels=()):
    """First stated value among element names, then among printed item names."""
    for name in elements:
        if by.get(name):
            return by[name]
    for label in labels:
        if by.get("label:" + label):
            return by["label:" + label]
    return None


def _is_issuer_offer(facts):
    """Issuer tender offers (自社株公開買付け) use the jptoi taxonomy; the filer buys its own shares."""
    return any(str(f.get("element", "")).startswith("jptoi") for f in facts)


_NAME_SPLIT = re.compile(r"\s*(?:、|,|及び|並びに)\s*")


def is_individual(name):
    """True when a stated offeror/filer name is, or includes, a natural person.

    Uses the organization test the investor pages use; anything that does not read
    as an organization is treated as a person, so the public layer masks it.  A
    joint offeror group counts as a person when any member does.
    """
    from .investors import looks_like_organization
    s = _norm(name)
    if not s:
        return None
    parts = [p for p in _NAME_SPLIT.split(s) if p and len(p) >= 2] or [s]
    return any(not looks_like_organization(p) for p in parts)


def _clean_heading(value):
    s = _norm(value)
    # Newer CSV taxonomies include printed section headings in text blocks.
    s = re.sub(r"^\(?\d+\)?\s*【[^】]+】", "", s)
    return s.strip()


def _int(value):
    return int(value.replace(",", ""))


def _quote(value, limit=QUOTE_MAX):
    s = _norm(value)
    if not s:
        return None
    # Drop a printed subsection heading, but keep the disclosure's own words.
    s = re.sub(r"^\(?\d+\)?(?![\d,年月日株円%])\s*(?:【[^】]+】)?\s*", "", s)
    if len(s) <= limit:
        return s
    end = max(s.rfind("。", 0, limit), s.rfind(". ", 0, limit))
    return s[:end + 1] if end >= 60 else s[:limit - 1] + "…"


def _dates(value):
    return [date for date in (parse_jp_date(m.group(0)) for m in _DATE.finditer(_norm(value))) if date]


_YEARLESS_END = re.compile(r"から\s*(同年)?\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")


def _period(value):
    dates = _dates(value)
    if len(dates) == 1:
        # 「2024年6月10日(月曜日)から8月15日(木曜日)まで」/「…から同年4月15日まで」 (S100U8T1, S100VMQ9):
        # the end date's year is the start's, stated by 同年 or by omission.
        s = _norm(value)
        start = _DATE.search(s)
        m = _YEARLESS_END.search(s, start.end()) if start else None
        if m and m.start() - start.end() <= 12:
            year, month, day = int(dates[0][:4]), int(m.group(2)), int(m.group(3))
            if not m.group(1) and (month, day) < (int(dates[0][5:7]), int(dates[0][8:10])):
                year += 1
            try:
                dates.append(datetime.date(year, month, day).isoformat())
            except ValueError:
                pass
    return (dates[0] if dates else None, dates[1] if len(dates) > 1 else None)


def _price(value):
    s = _norm(value)
    # Restrict the match to the first stated per-share price; later text contains
    # market prices and valuation ranges which are not the offer price.
    m = None
    if "買付け等の価格" in s:
        m = re.search(r"買付け等の価格.{0,40}?1\s*株(?:につき|当たり)[、,]?\s*(?:金)?\s*([\d,]+)\s*円", s, re.I)
    if not m:
        m = re.search(r"普通株式(?:(?!終値|市場価格).){0,35}?1\s*株(?:につき|当たり)[、,]?\s*(?:金)?\s*([\d,]+)\s*円", s, re.I)
    if not m:
        m = re.search(r"買付け等の価格.{0,40}?1\s*株(?:につき|当たり)[、,]?\s*(?:金)?\s*([\d,]+)\s*円", s, re.I)
    return _int(m.group(1)) if m else None


_CELL = r"([\d,]+|[" + re.escape(_DASH) + r"]+)\s*" + _UNIT


def _labelled_cell(label, text):
    """``(found, number)`` for a cell printed right after its label; a dash is a stated null."""
    m = re.search(label + r"\s*" + _CELL, text)
    if not m:
        return False, None
    return True, (_int(m.group(1)) if m.group(1)[0].isdigit() else None)


def _stated_number_after(label, text):
    """Number/dash in a labelled cell. A printed dash is a stated null."""
    return _labelled_cell(label, text)[1]


def _planned_numbers(value):
    s = _norm(value)
    labelled = [_labelled_cell(label, s) for label in (r"買付予定数(?!の)", r"買付予定数の下限", r"買付予定数の上限")]
    # A compact table prints the three headers together; there the first value
    # follows 上限 and is not its cell (S100TJQ8).
    compact = re.search(r"買付予定数\s*買付予定数の下限\s*買付予定数の上限", s)
    if not compact and (labelled[1][0] or labelled[2][0]):
        # Each cell stands on its own: a dash in one limit does not void the others.
        return tuple(value for _, value in labelled)
    prefix = re.split(r"\(注|（注", s, maxsplit=1)[0]
    # In compact tables all three labels precede all three values. Preserve the
    # filing's column order; do not derive a missing limit from the planned size.
    # With several share classes the limits apply to the 合計 row (S100UYAF).
    total_row = prefix.rsplit("合計", 1)[1] if "合計" in prefix else ""
    for part in (total_row, prefix):
        cells = [None if not c[0].isdigit() else _int(c) for c in re.findall(_CELL, part)]
        if len(cells) >= 3:
            return cells[0], cells[1], cells[2]
    return None, None, None


def _overview_numbers(value):
    s = _norm(value)
    price = _price(s)
    low = _stated_number_after(r"買付予定数の下限", s)
    high = _stated_number_after(r"買付予定数の上限", s)
    return price, low, high


def _result_numbers(value):
    s = _norm(value)
    a = re.search(r"応募数\s*\(株\)\s*([\d,]+)", s)
    b = re.search(r"買付数\s*\(株\)\s*([\d,]+)", s)
    if a and b:
        return _int(a.group(1)), _int(b.group(1))
    prefix = re.split(r"(?:新株予約権|合計|潜在株券)", s, maxsplit=1)[0]
    cells = re.findall(r"([\d,]+)\s*" + _UNIT, prefix)
    if len(cells) < 2:
        # Issuer tender-offer reports use 応募数(株)2,500 / 買付数(株)2,500.
        a = re.search(r"応募数\s*\(株\)\s*([\d,]+)", s)
        b = re.search(r"買付数\s*\(株\)\s*([\d,]+)", s)
        return (_int(a.group(1)) if a else None, _int(b.group(1)) if b else None)
    return _int(cells[0]), _int(cells[1])


# Joint offerors: the cover page of a 240/260/270 repeats 届出者/報告者の氏名又は名称 as
# ...CoverPage2, 3, ... (S100PXID, S100US8U). The address elements are never read.
_JOINT_NAME = re.compile(r"^FullNameOrNameOfFiler\w*CoverPage(\d+)$")
# A fund's name may continue with its general partner's representative (S100UJD8).
_REPRESENTATIVE = re.compile(r"\s*(?:無限責任組合員|業務執行組合員|代表取締役|代表執行役|代表社員|代表者|代表理事).*$")


def _joint_offerors(facts):
    """Names of co-offerors stated on the cover page besides the filer (in printed order)."""
    found = {}
    for fact in facts:
        m = _JOINT_NAME.match(str(fact.get("element", "")).split(":")[-1])
        if m and int(m.group(1)) >= 2:
            name = _REPRESENTATIVE.sub("", _clean_heading(fact.get("value")))
            if name and name not in ("該当事項はありません。", "該当事項はありません"):
                found.setdefault(int(m.group(1)), name)
    return [found[k] for k in sorted(found)]


def _offeror_from_opinion(value):
    s = _clean_heading(value)
    # 氏名 is the item for a natural-person offeror; skip the heading 氏名又は名称及び住所.
    m = re.search(r"(?:氏\s*名|名\s*称)(?!又は|及び)\s*[:：]?\s*(.{2,80}?)\s*(?:所在地|住所)", s)
    return m.group(1).strip(" :：") if m else None


def _opinion_quote(value):
    s = _norm(value)
    if not s:
        return None
    # The first subsection is explicitly the board's stated opinion.  Stop
    # before the reasons so the excerpt cannot accidentally be our summary.
    start = (re.search(r"\(1\)\s*【?意見の内容】?", s)
             or re.search(r"\(1\)\s*本公開買付けに関する意見の内容", s))
    if start:
        s = s[start.end():]
    # Require whitespace before subsection (2): the first paragraph often
    # refers to 下記「(2)意見の根拠...」 and that reference is not the boundary.
    stop = re.search(r"(?<=。)\s*\(2\)\s*(?:【)?(?:本公開買付けに関する)?意見の(?:根拠|理由)", s)
    if stop:
        s = s[:stop.start()]
    return _quote(s)


def parse(facts, doc_type_code):
    """Parse one ``xbrlcsv.read_facts`` result into a source-faithful dict."""
    if doc_type_code not in DOC_TYPES:
        raise ValueError(f"unsupported tender-offer doc type: {doc_type_code}")
    by = _by_element(facts)
    filer = _clean_heading(by.get("FilerNameInJapaneseDEI")) or None
    security = _clean_heading(by.get("SecurityCodeDEI")) or None
    ticker = security[:4] if security and len(security) == 5 and security.endswith("0") else None
    rec = {
        "document_kind": DOC_TYPES[doc_type_code],
        "target_name_ja": None,
        "offeror_name_ja": filer,
        "target_ticker": ticker,
        "share_class_ja": None,
        "price_jpy": None,
        "period_start": None,
        "period_end": None,
        "planned_shares": None,
        "minimum_shares": None,
        "maximum_shares": None,
        "purpose_quote_ja": None,
        "board_opinion_quote_ja": None,
        "tendered_shares": None,
        "purchased_shares": None,
        "result_quote_ja": None,
        "withdrawal_quote_ja": None,
        "issuer_tender_offer": _is_issuer_offer(facts),
        "joint_offerors": [],
        "warnings": [],
    }
    subject = _clean_heading(_pick(by, ("NameOfSubjectCompanyTextBlock",), ("対象者名",))) or None
    if doc_type_code == "240":
        # Issuer tender offers have no 対象者名 block: the filer buys its own shares (S100YVRY).
        rec["target_name_ja"] = subject or (filer if rec["issuer_tender_offer"] else None)
        rec["share_class_ja"] = _clean_heading(_pick(by, (
            "ClassesOfShareCertificatesEtcToAcquireByPurchaseEtcTextBlock",
            "ClassesOfListedShareCertificatesEtcToPurchaseEtcTextBlock",
            "ClassesOfSharesRelatedToListedShareCertificatesEtcToAcquireByPurchaseEtcTextBlock"),
            ("買付け等をする株券等の種類", "買付け等をする上場株券等の種類"))) or None
        rec["purpose_quote_ja"] = _quote(_pick(by, (
            "PurposesOfPurchaseEtcTextBlock", "PurposesOfPurchaseEtcOfListedShareCertificatesEtcTextBlock"),
            ("買付け等の目的",)))
        period = _pick(by, ("OriginalPeriodAtFilingTextBlock", "PeriodOfPurchaseEtcTextBlock"),
                       ("届出当初の期間", "買付け等の期間"))
        rec["period_start"], rec["period_end"] = _period(period)
        price_block = _pick(by, ("PriceOfPurchaseEtcTextBlock", "PriceOfPurchaseEtcOfListedShareCertificatesEtcTextBlock"),
                            ("買付け等の価格", "買付け等の価格等"))
        rec["price_jpy"] = _price(price_block)
        overview = by.get("OverviewOfPurchaseEtcTextBlock")
        if rec["price_jpy"] is None and overview:
            rec["price_jpy"] = _price(overview)
        planned = _pick(by, ("NumberOfShareCertificatesEtcIntendedToPurchaseTextBlock",
                             "NumberOfListedShareCertificatesEtcIntendedToPurchaseTextBlock"),
                        ("買付予定の株券等の数", "買付予定の上場株券等の数"))
        rec["planned_shares"], rec["minimum_shares"], rec["maximum_shares"] = _planned_numbers(planned)
    elif doc_type_code == "260":
        rec["target_name_ja"] = subject or (filer if rec["issuer_tender_offer"] else None)
        rec["withdrawal_quote_ja"] = _quote(_pick(by, (
            "ReasonsForWithdrawalEtcOfTenderOfferTextBlock", "ContentsOfWithdrawalEtcOfTenderOfferTextBlock",
            "ReasonForWithdrawalEtcOfTenderOfferTextBlock"),
            ("公開買付けの撤回等の理由", "撤回等の理由", "公開買付けを撤回等する旨", "撤回等の内容")) or next(
                (v for k, v in by.items() if k.startswith("label:") and "撤回" in k and "開示の方法" not in k), None))
    elif doc_type_code == "270":
        # Issuer tender offers: the filer is the target (S100Z0EY). Otherwise the filer
        # is the offeror and must never be reported as the target.
        rec["target_name_ja"] = subject or (filer if rec["issuer_tender_offer"] else None)
        rec["share_class_ja"] = _clean_heading(_pick(by, (
            "ClassesOfShareCertificatesEtcRelatedToPurchaseEtcTextBlock",
            "ClassesOfShareCertificatesRelatedToListedShareCertificatesEtcRelatedToPurchaseEtcTextBlock"),
            ("買付け等に係る株券等の種類", "買付け等に係る上場株券等の種類"))) or None
        rec["period_start"], rec["period_end"] = _period(_pick(
            by, ("TenderOfferPeriodTextBlock", "PeriodOfPurchaseEtcTextBlock"), ("公開買付期間", "買付け等の期間")))
        result = _pick(by, ("NumberOfShareCertificatesEtcAcquiredByPurchaseEtcTextBlock",
                            "NumberOfListedShareCertificatesEtcAcquiredByPurchaseEtcTextBlock"),
                       ("買付け等を行った株券等の数", "買付け等を行った上場株券等の数"))
        rec["tendered_shares"], rec["purchased_shares"] = _result_numbers(result)
        rec["result_quote_ja"] = _quote(_pick(by, ("SuccessOrFailureOfTenderOfferTextBlock",), ("公開買付けの成否",)))
    else:
        rec["target_name_ja"] = filer
        rec["offeror_name_ja"] = _offeror_from_opinion(_pick(
            by, ("NameAndResidentialAddressOrLocationOfTenderOfferorTextBlock",), ("公開買付者の氏名又は名称及び住所又は所在地",)))
        rec["share_class_ja"] = _clean_heading(_pick(
            by, ("ClassesOfShareCertificatesEtcForTenderOfferorToAcquireByPurchaseEtcTextBlock",),
            ("公開買付者が買付け等を行う株券等の種類",))) or None
        overview = _pick(by, ("OverviewOfPurchaseEtcTextBlock",), ("買付け等の概要",))
        if overview:
            rec["period_start"], rec["period_end"] = _period(overview)
            rec["price_jpy"], rec["minimum_shares"], rec["maximum_shares"] = _overview_numbers(overview)
        opinion = _pick(by, ("OpinionRegardingSaidTenderOfferAndBasisAndReasonsEtcTextBlock",
                             "OpinionAndBasisAndReasonOfOpinionRegardingSaidTenderOfferTextBlock"),
                        ("当該公開買付けに関する意見の内容",))
        rec["board_opinion_quote_ja"] = _opinion_quote(opinion)

    # A natural person as offeror (or as filer of a 240/260/270) is flagged so the
    # public layer masks the name; the name is kept as filed so every spelling can
    # be scrubbed from the quotes (same convention as events.filer_is_individual).
    rec["offeror_is_individual"] = is_individual(rec["offeror_name_ja"])
    rec["filer_is_individual"] = is_individual(filer) if doc_type_code != "290" else False
    if doc_type_code != "290":
        # A person among joint offerors flags the whole group; the person's name is
        # not kept (only organizations are listed, a person is null).
        for name in _joint_offerors(facts):
            person = bool(is_individual(name))
            rec["joint_offerors"].append({"name_ja": None if person else name, "is_individual": person})
            if person:
                rec["offeror_is_individual"] = True

    required = {
        "240": ("target_name_ja", "offeror_name_ja", "price_jpy", "period_start", "period_end", "purpose_quote_ja"),
        "260": ("target_name_ja", "offeror_name_ja"),
        "270": ("target_name_ja", "offeror_name_ja", "period_start", "period_end", "tendered_shares", "purchased_shares"),
        "290": ("target_name_ja", "offeror_name_ja", "board_opinion_quote_ja"),
    }[doc_type_code]
    rec["warnings"] = [f"{name}_not_found" for name in required if rec[name] is None]
    rec["parse_status"] = "parsed" if not rec["warnings"] else "partial"
    return rec


# Parent document type -> its amendment type (訂正届出書/訂正報告書/訂正意見表明報告書).
AMENDMENT_OF = {"240": "250", "270": "280", "290": "300"}


def link_amendments(con):
    """Record later amendments on each parent row, from EDINET metadata only.

    Amendments restate corrected passages (a raised price, an extended period) as
    text and are not parsed here, so the parent's numbers may be superseded: the
    parent gets ``amendments`` (doc ids, links) and the warning ``amended_later``.
    ``parse_status`` is unchanged.  Returns the number of parents with amendments.
    """
    found = {}
    for doc_id, code, parent, submitted in con.execute(
            "SELECT doc_id, doc_type_code, parent_doc_id, submit_datetime FROM filings "
            "WHERE doc_type_code IN ('250', '280', '300') AND withdrawn = 0 AND parent_doc_id IS NOT NULL "
            "ORDER BY submit_datetime, doc_id"):
        found.setdefault(parent, []).append({"doc_id": doc_id, "doc_type_code": code, "submit_datetime": submitted,
                                             "source_url": SOURCE_URL.format(doc_id)})
    linked = 0
    for doc_id, code, record in con.execute("SELECT doc_id, doc_type_code, record FROM tender_offers").fetchall():
        rec = json.loads(record)
        amendments = [a for a in found.get(doc_id, []) if a["doc_type_code"] == AMENDMENT_OF.get(code)]
        warnings = [w for w in rec.get("warnings", []) if w != "amended_later"] + (["amended_later"] if amendments else [])
        linked += bool(amendments)
        if amendments == rec.get("amendments", []) and warnings == rec.get("warnings", []):
            continue
        rec["amendments"], rec["warnings"] = amendments, warnings
        con.execute("UPDATE tender_offers SET record = ? WHERE doc_id = ?", (json.dumps(rec, ensure_ascii=False), doc_id))
    con.commit()
    return linked


def parse_facts(facts, doc_type_code):
    """Compatibility alias following the naming used by other modules."""
    return parse(facts, doc_type_code)


def _read_facts(doc_id):
    path = os.path.join(edinet.CSV_CACHE, doc_id + ".zip")
    if os.path.exists(path):
        with open(path, "rb") as fh:
            return xbrlcsv.read_facts(fh.read())
    return xbrlcsv.read_facts(edinet.download_csv_zip(doc_id))


def _cached(doc_id):
    return os.path.exists(os.path.join(edinet.CSV_CACHE, doc_id + ".zip"))


# A target delisted after its tender offer loses issuers.ticker; its own earlier
# filings still carry the code it traded under.
_TICKER_OF = ("COALESCE({t}.ticker, (SELECT g.ticker FROM filings g WHERE g.edinet_code = {code} "
              "AND g.ticker IS NOT NULL ORDER BY g.submit_datetime DESC LIMIT 1))")
_CANDIDATES = (
    "SELECT f.doc_id, f.doc_type_code, f.parent_doc_id, f.submit_datetime, f.edinet_code, f.filer_name_ja, "
    + _TICKER_OF.format(t="target", code="f.subject_edinet_code") + ", "
    + _TICKER_OF.format(t="filer", code="f.edinet_code") + " "
    "FROM filings f LEFT JOIN issuers target ON target.edinet_code = f.subject_edinet_code "
    "LEFT JOIN issuers filer ON filer.edinet_code = f.edinet_code "
    "WHERE f.doc_type_code IN ({types}) AND f.withdrawn = 0 AND f.has_csv = 1 {pending}"
    "ORDER BY f.submit_datetime DESC")


def candidates(con, pending_only=True):
    """Tender-offer filings with a CSV package (newest first); by default only those not yet fully parsed."""
    types = ",".join("'%s'" % c for c in DOC_TYPES)
    pending = "AND f.doc_id NOT IN (SELECT doc_id FROM tender_offers WHERE parse_status = 'parsed') " if pending_only else ""
    return con.execute(_CANDIDATES.format(types=types, pending=pending)).fetchall()


def coverage(con):
    """Read-only counts per doc type: filings with CSV, stored rows, and CSVs missing from the cache."""
    stored = {}
    if con.execute("SELECT 1 FROM sqlite_master WHERE name = 'tender_offers'").fetchone():
        stored = {(c, s): n for c, s, n in con.execute(
            "SELECT doc_type_code, parse_status, count(*) FROM tender_offers GROUP BY 1, 2")}
    out = {}
    for doc_id, code, *_ in candidates(con, pending_only=False):
        o = out.setdefault(code, {"with_csv": 0, "cached": 0, "not_cached": 0})
        o["with_csv"] += 1
        o["cached" if _cached(doc_id) else "not_cached"] += 1
    for code, o in out.items():
        o["stored_parsed"] = stored.get((code, "parsed"), 0)
        o["stored_partial"] = stored.get((code, "partial"), 0)
    return out


def run(con, limit=None, cached_only=False, reparse=False):
    """Read unprocessed 240/260/270/290 filings. Returns ``(read, fully_parsed)``.

    Cached CSV packages are used first.  The table itself is the resume marker;
    failed downloads are left unmarked so a later run can retry them.
    ``reparse`` re-reads already parsed rows too (use with ``cached_only``).
    Amendment links are refreshed from metadata at the end of every run.
    """
    con.executescript(SCHEMA)
    con.execute("DELETE FROM tender_offers WHERE doc_id IN (SELECT doc_id FROM filings WHERE withdrawn = 1) "
                "OR related_offer_doc_id IN (SELECT doc_id FROM filings WHERE withdrawn = 1 AND doc_type_code = '240')")
    rows = candidates(con, pending_only=not reparse)
    if cached_only:
        rows = [r for r in rows if _cached(r[0])]
    # Prefer local packages without making a cache manifest table in the user's DB.
    rows.sort(key=lambda r: not _cached(r[0]))
    if limit is not None:
        rows = rows[:limit]
    read = parsed_count = 0
    for doc_id, code, parent, submitted, edinet_code, filer, subject_ticker, filer_ticker in rows:
        target_ticker = target_ticker_for(code, subject_ticker, filer_ticker)
        try:
            parsed = parse(_read_facts(doc_id), code)
        except Exception as exc:
            print(doc_id, "FAILED", exc, flush=True)
            continue
        store(con, parsed, doc_id, code, parent, submitted, edinet_code, filer, target_ticker)
        con.commit()
        read += 1
        parsed_count += parsed["parse_status"] == "parsed"
    link_amendments(con)
    return read, parsed_count


def target_ticker_for(code, subject_ticker, filer_ticker):
    """Ticker of the company being bought.

    EDINET's ``subject_edinet_code`` is the *other* party to the filing: the target
    of a 240/270 (filed by the offeror), but the offeror of a 290 (filed by the
    target).  A 290's filer is the target, so its own ticker is the answer; its
    subject code must never be used (S100Y4SF got the offeror's 5471, not 5484).
    """
    return filer_ticker if code == "290" else subject_ticker


def store(con, parsed, doc_id, code, parent, submitted, edinet_code, filer, target_ticker):
    rec = dict(parsed)
    rec.update({
        "doc_id": doc_id,
        "doc_type_code": code,
        "related_offer_doc_id": parent if code in ("260", "270", "290") else doc_id,
        # For 240/270 the DEI security code identifies the offeror.  EDINET's
        # subject_edinet_code identifies the company named in the filing's
        # 対象者名 block, and is resolved to a ticker by run().  For 290 the
        # filer is the target, and the DEI code remains a safe fallback.
        "ticker": target_ticker or (rec.get("target_ticker") if code == "290" else None),
        "edinet_code": edinet_code,
        "filer_name_ja": filer,
        "submit_date": submitted[:10],
        "source_url": SOURCE_URL.format(doc_id),
    })
    con.execute("INSERT OR REPLACE INTO tender_offers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        doc_id, code, rec["related_offer_doc_id"], rec["ticker"], edinet_code, submitted,
        rec["target_name_ja"], rec["offeror_name_ja"], rec["price_jpy"], rec["period_start"], rec["period_end"],
        rec["minimum_shares"], rec["maximum_shares"], rec["tendered_shares"], rec["purchased_shares"],
        rec["parse_status"], rec["source_url"], json.dumps(rec, ensure_ascii=False)))
