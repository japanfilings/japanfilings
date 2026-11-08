"""Normalization helpers for Japanese disclosure text."""
import re
import unicodedata
from datetime import date

# Japanese era start years (year 1 of each era).
_ERAS = {"令和": 2019, "平成": 1989, "昭和": 1926}

_DATE_RE = re.compile(r"(令和|平成|昭和)?\s*(\d{1,4}|元)\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_NUM_RE = re.compile(r"^-?[\d,]+(?:\.\d+)?$")


def nfkc(s: str) -> str:
    """Fold full-width digits/letters to ASCII and trim."""
    return unicodedata.normalize("NFKC", s).strip()


def parse_jp_date(s: str) -> str | None:
    """Return ISO date for '2026年10月21日' or '令和8年10月15日'; None otherwise."""
    m = _DATE_RE.search(nfkc(s))
    if not m:
        return None
    era, year, month, day = m.groups()
    y = 1 if year == "元" else int(year)
    if era:
        y = _ERAS[era] + y - 1
    try:
        return date(y, int(month), int(day)).isoformat()
    except ValueError:
        return None


def parse_number(s: str) -> float | None:
    s = nfkc(s)
    if not _NUM_RE.match(s):
        return None
    return float(s.replace(",", ""))
