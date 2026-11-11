"""Credit ratings as companies state them in bond filings on EDINET (発行登録追補書類, docTypeCode 100).

Filings for a rated bond name each agency, the rating and the date obtained. We record that statement with its
source document. We do not read agencies' websites, do not rate anything ourselves, and a row says only what
the filing said on its date; agencies can change or withdraw a rating at any time.
"""
import json
import re
import subprocess
import tempfile
import unicodedata

from . import edinet, store
from .textutil import parse_jp_date, require_japanese

SCHEMA = """
CREATE TABLE IF NOT EXISTS disclosed_ratings (
  doc_id TEXT,
  agency TEXT,
  rating TEXT,
  rating_date TEXT,
  submit_date TEXT,
  edinet_code TEXT,
  ticker TEXT,
  record TEXT NOT NULL,
  PRIMARY KEY (doc_id, agency, rating)
);
CREATE INDEX IF NOT EXISTS dr_ticker ON disclosed_ratings(ticker, submit_date);
"""
AGENCIES = {"R&I": r"格付投資情報センター|R&I", "JCR": r"日本格付研究所|JCR", "Moody's": r"ムーディーズ|Moody", "S&P": r"S&P|スタンダード", "Fitch": r"フィッチ|Fitch"}
_RATING = r"(?<![A-Za-z])(AAA|AA|A|BBB|BB|B|CCC|CC|C|Aaa|Aa|Baa|Ba|Caa)\s*([+\-1-3])?(?![A-Za-z0-9])"
_HEAD = re.compile(r"\(\s*\d\s*\)\s*([^\n]{0,40}?)(?:\(以下|\n)")
_NEG = re.compile(r"取得していない|取得しておりません|取得の予定はありません|付与されていない")
_BOND = re.compile(r"第\s*\d+\s*回[^\n]{0,50}?(?:社債|債券)")


def _normal(text):
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"(?<=[A-Za-z])[ー]", "-", text)  # 「AAー」; 長音の「ムーディーズ」は変えない
    return re.sub(r"[ \t]+", " ", text.replace("−", "-").replace("‐", "-"))


def parse_text(text):
    """[{agency, rating, rating_date}] from the rating note; [] when the filing states none."""
    text = _normal(text)
    start = re.search(r"信用格付業者から(?:提供|取得)|本社債について[^\n]{0,30}信用格付", text)
    if not start:
        return []
    note = re.sub(r"\s*\n\s*", "", text[start.start():start.start() + 6000])  # sentences wrap, even inside 信用格付
    out, seen = [], set()
    # One agency, written as a sentence: "...(以下「R&I」という。)からA+(シングルAプラス)の信用格付を2026年10月22日付で取得".
    for m in re.finditer(r"(?:から|より)\s*" + _RATING + r"\s*(?:\([^)]{0,20}\))?\s*の\s*信用格付を?\s*([^\n。]{0,25}?\d{1,2}\s*日)?", note):
        if re.search(r"予定|取得していない|取得しておりません", note[m.end():m.end() + 30]):
            continue  # a plan or a denial is not a rating obtained
        before = note[max(0, m.start() - 70):m.start()]
        hits = [(x.start(), a) for a, rx in AGENCIES.items() for x in re.finditer(rx, before)]
        if hits:
            agency, rating = max(hits)[1], m.group(1) + (m.group(2) or "")
            if (agency, rating) not in seen:
                seen.add((agency, rating))
                out.append({"agency": agency, "rating": rating, "rating_date": parse_jp_date(re.sub(r"\s", "", m.group(3) or ""))})
    # One agency, as a list: "株式会社格付投資情報センター(以下「R&I」という。)信用格付:A+(シングルAプラス)(取得日 2026年1月16日)".
    for m in re.finditer(r"信用格付\s*[::]\s*" + _RATING + r"[^。]{0,40}?取得日\s*([^)]{0,20}日)", note):
        before = note[max(0, m.start() - 70):m.start()]
        hits = [(before.rfind(x.group(0)), a) for a, rx in AGENCIES.items() for x in re.finditer(rx, before)]
        if hits:
            agency, rating = max(hits)[1], m.group(1) + (m.group(2) or "")
            if (agency, rating) not in seen:
                seen.add((agency, rating))
                out.append({"agency": agency, "rating": rating, "rating_date": parse_jp_date(re.sub(r"\s", "", m.group(3)))})
    pieces = re.split(r"(?=\(\s*\d\s*\)\s*(?:株式会社|Moody|ムーディーズ|S&P|スタンダード|フィッチ|Fitch|R&I|JCR))", note)
    for piece in pieces[1:]:
        head = piece[:60]
        named = [(x.start(), a) for a, rx in AGENCIES.items() for x in [re.search(rx, head)] if x]
        agency = min(named)[1] if named else None
        if not agency:
            continue
        # "R&IからAA-の信用格付を2026年10月23日付で取得", or "信用格付 AA(取得日 2026年10月23日)".
        m = re.search(_RATING + r"[^\n。]{0,30}?(?:の信用格付|\(|（)", piece[:500]) or re.search(r"信用格付\s*[::]?\s*" + _RATING, piece[:500])
        if not m:
            continue
        rating = m.group(1) + (m.group(2) or "")
        date = parse_jp_date(re.sub(r"\s", "", piece[m.start():m.start() + 120]))
        if (agency, rating) not in seen:
            seen.add((agency, rating))
            out.append({"agency": agency, "rating": rating, "rating_date": date})
    if not out and _NEG.search(note[:400]):
        return []
    return out


def _pdf_text(pdf):
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(pdf)
        f.flush()
        return require_japanese(subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", f.name, "-"], capture_output=True, encoding="utf-8", check=True).stdout)


def run(con, limit=None):
    """Read every bond supplement not yet read. Returns (documents read, ratings stored)."""
    con.executescript(SCHEMA)
    con.execute("CREATE TABLE IF NOT EXISTS ratings_read (doc_id TEXT PRIMARY KEY, found INTEGER)")
    # Once: supplements filed from 2026-11-01 were read without Japanese text and found nothing; read them again.
    store.run_once(con, "reread_ratings_2026_11", "DELETE FROM ratings_read WHERE found = 0 AND doc_id IN "
                   "(SELECT doc_id FROM filings WHERE doc_type_code = '100' AND submit_datetime >= '2026-11-01')")
    rows = con.execute(
        "SELECT f.doc_id, f.submit_datetime, f.edinet_code, f.description_ja, f.filer_name_ja, i.ticker FROM filings f LEFT JOIN issuers i ON i.edinet_code = f.edinet_code "
        "WHERE f.doc_type_code = '100' AND f.withdrawn = 0 AND f.doc_id NOT IN (SELECT doc_id FROM ratings_read) ORDER BY f.submit_datetime DESC").fetchall()
    read = stored = 0
    for doc_id, submitted, code, desc, name, ticker in rows[:limit]:
        try:
            text = _pdf_text(edinet.download_pdf(doc_id))
        except Exception as e:
            print(doc_id, "FAILED", e, flush=True)
            continue
        found = parse_text(text)
        bond = _BOND.search(_normal(text))
        for r in found:
            rec = {**r, "doc_id": doc_id, "submit_date": submitted[:10], "edinet_code": code, "ticker": ticker, "issuer_ja": name,
                   "bond_ja": re.sub(r"\s+", "", bond.group(0)) if bond else None,
                   "source_url": f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{doc_id}"}
            con.execute("INSERT OR REPLACE INTO disclosed_ratings VALUES (?,?,?,?,?,?,?,?)",
                        (doc_id, r["agency"], r["rating"], r["rating_date"], submitted[:10], code, ticker, json.dumps(rec, ensure_ascii=False)))
            stored += 1
        con.execute("INSERT OR REPLACE INTO ratings_read VALUES (?, ?)", (doc_id, len(found)))
        con.commit()
        read += 1
    return read, stored
