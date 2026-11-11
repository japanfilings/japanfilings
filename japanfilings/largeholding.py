"""Parse EDINET large-shareholding reports (5% rule) from PDF text.

Reads the PDF fetched through the EDINET API. Prefer the XBRL CSV (type=5)
of the same document once that parser exists.
"""
import re
import subprocess

from .textutil import nfkc, parse_jp_date, parse_number, require_japanese

# Human-readable link to the original; not used for automated downloads.
PDF_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/{doc_id}.pdf"

REPORT_KINDS = [
    ("訂正報告書", "amendment"),
    ("変更報告書", "change_report"),
    ("大量保有報告書", "initial_report"),
]

# Keyword -> purpose tag. Order matters only for readability; all matches are kept.
PURPOSE_TAGS = [
    ("重要提案行為", "may_make_proposals"),
    ("純投資", "pure_investment"),
    ("政策投資", "strategic_holding"),
    ("経営参加", "management_participation"),
    ("経営に関与", "management_participation"),
    ("支配", "control"),
    ("提案", "may_make_proposals"),
    ("株主価値", "shareholder_value_engagement"),
    ("割安", "undervaluation_thesis"),
    ("担保", "collateral"),
    ("業務提携", "business_alliance"),
    ("資本提携", "business_alliance"),
]


def pdf_to_text(pdf_path: str) -> str:
    return require_japanese(subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", pdf_path, "-"],
                                           check=True, capture_output=True, encoding="utf-8").stdout)


def _bracket_field(lines, label):
    """Value of a cover field like '【提出日】  2026年10月22日'."""
    for ln in lines:
        s = ln.strip()
        if s.startswith(f"【{label}】"):
            return nfkc(s[len(label) + 2:]) or None
    return None


def _plain_field(lines, label):
    """Value of a table row like ' 証券コード   9409'."""
    for ln in lines:
        s = ln.strip()
        if s.startswith(label):
            v = nfkc(s[len(label):])
            if v:
                return v
    return None


def _number_after(lines, idx, window=3):
    """First number on line idx (after the label) or the next `window` lines."""
    for j in range(idx, min(idx + window + 1, len(lines))):
        for tok in reversed(lines[j].split()):
            v = parse_number(tok)
            if v is not None:
                return v
    return None


def _ratios(lines):
    current = previous = None
    for i, ln in enumerate(lines):
        s = nfkc(ln)
        if "上記提出者の株券等保有割合(%)" in s:
            v = _number_after(lines, i)
            if v is not None:
                current = v  # last occurrence = summary table for joint holders
        if s.startswith("直前の報告書に記載された"):
            v = _number_after(lines, i, window=2)
            previous = v  # may legitimately be empty on initial reports
    return current, previous


def _purpose(lines):
    for i, ln in enumerate(lines):
        if "【保有目的】" in ln:
            buf = []
            for nxt in lines[i + 1:i + 12]:
                s = nxt.strip()
                if not s:
                    continue
                if s.startswith(("（３）", "(3)", "（3）")) or "EDINET提出書類" in s or re.fullmatch(r"\d+/\d+", s):
                    break
                buf.append(s)
            text = "".join(buf)
            return text or None
    return None


def _report_kind(title):
    for ja, en in REPORT_KINDS:
        if title and ja in title:
            return en
    return "unknown"


def parse_text(text: str, doc_id: str | None = None) -> dict:
    lines = text.splitlines()
    header = next((nfkc(l) for l in lines[:5] if re.search(r"\(E\d{5}\)", nfkc(l))), "")
    m = re.search(r"^(.*)\((E\d{5})\)$", header)
    title = _bracket_field(lines, "提出書類")
    current, previous = _ratios(lines)
    purpose = _purpose(lines)
    joint = _bracket_field(lines, "提出者及び共同保有者の総数(名)") or _bracket_field(lines, "提出者及び共同保有者の総数（名）")
    reason = _bracket_field(lines, "変更報告書提出事由")
    return {
        "doc_id": doc_id,
        "report_kind": _report_kind(title),
        "report_title_ja": title,
        "filer_name_ja": m.group(1).strip() if m else None,
        "filer_edinet_code": m.group(2) if m else None,
        "obligation_date": parse_jp_date(_bracket_field(lines, "報告義務発生日") or ""),
        "submit_date": parse_jp_date(_bracket_field(lines, "提出日") or ""),
        "holders_count": int(joint) if joint and joint.isdigit() else None,
        "change_reason_ja": None if reason in (None, "-", "−", "該当事項なし") else reason,
        "issuer_name_ja": _plain_field(lines, "発行者の名称"),
        "issuer_ticker": _plain_field(lines, "証券コード"),
        "exchange_ja": _plain_field(lines, "上場金融商品取引所"),
        "holding_ratio_pct": current,
        "previous_ratio_pct": previous,
        "ratio_change_pt": round(current - previous, 2) if current is not None and previous is not None else None,
        "purpose_ja": purpose,
        "purpose_tags": sorted({tag for kw, tag in PURPOSE_TAGS if purpose and kw in purpose}),
        "source_url": PDF_URL.format(doc_id=doc_id) if doc_id else None,
        "source": "EDINET (Financial Services Agency of Japan)",
    }


def record_warnings(rec):
    """Problems visible in one 5% report that the filing itself gets wrong or leaves ambiguous.  The values stay as
    filed; readers get the warning.  Codes: obligation_date_after_submit_date, obligation_date_implausibly_old (a
    first or change report whose obligation arose more than five years before it was filed; a correction may
    legitimately cite an old date), filed_by_issuer (the issuer filed on behalf of the holder, who may be an
    individual or a shareholders' association, so `filer_name_ja` is not the holder), pdf_ratio_differs_from_xbrl."""
    out = set()
    ob, sub = rec.get("obligation_date"), rec.get("submit_date")
    if ob and sub:
        if ob > sub:
            out.add("obligation_date_after_submit_date")
        elif rec.get("report_kind") != "amendment" and ob < f"{int(sub[:4]) - 5:04d}{sub[4:]}":
            out.add("obligation_date_implausibly_old")
    if rec.get("filer_edinet_code") and rec.get("filer_edinet_code") == rec.get("issuer_edinet_code"):
        out.add("filed_by_issuer")
    a, b = rec.get("pdf_ratio_pct"), rec.get("holding_ratio_pct")
    if a is not None and b is not None and abs(a - b) > 0.05:
        out.add("pdf_ratio_differs_from_xbrl")
    return sorted(out)
