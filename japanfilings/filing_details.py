"""Further items from annual securities reports (有価証券報告書, docTypeCode 120), read from the XBRL CSV.

One row per (doc_id, item). Items: segments, sga, director_compensation, main_customers, affiliates, capex,
major_facilities, dividend_policy, diversity. Each row has a status:
  found         the item was read
  partial       some of it was read; `warnings` says what was not
  none_stated   the filing says there is nothing to report (e.g. no customer reaches 10% of sales)
  single_segment  segments only: the filing states one segment, so there is no segment table
  not_found     no tagged value or readable text
Values are what the filing states. A value that cannot be read with certainty is null. Nothing is scored, summed in
place of a stated figure, or carried over from another year.

Text blocks in the CSV have their table cells run together, so `441447` may be two cells. A run is split only when
exactly one reading gives two well-formed numbers; otherwise the item keeps its name and the value is null.

Known limits: affiliate rows and facility names come from run-together cells. Affiliate rows drop what cannot be
separated, so they are incomplete; a facility name can include its subsidiary's name; book values are not read.

Personal data: director compensation holds category totals only. The table of individuals paid 100 million yen or
more is not read. Birth dates and home addresses are never read. Personal names in quotes are masked.

`run` reads only filings already in data/csv_cache unless cached_only=False, which would mean about 6,000 requests.
"""
import json
import os
import time
import re
import unicodedata

from . import edinet, xbrlcsv
from .textutil import parse_number

SCHEMA = """
CREATE TABLE IF NOT EXISTS filing_details (
  doc_id TEXT NOT NULL,
  item TEXT NOT NULL,
  ticker TEXT,
  edinet_code TEXT,
  fiscal_year_end TEXT,
  submit_date TEXT,
  status TEXT NOT NULL,
  source_url TEXT NOT NULL,
  record TEXT NOT NULL,
  PRIMARY KEY (doc_id, item)
);
CREATE INDEX IF NOT EXISTS fd_ticker ON filing_details(ticker, item, fiscal_year_end);
CREATE TABLE IF NOT EXISTS filing_details_read (
  doc_id TEXT PRIMARY KEY,
  status TEXT,
  detail TEXT
);
"""

ITEMS = ("segments", "sga", "director_compensation", "main_customers", "affiliates", "capex", "major_facilities",
         "dividend_policy", "diversity")
SOURCE_URL = "https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{}"
QUOTE_MAX = 199

_WELL_FORMED = re.compile(r"^[1-9]\d{0,2}(?:,\d{3})*$|^0$")
_NONE_WORDS = re.compile(r"該当事項はありません|該当事項なし|記載を省略|記載しておりません|記載はありません|ないため|おりません")
_DASHES = "―—‐−–－"
_UNITS = {"百万円": 1_000_000, "千円": 1_000, "円": 1}


def _norm(text):
    """NFKC, blanks folded, table dashes unified to '-'. A long vowel mark counts as a dash only next to a digit or blank."""
    s = unicodedata.normalize("NFKC", text or "")
    s = re.sub(f"[{_DASHES}]", "-", s)
    s = re.sub(r"(?<=[\d\s])ー(?=[\d\s]|$)", "-", s)
    return re.sub(r"\s+", " ", s).strip()


def _num(v):
    return parse_number(v) if v and v.strip() not in ("－", "-", "―", "") else None


def _block(facts, name, context=None):
    """Text of a block. With no context, the current-year consolidated value is preferred."""
    hits = [f for f in facts if f["element"].endswith(":" + name)]
    if context is not None:
        hits = [f for f in hits if f["context"] == context]
    elif len(hits) > 1:  # a prior-year (or non-consolidated) copy next to the current one must not be joined to it
        current = [f for f in hits if f["context"].startswith("CurrentYear") and "NonConsolidated" not in f["context"]] \
            or [f for f in hits if f["context"].startswith("CurrentYear")]
        hits = current or hits
    return "\n".join(f["value"] for f in hits) or None


def _split_run(run):
    """Every reading of a digit run as two well-formed numbers."""
    out = []
    for i in range(1, len(run)):
        a, b = run[:i], run[i:]
        if _WELL_FORMED.match(a) and _WELL_FORMED.match(b):
            out.append((int(a.replace(",", "")), int(b.replace(",", ""))))
    return out


def scrub(text):
    """Mask personal names in a quote: '山田太郎氏', and a name straight after a representative's title."""
    # The title rule runs first so that the name, not the title's tail, is what the 氏 rule would otherwise cut into.
    text = re.sub(r"(代表(?:取締役|執行役)(?:社長|会長|副社長|専務|常務)?(?:執行役員)?(?:CEO|COO)?)\s*"
                  r"(?!社長|会長|副|兼|及び|および|並びに|以下)(?:(?!氏)[一-龥々]){1,5} ?(?:(?!氏)[一-龥々ぁ-んァ-ヶ]){1,5}(?<![はがのにを])(?=は|が|の|に|を|、|。|氏|$)",
                  r"\1[氏名省略]", text)
    text = re.sub(r"(?<![同当両各])[一-龥々]{2,6}氏(?![名族])", "[氏名省略]氏", text)
    # a one-character surname ('林氏'), but not 摂氏 / 某氏 and the like
    return re.sub(r"(?<![一-龥々ぁ-んァ-ヶ])(?!(?:摂|華|某|他|各|両|同|当|諸|本|該|貴|女|男)氏)[一-龥々]氏(?![名族])", "[氏名省略]氏", text)


def quote(text, limit=QUOTE_MAX):
    """A verbatim excerpt under the limit, cut at a sentence end when one fits. Names masked first."""
    text = scrub(_norm(text))
    if not text:
        return None
    if len(text) <= limit:
        return text
    cut = text.rfind("。", 0, limit)
    return text[:cut + 1] if cut >= limit // 3 else text[:limit - 1] + "…"


# ---- segments ----------------------------------------------------------------------------------------------------

_CTX_SEG = re.compile(r"^CurrentYear(Duration|Instant)(_NonConsolidatedMember)?_((?:(?!_)\S)*?(?:jpcrp\w*?-asr_E\d+-\d+)?\w*Member)$")
_FILER_MEMBER = re.compile(r"^(?:jpcrp\w*-asr_E\d+-\d+)(\w*?)(?:ReportableSegments?|OperatingSegments?|Segments?)?Member$")
_EXTERNAL = re.compile(r"^(RevenuesFromExternalCustomers|(?:Revenues?|Sales|NetSales|OperatingRevenues?|NetSalesAndOperatingRevenues?)"
                       r"(?:From|To)ExternalCustomers\d*IFRS|TransactionsWithExternalCustomersIFRS)$")
_INTERSEG = re.compile(r"^(TransactionsWithOtherSegments|(?:Inter[Ss]egment|Internal)\w*IFRS)$")  # IFRS: Sony IntersegmentSalesIFRS, Toyota IntersegmentOperatingRevenuesIFRS
_SEG_MARKER = re.compile(r"^(DepreciationSegmentInformation|SegmentProfitLossIFRS)$")
# IFRS tables that tag only revenue/profit (no external/intersegment split): counted only for members that are named segments.
_SEG_WEAK = re.compile(r"^(Revenue\d*IFRS|OperatingRevenues?\d*IFRS|NetSalesIFRS|OperatingProfitLossIFRS|ProfitLossBeforeTaxIFRS"
                       r"|ProfitLossAttributableToOwnersOfParentIFRS|GrossProfitIFRS)$")
_SALES_NAME = re.compile(r"^(NetSales|Revenue\d*IFRS|Revenue\d*|OperatingRevenue\d*|TotalRevenues?\w*|OrdinaryIncomeBNK|OrdinaryIncomeINS"
                         r"|NetSalesOfCompletedConstructionContracts\w*"
                         r"|NetSalesIFRS|SalesRevenuesIFRS|OperatingRevenues?(?:\d*)IFRS|NetSalesAndOperatingRevenues?IFRS|SegmentTotalRevenueIFRS"
                         r"|OperatingRevenueRevenue\d*IFRS)$")
_SALES_LABEL = re.compile(r"売上|収益|営業総収入|完成工事高|経常収入")
_SALES_NOT = re.compile(r"外部|内部|振替|受取|利息|営業外|持分|金融収益|その他の収益|原価|費用|利益|損失|配当|External|Intersegment")
_PROFIT_ORDER = [
    re.compile(r"^SegmentProfitLoss\w*$"), re.compile(r"^(OperatingIncome|OperatingProfitLossIFRS|OperatingProfitLoss\d*)$"),
    re.compile(r"^(BusinessProfitLoss|BusinessProfit|SegmentIncomeLoss)IFRS$"),
    re.compile(r"^(OrdinaryIncome|OrdinaryProfit\w*)$"), re.compile(r"^(ProfitLossBeforeTax\w*|IncomeBeforeIncomeTaxes\w*)$"),
]
_PROFIT_LABEL = re.compile(r"セグメント(?:利益|損失|損益)|営業利益|営業損失|事業利益|経常利益|経常損失|税引前|利益|損益")
_PROFIT_NOT = re.compile(r"減損|持分|為替|売却|総利益|評価|受取|営業外|負ののれん")
_ASSET_NAME = re.compile(r"^(Assets|AssetsIFRS|SegmentAssets\w*)$")


_SMALL_WORDS = {"And", "Of", "For", "The", "In", "Or", "To"}


# Member IDs that are numbering, not a name ("Element001", "EREMENT02", "Test3", "Segment2"): no English name is read from them.
_PLACEHOLDER_ID = re.compile(r"(?i)^(?:e[lr]ement|test|segment|seg|member|item|no|number|sample)_?\d+$|^\d+$")


def _member_name(member):
    m = _FILER_MEMBER.match(member)
    if not m or not m.group(1) or _PLACEHOLDER_ID.match(m.group(1)):
        return None
    # Split camel case, but keep tokens such as D2C, B2B and Web3 whole.
    words = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z0-9])(?=[A-Z][a-z])", " ", m.group(1)).split()
    if len(words) > 1 and words[-1] == "IFRS":
        words.pop()  # some filers put the standard into the member name
    return " ".join(w.lower() if i and w in _SMALL_WORDS else w for i, w in enumerate(words))


# ---- English segment names ---------------------------------------------------------------------------------------
# Order of sources for name_en (recorded in name_en_source):
#   filer_label  the filer's English label for the member (label linkbase *_lab-en.xml of the full XBRL package)
#   member_id    words of the filer's (or the standard taxonomy's) member element ID, e.g. AutomotiveReportableSegmentMember
#   fallback     the Japanese name translated term by term with _JA_EN; used only when every part of the name is a known term
#   None         no English name; the row keeps name_ja only
# The CSV package (API type=5) that `run` reads has Japanese labels only, so filer_label needs en_labels from
# english_labels() on a type=1 package; nothing here downloads one.

_LABEL_ROLES = ("http://www.xbrl.org/2003/role/label", "http://www.xbrl.org/2003/role/terseLabel",
                "http://www.xbrl.org/2003/role/verboseLabel")


def english_labels(zip_bytes):
    """{member as written in context IDs (prefix + local name): English label} from the *_lab-en.xml files of an
    XBRL package. The standard label role is preferred over terse and verbose labels."""
    import io
    import xml.etree.ElementTree as ET
    import zipfile
    xl, link = "{http://www.w3.org/1999/xlink}", "{http://www.xbrl.org/2003/linkbase}"
    out = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for name in z.namelist():
            if not name.endswith("_lab-en.xml"):
                continue
            root = ET.fromstring(z.read(name))
            for lnk in root.iter(link + "labelLink"):
                locs = {}
                for loc in lnk.iter(link + "loc"):
                    frag = (loc.get(xl + "href") or "").rpartition("#")[2]
                    prefix, _, local = frag.rpartition("_")
                    if local.endswith("Member") and prefix:
                        locs[loc.get(xl + "label")] = prefix + local
                labels = {}
                for lab in lnk.iter(link + "label"):
                    lang = lab.get("{http://www.w3.org/XML/1998/namespace}lang") or ""
                    if lang.lower().startswith("en") and (lab.text or "").strip():
                        labels.setdefault(lab.get(xl + "label"), []).append((lab.get(xl + "role"), lab.text.strip()))
                for arc in lnk.iter(link + "labelArc"):
                    member = locs.get(arc.get(xl + "from"))
                    found = labels.get(arc.get(xl + "to"))
                    if not member or not found:
                        continue
                    rank = {r: i for i, r in enumerate(_LABEL_ROLES)}
                    best = min(found, key=lambda x: rank.get(x[0], len(rank)))
                    if member not in out or rank.get(best[0], 9) < out[member][0]:
                        out[member] = (rank.get(best[0], 9), best[1])
    return {k: v for k, (_r, v) in out.items()}


# Frequent terms in segment names. Longest match wins. A name is translated only when nothing is left over.
_JA_EN = {
    "事業部": "Division", "事業": "Business", "セグメント": "Segment", "部門": "Division", "その他": "Other", "関連": "Related",
    "金融": "Financial Services", "不動産": "Real Estate", "自動車": "Automotive", "建設": "Construction", "建築": "Building",
    "土木": "Civil Engineering", "住宅": "Housing", "情報通信": "Information and Communications", "情報": "Information",
    "通信": "Communications", "小売": "Retail", "卸売": "Wholesale", "製造": "Manufacturing", "物流": "Logistics",
    "運輸": "Transportation", "化学": "Chemicals", "医薬品": "Pharmaceuticals", "医薬": "Pharmaceuticals", "食品": "Food",
    "飲食": "Restaurants", "外食": "Restaurants", "電子": "Electronics", "機械": "Machinery", "エネルギー": "Energy",
    "再生可能エネルギー": "Renewable Energy", "環境": "Environment", "介護": "Nursing Care", "医療": "Medical", "教育": "Education",
    "人材": "Human Resources", "広告": "Advertising", "保険": "Insurance", "銀行": "Banking", "証券": "Securities",
    "リース": "Leasing", "賃貸": "Rental", "ホテル": "Hotels", "レジャー": "Leisure", "素材": "Materials", "鉄鋼": "Steel",
    "電力": "Electric Power", "ガス": "Gas", "半導体": "Semiconductors", "サービス": "Services", "ソリューション": "Solutions",
    "システム": "Systems", "ソフトウェア": "Software", "メディア": "Media", "エンターテインメント": "Entertainment",
    "ヘルスケア": "Healthcare", "農業": "Agriculture", "水産": "Fisheries", "繊維": "Textiles", "紙": "Paper",
    "日本": "Japan", "米国": "United States", "北米": "North America", "欧州": "Europe", "アジア": "Asia", "中国": "China",
    "海外": "Overseas", "国内": "Domestic", "流通": "Distribution", "開発": "Development", "セキュリティ": "Security",
    "インダストリアル・プロダクツ": "Industrial Products", "インダストリアル": "Industrial", "プロダクツ": "Products", "製品": "Products", "サステナビリティ": "Sustainability",
    "ライフサイエンス": "Life Sciences", "リースバック": "Leaseback",
}
_JA_SEP = {"・": " & ", "、": ", ", "及び": " and ", "および": " and ", "並びに": " and "}


def _fallback_name_en(name_ja):
    """The Japanese name term by term, or None when any part is not a known term (nothing is guessed)."""
    s = _norm(name_ja or "")
    if not s:
        return None
    parts, i = [], 0
    keys = sorted(list(_JA_EN) + list(_JA_SEP), key=len, reverse=True)
    while i < len(s):
        ascii_run = re.match(r"[A-Za-z][A-Za-z0-9&\-]*", s[i:])
        if ascii_run:  # Latin letters in a Japanese name are already the filer's own (e.g. RV事業, IT)
            parts.append(("word", ascii_run.group(0)))
            i += ascii_run.end()
            continue
        if s[i] == " ":
            i += 1
            continue
        key = next((k for k in keys if s.startswith(k, i)), None)
        if key is None:
            return None
        parts.append(("sep", _JA_SEP[key]) if key in _JA_SEP else ("word", _JA_EN[key]))
        i += len(key)
    if not parts or parts[0][0] == "sep" or parts[-1][0] == "sep" or not any(k == "word" for k, _ in parts):
        return None
    out = ""
    for (kind, text), prev in zip(parts, [None] + parts[:-1]):
        out += text if kind == "sep" or prev is None or prev[0] == "sep" else " " + text
    return out


def _name_en(member, name_ja, en_labels, generic_other):
    """(name_en, name_en_source)."""
    label = (en_labels or {}).get(member)
    if label:
        return label, "filer_label"
    name = _member_name(member) or ("Other" if generic_other else None)
    if name:
        return name, "member_id"
    name = _fallback_name_en(name_ja)
    return (name, "fallback") if name else (None, None)


# Members that carry reconciling amounts, not a business: they go to "adjustments" (and never get a name).
_ADJ_CORE = re.compile(r"^(?:Adjustments?(?:And(?:Eliminations?|Others?))?|Eliminations?(?:And(?:Adjustments?|Corporate\w*))?|ReconcilingItems?"
                       r"|Unallocated\w*|Corporate(?:Expenses|Assets|Common|Shared)?And(?:Eliminations?|Adjustments?|Others?)"
                       r"|CorporateExpenses|GroupTotal\w*|(?:TransferTo)?DiscontinuedOperations)$")
# Also filer members that name the total column: S100YDV3 (8025) "ReportableSegmentsReportableSegmentsMember",
# S100Z2TG (4073) "CONSOLDATEDTOTALORCOMPANYWIDETOTALMember" (sic).
_TOTAL_CORE = re.compile(r"^(?:Total(?:Of(?:Reportable)?Segments?(?:AndOthers?)?)?|TotalReportable|ReportableSegments?"
                         r"|(?i:consol?i?dated_?total(?:_?or_?company_?wide_?total)?|company_?wide_?total))$")
_OTHER_GENERIC = {"OperatingSegmentsNotIncludedInReportableSegmentsAndOtherRevenueGeneratingBusinessActivitiesMember",
                  "OtherReportableSegmentsMember"}
_TOTAL_GENERIC = {"ReportableSegmentsMember"}
_ADJ_GENERIC = {"ReconcilingItemsMember", "UnallocatedAmountsAndEliminationMember", "AdjustmentsMember",
                "EliminationsMember", "ReconcilingItemsAndEliminationsMember", "AdjustmentsAndEliminationsMember"}


def _member_role(member):
    """'segment' | 'other' | 'total' | 'adjustment' | None (not a segment-table member)."""
    if member in _OTHER_GENERIC:
        return "other"
    if member in _TOTAL_GENERIC or member == "TotalOfReportableSegmentsAndOthersMember":
        return "total"
    if member in _ADJ_GENERIC:
        return "adjustment"
    if not member.startswith("jpcrp"):
        return None
    m = re.match(r"^jpcrp\w*-asr_E\d+-\d+(\w*?)(?:ReportableSegments?|OperatingSegments?|Segments?)?Member$", member)
    core = m.group(1) if m else ""
    if _ADJ_CORE.match(core):
        return "adjustment"
    if _TOTAL_CORE.match(core):
        return "total"
    return "segment"


def _segment_names_ja(facts, members):
    """Japanese names from the employee table: the text just before each segment's tagged employee count."""
    text = _norm(_block(facts, "InformationAboutEmployeesTextBlock") or "")
    start = text.find("セグメントの名称")
    if start < 0:
        return {}
    text = text[start:]
    names = {}
    counts = []
    for f in facts:
        if not f["element"].endswith(":NumberOfEmployees"):
            continue
        m = re.match(r"^CurrentYearInstant_(\S+Member)$", f["context"])
        n = _num(f["value"])
        if not m or m.group(1) not in members or n is None:
            continue
        counts.append((m.group(1), n))
    counts = sorted(set(counts))
    shared = {n for _m, n in counts if sum(1 for _x, k in counts if k == n) > 1}  # the same headcount cannot tell segments apart
    for member, n in counts:
        if n in shared:
            continue
        found = re.search(r"([^\d(),]{1,40}?)\s*" + f"{int(n):,}" + r"(?![\d,])", text)
        if found:
            name = re.sub(r"^.*(?:従業員数\s*(?:[(（〔\[][^)）〕\]]*[)）〕\]]|[人名])?|[)）\]〕】］])", "", found.group(1)).strip(" ・:〕〔]")
            name = re.sub(r"^報告セグメント(?=.)", "", name)  # a header cell glued to the first row
            name = name.strip()
            if re.search(r"従業員|名称|組合|であり|。|事業(?!及び|および|並びに|・|、|等|-|部).*事業", name) or len(name) < 2 or re.fullmatch(r"[\W_]+", name):
                continue  # sentence or header text, not a segment name
            if name and name not in ("合計", "計", "全社") and not re.search(r"現在|単位", name):
                names[member] = name
    return names


def _swap_names_by_note(facts, rows):
    """Swap the Japanese names of two reportable rows when the segment note says they belong the other way round.
    The employee table names come through the headcount tags, and a filer can put those on the wrong members:
    S100YHHI (7745) tags 計測・計量機器事業's 1,158 people on the medical member, so the two businesses' names came out
    swapped. Notes that print one table per segment (name, then that segment's figures) show it: the stretch after
    each name, up to the next name, holds that segment's sales figure. A pair is swapped only when each row's figure
    is found after the other's name and not after its own; tables with all names in one header row give no evidence
    (the stretch between two names holds no figures), so nothing changes there. Returns the swapped pairs."""
    text = _norm(_block(facts, "NotesSegmentInformationEtcConsolidatedFinancialStatementsTextBlock")
                 or _block(facts, "NotesSegmentInformationEtcFinancialStatementsTextBlock") or "")
    cur = next((m.start() for m in re.finditer(r"当(?:連結会計|事業)年度", text) if "外部顧客" in text[m.start():m.start() + 600]), -1)
    if cur < 0:
        return []
    text = text[cur:]
    text = text[:text.find("【")] if "【" in text else text
    unit = 1_000_000 if "百万円" in text[:200] else 1_000 if "千円" in text[:200] else None
    named = [r for r in rows if r["kind"] == "reportable" and r.get("name_ja") and r.get("sales_jpy")]
    if unit is None or len({r["name_ja"] for r in named}) < len(named) or any(r["sales_jpy"] % unit for r in named):
        return []
    starts = {r["name_ja"]: text.find(r["name_ja"]) for r in named}
    if min(starts.values(), default=-1) < 0 or any(a != b and a in b for a in starts for b in starts):
        return []
    cuts = sorted(starts.values()) + [len(text)]
    window = {n: text[p:cuts[cuts.index(p) + 1]] for n, p in starts.items()}

    def figure(r):
        return f"{int(r['sales_jpy'] // unit):,}"
    swapped = []
    for i, a in enumerate(named):
        for b in named[i + 1:]:
            fa, fb, wa, wb = figure(a), figure(b), window[a["name_ja"]], window[b["name_ja"]]
            if fa in wb and fb in wa and fa not in wa and fb not in wb:
                a["name_ja"], b["name_ja"] = b["name_ja"], a["name_ja"]
                swapped.append((a["member"], b["member"]))
    return swapped


def _is_marker(element):
    return bool(_SEG_MARKER.match(element) or _EXTERNAL.match(element) or _INTERSEG.match(element))


_REVENUE_CONSOLIDATED = re.compile(
    r"^(?:(?:Revenues?|Sales|NetSales|OperatingRevenues?|NetSalesAndOperatingRevenues?)\d*(?:IFRS)?(?:SummaryOfBusinessResults|KeyFinancialData)?"
    r"|(?:Revenues?|Sales|NetSales|OperatingRevenues?|NetSalesAndOperatingRevenues?)(?:From|To)ExternalCustomers\d*IFRS"
    r"|TotalNetRevenuesIFRS|SalesRevenuesIFRS|NetSalesIFRS)$")


def _consolidated_revenue(facts):
    """Current-year consolidated revenue candidates (no dimension), statement values first."""
    out = []
    for f in facts:
        if f["context"] != "CurrentYearDuration":
            continue
        e = f["element"].split(":")[1]
        v = _num(f["value"])
        if v is not None and _REVENUE_CONSOLIDATED.match(e) and v not in out:
            out.append(v)
    return out


def _sum_values(rows, key):
    vals = [r.get(key) for r in rows]
    return None if not vals or any(v is None for v in vals) else sum(vals)


# Rows some filers tag like segments: totals, adjustments, eliminations. Whole-name match only, so names such as
# "Total Pack Produce" stay.
_NOT_SEGMENT = re.compile(r"(?i)^(?:小計|合計|調整額|消去|total segments?|total|subtotal|adjustments?(?: amount)?|eliminations?(?: of inter ?segment transactions)?"
                          r"|reportable segments?(?: total)?|consol?i?dated ?total(?: ?or ?company[- ]?wide ?total)?|company[- ]?wide ?total)$")


def _not_a_segment(row):
    names = [n.strip() for n in (row.get("name_ja"), row.get("name_en")) if n]
    return any(_NOT_SEGMENT.match(n) for n in names) or any(n.startswith("小計") for n in names if row.get("name_ja") == n)


# Some filers tag a group subtotal as a reportable segment next to the segments it adds up (S100YHRI 7087
# 「人財系フィールド」= 製造請負・派遣事業 + 技術者派遣事業). Such a row is kept out of `segments` and listed in
# `segment_groups`. A row is a subtotal only when it equals the sum of a block of >= 2 rows directly before or
# after it, with no metric that both sides state disagreeing. Tolerance: one unit of the filer's precision (1 yen /
# 1,000 / 1,000,000, the coarsest unit that divides every value) per added row, which is the most that rounding each
# figure can account for. A metric counts as agreeing only when every member states it, the total is at least
# _MIN_SIZE tolerances (tiny yen figures that happen to be multiples of 1,000 prove nothing), and each member is
# non-zero on some agreeing metric. Rows with no figure at all are left out of a block (S100YHHI 7745: the America
# and Europe semiconductor rows are empty), but >= 2 rows with figures must remain. Evidence needed: >= 3 agreeing
# metrics, or 2 that are not one figure twice (sales and external sales differ), or 2 under a grouping name (計,
# フィールド, Total...). Checked on all 3,830 cached annual reports: the 21 subtotals in 17 filings all pass, plus
# S100YHHI 半導体関連事業; nothing else is flagged.
_GROUP_KEYS = ("sales_jpy", "external_sales_jpy", "profit_jpy", "assets_jpy")
_MIN_SIZE = 100
_GROUP_HINT = re.compile(r"(?i)計$|合計|小計|フィールド|事業本部|グループ|部門|total|group|field|division")


def _precision_unit(rows):
    vals = [abs(r[k]) for r in rows for k in _GROUP_KEYS if r.get(k)]
    for unit in (1_000_000, 1_000):
        if vals and all(v % unit == 0 for v in vals):
            return unit
    return 1


def _is_subtotal(row, block, unit):
    """The rows of `block` that `row` adds up (rows without any figure dropped), or None."""
    block = [b for b in block if any(b.get(k) for k in _GROUP_KEYS)]
    if len(block) < 2:
        return None
    tol = len(block) * unit
    agree = []
    for key in _GROUP_KEYS:
        total = row.get(key)
        if total is None:
            continue
        parts = [b.get(key) for b in block]
        stated = [p for p in parts if p is not None]
        if len(stated) < len(parts):
            # a member does not state it: no evidence, but non-negative members already above the total contradict it
            if total >= 0 and all(p >= 0 for p in stated) and sum(stated) > total + tol:
                return None
            continue
        if abs(total - sum(parts)) > tol:
            return None  # a stated figure that does not add up rules the row out
        if abs(total) >= _MIN_SIZE * tol:
            agree.append(key)
    # every member must add something to an agreeing metric, so a zero row cannot join a block or make a duplicate
    # look like a sum
    if not agree or not all(any(b.get(k) for k in agree) for b in block):
        return None
    names = [n for n in (row.get("name_ja"), row.get("name_en")) if n]
    if len(agree) >= 3 or len(agree) == 2 and (len({row[k] for k in agree}) == 2 or any(_GROUP_HINT.search(n) for n in names)):
        return block
    return None


def _split_groups(rows):
    """(segments, segment_groups): reportable subtotal rows moved out of the segment list."""
    cand = [r for r in rows if r["kind"] == "reportable" and not _not_a_segment(r)]
    unit = _precision_unit(cand)
    groups = []
    for i, row in enumerate(cand):
        # runs of consecutive rows on one side of the row; nearest first, then shortest. The run need not touch the
        # row: in S100YC72 (5332) セラミック事業 sits between the five regions and グローバル住設事業計.
        runs = [(a, b) for a in range(len(cand)) for b in range(a + 2, len(cand) + 1) if b <= i or a > i]
        runs.sort(key=lambda r: (r[0] - i - 1 if r[0] > i else i - r[1], r[1] - r[0]))
        block = next((blk for a, b in runs if (blk := _is_subtotal(row, cand[a:b], unit))), None)
        if block:
            groups.append((row, block))
    if not groups:
        return rows, []
    sub = {id(g) for g, _b in groups}
    out = [{k: v for k, v in g.items() if k != "kind"} | {
        "members": [b.get("name_ja") or b.get("name_en") or b["member"] for b in block],
        "member_ids": [b["member"] for b in block]} for g, block in groups]
    return [r for r in rows if id(r) not in sub], out


def segments(facts, en_labels=None):
    """en_labels: optional {member: English label} from english_labels(); see name_en_source."""
    rows, totals = {}, {}
    for f in facts:
        m = _CTX_SEG.match(f["context"])
        if not m or m.group(3).count("Member") != 1:
            continue
        rows.setdefault((bool(m.group(2)), m.group(3)), []).append((f["element"].split(":")[1], f["label"], _num(f["value"]), m.group(1)))
    marked = {k: v for k, v in rows.items() if any(_is_marker(e) or (_SEG_WEAK.match(e) and _member_role(k[1]) is not None
                                                                   and (k[1].startswith("jpcrp") is False or "Segment" in k[1])) for e, *_ in v)}
    basis_nc = not any(not nc for nc, _ in marked)
    out = []
    adjustments = []
    warnings = []
    roles = {member: _member_role(member) for nc, member in marked if nc == basis_nc}
    names_ja = _segment_names_ja(facts, {m for m, r in roles.items() if r in ("segment", "other")}) if marked else {}
    for (nc, member), items in marked.items():
        if nc != basis_nc:
            continue
        role = roles[member]
        if role == "total":
            if member in _TOTAL_GENERIC or not totals:
                totals = items
            continue
        if role == "adjustment":
            adjustments.append(_segment_values(items))
            continue
        if role is None:
            continue  # other reconciling or unknown generic members
        filer = role == "segment"
        name_en, source = _name_en(member, names_ja.get(member), en_labels, generic_other=not filer)
        out.append({"member": member, "name_en": name_en, "name_en_source": source,
                    "name_ja": names_ja.get(member), "kind": "reportable" if filer else "other", **_segment_values(items)})
    for pair in _swap_names_by_note(facts, out):
        for r in out:
            if r["member"] in pair and r["name_en_source"] == "fallback":
                r["name_en"], r["name_en_source"] = _name_en(r["member"], r["name_ja"], en_labels, generic_other=False)
        warnings.append("segment_names_ja_swapped_by_note")
    single = _block(facts, "DescriptionOfFactThatCompanysBusinessComprisesSingleSegment")
    if not out:
        text = _norm(_block(facts, "NotesSegmentInformationEtcConsolidatedFinancialStatementsTextBlock")
                     or _block(facts, "NotesSegmentInformationEtcFinancialStatementsTextBlock")
                     or _block(facts, "NotesSegmentInformationConsolidatedFinancialStatementsIFRSTextBlock") or "")  # IFRS filers' note
        if single or re.search(r"単一(?:の)?(?:報告|事業)?セグメント|報告セグメントが単一|報告セグメントは[^。]{0,14}単一|区分すべき事業セグメントが存在しない|単一事業|複数の事業セグメントを有して", text[:600]):
            return "single_segment", {"segments": [], "single_segment": True, "warnings": []}
        # US GAAP filers tag no segment facts, and the CSV cuts their notes text block at 30,000 characters, before
        # the segment note (checked on all six cached: 7751, 6301, 8591, 6645, 8604, 4901).
        us_gaap = _block(facts, "NotesToConsolidatedFinancialStatementsUSGAAPTextBlock") is not None
        return "not_found", {"segments": [], "single_segment": False,
                             "warnings": ["us_gaap_segment_note_not_in_csv"] if us_gaap else []}
    out, groups = _split_groups(out)
    total = _segment_values(totals) if totals else {}
    reportable = [r for r in out if r["kind"] == "reportable"]
    check = None
    for key in ("sales_jpy", "external_sales_jpy"):
        vals = [r[key] for r in reportable]
        if total.get(key) is not None and all(v is not None for v in vals):
            check = "match" if abs(sum(vals) - total[key]) <= max(len(vals) * 1_000_000, total[key] * 0.001) else "mismatch"
            others = [r[key] for r in out if r["kind"] == "other"]
            if check == "mismatch" and others and all(v is not None for v in others) \
                    and abs(sum(vals) + sum(others) - total[key]) <= max(len(out) * 1_000_000, total[key] * 0.001):
                check = "match"  # some filers' "reportable segments" total already includes the Other column
            break
    if check == "mismatch":
        warnings.append("segment_sum_differs_from_tagged_total")
    adj = None
    if adjustments:
        adj = {k: (sum(a[k] for a in adjustments if a.get(k) is not None) if any(a.get(k) is not None for a in adjustments) else None)
               for k in ("sales_jpy", "external_sales_jpy", "intersegment_sales_jpy", "profit_jpy", "assets_jpy")}
    # Segment external sales plus the reconciling items should equal consolidated revenue.
    rev_check, revenue = None, _consolidated_revenue(facts)
    if not basis_nc and revenue:
        for key in ("external_sales_jpy", "sales_jpy"):
            part = _sum_values(out, key)
            if part is None:
                continue
            part += (adj or {}).get(key) or 0
            rev_check = "match" if any(abs(part - r) <= max(len(out) * 1_000_000, abs(r) * 0.001) for r in revenue) else "mismatch"
            if rev_check == "match":
                break
        if rev_check == "mismatch":
            warnings.append("segment_revenue_differs_from_consolidated")
    return ("found" if any(r["sales_jpy"] is not None or r["external_sales_jpy"] is not None for r in reportable) else "partial"), {
        "basis": "non_consolidated" if basis_nc else "consolidated", "segments": [x for x in out if not _not_a_segment(x)], "segment_groups": groups, "single_segment": False,
        "reportable_total": total or None, "sum_check": check, "adjustments": adj,
        "consolidated_revenue_jpy": revenue[0] if revenue else None, "revenue_check": rev_check, "warnings": warnings}


def _segment_values(items):
    sales = next((v for e, _l, v, _p in items if _SALES_NAME.match(e) and v is not None), None)
    if sales is None:
        sales = next((v for e, lab, v, _p in items if v is not None and _p == "Duration" and _SALES_LABEL.search(lab)
                      and not _SALES_NOT.search(lab + e)), None)
    external = next((v for e, _l, v, _p in items if _EXTERNAL.match(e) and v is not None), None)
    inter = next((v for e, _l, v, _p in items if _INTERSEG.match(e) and v is not None), None)
    profit = label = None
    for rx in _PROFIT_ORDER:
        hit = next(((v, lab) for e, lab, v, _p in items if rx.match(e) and v is not None and not _PROFIT_NOT.search(lab)), None)
        if hit:
            profit, label = hit
            break
    if profit is None:
        hit = next(((v, lab) for _e, lab, v, _p in items if v is not None and _p == "Duration" and _PROFIT_LABEL.search(lab)
                    and not _PROFIT_NOT.search(lab)), None)
        if hit:
            profit, label = hit
    assets = next((v for e, _l, v, _p in items if _ASSET_NAME.match(e) and v is not None), None)
    if assets is None:
        assets = next((v for _e, lab, v, _p in items if v is not None and _p == "Instant" and re.fullmatch(r"(?:セグメント)?資産(?:合計)?", lab)), None)
    return {"sales_jpy": sales, "external_sales_jpy": external, "intersegment_sales_jpy": inter, "profit_jpy": profit, "profit_label_ja": label, "assets_jpy": assets}


# ---- SG&A --------------------------------------------------------------------------------------------------------

_SGA_TAG = re.compile(r"(SGA|SellingGeneralAndAdministrativeExpenses)(IFRS)?$")
_SGA_TOTAL = re.compile(r"^(TotalSellingGeneralAndAdministrativeExpenses\w*|SellingGeneralAndAdministrativeExpenses\w*)$")


def parse_sga_text(text):
    """[{item_ja, current_jpy, prior_jpy, ...}] and warnings. Only tables with two columns, prior year first, are read."""
    s = _norm(text)
    warnings = []
    m = re.search(r"(?:当|前)(?:連結)?(?:事業年度|会計年度|会計期間)", s)
    headers = list(re.finditer(r"(前|当)(?:連結)?(?:事業年度|会計年度|会計期間)", s))
    if len(headers) < 2 or not m:
        return [], ["column_headers_not_found"]
    prior_first = headers[0].group(1) == "前"
    # Rows start after the last header's closing bracket.
    close = s.rfind(")", 0, headers[1].end() + 60)
    body = s[max(close + 1, headers[1].end()):]
    body = re.sub(r"^[\s)]*", "", body)
    if headers and re.match(r"\s*\(?自", s[headers[-1].end():headers[-1].end() + 3]) is None and close < headers[-1].end():
        body = s[headers[-1].end():]
    stop = re.search(r"おおよその割合|販売費(?:に属する|\s*\d)|\(注\)|※\s*\d", body)
    if stop:
        body = body[:stop.start()]
    unit_default = next((v for k, v in _UNITS.items() if re.search(rf"単位\s*[::]?\s*{k}", s)), None)
    rows = []
    last_unit = unit_default
    # A row: label, then cells up to the next label.
    for row in re.finditer(r"([^\d△▲\s-][^\d△▲]*?)\s*((?:[△▲-]?\s*\d[\d,]*\s*(?:百万円|千円|円|〃|\"|”|″|同)?\s*|-\s*)+)", body):
        label = row.group(1).strip(" ・,、")
        if not label or len(label) > 40:
            continue
        cells, cell_ok = _sga_cells(row.group(2))
        if not cell_ok:
            rows.append({"item_ja": label, "current_jpy": None, "prior_jpy": None, "stated_unit": None})
            warnings.append("ambiguous_cell:" + label)
            continue
        vals, unit = cells, next((c[1] for c in cells if c[1] in _UNITS), None)
        last_unit = unit or last_unit
        if len(vals) != 2:
            rows.append({"item_ja": label, "current_jpy": None, "prior_jpy": None, "stated_unit": None})
            warnings.append("not_two_columns:" + label)
            continue
        scale = _UNITS.get(last_unit)
        a, b = vals[0][0], vals[1][0]
        if scale is None:
            rows.append({"item_ja": label, "current_jpy": None, "prior_jpy": None, "stated_unit": None})
            warnings.append("unit_not_found:" + label)
            continue
        prior, current = (a, b) if prior_first else (b, a)
        rows.append({"item_ja": label, "current_jpy": None if current is None else current * scale,
                     "prior_jpy": None if prior is None else prior * scale, "stated_unit": last_unit})
    return rows, warnings


def _sga_cells(run):
    """[(value, unit)] for a row's cells, or ([], False) when a jammed digit run has more than one reading."""
    out = []
    for part in re.finditer(r"([△▲]?)\s*(\d[\d,]*)\s*(百万円|千円|円|〃|\"|”|″|同)?|(-)", run):
        if part.group(4):
            out.append((None, None))
            continue
        sign = -1 if part.group(1) else 1
        digits = part.group(2)
        if _WELL_FORMED.match(digits):
            out.append((sign * int(digits.replace(",", "")), part.group(3)))
            continue
        pairs = _split_run(digits)
        if len(pairs) != 1:
            return [], False
        out.append((sign * pairs[0][0], None))
        out.append((pairs[0][1], part.group(3)))
    return out, True


def sga(facts):
    """Tagged note facts when a filing has them: exact values, no cell splitting. Otherwise the note's table."""
    tagged, prior = {}, {}
    for f in facts:
        name = f["element"].split(":")[1]
        if not (_SGA_TAG.search(name) and not _SGA_TOTAL.match(name) and f["element"].startswith(("jppfs_cor:", "jpigp_cor:", "jpcrp030000"))):
            continue
        v = _num(f["value"])
        if v is None:
            continue
        if f["context"] in ("CurrentYearDuration", "CurrentYearDuration_NonConsolidatedMember"):
            tagged.setdefault(f["context"], {}).setdefault(name, {"item_ja": f["label"].split("、")[0], "element": name, "current_jpy": v})
        elif f["context"] in ("Prior1YearDuration", "Prior1YearDuration_NonConsolidatedMember"):
            prior.setdefault(f["context"].replace("Prior1Year", "CurrentYear"), {}).setdefault(name, v)
    for ctx, basis in (("CurrentYearDuration", "consolidated"), ("CurrentYearDuration_NonConsolidatedMember", "non_consolidated")):
        if len(tagged.get(ctx, {})) >= 2:
            items = [{**r, "prior_jpy": prior.get(ctx, {}).get(r["element"])} for r in tagged[ctx].values()]
            return "found", {"basis": basis, "method": "tagged", "items": items, "warnings": []}
        text = _block(facts, "MajorComponentsOfSellingGeneralAndAdministrativeExpensesTextBlock", ctx)
        if text:
            rows, warnings = parse_sga_text(text)
            readable = [r for r in rows if r["current_jpy"] is not None]
            if readable:
                return ("partial" if warnings else "found"), {"basis": basis, "method": "text", "items": rows, "warnings": warnings}
            return "partial", {"basis": basis, "method": "text", "items": rows, "warnings": warnings or ["no_rows_read"]}
    return "not_found", {"items": [], "warnings": []}


# ---- director compensation ----------------------------------------------------------------------------------------

_COMP_EL = re.compile(r"(RemunerationEtc|Remuneration)ByCategoryOfDirectorsAndOtherOfficers$")
_COMP_CTX = re.compile(r"^CurrentYearDuration_(\w+Member)$")
_COMP_CATEGORY_JA = {
    "DirectorsExcludingOutsideDirectorsMember": "取締役(社外取締役を除く)",
    "CorporateAuditorsExcludingOutsideCorporateAuditorsMember": "監査役(社外監査役を除く)",
    "OutsideDirectorsAndOtherOfficersMember": "社外役員",
    "DirectorsAppointedAsAuditAndSupervisoryCommitteeMembersExcludingOutsideDirectorsMember": "監査等委員である取締役(社外取締役を除く)",
    "DirectorsExcludingAuditAndSupervisoryCommitteeMembersAndOutsideDirectorsMember": "取締役(監査等委員及び社外取締役を除く)",
    "ExecutiveOfficersMember": "執行役",
    "OutsideDirectorsMember": "社外取締役",
    "OutsideCorporateAuditorsMember": "社外監査役",
    "DirectorsExcludingAuditCommitteeMembersAndOutsideDirectorsMember": "取締役(監査委員及び社外取締役を除く)",
}


COMP_PER_OFFICER_MAX_JPY = 3_000_000_000  # no listed company pays an average officer in a category more than this
COMP_PER_OFFICER_MIN_JPY = 100_000


def compensation_warnings(rows):
    """Category averages no real pay table has mean the filer tagged the wrong unit (千円 or 円 as 円 or 百万円).
    The figure stays as filed; the warning tells readers not to trust the scale (S100XVK0, S100YTBE high; S100YLZQ low)."""
    out = []
    for r in rows:
        total, n = r.get("total_jpy"), r.get("officers")
        if not total or not n:
            continue
        avg = total / n
        if avg > COMP_PER_OFFICER_MAX_JPY:
            out.append("scale_suspect_high:" + r["category_member"])
        elif avg < COMP_PER_OFFICER_MIN_JPY:
            out.append("scale_suspect_low:" + r["category_member"])
    return out


def director_compensation(facts):
    """Category totals only. Facts of the per-person table use other elements and are never matched here."""
    cats = {}
    for f in facts:
        name = f["element"].split(":")[1]
        m = _COMP_CTX.match(f["context"])
        if not m or not _COMP_EL.search(name):
            continue
        c = cats.setdefault(m.group(1), {"category_member": m.group(1), "category_ja": _COMP_CATEGORY_JA.get(m.group(1)),
                                         "total_jpy": None, "officers": None, "by_type": {}})
        v = _num(f["value"])
        if name.startswith("TotalAmountOfRemuneration"):
            c["total_jpy"] = v
        elif name.startswith("NumberOfDirectorsAndOtherOfficers"):
            c["officers"] = v
        elif name.startswith("NumberOf"):
            continue
        elif v is not None:
            c["by_type"][re.sub(r"、.*$", "", f["label"])] = v
    # A category shown only with dashes has no officers paid; it stays out rather than appearing as zero.
    rows = [c for c in cats.values() if c["total_jpy"] is not None or c["officers"] is not None or c["by_type"]]
    if not rows:
        return "not_found", {"categories": [], "warnings": []}
    ok = all(r["total_jpy"] is not None for r in rows)
    warnings = ([] if ok else ["category_total_missing"]) + compensation_warnings(rows)
    return ("found" if ok else "partial"), {"categories": rows, "warnings": warnings}


# ---- main customers -----------------------------------------------------------------------------------------------

_CUST_AMOUNT = re.compile(r"(?<![\d,])(?<!\d\.)(\d[\d,]*)(?:\s*(百万円|千円|円))?(?![\d.]*%)")
_CUST_TABLE = re.compile(r"(?:名称|氏名|相手先|顧客名|販売先).{0,40}?(?:売上|収益|セグメント)")
_SEG_END = re.compile(r"^(.+?(?:事業|部門|セグメント|ビジネス))(?=\S)")


def parse_customers(text, segment_names=()):
    """Rows of the 主要な顧客 table. Returns (status, rows, warnings)."""
    s = _norm(text)
    table = _CUST_TABLE.search(s)
    if not table:
        return ("none_stated" if _NONE_WORDS.search(s) or "10%" in s else "not_found"), [], []
    header = None
    for header in re.finditer(r"関連するセグメント(?:名称?|の名称)?|関連セグメント名?|セグメント(?:の)?名称?|売上高(?:\s*\([^)]*\))?|売上収益(?:\s*\([^)]*\))?|収益(?:\s*\([^)]*\))?", s[:table.end() + 60]):
        pass
    if not header:
        return "not_found", [], ["table_header_not_found"]
    head_text = s[:header.end()]
    body = s[header.end():]
    body = re.sub(r"(\d{1,3}(?:,\d{3})+)(\d{1,3}\.\d+\s*%)", r"\1 \2", body)  # '3,738,43412.8%': amount and ratio run together
    body = re.sub(r"\(注\s*\d*\)\s*(?=[\d,]+)", "", body)
    body = re.sub(r"\*\d*\s*(?=[\d,]+)", "", body)
    body = re.split(r"\(注|※\s*\d|(?:^| )注\s*\d", body, maxsplit=1)[0].strip()
    if not re.search(r"\d", body):
        return ("none_stated" if _NONE_WORDS.search(s) else "not_found"), [], []
    if re.search(r"(?:前|当)(?:連結)?(?:事業年度|会計年度)", body[:120]):  # one column per year: names and years interleave
        return "not_found", [], ["two_year_layout_not_read"]
    sentence = body.find("。")  # a remark after the table has dates and figures that are not rows
    if sentence >= 0:
        body = body[:max(body.rfind(" ", 0, sentence), 0)].strip()
    unit = next((v for k, v in _UNITS.items() if re.search(rf"単位\s*[::]?\s*{k}", s) or re.search(rf"\(\s*{k}\s*\)", head_text)), None)
    columns = s[table.start():header.end()]  # the preamble may mention 単一セグメント without a segment column
    seg_first = bool(re.search(r"セグメント.*(?:売上|収益)", columns))
    has_segment_column = "セグメント" in columns
    amounts = list(_CUST_AMOUNT.finditer(body))
    names = {n for n in segment_names if n}
    if amounts and not seg_first and has_segment_column:  # the last row's segment has nothing after it, so it names a segment for the other rows
        last = re.sub(r"^\s*\d+(?:\.\d+)?\s*%?\s*", "", body[amounts[-1].end():]).strip(" ,、")
        if last:
            names.add(last)
    names = sorted(names, key=len, reverse=True)
    rows, warnings = [], []
    pos = 0
    for i, a in enumerate(amounts):
        name = body[pos:a.start()].strip(" ,、")
        if not name or re.fullmatch(r"[\d.%\s]*", name):
            pos = a.end()
            continue
        seg = None
        if not has_segment_column:
            pos = a.end()
        elif seg_first:
            hit = next((n for n in names if name.endswith(n)), None)
            if hit:
                name, seg = name[:-len(hit)].strip(), hit
            else:
                warnings.append("name_segment_boundary_unresolved")
                name = None
            pos = a.end()
        elif i + 1 == len(amounts):
            seg = re.sub(r"^\s*\d+(?:\.\d+)?\s*%?\s*", "", body[a.end():]).strip(" ,、") or None
            pos = len(body)
        else:
            tail = re.sub(r"^\s*(?:\d+(?:\.\d+)?\s*%|\d+\.\d+)\s*", "", body[a.end():amounts[i + 1].start()])
            skipped = len(body[a.end():amounts[i + 1].start()]) - len(tail)
            hit = next((n for n in names if tail.lstrip().startswith(n)), None)
            if not hit:
                end = _SEG_END.match(tail.lstrip())
                hit = end.group(1) if end else None
            if hit:
                seg = hit
                pos = a.end() + skipped + (len(tail) - len(tail.lstrip())) + len(hit)
            else:
                warnings.append("name_segment_boundary_unresolved")
                pos = a.end()
        if name is not None and re.search(r"合計|^計$", name):
            continue
        amount = a.group(1)
        value = int(amount.replace(",", "")) if _WELL_FORMED.match(amount) else None
        scale = _UNITS.get(a.group(2) or "") or unit
        if value is None:
            warnings.append("amount_not_read")
        if scale is None:
            warnings.append("unit_not_found")
        rows.append({"name_ja": name, "sales_jpy": value * scale if value is not None and scale else None, "segment_ja": seg})
    if not rows:
        return "not_found", [], warnings
    return ("partial" if warnings else "found"), rows, sorted(set(warnings))


def main_customers(facts, segment_names=()):
    text = _block(facts, "InformationForEachOfMainCustomersTextBlock", "CurrentYearDuration") \
        or _block(facts, "InformationForEachOfMainCustomersTextBlock", "CurrentYearDuration_NonConsolidatedMember")
    if not text:
        for f in facts:
            if "Segment" in f["element"] and f["element"].endswith("TextBlock") and f["context"].startswith("CurrentYear") and "主要な顧客" in f["value"]:
                piece = f["value"][f["value"].rfind("主要な顧客"):]
                text = re.split(r"【|\s[4-9][\.．]\s", piece, maxsplit=1)[0]
                break
    if not text:
        return "not_found", {"customers": [], "warnings": []}
    status, rows, warnings = parse_customers(text, segment_names)
    return status, {"customers": rows, "warnings": warnings}


# ---- subsidiaries and affiliates ----------------------------------------------------------------------------------

_KIND = re.compile(r"\(\s*(連結子会社|持分法適用(?:非連結子会社|関連会社|会社)?|関連会社|非連結子会社|親会社|その他の関係会社|持分法非適用[^)]{0,10})\s*\)")
_PREFECTURES = "北海道|青森県|岩手県|宮城県|秋田県|山形県|福島県|茨城県|栃木県|群馬県|埼玉県|千葉県|東京都|神奈川県|新潟県|富山県|石川県|福井県|山梨県|長野県|岐阜県|静岡県|愛知県|三重県|滋賀県|京都府|大阪府|兵庫県|奈良県|和歌山県|鳥取県|島根県|岡山県|広島県|山口県|徳島県|香川県|愛媛県|高知県|福岡県|佐賀県|長崎県|熊本県|大分県|宮崎県|鹿児島県|沖縄県"
_CITIES = "札幌市|仙台市|さいたま市|千葉市|横浜市|川崎市|相模原市|新潟市|静岡市|浜松市|名古屋市|京都市|大阪市|堺市|神戸市|岡山市|広島市|北九州市|福岡市|熊本市"
_COUNTRIES = "米国|アメリカ|中国|香港|台湾|韓国|タイ|シンガポール|ドイツ|英国|イギリス|フランス|インド|ベトナム|マレーシア|インドネシア|フィリピン|カナダ|メキシコ|ブラジル|オランダ|オーストラリア|スイス"
_ADDR = "(?:" + _PREFECTURES + "|" + _CITIES + "|" + _COUNTRIES + ")"
_AFF_NAME = re.compile(r"((?:\(株\)|株式会社|有限会社|合同会社)[^\s\d(),、。]{1,30}?|[^\s\d(),、。)]{1,30}?(?:\(株\)|株式会社|有限会社|合同会社)|[A-Za-z][\w.,&' -]{1,50}?(?:Inc\.?|INC\.?|Ltd\.?|LTD\.?|LLC|Corp\.?|Co\.|GmbH|Limited|LIMITED|Corporation|CORPORATION))\s*(?:\(注[^)]*\)[\d、,.]*\s*)?(?=" + _ADDR + ")")
_NOT_A_NAME = re.compile(r"^\d*名|出資|役員|兼任|資金|援助|取引|賃貸|賃借|委託|受託|業務|なし|あり|当社|販売|仕入|貸付|保証|提供|管理|。|\.\.\.")
_COMPANY_MARK = re.compile(r"株式会社|\(株\)|有限会社|合同会社|有限公司|Inc|INC|Ltd|LTD|LLC|Corp|CORP|Co\.|CO\.|GmbH|Limited|LIMITED|S\.A|B\.V|PTE|Pte")
_NAME_JUNK = re.compile(r"記載|該当|効力|吸収|合併|連結の範囲|企業の概況|とおり|であります|重要性|新規|設立|当連結|上記|について|において")
_CELL_NUMBER = re.compile(r"(?<![\d,.])\d[\d,]*(?:\.\d+)?")


def parse_affiliate_rows(text):
    """Named rows of 関係会社の状況 with the voting share as stated. Precision over recall: a row is dropped when it has no
    company marker, or when its name picked up the previous row's relation cell, which cannot be cut off reliably."""
    s = _norm(text)
    kinds = list(_KIND.finditer(s))
    rows = []
    for m in _AFF_NAME.finditer(s):
        if _NOT_A_NAME.search(m.group(1)):
            continue
        kind = None
        for k in kinds:
            if k.start() < m.start():
                kind = k.group(1)
        after = s[m.end():m.end() + 160]
        # Stop at the next company so its percentage cannot be taken.
        nxt = _AFF_NAME.search(after)
        if nxt:
            after = after[:nxt.start()]
        # After the address come capital, business, then the share; a share over 100 means the cells were misread.
        cells = _CELL_NUMBER.findall(re.sub(r"\(内[^)]*\)|\([^)]*(?:円|ドル|元|バーツ|ユーロ|ウォン)[^)]*\)", " ", after))
        share = float(cells[1].replace(",", "")) if len(cells) > 1 else None
        # CSV cells can attach the preceding column heading to the first company name.
        name = re.sub(r"^(?:関係内容|会社名|名称)+", "", m.group(1).strip()).strip()
        rows.append({"kind_ja": kind, "name_ja": name, "voting_share_pct": share if share is not None and share <= 100 else None})
    return rows


def _major_names(text):
    """Names from '主要な連結子会社の名称: A、B'. None when the note does not list them plainly."""
    s = _norm(text)
    m = re.search(r"[^。:]{0,24}?(?:の名称|会社名)\s*[::]?\s*(?:は?(?:次|以下)の?(?:とおり|通り)[^。]*。)?", s) \
        or re.search(r"(?:子会社|会社)の数\s*[::]?\s*\d+\s*社\s*", s)
    if not m:
        return None
    tail = re.split(r"。|\(\d\)|\(注|\(連結|なお|ほか|他\s*\d|②|\d\.", s[m.end():], maxsplit=1)[0]
    tail = re.sub(r"^.*(?:の名称|会社名)\s*[::]?", "", tail)
    parts = [p.strip(" ・:") for p in re.split(r"、|,(?! ?(?:LTD|Ltd|INC|Inc|Co|CO|LLC|S\.A))| (?=\(株\)|株式会社)|(?<=株式会社) |(?<=\(株\)) ", tail) if p.strip(" ・:")]
    parts = [p for p in parts if 2 <= len(p) <= 45 and _COMPANY_MARK.search(p) and not _NAME_JUNK.search(p)]
    return parts or None


def affiliates(facts):
    n_sub = _num(_block(facts, "NumberOfConsolidatedSubsidiaries") or "")
    n_assoc = _num(_block(facts, "NumberOfAssociatesAccountedForUsingEquityMethod") or "")
    n_eq = _num(_block(facts, "NumberOfUnconsolidatedSubsidiariesAndAssociatesAccountedForUsingEquityMethod") or "")
    overview = _block(facts, "OverviewOfAffiliatedEntitiesTextBlock") or ""
    subs_text = _block(facts, "NumberOfConsolidatedSubsidiariesAndNamesOfMajorConsolidatedSubsidiariesTextBlock")
    assoc_text = _block(facts, "NumberOfUnconsolidatedSubsidiariesAndAssociatesAccountedForUsingEquityMethodAndNamesOfMajorEntitiesAccountedForUsingEquityMethodTextBlock")
    major_subs = _major_names(subs_text) if subs_text else None
    major_assoc = _major_names(assoc_text) if assoc_text else None
    rows = parse_affiliate_rows(overview)
    has_any = rows or major_subs or major_assoc or n_sub is not None or n_assoc is not None or n_eq is not None
    if not has_any:
        o = _norm(overview)
        if not o or re.search(r"該当事項はありません|関係会社は(?:あり|有り)ません", o):
            return "none_stated" if o else "not_found", {"rows": [], "warnings": []}
        return "not_found", {"rows": [], "warnings": []}
    return "found", {"consolidated_subsidiaries": n_sub, "equity_method_associates": n_assoc if n_assoc is not None else n_eq,
                     "major_subsidiaries_ja": major_subs, "major_equity_method_ja": major_assoc, "rows": rows, "warnings": []}


# ---- capex and facilities -----------------------------------------------------------------------------------------

_FACILITY = re.compile(r"([^\d(),、。:%\[\]〔〕〈〉\s][^\d(),、。:%\[\]〔〕〈〉]{0,30}?)\s*\(([^()]{2,30}?)\)")
_LOC_MARK = re.compile(r"[都道府県市区町村郡州]|米国|アメリカ|中国|香港|台湾|韓国|タイ|シンガポール|ドイツ|英国|イギリス|フランス|インド|ベトナム|マレーシア|インドネシア|フィリピン|カナダ|メキシコ|ブラジル|オランダ|オーストラリア|スイス|他$")
_LOC_NOT = re.compile(r"百万|千円|単位|注|㎡|名\)|人\)|面積|所在地|平均|外数|現在|年|月|円|ドル|実績")
_NAME_NOT = re.compile(r"^(?:提出会社|国内子会社|在外子会社|事業所名|会社名|設備の内容|セグメント|帳簿価額|従業員数|土地|建物|機械|リース|合計|その他|工具)")
_GROUP = re.compile(r"(提出会社|国内子会社|在外子会社)")


def parse_facilities(text):
    s = re.sub(r"\(注\)?\s*\d*\)?\s*(?=\()", "", _norm(text))  # '本社(注)3(横浜市西区)'
    groups = list(_GROUP.finditer(s))
    rows = []
    # A later section restarts after the notes of the first, so notes are skipped by section rather than by a single cut.
    spans = []
    start = 0
    for g in re.finditer(r"\(注\)?\s*\d?.*?(?=\(\d\)\s*(?:国内子会社|在外子会社)|$)", s):
        spans.append((start, g.start()))
        start = g.end()
    spans.append((start, len(s)))
    for a, b in spans:
        for m in _FACILITY.finditer(s, a, b):
            loc = m.group(2)
            name = re.sub(r"^.*(?:合計|帳簿価額|従業員数)", "", m.group(1)).strip(" ・-")
            name = re.sub(r"^(?:金額|事業所名|会社名|所在地)+", "", name).strip(" ・-")
            ordinal = re.search(r"第\d+$", s[a:m.start(1)])  # '第2製造工場': the digit is outside the name class
            if ordinal:
                name = ordinal.group(0) + name
            if re.fullmatch(r"(?:店舗?|拠点|カ所|ヶ所|か所|箇所|工場|事業所|営業所)", name):  # '浜木綿業態31店舗': the count splits the cell
                count = re.search(r"([^\d(),、。:%\[\]〔〕〈〉\s]{1,20}\d{1,4})\s*$", s[:m.start(1)])
                name = count.group(1) + name if count else name
            if not _LOC_MARK.search(loc) or _LOC_NOT.search(loc) or _NAME_NOT.match(name) or len(name) < 2:
                continue
            group = None
            for g in groups:
                if g.start() < m.start():
                    group = g.group(1)
            rows.append({"group_ja": group, "name_ja": name, "location_ja": loc})
    return rows


def capex(facts):
    total = None
    by_segment = []
    for f in facts:
        if not f["element"].endswith(":CapitalExpendituresOverviewOfCapitalExpendituresEtc"):
            continue
        v = _num(f["value"])
        if f["context"] == "CurrentYearDuration":
            total = v
        elif f["context"].startswith("CurrentYearDuration_") and v is not None:
            member = f["context"].split("_", 1)[1]
            by_segment.append({"member": member, "name_en": _member_name(member), "capex_jpy": v})
    text = _block(facts, "OverviewOfCapitalExpendituresEtcTextBlock")
    body = re.sub(r"^\s*\d*\s*【[^】]*】", "", _norm(text or ""))
    q = quote(body) if body else None
    if total is None and not by_segment and not q:
        return "not_found", {"warnings": []}
    return "found", {"total_jpy": total, "by_segment": by_segment, "overview_quote_ja": q, "warnings": []}


def major_facilities(facts):
    text = _block(facts, "MajorFacilitiesTextBlock")
    if not text:
        return "not_found", {"facilities": [], "warnings": []}
    rows = parse_facilities(text)
    body = re.sub(r"^\s*\d*\s*【[^】]*】", "", _norm(text))
    if rows:
        return "found", {"facilities": rows, "warnings": []}
    if len(body) < 60 and _NONE_WORDS.search(body):
        return "none_stated", {"facilities": [], "warnings": []}
    return "not_found", {"facilities": [], "warnings": ["table_not_read"]}


# ---- dividend policy ----------------------------------------------------------------------------------------------

_POLICY_WORDS = re.compile(r"基本方針|基本的な方針|方針|還元|安定")


def dividend_policy(facts):
    text = _block(facts, "DividendPolicyTextBlock")
    if not text:
        return "not_found", {"quote_ja": None, "warnings": []}
    body = re.sub(r"^\s*\d*\s*【[^】]*】", "", _norm(text)).strip()
    if not body:
        return "not_found", {"quote_ja": None, "warnings": []}
    sentences = [x.strip() + "。" for x in body.split("。") if x.strip()]
    usable = [x for x in sentences if "会社法" not in x and "定款" not in x]
    chosen = next((x for x in usable if "配当" in x and re.search(r"基本|方針", x)), None) \
        or next((x for x in usable if "配当" in x and _POLICY_WORDS.search(x)), None) or next((x for x in sentences if "配当" in x), None)
    if not chosen:
        return "not_found", {"quote_ja": None, "warnings": ["no_dividend_sentence"]}
    return "found", {"quote_ja": quote(chosen), "warnings": []}


# ---- diversity ----------------------------------------------------------------------------------------------------

_DIV = [
    (re.compile(r"^RatioOfFemaleEmployeesInManagerialPositionsMetricsOf"), "female_manager_pct"),
    (re.compile(r"^AllEmployeesDifferencesInWagesBetweenMaleAndFemaleEmployeesMetricsOf"), "pay_gap_all_pct"),
    (re.compile(r"^RegularEmployeesDifferencesInWagesBetweenMaleAndFemaleEmployeesMetricsOf"), "pay_gap_regular_pct"),
    (re.compile(r"^NonRegularEmployeesDifferencesInWagesBetweenMaleAndFemaleEmployeesMetricsOf"), "pay_gap_non_regular_pct"),
    (re.compile(r"^AllEmployeesCalculatedBasedOnProvisionsOfArticle714Item1"), "male_parental_leave_item1_pct"),
    (re.compile(r"^AllEmployeesCalculatedBasedOnProvisionsOfArticle714Item2"), "male_parental_leave_item2_pct"),
    (re.compile(r"^AllEmployeesRatioOfMaleEmployeesTakingChildcareLeaveMetricsOf"), "male_parental_leave_pct"),
    (re.compile(r"^RegularEmployees(?:CalculatedBasedOnProvisionsOfArticle714Item\d|RatioOfMaleEmployeesTakingChildcareLeave)\w*"), "male_parental_leave_regular_pct"),
    (re.compile(r"^NonRegularEmployees(?:CalculatedBasedOnProvisionsOfArticle714Item\d|RatioOfMaleEmployeesTakingChildcareLeave)\w*"), "male_parental_leave_non_regular_pct"),
]


def _fraction_pct(v):
    """The tag holds a fraction: 0.169 is 16.9%. Kept as filed, so a filer's scale slip shows as a very large figure."""
    return None if v is None else round(v * 100, 2)


def diversity(facts):
    company = {}
    subs = {}
    warnings = []
    for f in facts:
        name = f["element"].split(":")[1]
        if "Metric" not in name and "Article714" not in name:
            continue
        key = next((k for rx, k in _DIV if rx.match(name)), None)
        if key is None and not name.startswith("ConsolidatedSubsidiaries"):
            continue
        reporting = "ReportingCompany" in name or ("Article714" in name and "ConsolidatedSubsidiaries" not in name)
        if name.startswith("ConsolidatedSubsidiaries"):
            m = re.search(r"(Row\d+Member)", f["context"])
            if m:
                subs.setdefault(m.group(1), {})["name_ja"] = f["value"].strip()
            continue
        v = _fraction_pct(_num(f["value"]))
        if reporting and f["context"].startswith("CurrentYear"):
            company[key] = v
            if v is not None and v > (100 if key == "female_manager_pct" else 150 if key.startswith("pay_gap") else 300):
                warnings.append("scale_suspect:" + key)
        elif not reporting:
            m = re.search(r"(Row\d+Member)", f["context"])
            if m:
                subs.setdefault(m.group(1), {})[key] = v
    if not company and not subs:
        return "not_found", {"reporting_company": {}, "subsidiaries": [], "warnings": []}
    subs_rows = [dict(s) for s in subs.values() if s.get("name_ja")]
    stated = any(v is not None for v in company.values()) or any(v is not None for s in subs_rows for k, v in s.items() if k != "name_ja")
    return ("found" if stated else "none_stated"), {"reporting_company": company, "subsidiaries": subs_rows, "warnings": warnings}


# ---- whole filing -------------------------------------------------------------------------------------------------


def parse(facts, en_labels=None):
    """{item: (status, record)} for one annual report, from xbrlcsv.read_facts output."""
    seg_status, seg = segments(facts, en_labels)
    names = [r.get("name_ja") for r in seg.get("segments", [])]
    return {
        "segments": (seg_status, seg),
        "sga": sga(facts),
        "director_compensation": director_compensation(facts),
        "main_customers": main_customers(facts, names),
        "affiliates": affiliates(facts),
        "capex": capex(facts),
        "major_facilities": major_facilities(facts),
        "dividend_policy": dividend_policy(facts),
        "diversity": diversity(facts),
    }


def _dei(facts):
    return {f["element"].split(":")[1]: f["value"] for f in facts if f["element"].startswith("jpdei_cor:")}


def store(con, doc_id, parsed, ticker, edinet_code, fiscal_year_end, submit_date):
    url = SOURCE_URL.format(doc_id)
    for item, (status, rec) in parsed.items():
        con.execute("INSERT OR REPLACE INTO filing_details VALUES (?,?,?,?,?,?,?,?,?)", (
            doc_id, item, ticker, edinet_code, fiscal_year_end, submit_date, status, url,
            json.dumps({**rec, "doc_id": doc_id, "item": item, "status": status, "source_url": url}, ensure_ascii=False)))


def run(con, limit=None, cached_only=True, minutes=None):
    """Read annual reports not yet read, stopping cleanly after `minutes` so the run is still recorded. Stock companies' reports only (those with an annual_reports row, or all type 120
    named 有価証券報告書 when that table is empty). Returns (documents read, documents failed)."""
    con.executescript(SCHEMA)
    rows = con.execute(
        "SELECT f.doc_id, f.submit_datetime, f.edinet_code, i.ticker FROM filings f LEFT JOIN issuers i ON i.edinet_code = f.edinet_code "
        "WHERE f.doc_type_code = '120' AND f.withdrawn = 0 AND f.has_csv = 1 AND f.description_ja LIKE '有価証券報告書%' "
        "AND f.doc_id NOT IN (SELECT doc_id FROM filing_details_read WHERE status = 'read') ORDER BY f.submit_datetime DESC").fetchall()
    if cached_only:
        rows = [r for r in rows if os.path.exists(os.path.join(edinet.CSV_CACHE, f"{r[0]}.zip"))]
    read = failed = 0
    deadline = time.monotonic() + minutes * 60 if minutes else None
    for doc_id, submitted, edinet_code, ticker in rows[:limit]:
        if deadline and time.monotonic() > deadline:
            break  # the rest is read by the next run
        try:
            facts = xbrlcsv.read_facts(edinet.download_csv_zip(doc_id))
            parsed = parse(facts)
        except Exception as e:  # one bad filing must not stop the sweep; it is recorded and can be retried
            con.execute("INSERT OR REPLACE INTO filing_details_read VALUES (?,?,?)", (doc_id, "failed", str(e)[:300]))
            con.commit()
            failed += 1
            continue
        dei = _dei(facts)
        sec = (dei.get("SecurityCodeDEI") or "").strip()
        store(con, doc_id, parsed, ticker or (sec[:4] if sec and sec not in ("－", "-") else None), edinet_code,
              dei.get("CurrentFiscalYearEndDateDEI"), (submitted or "")[:10])
        con.execute("INSERT OR REPLACE INTO filing_details_read VALUES (?,?,?)", (doc_id, "read", None))
        con.commit()
        read += 1
    return read, failed


def coverage(con):
    """{item: {status: documents}} over everything read so far."""
    out = {}
    for item, status, n in con.execute("SELECT item, status, COUNT(*) FROM filing_details GROUP BY 1, 2"):
        out.setdefault(item, {})[status] = n
    return out
