"""Governance disclosures as filed on EDINET: internal control reports and auditor changes.

Two event types, each saying only what the filing says:

* internal_control: 内部統制報告書 (docTypeCode 235) and its amendments (236). Management's stated conclusion
  (effective / not_effective / could_not_conclude), any stated material weakness (開示すべき重要な不備) and a quote.
* auditor_change: the 監査公認会計士等の異動 item of 臨時報告書 (180). Firm names, change date, the stated reason and
  what the filing says about disagreements.

Nothing is scored or interpreted. A null means the filing did not state it in a form the parser could read.
Individual accountants' names are not stored (the firm field stays null and a flag says so); they are also masked
in quotes, as are representatives' names. Addresses are never read.

Finding auditor changes: EDINET's metadata calls every one "臨時報告書", so the document has to be opened. The XBRL
CSV (type=5, about 3 KB) carries a dedicated element, ChangeInIndependentAuditorsTextBlock, which identifies the
item exactly. `auditor_candidates` narrows what must be opened without reading anything: listed domestic filers
only, and not filed from 2 days before to 14 days after the filer's annual report, the window where shareholder
meeting voting results cluster. That removes about half of the listed reports. See `auditor_candidates`.
"""
import csv
import io
import json
import re
import subprocess
import tempfile
import unicodedata
import zipfile

from . import edinet, store
from .textutil import parse_jp_date, require_japanese

SCHEMA = """
CREATE TABLE IF NOT EXISTS governance_events (
  doc_id TEXT NOT NULL,
  type TEXT NOT NULL,
  ticker TEXT,
  edinet_code TEXT,
  submit_date TEXT,
  record TEXT NOT NULL,
  PRIMARY KEY (doc_id, type)
);
CREATE INDEX IF NOT EXISTS ge_ticker ON governance_events(ticker, submit_date);
CREATE INDEX IF NOT EXISTS ge_type ON governance_events(type, submit_date);
CREATE TABLE IF NOT EXISTS governance_read (
  doc_id TEXT PRIMARY KEY,
  kind TEXT,
  status TEXT,
  detail TEXT
);
"""

SOURCE_URL = "https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{}"
QUOTE_MAX = 199  # quotes stay under 200 characters

# Filed from this many days before to this many days after the annual report: skipped by the default sweep.
AGM_WINDOW_DAYS = (-2, 14)

_E_RESULT = "ResultOfEvaluationTextBlock"
_E_SCOPE = "ScopeDateAndProceduresForEvaluationTextBlock"
_E_NOTE = "SupplementaryInformationTextBlock"
_E_AUDITOR = "ChangeInIndependentAuditorsTextBlock"
_E_REASON = "ReasonForFilingTextBlock"

_KANJI_NAME = r"[一-龥々]{1,5}[ 　]?[一-龥々ぁ-んァ-ヶ]{1,5}"


def _norm(text):
    """NFKC with runs of blanks folded. Line breaks become spaces. Circled digits stay: NFKC would turn ② into a bare 2."""
    folded = re.sub(r"[^①-⑳]+", lambda m: unicodedata.normalize("NFKC", m.group()), text or "")
    return re.sub(r"\s+", " ", folded).strip()


# "当社代表取締役会長甲井一及び当社取締役 CFO山田次郎は": a run of titles, then a name before 及び/は/が/、.
_OFFICER_TITLES = (r"(?:(?:当社|前|元)?(?:代表取締役|代表執行役|取締役|執行役員|執行役|最高財務責任者|最高経営責任者|財務担当|経理担当"
                   r"|CFO|CEO|COO|(?<![会本])社長|会長|副社長|専務|常務|兼|[一-龥]{1,4}本部長|[一-龥]{1,4}部長)[ 　]?)+")
_OFFICER_NAME = re.compile(r"(" + _OFFICER_TITLES + r")(?!社長|会長|副|兼|及び|および|並びに|以下|会|等|として|締役|執行|\[)"
                           r"([一-龥々\uf900-\ufaff]{1,4}[ 　]?[一-龥々\uf900-\ufaff]{1,4})(?=及び|および|並びに|ならびに|は|が|、|。|$)")


# Longer runs of titles than _OFFICER_TITLES: a function's head ("財経担当", "リスクマネジメント担当", "営業本部長"),
# CIO/CTO, 監査役. The name may also end the proposal title it stands in ("取締役乙木靖子解任の件").
_TITLES_EXT = (r"(?:(?:当社|同社|前|元|現)?(?:代表取締役|代表執行役|取締役|執行役員|執行役|監査役|最高[一-龥]{0,6}責任者|"
               r"CFO|CEO|COO|CIO|CTO|CHRO|CMO|CSO|(?<![会本])社長|会長|副社長|副会長|専務|常務|兼|理事長|"
               r"[一-龥ァ-ヶー・]{1,12}?(?:担当|管掌|統括|本部長|部長|室長|責任者))[ 　]?)+")
_TITLED_NAME = re.compile(r"(" + _TITLES_EXT + r")(?!社長|会長|副|兼|及び|および|並びに|以下|会|等|として|締役|執行|役|部|者|室|全|各|\[)"
                          r"([一-龥々㐀-䶿豈-﫿]{1,4}[ 　]?(?:[一-龥々㐀-䶿豈-﫿]|[ぁ-ゖ](?<![のがはをにでともやへ])){1,4})"
                          r"(?=及び|および|並びに|ならびに|は|が|、|。|の(?:解任|選任|再任)|解任|選任|再任|辞任|退任|就任|氏|$)")
# What follows a title but is no name ("経理担当役員は", "取締役監査等委員は"): left alone.
_NOT_NAME = re.compile(r"役|会|部|課|室|員|者|職|制|業|監|報|酬|規|程|責|選|候|補|等|経|営|執|統|括|掌|株|件|議|案|額|期|委|任|全|各|数|名|"
                       r"当社|同社|自身|本人|以外|以上|以下|管理|事業|分野|領域|範囲|体制|機能|組織|本部|部門|グループ|"
                       r"財務|経理|総務|人事|法務|広報|企画|技術|開発|生産|販売|営業|品質|購買|調達|情報|内部|監督|審議|方針|"
                       r"工場|支店|支社|本社|営業所|事業所|センター|研究所|拠点")
# "<name>氏", but not 同氏, 両氏, 上記両氏, 当該氏名 or 摂氏. Kana may stand inside a name ("甲村けん牛氏").
_UJI = re.compile(r"([一-龥々〆ヵヶ㐀-䶿豈-﫿]{1,4}(?:[ぁ-ゖァ-ヶー](?<![のがはをにでともやへ])){0,2}[一-龥々㐀-䶿豈-﫿]{0,4})[ 　]?氏(?![名族])")
_UJI_AFTER = ("上記", "当該", "前記", "後記", "下記", "両", "各", "同", "諸", "某", "他")
_UJI_ALONE = {"摂", "華", "本", "当", "貴", "女", "男", "該", "源", "平"}


def _uji(m):
    run = m.group(1)
    return m.group(0) if run.endswith(_UJI_AFTER) or run in _UJI_ALONE else "[氏名省略]氏"


def _titled(m):
    name = m.group(2)
    cut = re.search(r"及び?|並びに?|ならびに|および", name)
    rest = name[cut.start():] if cut else ""
    name = name[:cut.start()] if cut else name
    if len(name) < 2 or _NOT_NAME.search(name):
        return m.group(0)
    return m.group(1) + "[氏名省略]" + rest


def scrub(text):
    """Mask personal names of accountants and representatives in a quote. The surrounding words stay verbatim."""
    if not text:
        return text
    if "公認会計士" in text:
        # "甲田洋(甲田公認会計士事務所)", then "甲田公認会計士事務所", then "公認会計士 乙坂直慶氏".
        text = re.sub(_KANJI_NAME + r"\s*\([^)]{0,30}公認会計士[^)]{0,20}\)", "[氏名省略]", text)
        text = re.sub(r"(?:" + _KANJI_NAME + r"|[ぁ-んァ-ヶ一-龥々]{1,8})公認会計士(?:共同)?事務所", "[事務所名省略]", text)
        text = re.sub(r"公認会計士(?!等|協会|法|事務所|監査|資格|試験|・)\s*" + _KANJI_NAME + r"(?:氏|様)?", "公認会計士[氏名省略]", text)
    if "氏" in text:
        text = re.sub(r"[一-龥々]{2,6}(?:及び|、|・|と)[一-龥々]{2,6}の?(?:両|各)?氏(?![名族])", "[氏名省略]", text)
        text = _UJI.sub(_uji, text)
    text = re.sub(r"((?:前|元)?代表(?:取締役|執行役)(?:社長|会長|副社長|専務|常務)?(?:執行役員)?(?:CEO|COO)?)\s*"
                  r"(?!社長|会長|副|兼|及び|および|並びに|以下)(" + _KANJI_NAME + r")(?=は|が|の|に|氏|を|、|。|$)",
                  r"\1[氏名省略]", text)
    text = _TITLED_NAME.sub(_titled, text)
    return _OFFICER_NAME.sub(_titled, text)


def _quote(text, limit=QUOTE_MAX):
    """A verbatim excerpt, cut at a sentence end when one fits, always under the limit. Names masked first."""
    text = scrub(_HEADING.sub("", text, count=1)).strip()
    if len(text) <= limit:
        return text or None
    cut = text.rfind("。", 0, limit)
    return text[:cut + 1] if cut >= limit // 3 else text[:limit - 1] + "…"


_HEADING = re.compile(r"^\s*[0-9]+\s*【[^】]{1,30}】\s*")
_NONE_STATED = re.compile(r"(?:該当|付記|特記)(?:すべき|する)?事項は?(?:ありません|ございません|なし)。?|なし。?|[-－]")


def _sentences(text):
    return [s.strip() + "。" for s in text.split("。") if s.strip()]


def text_blocks(zip_bytes):
    """{short element name: text} for the text blocks of an XBRL-to-CSV package. Repeats are joined."""
    out = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for name in z.namelist():
            if not name.lower().endswith(".csv") or "XBRL_TO_CSV" not in name:
                continue
            for r in list(csv.reader(io.StringIO(z.read(name).decode("utf-16")), delimiter="\t"))[1:]:
                if len(r) >= 9 and "TextBlock" in r[0]:
                    short = r[0].split(":")[-1]
                    out[short] = (out[short] + " " + r[8]) if short in out else r[8]
    return out


# ---- internal control reports ------------------------------------------------------------------------------------

_COULD_NOT = re.compile(r"(?:評価結果|結論|評価)を?(?:は)?表明(?:でき|し)(?:ない|ません|ておりません)|評価結果を表明することができ"
                        r"|(?:評価結果|結論)を(?:得|出す)ことができ|重要な評価手続を?実施でき")
_NOT_EFF = re.compile(r"有効で(?:は)?(?:ない|なかった|ありません|ございません)|有効に機能していない|有効(?:である|だ)?と(?:は)?認められ(?:ない|ません)")
_EFF = re.compile(r"有効で(?:ある|あり|あった|す)|有効と(?:判断|評価|結論)")
_PRIOR = re.compile(r"前(?:事業年度|連結会計年度|期)|過年度|前々")
_WEAK_YES = re.compile(r"開示すべき重要な不備[^。]{0,40}?(?:があ|が存在(?!しな)|が認め(?!られな)|を識別|に該当|と判断)")
_WEAK_NO = re.compile(r"開示すべき重要な不備[^。]{0,12}?(?:はな|はあり|は存在しな|は識別され|は認められな|がな|が存在しな|が認められな)")
_WEAK_BODY = re.compile(r"開示すべき重要な不備の内容\s*[:：]?\s*(?:は(?:次|以下)[^。]{0,20}。)?(.{5,})")
_AMEND_AFTER = re.compile(r"[(<【\[]\s*訂正後\s*[)>】\]]")
_AMEND_BEFORE = re.compile(r"[(<【\[]\s*訂正前\s*[)>】\]]")


def _after_amendment(text):
    """Only the corrected wording of an amendment that shows before and after."""
    after = list(_AMEND_AFTER.finditer(text))
    if not after:
        return text
    piece = text[after[-1].end():]
    before = _AMEND_BEFORE.search(piece)  # some filings list 訂正後 first
    return piece[:before.start()] if before else piece


def classify_conclusion(result_text):
    """(conclusion, sentence) from the 評価結果 text. A sentence about an earlier year is not this year's conclusion."""
    sentences = _sentences(_after_amendment(_HEADING.sub("", _norm(result_text))))
    # Checked first and across the whole text: some filers say "not effective within what we assessed" and only
    # later "unable to express a result" (S100Y1NQ, S100Z5EA). The final statement is the conclusion.
    for sent in sentences:
        if _COULD_NOT.search(sent) and not _PRIOR.search(sent):
            return "could_not_conclude", sent
    for sent in sentences:
        if "内部統制" not in sent and not re.search(r"有効で|有効と", sent):
            continue
        if _PRIOR.search(sent) and not re.search(r"当(?:事業年度|連結会計年度|期)", sent):
            continue
        if _NOT_EFF.search(sent):
            return "not_effective", sent
        if _EFF.search(sent):
            return "effective", sent
    return None, None


def _weakness(result_text, conclusion):
    """(stated, quote). stated is null when the result text does not address a material weakness at all."""
    text = _after_amendment(_norm(result_text))
    current = [s for s in _sentences(text) if not (_PRIOR.search(s) and not re.search(r"当(?:事業年度|連結会計年度|期)", s))]
    if any(_WEAK_NO.search(s) for s in current) and not any(_WEAK_YES.search(s) for s in current):
        return False, None
    if not any(_WEAK_YES.search(s) for s in current) and not (conclusion == "not_effective" and "開示すべき重要な不備" in text):
        return None, None
    body = _WEAK_BODY.search(text)
    listed = re.search(r"。\s*記\s*(.{5,})", text)  # "...有効でないと判断した。 記 <what happened>"
    if listed and not body:
        return True, _quote(listed.group(1))
    if body:
        piece = re.split(r"[(（]?\s*[2２②]\s*[)）]?\s*(?:当事業年度|事業年度|是正|開示すべき重要な不備の是正)", body.group(1))[0]
        return True, _quote(piece.lstrip("」』)、。 "))
    return True, _quote(next(s for s in current if "開示すべき重要な不備" in s) if any("開示すべき重要な不備" in s for s in current) else text)


def _period(description):
    m = re.search(r"第\s*(\d+)\s*期\s*[(（]\s*(\d{4}/\d{2}/\d{2})\s*[－\-–~〜]\s*(\d{4}/\d{2}/\d{2})", description or "")
    if not m:
        return None, None, None
    return f"第{m.group(1)}期", m.group(2).replace("/", "-"), m.group(3).replace("/", "-")


def parse_internal_control(blocks, description=None, amends_doc_id=None):
    """Fields and quote of one internal control report. `blocks` is text_blocks() output."""
    result = blocks.get(_E_RESULT)
    amended = "訂正" in (description or "") or bool(amends_doc_id)
    if not result and amended:  # an amendment can carry only 訂正の内容 / a block of its own
        result = " ".join(v for k, v in blocks.items() if "Amend" in k or "訂正" in v[:200] or "評価結果" in v)
    conclusion, sentence = classify_conclusion(result or "")
    stated, weak_quote = _weakness(result or "", conclusion)
    label, start, end = _period(description)
    if not end:
        m = re.search(r"((?:令和|平成)?\s*\d{1,4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)\s*を?基準日", _norm(blocks.get(_E_SCOPE, "")))
        end = parse_jp_date(m.group(1)) if m else None
    note = _HEADING.sub("", _norm(blocks.get(_E_NOTE, "")))
    if not note or _NONE_STATED.fullmatch(note):
        note = None
    fields = {
        "conclusion": conclusion,
        "not_effective_also_stated": bool(conclusion == "could_not_conclude" and _NOT_EFF.search(_after_amendment(_norm(result or "")))),
        "material_weakness_stated": stated,
        "material_weakness_ja": weak_quote,
        "fiscal_period": label,
        "fiscal_year_start": start,
        "fiscal_year_end": end,
        "is_amendment": amended,
        "amends_doc_id": amends_doc_id,
        "supplementary_note_ja": _quote(note) if note else None,
    }
    return fields, _quote(sentence) if sentence else None


# ---- auditor changes ---------------------------------------------------------------------------------------------

_INDIVIDUAL = re.compile(r"公認会計士|^[一-龥々]{2,5}\s?[一-龥々]{1,4}$")
_FIRM_WORD = re.compile(r"監査法人|有限責任|監査事務所|Audit|LLP|LLC|PricewaterhouseCoopers|Deloitte|Ernst|KPMG|PwC|Grant Thornton|BDO|Mazars", re.I)
_SECTION = re.compile(r"[(（]\s*([1-9])\s*[)）]\s*")


def is_auditor_change(blocks):
    """Exact on the dedicated element; the ordinance clause or item title in the filing reason is the fallback."""
    if blocks.get(_E_AUDITOR):
        return True
    reason = _norm(blocks.get(_E_REASON, ""))
    return bool(re.search(r"第19条第2項第9号の4|監査公認会計士等の異動", reason))


_NUMBERING = r"(?:[(（]?\s*(?:[1-9]|[a-f]|[①-⑥]|[ア-カ])\s*[)）.．]?\s*)?"
_LABELS = {
    "names": r"(?:当該)?異動に係る(?:(?:監査)?公認会計士等|会計監査人)",
    "date": r"(?:当該)?異動の?年月日",
    "since": r"(?:退任|消滅)する(?:監査)?公認会計士等が(?:監査)?公認会計士等(?:と|に)なった年月日",
    "matters": r"(?:退任|消滅)する(?:監査)?公認会計士等が直近[3３]年間に作成した監査報告書等における意見(?:等)?(?:に関する事項)?",
    "reason": r"(?:当該)?異動の決定(?:又は|または|及び)(?:当該)?異動に至った理由及び経[緯過]|(?:当該)?異動に至った理由及び経[緯過]|(?:当該)?異動の理由及び経[緯過]",
    "opinions": r"(?:上記)?[(（]?[^。 ]{0,8}理由及び経[緯過]に対する(?:意見|監査役会等の意見)",
}


def _sections(text):
    """{label: body} cut at the ordinance's item titles. Filers number the items (1), 1., (a) or not at all, and
    some reorder them, so the titles are the anchor rather than the numbers."""
    marks = []
    pos = 0
    first = re.search(_LABELS["names"], text)
    if first:
        pos = first.start()
    for key, rx in _LABELS.items():
        m = re.search(rx, text[pos:])
        if m:
            marks.append((pos + m.start(), pos + m.end(), key))
    marks.sort()
    out = {}
    for i, (_, end, key) in enumerate(marks):
        body = text[end:marks[i + 1][0] if i + 1 < len(marks) else len(text)]
        out[key] = re.sub(_NUMBERING + r"$", "", body.strip()).strip()
    return out


def _firm(piece):
    """(firm name, is individual accountant) from the text after a 選任/退任 label."""
    piece = re.sub(r"^\s*(?:監査公認会計士等の)?(?:名称|氏名又は名称|氏名)?(?:及び所在地)?\s*[:：]?\s*", "", piece)
    piece = re.split(r"所在地|住所|[①②③④⑤]|\([2-9]\)|[(（]\s*(?:業務|注)|なお|異動の年月日", piece)[0]
    piece = re.sub(r"\s*(?:名称|氏名)\s*[:：]?\s*", " ", piece)
    piece = re.sub(r"\s*(?:[アイ][.．]?|i{1,3}[.．]?)$", "", piece).strip(" ・、,:：")
    if not piece or re.fullmatch(r"(該当事項は?(?:ありません|なし)。?|なし|-)", piece):
        return None, False
    if not _FIRM_WORD.search(piece) and _INDIVIDUAL.search(piece):
        return None, True
    if re.search(r"公認会計士(?:共同)?事務所", piece) and not re.search(r"監査法人|有限責任", piece):
        return None, True
    return piece[:80], False


def _names(section):
    incoming = re.search(r"(?:選任|就任|新任|異動後|存続)(?:する|した|予定の)?(?:監査公認会計士等|監査会計士等|公認会計士等|会計監査人)?(?:の(?:氏名又は)?名称)?(.*?)(?=(?:[②③]|[(（]?\s*[2イb]\s*[)）.．]?)?\s*(?:退任|異動前|辞任|解任|消滅)|$)", section)
    outgoing = re.search(r"(?:退任|異動前|辞任|解任|消滅)(?:する|した|予定の)?(?:監査公認会計士等|監査会計士等|公認会計士等|会計監査人)?(?:の(?:氏名又は)?名称)?(.*?)(?=(?:[②③]|[(（]?\s*[2イb]\s*[)）.．]?)?\s*(?:選任|就任|新任|異動後|存続)|$)", section)
    return (_firm(incoming.group(1)) if incoming else (None, False)), (_firm(outgoing.group(1)) if outgoing else (None, False))


_NO_DISAGREE = re.compile(r"(?:特段|特に|格別)の?(?:意見|異議|申し出|申出)?[^。]{0,12}(?:ない|無い|ありません|なし|ございません)"
                          r"|(?:意見|異議|見解の相違|意見の相違|相違)[^。]{0,8}(?:はない|は無い|はありません|はございません|なし|ない旨|無い旨|ありません)"
                          r"|妥当である|同意|了承")
_YES_DISAGREE = re.compile(r"(?:意見|見解)の(?:相違|不一致)(?:が|を)|同意でき|同意しかね|異議がある|異議を述べ|不服|認識(?:の相違|が異なる)")


_DISAGREE_DENIED = re.compile(r"(?:意見の相違が|異議を述べる)[^。]{0,12}?(?:はありません|はない|ありません|ございません)")


def _disagreement(section6, full_text):
    """True / False only on explicit wording about the outgoing auditor; otherwise null with the words kept."""
    m = re.search(r"(?:退任|消滅)する(?:(?:監査)?公認会計士等|会計監査人)の意見(.*?)(?:[②]|\(2\)|監査役|監査等委員会|監査委員会|$)", section6 or "")
    said = re.sub(r"\s*(?:[(（]|i{1,3}[.．])\s*$", "", (m.group(1) if m else section6 or "")).strip(" )）")
    stated_none = _DISAGREE_DENIED.search(said)  # "意見の相違が生じた事実はありません" is a denial, not a disagreement
    if _YES_DISAGREE.search(_DISAGREE_DENIED.sub("", said)):
        return True, said
    if said and (stated_none or _NO_DISAGREE.search(said)):
        return False, said
    return None, said or None  # no section on the outgoing auditor's view: the rest of the text says nothing about it


def parse_auditor_change(blocks_or_text):
    """(fields, quote) from the 異動 text block, or from a dict of text blocks."""
    raw = blocks_or_text.get(_E_AUDITOR, "") if isinstance(blocks_or_text, dict) else blocks_or_text
    text = _norm(raw)
    sec = _sections(text)
    (incoming, in_individual), (outgoing, out_individual) = _names(sec.get("names", ""))
    change = sec.get("date", "")
    reason_body = sec.get("reason", "")
    opinions = sec.get("opinions")
    matters_body = sec.get("matters", "")
    disagreement, said = _disagreement(opinions, text)
    fields = {
        "outgoing_auditor": outgoing,
        "incoming_auditor": incoming,
        "outgoing_is_individual_accountant": out_individual,
        "incoming_is_individual_accountant": in_individual,
        "change_date": parse_jp_date(change),
        "change_date_scheduled": True if re.search(r"予定", change + " " + reason_body) else False if change else None,
        "disagreement_stated": disagreement,
        "outgoing_auditor_statement_ja": _quote(said) if said else None,
        "prior_audit_opinion_matters_ja": _quote(matters_body) if matters_body else None,
    }
    return fields, _quote(reason_body) if reason_body else None


# ---- PDF fallback (a few filings have no CSV) ----------------------------------------------------------------------

def _pdf_text(pdf):
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(pdf)
        f.flush()
        return require_japanese(subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", f.name, "-"], capture_output=True, encoding="utf-8", check=True).stdout)


def blocks_from_pdf_text(text):
    """Text blocks of a PDF filing, split on the printed headings, so one parser serves both sources."""
    text = re.sub(r"(?<=[^\x00-\x7f]) (?=[^\x00-\x7f])", "", _norm(text))  # undo pdftotext's line wraps
    out = {}
    m = re.search(r"評価結果に関する事項】?(.*?)(?:[0-9]\s*【?\s*付記事項|$)", text)
    if m:
        out[_E_RESULT] = m.group(1)
    m = re.search(r"評価の範囲、基準日及び評価手続に関する事項(.*?)評価結果に関する事項", text)
    if m:
        out[_E_SCOPE] = m.group(1)
    m = re.search(r"付記事項】?(.*?)(?:[0-9]\s*【?\s*特記事項|$)", text)
    if m:
        out[_E_NOTE] = m.group(1)
    m = re.search(r"(当該異動に係る監査公認会計士等.*)", text)
    if m:
        out[_E_AUDITOR] = m.group(1)
    return out


# ---- ingest --------------------------------------------------------------------------------------------------------

def _row(event_type, meta, fields, quote):
    return {"type": event_type, "ticker": meta["ticker"], "doc_id": meta["doc_id"], "submit_date": meta["submit_date"],
            "edinet_code": meta["edinet_code"], "filer_name_ja": meta["filer_name_ja"], "fields": fields, "quote_ja": quote,
            "source_url": SOURCE_URL.format(meta["doc_id"])}


def _meta(r):
    return dict(zip(("doc_id", "submit_date", "edinet_code", "filer_name_ja", "description_ja", "parent_doc_id", "has_csv", "ticker"), r))


_COLS = "f.doc_id, substr(f.submit_datetime, 1, 10), f.edinet_code, f.filer_name_ja, f.description_ja, f.parent_doc_id, f.has_csv, COALESCE(i.ticker, f.ticker)"


def internal_control_candidates(con):
    return [_meta(r) for r in con.execute(
        f"SELECT {_COLS} FROM filings f LEFT JOIN issuers i ON i.edinet_code = f.edinet_code "
        "WHERE f.doc_type_code IN ('235', '236') AND f.withdrawn = 0 "
        "AND f.doc_id NOT IN (SELECT doc_id FROM governance_read WHERE status != 'failed') ORDER BY f.submit_datetime DESC")]


def auditor_candidates(con, include_agm_window=True):
    """Extraordinary reports worth opening, newest first: domestic form, listed filer, not yet read. Auditor changes are
    often filed around the shareholders' meeting, so the annual-report window is read unless the caller skips it. Funds (内国特定有価証券) and foreign filers (外国会社臨時報告書) are left out."""
    window = "" if include_agm_window else (
        "AND NOT EXISTS (SELECT 1 FROM filings a WHERE a.edinet_code = f.edinet_code AND a.doc_type_code = '120' "
        "AND julianday(substr(f.submit_datetime, 1, 10)) - julianday(substr(a.submit_datetime, 1, 10)) "
        f"BETWEEN {AGM_WINDOW_DAYS[0]} AND {AGM_WINDOW_DAYS[1]}) ")
    return [_meta(r) for r in con.execute(
        f"SELECT {_COLS} FROM filings f LEFT JOIN issuers i ON i.edinet_code = f.edinet_code "
        "WHERE f.doc_type_code = '180' AND f.withdrawn = 0 AND f.description_ja = '臨時報告書' AND COALESCE(i.ticker, f.ticker) IS NOT NULL "
        f"{window}AND f.doc_id NOT IN (SELECT doc_id FROM governance_read WHERE status != 'failed') ORDER BY f.submit_datetime DESC")]


def auditor_amendments(con):
    """訂正臨時報告書 (190) of reports already found to be auditor changes. The link is exact, so nothing is guessed."""
    return [_meta(r) for r in con.execute(
        f"SELECT {_COLS} FROM filings f LEFT JOIN issuers i ON i.edinet_code = f.edinet_code "
        "WHERE f.doc_type_code = '190' AND f.withdrawn = 0 "
        "AND f.parent_doc_id IN (SELECT doc_id FROM governance_events WHERE type = 'auditor_change') "
        "AND f.doc_id NOT IN (SELECT doc_id FROM governance_read WHERE status != 'failed') ORDER BY f.submit_datetime DESC")]


def _load_blocks(meta):
    if meta["has_csv"]:
        return text_blocks(edinet.download_csv_zip(meta["doc_id"]))
    return blocks_from_pdf_text(_pdf_text(edinet.download_pdf(meta["doc_id"])))


def _store(con, row):
    con.execute("INSERT OR REPLACE INTO governance_events VALUES (?,?,?,?,?,?)",
                (row["doc_id"], row["type"], row["ticker"], row["edinet_code"], row["submit_date"], json.dumps(row, ensure_ascii=False)))


def _mark(con, doc_id, kind, status, detail=None):
    con.execute("INSERT OR REPLACE INTO governance_read VALUES (?,?,?,?)", (doc_id, kind, status, detail))
    con.commit()


def process(con, meta, kind):
    """Read one filing. Returns the status written to governance_read."""
    blocks = _load_blocks(meta)
    if kind == "internal_control":
        fields, quote = parse_internal_control(blocks, meta["description_ja"], meta["parent_doc_id"])
        _store(con, _row("internal_control", meta, fields, quote))
        status = "parsed" if fields["conclusion"] else "unparsed"
    elif not is_auditor_change(blocks):
        status = "other_item"
    else:
        fields, quote = parse_auditor_change(blocks)
        fields["is_amendment"] = bool(meta["parent_doc_id"])
        fields["amends_doc_id"] = meta["parent_doc_id"]
        if fields["change_date"] and meta["submit_date"] and fields["change_date"] > meta["submit_date"] and not fields["change_date_scheduled"]:
            fields["warnings"] = ["future_change_date_not_marked_scheduled"]
        _store(con, _row("auditor_change", meta, fields, quote))
        status = "parsed" if (fields["outgoing_auditor"] or fields["outgoing_is_individual_accountant"]) and fields["change_date"] else "unparsed"
        if status == "parsed" and fields.get("warnings"):
            status = "partial"
    _mark(con, meta["doc_id"], kind, status)
    return status


def run(con, limit=None, kinds=("internal_control", "auditor_change"), include_agm_window=True):
    """Read filings not yet read, sequentially at EDINET's pace, resuming where it stopped. Returns status counts.
    `limit` caps documents opened in this call. Every read is cached in data/csv_cache."""
    con.executescript(SCHEMA)
    # Once: filings from 2026-11-01 were read without Japanese text (poppler-data missing) and found no
    # event; read them again. A filing that really has no event is simply recorded again.
    store.run_once(con, "reread_governance_2026_11", "DELETE FROM governance_read WHERE doc_id IN "
                   "(SELECT doc_id FROM filings WHERE submit_datetime >= '2026-11-01') "
                   "AND doc_id NOT IN (SELECT doc_id FROM governance_events)")
    todo = []
    if "internal_control" in kinds:
        todo += [(m, "internal_control") for m in internal_control_candidates(con)]
    if "auditor_change" in kinds:
        todo += [(m, "auditor_change") for m in auditor_candidates(con, include_agm_window)]
    counts = {}
    done = _sweep(con, todo[:limit], counts)
    if "auditor_change" in kinds and (limit is None or done < limit):
        rest = [(m, "auditor_change") for m in auditor_amendments(con)]
        _sweep(con, rest if limit is None else rest[:limit - done], counts)
    return counts


def _sweep(con, todo, counts):
    for meta, kind in todo:
        try:
            status = process(con, meta, kind)
        except Exception as e:  # a broken document must not stop the sweep; candidates retry 'failed' rows on the next run
            status = "failed"
            print(meta["doc_id"], "FAILED", e, flush=True)
            _mark(con, meta["doc_id"], kind, status, str(e)[:200])
        key = f"{kind}:{status}"
        counts[key] = counts.get(key, 0) + 1
    return len(todo)
