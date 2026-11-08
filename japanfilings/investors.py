"""Consolidated investor groups from 5% filers, top-shareholder names and N-PORT registrants.

Deterministic: the same store and override file give the same groups.

Merges happen only by:
  same_edinet_code  (high)    names filed under one EDINET code
  exact_name        (high)    same name after removing spacing, legal-form spelling and account suffixes;
                              different stated legal forms are not merged
  curated_name      (high)    a name listed in `assign` of investor_overrides.json
  curated_pattern   (medium)  a reviewed pattern in `groups` of investor_overrides.json
Everything else stays separate and similar names go to the review queue. A group says who is related by
ownership or brand as listed in the override file. It never says members act together: only
`investor_joint_filings`, taken from filings that name joint holders, records that.

Natural persons are never published. An entity is published only when it is positively an organization
(type stated in a filing, a listed issuer, or a legal-form or institution word in its name). Names with
neither are treated as possible persons and left out, and only their counts are kept, never the names.

  python -m japanfilings.investors rebuild
  python -m japanfilings.investors review out.csv     # alias table and review queue, for manual checking
  python -m japanfilings.investors d1 out.sql         # INSERT statements for a local D1
"""
import csv
import hashlib
import html
import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict

from . import store

OVERRIDES = os.path.join(os.path.dirname(__file__), "investor_overrides.json")

SCHEMA = """
CREATE TABLE IF NOT EXISTS investor_groups (
  group_id TEXT PRIMARY KEY, name_en TEXT, name_ja TEXT, kind TEXT, confidence TEXT,
  n_members INTEGER, n_companies INTEGER, search_text TEXT, record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ig_rank ON investor_groups(kind, n_companies);
CREATE TABLE IF NOT EXISTS investor_group_members (
  group_id TEXT, member_key TEXT, name TEXT, edinet_code TEXT, sources TEXT, match_rule TEXT, confidence TEXT,
  PRIMARY KEY (group_id, member_key)
);
CREATE INDEX IF NOT EXISTS igm_code ON investor_group_members(edinet_code);
CREATE TABLE IF NOT EXISTS investor_positions (
  group_id TEXT, ticker TEXT, source TEXT, holder_key TEXT, filer_edinet_code TEXT, holder_name TEXT,
  first_date TEXT, first_kind TEXT, first_ratio_pct REAL, last_date TEXT, last_ratio_pct REAL, last_doc_id TEXT,
  status TEXT, exit_date TEXT, n_filings INTEGER, history TEXT,
  PRIMARY KEY (group_id, ticker, source, holder_key)
);
CREATE INDEX IF NOT EXISTS ip_ticker ON investor_positions(ticker, status);
CREATE TABLE IF NOT EXISTS investor_joint_filings (
  group_a TEXT, group_b TEXT, ticker TEXT, latest_doc_id TEXT, latest_date TEXT, n_filings INTEGER,
  PRIMARY KEY (group_a, group_b, ticker)
);
CREATE INDEX IF NOT EXISTS ijf_b ON investor_joint_filings(group_b);
CREATE TABLE IF NOT EXISTS investor_excluded_filers (edinet_code TEXT PRIMARY KEY);
"""
# Local only, not sent to D1.
LOCAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS investor_aliases (
  member_key TEXT PRIMARY KEY, name TEXT, group_id TEXT, match_rule TEXT, confidence TEXT, sources TEXT, published INTEGER
);
CREATE TABLE IF NOT EXISTS investor_review_queue (reason TEXT, name_a TEXT, name_b TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS investor_filter_stats (key TEXT PRIMARY KEY, value INTEGER);
"""
D1_TABLES = ["investor_groups", "investor_group_members", "investor_positions", "investor_joint_filings", "investor_excluded_filers"]

# Account and agent wording that belongs to the shareholder register, not the holder.
_PAREN = re.compile(r"[\(\[][^()\[\]]*[\)\]]")
_TAIL = re.compile(r"(常任代理人.*|無限責任組合員.*|信託口.*|投信口.*|共有口.*|退職給付信託.*|再信託.*|口座.*|\s+\d{3,}.*)$")
_JP_FORMS = [("KK", re.compile(r"株式会社|\(株\)|㈱")), ("YK", re.compile(r"有限会社|\(有\)|㈲")), ("GK", re.compile(r"合同会社|\(同\)")),
             ("SOGO", re.compile(r"相互会社|\(相\)"))]
_EN_FORMS = [("LTD", r"(CO\.?,?\s*)?(LTD\.?|LIMITED)"), ("INC", r"INC\.?|INCORPORATED"), ("LLC", r"L\.?L\.?C\.?"), ("LLP", r"L\.?L\.?P\.?"),
             ("LP", r"L\.?P\.?"), ("PLC", r"P\.?L\.?C\.?"), ("CORP", r"CORP\.?|CORPORATION"), ("AG", r"AG"), ("SA", r"S\.?A\.?"), ("PTE", r"PTE\.?\s*(LTD\.?|LIMITED)?")]
_EN_FORM = [(k, re.compile(rf"[\s,]+(?:{p})\s*$", re.I)) for k, p in _EN_FORMS]
_STRIP = re.compile(r"[\s　・･,.．，、'’`\"&/\\\-−‐–—=＝_:;!+*#]+")

_ORG_JA = re.compile(r"株式会社|\(株\)|㈱|有限会社|㈲|合同会社|合資会社|合名会社|相互会社|\(相\)|\(有\)|法人|銀行|信託|証券|保険|生命|金庫|信用組合|組合|共済|財団|基金|協会|機構|"
                     r"持株会|投信|投資|アセット|キャピタル|マネジメント|マネージメント|ファンド|ホールディングス|インベストメント|インベスターズ|パートナーズ|アドバイザーズ|"
                     r"コープ|ベンチャーズ|エルティーディー|ピーティーワイ|有限公司|リミテッド|エルエルシー|エルエルピー|エルピー|ピーティーイー|ピーエルシー|インク|カンパニー|コーポレーション|トラスト|バンク|グループ|商事|商会|工業|製作所|"
                     r"不動産|興産|物産|建設|電機|電気|鉄道|新聞|放送|大学|学園|政府|会社|社団|商店|運輸|倉庫|製薬|化学|産業|ビー・?[ヴブ]ィ?ー?|ビー・?ブイ|エル・?ピー|アーゲー|エヌ・?[ヴブ]イ|ゲーエムベーハ|ゲゼルシャフト|エス・?エ[ーイ]|エス・?アー・?エール・?エル|エス・?ピー・?エ[ーイ]|ソシエテ|リミティッド|エ[ーイ]・ジー|・エービー")
_ORG_EN = re.compile(r"\b(LTD|LIMITED|LLC|L\.L\.C|LP|L\.P|LLP|PLC|INC|INCORPORATED|CORP|CORPORATION|COMPANY|CO|FUNDS?|TRUST|BANK|CAPITAL|PARTNERS|MANAGEMENT|INVESTMENTS?|"
                     r"HOLDINGS?|ASSET|ASSETS|ADVISORS?|ADVISERS?|GROUP|PTE|GMBH|AG|SA|S\.A|NV|BV|GOVERNMENT|AUTHORITY|FOUNDATION|PENSION|SECURITIES|PORTFOLIOS?|ETF|SERIES|"
                     r"SPC|UCITS|RETIREMENT|LIMITADA|INSURANCE|ASSURANCE|UNIVERSITY|ENDOWMENT|CLIENTS?|ACCOUNT|NOMINEES?)\b", re.I)


# Officers named after the company on a share register or a filer field, e.g. '... 代表取締役 氏名',
# 'Arcus Investment Limited Director/Executive Director <name>', '<org> CEO <name>'. Never published.
# Same as TITLES_JA, OFFICER, SUFFIX_THEN_NAME, TITLE_WORDS, LEGAL_FORM_FIRST and PROFESSION in worker/src/privacy.ts. Keep in step.
_TITLES_JA = ("最高経営責任者|代表取締役|代表執行役|代表社員|代表理事|代表清算人|代表者|業務執行社員|業務執行組合員|業務執行者|無限責任組合員|職務執行者|"
              "会計参与|破産管財人|更生管財人|管財人|清算人|支配人|理事長|理事|評議員|監事|会長|取締役|執行役|支店長")
_OFFICER = re.compile(r"[\s,/&]*(" + _TITLES_JA + r"|"
                      r"(?:(?<=\s)|(?<=,)|(?<=、)|(?<=株式会社)|(?<=有限会社)|(?<=合同会社))(?:社長|頭取)|"
                      r"(?<=[A-Z]{3})(?:CEO|CFO|COO)(?=\s+(?!(?:INC|LTD|LLC|CORP|CO|PLC|LIMITED)\b)[A-Z])|"
                      r"\b(?:the\s+)?(?:REPRESENTATIVE|DIRECTOR|PRESIDENT|CEO|COO|CFO|CHAIRMAN|CHAIRPERSON|CHIEF)\b"
                      r"|\bEXECUTIVE\s+(?:DIRECTOR|OFFICER)\b|\bMANAGING\s+(?:DIRECTOR|PARTNER|MEMBER)\b|\bGENERAL\s+MANAGER\b).*$", re.I)
# '<company suffix> <personal name>' left when the title came after the name: 'KYOCERA CORPORATION Hideo Kasoumoto, Director ...'.
_SUFFIX_THEN_NAME = re.compile(r"^(\S.*[\s,]+(?:CO\.?,?\s*LTD\.?|LTD\.?|LIMITED|INC\.?|INCORPORATED|CORPORATION|CORP\.?|L\.?L\.?C\.?|G\.?K\.?|K\.?K\.?|"
                               r"L\.?L\.?P\.?|L\.?P\.?|PLC|PTE\.?\s*LTD\.?|GMBH|AG|S\.A\.?|B\.V\.?|N\.V\.?))[\s,]+"
                               r"[A-Z][A-Z.'\-]*(?:\s+[A-Z][A-Z.'\-]*){0,2}[\s,]*$", re.I)
# A legal form written before the name, after a space: '山田太郎 株式会社〇〇代表取締役' puts the person first.
_LEGAL_FORM_FIRST = re.compile(r"\s(?:株式会社|有限会社|合同会社|合資会社|合名会社|(?:一般|公益)(?:社団|財団)法人|学校法人|医療法人|社会福祉法人|宗教法人|"
                               r"特定非営利活動法人|NPO法人|\(株\)|㈱|\(有\)|㈲)(?=\S)")
# A professional acting for the holder ('弁護士 架 空 弘 典'). 弁護士法人 is a firm.
_PROFESSION = re.compile(r"^(?:代理人\s*)?(?:弁護士|司法書士|公認会計士|税理士|行政書士|弁理士)(?!法人)\s*(.+)$")
_PLACE = re.compile(r"\b(?:JAPAN|TOKYO|BRANCH|OFFICE|DIVISION|INTERNATIONAL|GLOBAL|ASIA|PACIFIC)\b", re.I)
_ACCOUNT = re.compile(r"信託口|投信口|共有口|管理信託|専用信託")


def _org_like(v):
    h = re.sub(r"[\(（][^()（）]*[\)）]", "", v).strip() or v
    return bool(_ORG_JA.search(h) or _ORG_EN.search(h))


def _trailing_person(t):
    """A word sequence after a company suffix that is a person, not a qualifier like 'Japan' or 'Tokyo Branch'."""
    return looks_like_romanized_person(t) or (len(t.split(" ")) >= 2 and not _org_like(t) and not _PLACE.search(t))


def clean_name(name):
    s = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", html.unescape(name or "")).strip())
    pro = _PROFESSION.match(s)
    if pro and not _org_like(pro.group(1)):
        return ""
    m = _OFFICER.search(s)
    if not m:
        return s
    head = s[:m.start()].strip()
    if head.count("(") > head.count(")"):  # '株式会社〇〇(代表取締役 山田太郎)': the bracket goes with the title
        head = head[:head.rfind("(")].strip()
    f = _LEGAL_FORM_FIRST.search(head)
    if f and head[:f.start()].strip() and not _org_like(head[:f.start()].strip()):
        head = head[f.start():].strip()
    n = _SUFFIX_THEN_NAME.match(head)
    if n and _trailing_person(head[len(n.group(1)):].strip(" ,")):
        head = n.group(1)
    return re.sub(r"[\s,、/&]+$", "", head).strip()


def _latin_only(s):
    return bool(s) and not re.search(r"[぀-ヿ一-鿿]", s)


# A Japanese personal name in Latin letters: two or three words, each spelt only in romanised kana
# ('kasou tarou', 'AKANE KAKUU', 'Jinichi Chouno'). 'Hikari Tsushin K.K.' or 'YOUR TURN' are not.
_ROMAJI = re.compile(r"^(?:(?:[kstpgdbcjfm])(?=[kstpgdbcjfm])|(?:ky|gy|sh|sy|jy|ch|cy|ts|ny|hy|by|py|my|ry|[kgsztdnhbpmyrwfj])?[aiueo]h?|n)+$", re.I)
# Romanised legal forms and trade words that are no part of a personal name ('House Kousan Kabushikikaisya').
_ROMAJI_ORG = re.compile(r"(?:KAI|GAI)S[HY]A|KABUSHIKI|YUU?GEN|GODO|GOUDOU|KOU?SAN|FUDOU?SAN|SHOTEN|KANKOU?|HOU?JIN|ZAIDAN|KUMIAI|SANGYOU?|BUSSAN|KENSETSU|SEISAKUSHO|KOU?GYOU?", re.I)


def looks_like_romanized_person(name):
    words = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", name or "")).strip().split(" ")
    return (2 <= len(words) <= 3 and not looks_like_organization(" ".join(words))
            and all(_ROMAJI.match(w) and not _ROMAJI_ORG.search(w) for w in words))


def usable_english_name(name):
    """An English filer name cleaned of titles and people, or None when what is left could be a personal name."""
    c = clean_name(name) if name else ""
    return c if _latin_only(c) and not looks_like_romanized_person(c) else None


def filer_is_person(rec):
    """True when the holder behind a 5% filing is a natural person whatever the filer field says: the issuer filed on
    a person's behalf (filed_by_issuer), or the only holder is stated to be an individual."""
    holders = rec.get("holders") or []

    def person(h):
        return h.get("holder_type") == "individual" or (h.get("holder_type") != "corporation" and not looks_like_organization(h.get("name")))

    by_issuer = "filed_by_issuer" in (rec.get("data_warnings") or []) or (
        rec.get("filer_edinet_code") and rec.get("filer_edinet_code") == rec.get("issuer_edinet_code"))
    if by_issuer:
        return not holders or any(person(h) for h in holders)
    return len(holders) == 1 and holders[0].get("holder_type") == "individual"


def public_filer_names(rec, master_en=None, joint_filing_name=False):
    """(Japanese, English) names of an organisation filer that are safe to publish: titles and the people after them
    cut, and an English field used only when what is left names an organisation. An English filing field that is a
    bare personal name ('kasou tarou' for トラストアップ株式会社) gives None, so readers fall back to Japanese."""
    ja = clean_name(rec.get("filer_name_ja")) or None
    sole = len(rec.get("holders") or []) <= 1
    # In stored records filer_name_en_filing can be the last joint holder's name, so it counts only for a sole holder.
    # Events have always fallen back to it (joint_filing_name), investor groups never do.
    cands = [rec.get("filer_name_en_filing") if sole else None, rec.get("filer_name_en"), master_en,
             rec.get("filer_name_en_filing") if joint_filing_name and not sole else None]
    en = next((c for c in map(usable_english_name, cands) if c), None)
    return ja, en


def split_name(name):
    """(normalized core, stated legal form or None). '㈱X', 'X株式会社' and 'X(株)' give one core."""
    s = clean_name(name)
    form = None
    for k, rx in _JP_FORMS:
        if rx.search(s):
            form = k
            s = rx.sub(" ", s)
    s = _PAREN.sub(" ", s)
    s = _TAIL.sub("", s).strip()
    for k, rx in _EN_FORM:
        t = rx.sub("", s)
        if t != s and t.strip():
            form, s = form or k, t
            break
    return _STRIP.sub("", s).upper(), form


_ESTATE = re.compile(r"相続人|相続財産|遺言執行者|成年後見")


def looks_like_organization(name):
    s = clean_name(name)
    if _ESTATE.search(str(name or "")):  # an heir or estate holds as a person, even when a company name follows
        return False
    head = re.sub(r"[\(（][^()（）]*[\)）]", "", s).strip() or s  # a person with a firm in brackets stays a person
    return bool(_ORG_JA.search(head) or _ORG_EN.search(head))


def _display(name):
    """Without register wording: agents, account labels and footnote marks."""
    s = re.sub(r"\(常任代理人.*$|\(常代.*$|\(注.*$|※.*$", "", clean_name(name))
    return s.strip() or clean_name(name)


def search_key(q):
    return _STRIP.sub("", clean_name(q)).upper()


def _load_overrides(path=None):
    with open(path or OVERRIDES, encoding="utf-8") as f:
        o = json.load(f)
    for g in o["groups"]:
        g["_rx"] = [re.compile(p) for p in g["patterns"]]
    return o


def _latin_alias(name):
    """'エフィッシモ...(Effissimo Capital Management Pte Ltd)' carries an English name in brackets."""
    m = re.search(r"\(([A-Za-z][A-Za-z0-9 ,.&'\-]{3,})\)", clean_name(name))
    return m.group(1).strip() if m else None


class Names:
    """Every name seen, keyed by normalized core plus stated form."""

    def __init__(self):
        self.obs = defaultdict(lambda: {"names": Counter(), "sources": set(), "codes": set(), "types": set(), "forms": set()})
        self.en = defaultdict(Counter)

    def add(self, name, source, code=None, type_=None, weight=1):
        core, form = split_name(name)
        if len(core) < 2:
            return None
        o = self.obs[core]
        o["names"][clean_name(name)] += weight
        o["sources"].add(source)
        if code:
            o["codes"].add(code)
        if type_:
            o["types"].add(type_)
        if form:
            o["forms"].add(form)
        return core


def collect(con):
    n = Names()
    filings_by_code = defaultdict(Counter)
    for r in con.execute("SELECT edinet_code, filer_name_ja, COUNT(*) c FROM filings WHERE doc_type_code IN "
                         "('350','360','240','250','260','270','280','290','300','310') AND edinet_code IS NOT NULL GROUP BY 1, 2"):
        filings_by_code[r["edinet_code"]][r["filer_name_ja"]] += r["c"]
        n.add(r["filer_name_ja"], "filer", r["edinet_code"], weight=r["c"])
    records = []
    for r in con.execute("SELECT record FROM large_holdings ORDER BY submit_date, doc_id"):
        rec = json.loads(r["record"])
        records.append(rec)
        code = rec.get("filer_edinet_code")
        if rec.get("filer_name_ja"):
            n.add(rec["filer_name_ja"], "filer", code)
        # An English name only when it names an organisation (public_filer_names), and never from a filing the
        # issuer made for a person: there the English field is that person's name.
        if code and not filer_is_person(rec):
            en = public_filer_names(rec)[1]
            if en:
                n.en[code][en] += 1
        for h in rec.get("holders") or []:
            if not h.get("name"):
                continue
            t = h.get("holder_type") or ("corporation" if re.search(r"法人|会社|パートナーシップ", h.get("holder_type_ja") or "") else None)
            n.add(h["name"], "joint_holder", None, t)
    top = []
    latest = con.execute("SELECT a.ticker, a.record FROM annual_reports a WHERE a.ticker IS NOT NULL AND a.rowid = ("
                         "SELECT b.rowid FROM annual_reports b WHERE b.ticker = a.ticker ORDER BY b.fiscal_year_end DESC, b.submit_date DESC LIMIT 1)")
    for r in latest:
        rec = json.loads(r["record"])
        for h in (rec.get("company") or {}).get("major_shareholders") or []:
            if h.get("name") and n.add(h["name"], "top_shareholder"):
                top.append((rec["ticker"], rec["fiscal_year_end"], rec["doc_id"], h))
    nport = defaultdict(lambda: {"tickers": set(), "value": 0.0, "quarter": None})
    for r in con.execute("SELECT ticker, record FROM us_fund_holdings"):
        rec = json.loads(r["record"])
        for f in rec.get("top_funds") or []:
            core = n.add(f.get("registrant") or "", "nport")
            if core:
                x = nport[core]
                x["tickers"].add(r["ticker"])
                x["value"] += f.get("value_usd") or 0
                x["quarter"] = rec.get("quarter")
    issuers = {}
    for r in con.execute("SELECT edinet_code, ticker, name_ja, name_en, listed FROM issuers"):
        issuers[r["edinet_code"]] = dict(r)
    return n, records, top, nport, issuers


def build(con, overrides=None):
    """Everything computed in memory; `rebuild` writes it."""
    ov = overrides or _load_overrides()
    n, records, top, nport, issuers = collect(con)
    obs = n.obs
    # Issuer code list: a filer with a code gets its official names; a holder name matching a listed issuer gets the code.
    by_issuer_core = {}
    for code, i in sorted(issuers.items()):
        if i["name_ja"]:
            core, _ = split_name(i["name_ja"])
            if i["listed"] or core not in by_issuer_core:
                by_issuer_core[core] = code
    for code in sorted({c for o in obs.values() for c in o["codes"]}):
        i = issuers.get(code)
        if i and i["name_ja"]:
            n.add(i["name_ja"], "issuer_list", code)
    for core, o in list(obs.items()):
        if not o["codes"] and core in by_issuer_core and "individual" not in o["types"]:
            o["codes"].add(by_issuer_core[core])
            o["sources"].add("issuer_list")

    # Classification. A person stays a person whatever else the name suggests.
    force_ind = {split_name(x)[0] for x in ov.get("individual", [])}
    force_org = {split_name(x)[0] for x in ov.get("organization", [])}
    individual_codes = set()
    for core, o in obs.items():
        if "individual" in o["types"] or core in force_ind:
            individual_codes |= o["codes"]
    excluded = {"individual_stated": set(), "no_org_evidence": set()}

    def classify(core, o):
        if core in force_org:
            return "org"
        if core in force_ind or "individual" in o["types"] or o["codes"] & individual_codes:
            return "individual_stated"
        names = o["names"]
        if any(_ESTATE.search(str(x)) for x in names):  # an heir or estate, even when filed as a company
            return "individual_stated"
        if "corporation" in o["types"] or any(looks_like_organization(x) for x in names):
            return "org"
        if any(issuers.get(c, {}).get("ticker") for c in o["codes"]):
            return "org"
        return "no_org_evidence"

    org = {}
    excluded_codes = set()
    stats = Counter()
    for core, o in obs.items():
        c = classify(core, o)
        if c == "org":
            org[core] = o
        else:
            excluded[c].add(core)
            excluded_codes |= o["codes"]
            for s in o["sources"]:
                stats[f"{c}:{s}"] += 1
    excluded_codes -= {c for o in org.values() for c in o["codes"]}
    excluded_codes = {c for c in excluded_codes if not issuers.get(c, {}).get("ticker")}

    # Union by EDINET code.
    parent = {k: k for k in org}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_code = defaultdict(list)
    for core in sorted(org):
        for c in org[core]["codes"]:
            by_code[c].append(core)
    for members in by_code.values():
        for m in members[1:]:
            a, b = find(members[0]), find(m)
            if a != b:
                parent[max(a, b)] = min(a, b)
    comps = defaultdict(list)
    for core in sorted(org):
        comps[find(core)].append(core)

    # Curated assignment.
    never = {split_name(x)[0] for x in ov.get("never_curate", [])}
    assigned = {split_name(a["name"])[0]: a["group"] for a in ov.get("assign", [])}
    curated = {g["id"]: g for g in ov["groups"]}

    def match(core):
        if core in never:
            return None, None
        if core in assigned:
            return assigned[core], "curated_name"
        for g in ov["groups"]:
            if any(rx.search(core) for rx in g["_rx"]):
                return g["id"], "curated_pattern"
        return None, None

    queue = []
    groups = defaultdict(list)  # group_id -> [(core, rule, confidence)]
    for root, members in comps.items():
        hits = {m: match(m) for m in members}
        ids = sorted({h[0] for h in hits.values() if h[0]})
        if len(ids) > 1:
            queue.append(("edinet_code_spans_two_curated_groups", members[0], members[-1], ",".join(ids)))
            ids = []
        if ids:
            gid = ids[0]
            for m in members:
                rule = hits[m][1] or "same_edinet_code"
                conf = "medium" if rule == "curated_pattern" else "high"
                groups[gid].append((m, rule, conf))
        else:
            codes = sorted({c for m in members for c in org[m]["codes"]})
            gid = f"ig_{codes[0].lower()}" if codes else "ig_" + hashlib.sha1(members[0].encode()).hexdigest()[:12]
            for m in members:
                rule = "same_edinet_code" if len(members) > 1 else "exact_name"
                groups[gid].append((m, rule, "high"))

    # Different stated legal forms under one core are a question for a person.
    for core in sorted(org):
        if len(org[core]["forms"]) > 1:
            queue.append(("same_name_different_legal_form", org[core]["names"].most_common(1)[0][0], "", ",".join(sorted(org[core]["forms"]))))

    core_group = {m: gid for gid, ms in groups.items() for m, _, _ in ms}
    code_group = {}
    for core, gid in core_group.items():
        for c in org[core]["codes"]:
            code_group[c] = gid

    # Positions from 5% filings, per group, filer and company.
    positions = {}
    joint = {}
    for rec in records:
        code, ticker = rec.get("filer_edinet_code"), rec.get("issuer_ticker")
        gid = code_group.get(code)
        # A filing the issuer made for a person, or one whose only holder is a person, is no position of the filer's group.
        if ticker and gid and code not in excluded_codes and not filer_is_person(rec):
            key = (gid, ticker, "filing_5pct", code)
            ratio, date_, kind = rec.get("holding_ratio_pct"), rec.get("submit_date"), rec.get("report_kind")
            p = positions.get(key)
            if p is None:
                p = positions[key] = {"group_id": gid, "ticker": ticker, "source": "filing_5pct", "holder_key": code, "filer_edinet_code": code,
                                      "holder_name": public_filer_names(rec)[1] or public_filer_names(rec)[0], "first_date": date_,
                                      "first_kind": "new_stake_reported" if kind == "initial_report" else "first_filing_on_record",
                                      "first_ratio_pct": ratio, "status": "held", "exit_date": None, "n_filings": 0, "history": []}
            p["n_filings"] += 1
            p["last_date"], p["last_doc_id"] = date_, rec["doc_id"]
            if ratio is not None:
                p["last_ratio_pct"] = ratio
            below = ratio is not None and ratio < 5
            if kind != "amendment" or below:
                if below and p["status"] == "held":
                    p["status"], p["exit_date"] = "below_5pct", date_
                    p["history"].append({"date": date_, "event": "exit_below_5pct", "ratio_pct": ratio, "doc_id": rec["doc_id"]})
                elif not below and p["status"] == "below_5pct":
                    p["status"], p["exit_date"] = "held", None
                    p["history"].append({"date": date_, "event": "reentry", "ratio_pct": ratio, "doc_id": rec["doc_id"]})
                elif p["n_filings"] == 1:
                    p["history"].append({"date": date_, "event": p["first_kind"], "ratio_pct": ratio, "doc_id": rec["doc_id"]})
        # Joint holders only as a filing states them.
        names = [h["name"] for h in rec.get("holders") or [] if h.get("name") and h.get("holder_type") != "individual"]
        ids = sorted({core_group[c] for c in (split_name(x)[0] for x in names) if c in core_group})
        if ticker:
            for i, a in enumerate(ids):
                for b in ids[i + 1:]:
                    j = joint.setdefault((a, b, ticker), {"n": 0})
                    j.update(n=j["n"] + 1, doc=rec["doc_id"], date=rec.get("submit_date"))

    for ticker, fye, doc_id, h in top:
        core, _ = split_name(h["name"])
        gid = core_group.get(core)
        # An account in a bank's or broker's name belongs to someone else, so it is no position of the group.
        if not gid or _ACCOUNT.search(clean_name(h["name"])):
            continue
        key = (gid, ticker, "top_shareholder", core)
        if key not in positions:
            positions[key] = {"group_id": gid, "ticker": ticker, "source": "top_shareholder", "holder_key": core, "filer_edinet_code": None,
                              "holder_name": _display(h["name"]), "first_date": fye, "first_kind": "listed_in_annual_report",
                              "first_ratio_pct": h.get("ratio_pct"), "last_date": fye, "last_ratio_pct": h.get("ratio_pct"), "last_doc_id": doc_id,
                              "status": "listed_in_latest_report", "exit_date": None, "n_filings": 1, "history": []}

    # Assemble. Unconnected top-shareholder names held in one company are not worth a published group.
    company_count = defaultdict(set)
    for p in positions.values():
        if p["status"] != "below_5pct":
            company_count[p["group_id"]].add(p["ticker"])
    out_groups, members_out, aliases = [], [], []
    for gid in sorted(groups):
        ms = groups[gid]
        srcs = set().union(*(org[m]["sources"] for m, _, _ in ms))
        cur = curated.get(gid)
        publish = bool(cur) or bool(srcs & {"filer", "nport", "joint_holder"}) or len(company_count[gid]) >= 2
        all_names = Counter()
        for m, _, _ in ms:
            all_names.update(org[m]["names"])
        ja = next((x for x, _ in sorted(all_names.items(), key=lambda kv: (-kv[1], kv[0])) if re.search(r"[぀-ヿ一-鿿]", x)), None)
        codes = sorted({c for m, _, _ in ms for c in org[m]["codes"]})
        en_counter = Counter()
        for c in codes:
            en_counter.update(n.en.get(c, {}))
            i = issuers.get(c, {})
            if i.get("name_en") and (i.get("ticker") or looks_like_organization(i["name_en"])):
                en_counter[clean_name(i["name_en"])] += 1
        for x in all_names:
            lat = _latin_alias(x)
            if lat and looks_like_organization(lat):
                en_counter[lat] += 1
            elif not lat and _latin_only(x):
                en_counter[_PAREN.sub("", x).strip()] += all_names[x]
        en = sorted(en_counter.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if en_counter else None
        name_en = (cur or {}).get("name_en") or en
        name_ja = (cur or {}).get("name_ja") or (_PAREN.sub("", ja).strip() if ja else None)
        tickers = sorted({issuers[c]["ticker"] for c in codes if issuers.get(c, {}).get("ticker")})
        kind = (cur or {}).get("kind") or ("listed_company" if tickers else "us_fund_registrant" if srcs == {"nport"} else "organization")
        conf = "medium" if any(c == "medium" for _, _, c in ms) else "high"
        ns = {"tickers": set(), "value": 0.0, "quarter": None}
        for m, _, _ in ms:
            if m in nport:
                ns["tickers"] |= nport[m]["tickers"]
                ns["value"] += nport[m]["value"]
                ns["quarter"] = nport[m]["quarter"]
        search = "|".join(sorted({search_key(x) for x in [name_en, name_ja, *(cur or {}).get("aliases", []), *en_counter, *map(_display, all_names)] if x}))
        rec = {"group_id": gid, "name_en": name_en, "name_ja": name_ja, "kind": kind, "confidence": conf, "listed_tickers": tickers,
               "edinet_codes": codes, "note": (cur or {}).get("note"),
               "us_funds": {"quarter": ns["quarter"], "tickers_in_top_fund_lists": len(ns["tickers"]), "value_usd_in_top_fund_lists": round(ns["value"])} if ns["tickers"] else None}
        if publish:
            out_groups.append((gid, name_en, name_ja, kind, conf, len(ms), len(company_count[gid]), search, json.dumps(rec, ensure_ascii=False)))
        for m, rule, c in ms:
            display = _display(org[m]["names"].most_common(1)[0][0])
            mc = sorted(org[m]["codes"])
            if publish:
                members_out.append((gid, m, display, mc[0] if mc else None, ",".join(sorted(org[m]["sources"])), rule, c))
            aliases.append((m, display, gid, rule, c, ",".join(sorted(org[m]["sources"])), int(publish)))
    published = {g[0] for g in out_groups}

    # Similar uncurated names, for a person to look at. Not merged.
    prefix = defaultdict(set)
    for m, _, gid, _, _, srcs, pub in aliases:
        if pub and gid not in curated and len(m) >= 6:
            prefix[m[:5]].add(m)
    for p, ms in sorted(prefix.items()):
        if 2 <= len(ms) <= 6:
            ms = sorted(ms)
            queue.append(("shared_name_prefix", ms[0], ms[1], p))

    pos_rows = [p for p in positions.values() if p["group_id"] in published]
    return {"groups": out_groups, "members": members_out, "aliases": aliases, "queue": queue, "positions": pos_rows,
            "joint": [(a, b, t, j["doc"], j["date"], j["n"]) for (a, b, t), j in sorted(joint.items()) if a in published and b in published],
            "excluded_codes": sorted(excluded_codes),
            "stats": {"names_seen": len(obs), "individual_stated": len(excluded["individual_stated"]), "no_org_evidence": len(excluded["no_org_evidence"]),
                      "excluded_filer_codes": len(excluded_codes), **stats}}


_POS_COLS = ["group_id", "ticker", "source", "holder_key", "filer_edinet_code", "holder_name", "first_date", "first_kind", "first_ratio_pct",
             "last_date", "last_ratio_pct", "last_doc_id", "status", "exit_date", "n_filings", "history"]


def _pos_tuple(p):
    return tuple(json.dumps(p.get(c), ensure_ascii=False) if c == "history" else p.get(c) for c in _POS_COLS)


def rebuild(con, overrides=None):
    b = build(con, overrides)
    con.executescript(SCHEMA + LOCAL_SCHEMA)
    with con:  # computed already, so this transaction is short
        for t in D1_TABLES + ["investor_aliases", "investor_review_queue", "investor_filter_stats"]:
            con.execute(f"DELETE FROM {t}")
        con.executemany("INSERT INTO investor_groups VALUES (?,?,?,?,?,?,?,?,?)", b["groups"])
        con.executemany("INSERT INTO investor_group_members VALUES (?,?,?,?,?,?,?)", b["members"])
        con.executemany(f"INSERT INTO investor_positions VALUES ({','.join('?' * len(_POS_COLS))})", [_pos_tuple(p) for p in b["positions"]])
        con.executemany("INSERT INTO investor_joint_filings VALUES (?,?,?,?,?,?)", b["joint"])
        con.executemany("INSERT INTO investor_excluded_filers VALUES (?)", [(c,) for c in b["excluded_codes"]])
        con.executemany("INSERT INTO investor_aliases VALUES (?,?,?,?,?,?,?)", b["aliases"])
        con.executemany("INSERT INTO investor_review_queue VALUES (?,?,?,?)", b["queue"])
        con.executemany("INSERT INTO investor_filter_stats VALUES (?,?)", list(b["stats"].items()))
    return {"groups": len(b["groups"]), "multi_member": sum(1 for g in b["groups"] if g[5] > 1), "members": len(b["members"]),
            "positions": len(b["positions"]), "joint": len(b["joint"]), "review_queue": len(b["queue"]), **b["stats"]}


def write_review(con, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["group_id", "group_name", "name", "match_rule", "confidence", "sources", "published"])
        for r in con.execute("SELECT a.group_id, COALESCE(g.name_en, g.name_ja), a.name, a.match_rule, a.confidence, a.sources, a.published "
                             "FROM investor_aliases a LEFT JOIN investor_groups g USING (group_id) ORDER BY a.group_id, a.name"):
            w.writerow(list(r))
        w.writerow([])
        w.writerow(["review_reason", "name_a", "name_b", "detail"])
        for r in con.execute("SELECT * FROM investor_review_queue ORDER BY 1, 2"):
            w.writerow(list(r))


def write_d1(con, path):
    def lit(v):
        if v is None:
            return "NULL"
        if isinstance(v, (int, float)):
            return str(v)
        return "'" + str(v).replace("'", "''") + "'"
    with open(path, "w", encoding="utf-8") as f:
        for t in D1_TABLES:
            f.write(f"DELETE FROM {t};\n")
            for r in con.execute(f"SELECT * FROM {t}"):
                f.write(f"INSERT INTO {t} VALUES ({','.join(lit(v) for v in r)});\n")


def main(argv=None):
    a = argv if argv is not None else sys.argv[1:]
    con = store.connect()
    if a[:1] == ["rebuild"]:
        print(json.dumps(rebuild(con), ensure_ascii=False))
    elif a[:1] == ["review"] and len(a) == 2:
        write_review(con, a[1])
    elif a[:1] == ["d1"] and len(a) == 2:
        write_d1(con, a[1])
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
