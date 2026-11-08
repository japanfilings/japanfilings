"""Natural persons' names never leave the store: the last step of every export (export_d1._export_row).

Two layers, both applied to every exported string, JSON `record` columns included (they are masked as text, which
is safe because the replacement holds no quote or backslash and no rule crosses a quote):

* A dictionary of real people's names found in the store itself: meeting candidates, officers and representatives
  in annual reports, individual 5% holders and filers, individual tender offerors, individual major shareholders
  and joint holders. Each name is matched with or without blanks between its characters, longest first. Individual
  filers' romanised names are matched too, in either word order.
* Pattern rules for names the dictionary cannot know: "<name>氏", "<name>様", and a name straight after an officer's
  title ("取締役CFO兼CIO山田太郎は"). These extend governance.scrub, which already does this for quotes.

Every match becomes "[氏名省略]". The result depends only on the store, so the same row always exports the same way
(the D1 delta compares hashes, and cross_holdings uses a masked column in its key).

Speed: the export runs every 10 minutes over every row. The dictionary is compiled once per process into one
regular expression shaped like a trie, whose first token is a single character class: the regex engine then skips
every character that cannot start a name in C, and dispatches on the first character through nested look-behind
buckets instead of trying thousands of alternatives in turn. Pure-ASCII strings skip the Japanese matcher.
"""
import json
import re
import unicodedata

from . import governance
from .investors import looks_like_organization, looks_like_romanized_person

OMIT = "[氏名省略]"
MIN_LEN, MAX_LEN = 3, 8

# Parse junk and ordinary words that are not names, whatever the source said.
_JUNK = re.compile(
    r"の件|議案|変更|配当|定款|剰余金|選任|解任|承認|動議|株式|会社|取締役|監査|報酬|役員|執行|代表|社長|会長|理事|"
    r"部長|担当|委員|候補|補欠|社外|常勤|議長|総会|決議|処分|処理|継続|導入|改定|改正|廃止|贈呈|退職|慰労|賞与|"
    r"相続|財産|名義|口座|信託|持株|共有|合計|その他|以下|以上|個人|氏名|省略|株主|提出|保有|共同|発行|当社|同社|"
    r"ファイナンス|リース|サービス|システム|エンタープライズ|ジャパン|インター|ナショナル|コンサル|ホールディング|企画|商事|興業|"
    r"開発|事務所|研究所|学校|病院|医院|クリニック|薬局|農園|牧場|工務店|商店|證券|代理人|[店社会都道府県市区町村郡]$|"
    r"[0-9０-９%％()（）\[\]［］「」『』、。,.:：;；/／]")
_JA_PERSON = re.compile(r"[一-龥々〆ヵヶ豈-﫿]{1,5}[一-龥々〆ヵヶぁ-ん豈-﫿]{1,6}")
_KATAKANA_NAME = re.compile(r"[ァ-ヶー・=]+")
_HAS_JA = re.compile(r"[぀-ヿ一-鿿豈-﫿]")
_LEGAL_FORM = re.compile(r"株式会社|有限会社|合同会社|合資会社|合名会社|\(株\)|\(有\)|㈱|㈲")
_PAREN = re.compile(r"[(（][^()（）]*[)）]")

# A representative's or officer's title in front of the name ("代表取締役社長 社長執行役員 丙々美 勉").
_TITLE_TOKEN = (r"(?:代表|取締役|執行役員|執行役|社長|会長|副|専務|常務|常勤|社外|兼|頭取|理事長|理事|監査役|監査等委員|委員長|"
                r"代表社員|業務執行社員|職務執行者|組合長|総裁|学長|所長|院長|最高[一-龥]{0,6}責任者|グループ|"
                r"CEO|COO|CFO|CIO|CTO|CHRO|CMO|CSO|CDO|[一-龥ァ-ヶー]{1,8}(?:担当|本部長|部長|室長|統括|管掌|責任者|支店長))")
_LEADING_TITLES = re.compile(r"^(?:" + _TITLE_TOKEN + r"[\s・、,/()（）]*)+")


def norm(s):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(s or "")))


def person_name(raw, explicit=True):
    """The name as a dictionary entry, or None. `explicit` when the source says this is a person; otherwise the
    name must also look like a Japanese personal name (kanji surname)."""
    s = unicodedata.normalize("NFKC", str(raw or "")).strip()
    s = _LEADING_TITLES.sub("", s)
    s = re.sub(r"\s+", "", s)
    longest = MAX_LEN if not (explicit and _KATAKANA_NAME.fullmatch(s)) else 20  # a long katakana foreign name, e.g. "ジョンカソウプレースホルダー"
    if not (MIN_LEN <= len(s) <= longest) or not _HAS_JA.search(s) or _JUNK.search(s):
        return None
    if looks_like_organization(s):
        return None
    if not explicit and not _JA_PERSON.fullmatch(s):
        return None
    return s


def english_name(raw):
    """A romanised personal name as a list of words, or None."""
    s = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(raw or ""))).strip(" ,")
    if not s or _HAS_JA.search(s) or looks_like_organization(s):
        return None
    words = [w for w in re.split(r"[\s,.]+", s) if w]
    if not (2 <= len(words) <= 3) or not all(re.fullmatch(r"[A-Za-z][A-Za-z'\-]+", w) for w in words):
        return None
    if sum(map(len, words)) < 6:
        return None
    return words


_OFFICER_AFTER_ORG = re.compile(r"(?:理事長|会長|代表取締役(?:社長)?|代表理事|代表者|代表社員)\s*([^\s()（）、,]{2,12})\s*$")


def _parts(raw):
    """A name and the spellings in its brackets ("山田太郎(やまだたろう)"), and each joint holder in "A、B、C"."""
    s = unicodedata.normalize("NFKC", str(raw or ""))
    out = [_PAREN.sub("", s)] + re.findall(r"[(（]([^()（）]*)[)）]", s)
    # "〇〇取引先持株会理事長 山田太郎": the person after an organisation's officer title.
    out += [m.group(1) for x in list(out) for m in [_OFFICER_AFTER_ORG.search(x)] if m]
    parts = [p for x in out for p in re.split(r"[、,，]", x) if p.strip()]
    # "相続人代表 山田花子", "相続人 山田花子・山田さち子"
    heirs = [re.sub(r"^\s*(?:相続人代表|相続人|遺言執行者|代表)\s*[:：]?", "", p) for p in parts if re.match(r"\s*(?:相続人|遺言執行者)", p)]
    return parts + [h for x in heirs for h in x.split("・") if h.strip()]


def collect(con):
    """(Japanese names, English names) of people in the store. Everything is read with SQLite's JSON functions."""
    ja, en, shaped = set(), [], set()

    def q(sql):
        try:
            return [r[0] for r in con.execute(sql) if r[0]]
        except Exception:  # a table that a job has not created yet
            return []

    explicit = []
    explicit += q("SELECT DISTINCT candidate_ja FROM voting_results WHERE candidate_ja IS NOT NULL")
    explicit += q("SELECT DISTINCT json_extract(o.value, '$.name') FROM annual_reports a, json_each(a.record, '$.company.officers') o")
    explicit += q("SELECT DISTINCT json_extract(record, '$.company.representative') FROM annual_reports")
    explicit += q("SELECT DISTINCT json_extract(h.value, '$.name') FROM large_holdings l, json_each(l.record, '$.holders') h "
                  "WHERE json_extract(h.value, '$.holder_type') = 'individual'")
    explicit += q("SELECT DISTINCT offeror_name_ja FROM tender_offers WHERE json_extract(record, '$.offeror_is_individual') = 1")
    explicit += q("SELECT DISTINCT json_extract(record, '$.filer_name_ja') FROM tender_offers WHERE json_extract(record, '$.filer_is_individual') = 1")
    explicit += q("SELECT DISTINCT json_extract(j.value, '$.name_ja') FROM tender_offers t, json_each(t.record, '$.joint_offerors') j "
                  "WHERE json_extract(j.value, '$.is_individual') = 1")
    explicit += q("SELECT DISTINCT json_extract(record, '$.filer_name_ja') FROM ownership_events WHERE json_extract(record, '$.filer_is_individual') = 1")
    # Judged by the name: holders not stated to be companies, major shareholders, trade-table holders, excluded filers.
    judged = []
    judged += q("SELECT DISTINCT json_extract(h.value, '$.name') FROM large_holdings l, json_each(l.record, '$.holders') h "
                "WHERE coalesce(json_extract(h.value, '$.holder_type'), '') NOT IN ('individual', 'corporation')")
    judged += q("SELECT DISTINCT json_extract(s.value, '$.name') FROM annual_reports a, json_each(a.record, '$.company.major_shareholders') s")
    judged += q("SELECT DISTINCT holder FROM holder_trades")
    judged += q("SELECT DISTINCT json_extract(l.record, '$.filer_name_ja') FROM large_holdings l "
                "JOIN investor_excluded_filers x ON x.edinet_code = l.filer_edinet_code")
    judged += q("SELECT DISTINCT f.filer_name_ja FROM filings f JOIN investor_excluded_filers x ON x.edinet_code = f.edinet_code")
    for raw in explicit:
        for p in _parts(raw):
            n = person_name(p, explicit=True)
            if n:
                ja.add(n)
            elif english_name(p):
                en.append(english_name(p))
    for raw in judged:
        for p in _parts(raw):
            if looks_like_organization(p):
                continue
            n = person_name(p, explicit=False)
            if n:
                shaped.add(n)
            elif looks_like_romanized_person(p):
                en.append(english_name(p))
    for raw in (q("SELECT DISTINCT json_extract(record, '$.filer_name_en') FROM ownership_events WHERE json_extract(record, '$.filer_is_individual') = 1")
                + q("SELECT DISTINCT json_extract(record, '$.filer_name_en_filing') FROM ownership_events WHERE json_extract(record, '$.filer_is_individual') = 1")
                + q("SELECT DISTINCT json_extract(l.record, '$.filer_name_en') FROM large_holdings l "
                    "JOIN investor_excluded_filers x ON x.edinet_code = l.filer_edinet_code")):
        w = english_name(raw)
        if w:
            en.append(w)
    # A name judged by its shape alone that is an organisation's name without its legal form ("野村證券" for
    # 野村證券株式会社) is an organisation. Stated individuals stay even then.
    cores = set()
    for raw in (q("SELECT name_ja FROM issuers") + q("SELECT DISTINCT holder FROM holder_trades")
                + q("SELECT DISTINCT json_extract(h.value, '$.name') FROM large_holdings l, json_each(l.record, '$.holders') h "
                    "WHERE json_extract(h.value, '$.holder_type') = 'corporation'")):
        for p in _parts(raw):
            if looks_like_organization(p):
                cores.add(_LEGAL_FORM.sub("", norm(p)))
    ja |= shaped - cores
    # Listed companies keep their names: a "name" inside one is dropped from the dictionary.
    listed = "\x00".join(norm(n) for n in q("SELECT name_ja FROM issuers WHERE listed = 1"))
    ja = {n for n in ja if n not in listed}
    listed_en = "\x00".join(norm(n).upper() for n in q("SELECT name_en FROM issuers WHERE listed = 1"))
    en_out = set()
    for w in en:
        if w and "".join(w).upper() not in listed_en:
            en_out.add(tuple(x.upper() for x in w))
    return ja, en_out


# ---------- the dictionary matcher ----------

_SEP = "[ 　]*"


def _trie(words):
    root = {}
    for w in words:
        d = root
        for c in w:
            d = d.setdefault(c, {})
        d[""] = True
    return root


def _ser(d, sep, esc):
    """Regex for a trie node. Greedy optional tails make the longest name win."""
    alts = []
    for c in sorted(k for k in d if k):
        rest = _ser(d[c], sep, esc)
        alts.append(esc(c) + (sep + rest if rest else ""))
    if not alts:
        return ""
    body = alts[0] if len(alts) == 1 else "(?:" + "|".join(alts) + ")"
    return "(?:" + body + ")?" if "" in d else body


def _cls(chars, esc):
    return "[" + "".join(esc(c) for c in chars) + "]"


def _dispatch(trie, sep, esc, tail="", after_first=""):
    """[all first characters], then the rest of the name chosen through two levels of look-behind buckets."""
    firsts = sorted(k for k in trie if k)
    if not firsts:
        return None
    n = len(firsts)
    b1, b2 = int(round(n ** (2 / 3))) + 1, int(round(n ** (1 / 3))) + 1
    outer = []
    for i in range(0, n, b1):
        big = firsts[i:i + b1]
        mids = []
        for j in range(0, len(big), b2):
            ch = big[j:j + b2]
            inner = "|".join("(?<=" + esc(c) + ")" + (sep + _ser(trie[c], sep, esc) if _ser(trie[c], sep, esc) else "") for c in ch)
            mids.append("(?<=" + _cls(ch, esc) + ")(?:" + inner + ")")
        outer.append("(?<=" + _cls(big, esc) + ")(?:" + "|".join(mids) + ")")
    return _cls(firsts, esc) + after_first + "(?:" + "|".join(outer) + ")" + tail


def _esc_ja(c):
    return re.escape(c)


class Masker:
    def __init__(self, ja_names, en_names=()):
        self.ja_names, self.en_names = set(ja_names), set(en_names)
        self.ja = None
        if self.ja_names:
            self.ja = re.compile(_dispatch(_trie(self.ja_names), _SEP, _esc_ja))
        self.en = None
        if self.en_names:
            # Upper-case and capitalised spellings, either word order; never inside a longer word.
            spellings = set()
            for words in self.en_names:
                orders = {tuple(words), tuple(reversed(words))}
                for o in orders:
                    for f in (str.upper, str.capitalize):
                        spellings.add("\x01".join(f(w) for w in o))
            # A trie of words: "\x01" marks a word gap, written as optional blanks or punctuation.
            tri = _trie(spellings)

            def esc(c):
                return "\x01" if c == "\x01" else re.escape(c)
            pat = _dispatch(tri, "", esc, tail="(?![A-Za-z])", after_first="(?<![A-Za-z].)")
            pat = pat.replace("\x01", "[\\s,.．・]*")
            self.en = re.compile(pat)

    def mask(self, s):
        if not s:
            return s
        if not s.isascii():
            if self.ja is not None:
                s = self.ja.sub(OMIT, s)
            s = scrub(s)
        if self.en is not None:
            s = self.en.sub(OMIT, s)
        return s


# ---------- pattern rules ----------

_TRIGGER = re.compile(r"氏|様|公認会計士|取締役|執行役|社長|会長|担当|管掌|統括|部長|室長|責任者|理事|監査役|CEO|CFO|COO|CIO|CTO")
# Katakana names before 氏 ("ジョン・スミス氏"): two words at least, so 'グループ氏' and the like stay.
_KATA_UJI = re.compile(r"(?<![ァ-ヶー・])[ァ-ヶー]{2,}(?:[・=＝][ァ-ヶー]{2,})+[ 　]?氏(?![名族])")
_SAMA = re.compile(r"([一-龥々\u3400-\u4dbf]{2,5})[ 　]?様(?![式々相態似])")
# Words before 様 that are no name: 皆様, 株主様, 同様, 多様, 仕様, 模様, お客様 ...
_SAMA_STOP = re.compile(r"(?:皆|客|株主|主|同|多|仕|模|異|一|有|態|左|各|貴|神|仏|王|者|方|人|員|先|社|家|殿|上|会|位|生|両|御|奥|若|姫|宮|"
                        r"子供|親|担当|関係|外|内|当|今|此|斯|如何|何|様|[郡町村市県区道都府])$")


def _sama(m):
    return m.group(0) if _SAMA_STOP.search(m.group(1)) else OMIT + "様"


# The contents of one JSON string that holds a non-ASCII character. The title rules cannot be skipped over ASCII by
# the regex engine, so a JSON record is scrubbed one such string at a time.
_JSON_TEXT = re.compile(r'"([^"\\]*?[^\x00-\x7f][^"\\]*)"')


def scrub(text):
    """Pattern rules only (no dictionary), on any text: governance.scrub (accountants, "<name>氏", a name after an
    officer's title) plus katakana names before 氏 and "<name>様". privacy.ts quote() is the Worker's copy."""
    if not text or not _TRIGGER.search(text):
        return text
    if text[0] in "{[":
        return _JSON_TEXT.sub(lambda m: '"' + _scrub(m.group(1)) + '"', text)
    return _scrub(text)


def _scrub(text):
    if not _TRIGGER.search(text):
        return text
    text = governance.scrub(text)
    if "氏" in text:
        text = _KATA_UJI.sub(OMIT + "氏", text)
    if "様" in text:
        text = _SAMA.sub(_sama, text)
    return text


_MASKERS = {}


def masker(con=None):
    """The matcher for a store, built once per process from that store's own rows."""
    if con is None:
        from . import store
        con = store.connect()
    path = next((r[2] for r in con.execute("PRAGMA database_list") if r[1] == "main"), "") or f"memory:{id(con)}"
    if path not in _MASKERS:
        _MASKERS[path] = Masker(*collect(con))
    return _MASKERS[path]


def mask_row(row, m):
    return [m.mask(v) if isinstance(v, str) else v for v in row]


def mask_value(v, m):
    """For callers that hold parsed JSON."""
    if isinstance(v, str):
        return m.mask(v)
    if isinstance(v, list):
        return [mask_value(x, m) for x in v]
    if isinstance(v, dict):
        return {k: mask_value(x, m) for k, x in v.items()}
    return v


__all__ = ["OMIT", "Masker", "collect", "masker", "mask_row", "mask_value", "scrub", "person_name", "english_name", "json"]
