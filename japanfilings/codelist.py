"""EDINET code list: the issuer universe (no API key required)."""
import csv
import html
import io
import re
import urllib.request
import zipfile

from .textutil import nfkc

CODELIST_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"


def download(url: str = CODELIST_URL) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()


def parse(zip_bytes: bytes) -> list[dict]:
    """Parse EdinetcodeDlInfo.csv (CP932, one metadata row then a header row)."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        text = z.read(name).decode("cp932")
    rows = list(csv.reader(io.StringIO(text)))
    out = []
    for r in rows[2:]:
        if len(r) < 13:
            continue
        sec = nfkc(r[11])
        out.append({
            "edinet_code": r[0],
            "filer_type": r[1],
            "listed": r[2] == "上場",
            "consolidated": r[3] == "有",
            "capital_million_jpy": float(r[4]) if r[4] else None,
            "fiscal_year_end": r[5],
            "name_ja": r[6],
            "name_en": r[7] or None,
            "name_kana": r[8],
            "industry_ja": r[10],
            # EDINET stores a 5-char code (4-char ticker + check digit '0').
            "ticker": sec[:4] if sec else None,
            "corporate_number": r[12] or None,
        })
    return out


# ---------- English names the code list leaves blank ----------
# EDINET has no English name for some listed issuers (403 of 3,812 on 2026-11-05), so English summaries fell back to
# the Japanese name. Two on-disk sources hold one the issuer itself or the market uses; nothing is translated here:
#   1. the English company name on the cover of the issuer's own latest annual report (CompanyNameInEnglishCoverPage);
#   2. the OpenFIGI security name (`identifiers.name_figi`, upper case and sometimes cut at 28 characters).
_FORMER = re.compile(r"\s*[(（]\s*(?:旧英訳名|旧商号|注).*$|\s*\(注\).*$")
_JA = re.compile(r"[぀-ヿ㐀-鿿ｦ-ﾟ]")


def clean_english_name(raw):
    """The cover-page name without notes such as "(旧英訳名 ...)(注)2025年...変更しました。"; None unless plain ASCII."""
    if not raw:
        return None
    s = nfkc(html.unescape(raw)).strip()
    s = _FORMER.sub("", s).strip(" ,;")
    s = re.sub(r"\s+", " ", s)
    if not s or _JA.search(s) or not s.isascii() or not re.search(r"[A-Za-z]", s):
        return None  # e.g. Cyrillic look-alike letters (6183 "HОLDINGS") or "SUN・LIFE": left to the next source
    return s


def english_name_fills(con):
    """[(edinet_code, name_en, source)] for issuers with a ticker and no English name. Read-only."""
    out = []
    rows = con.execute(
        "SELECT i.edinet_code, x.name_figi, (SELECT json_extract(a.record, '$.company.english_name') FROM annual_reports a "
        "  WHERE a.edinet_code = i.edinet_code AND json_extract(a.record, '$.company.english_name') IS NOT NULL "
        "  ORDER BY a.fiscal_year_end DESC, a.submit_date DESC LIMIT 1) "
        "FROM issuers i LEFT JOIN identifiers x ON x.ticker = i.ticker "
        "WHERE i.ticker IS NOT NULL AND (i.name_en IS NULL OR TRIM(i.name_en) = '')").fetchall()
    for code, figi_name, cover in rows:
        name = clean_english_name(cover)
        if name:
            out.append((code, name, "annual_report_cover"))
        elif figi_name and (fig := clean_english_name(figi_name)):
            out.append((code, fig, "openfigi"))
    return out


def fill_missing_name_en(con):
    """Fill blank issuers.name_en from english_name_fills(). Idempotent: only blank names are written, and the code
    list import (store.upsert_issuers) blanks them again, so run this after every `codelist`. Returns the count."""
    fills = english_name_fills(con)
    with con:
        con.executemany("UPDATE issuers SET name_en = ? WHERE edinet_code = ? AND (name_en IS NULL OR TRIM(name_en) = '')",
                        [(name, code) for code, name, _src in fills])
    return len(fills)


if __name__ == "__main__":  # python -m japanfilings.codelist fill-name-en [--dry-run]
    import collections
    import sys

    from . import store
    if sys.argv[1:2] != ["fill-name-en"]:
        sys.exit("usage: python -m japanfilings.codelist fill-name-en [--dry-run]")
    con = store.connect()
    if "--dry-run" in sys.argv:
        fills = english_name_fills(con)
        print(len(fills), dict(collections.Counter(s for _c, _n, s in fills)))
    else:
        print("filled", fill_missing_name_en(con))
