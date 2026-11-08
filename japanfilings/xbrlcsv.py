"""Read EDINET's XBRL-to-CSV package (API type=5): UTF-16 tab-separated files.

Columns: 要素ID, 項目名, コンテキストID, 相対年度, 連結・個別, 期間・時点, ユニットID, 単位, 値.
Facts are looked up by Japanese label (項目名) so the code does not depend on taxonomy
element IDs. Labels for large-holding reports are not yet verified against real files.
"""
import csv
import io
import re
import zipfile

from .textutil import nfkc, parse_number


def read_facts(zip_bytes: bytes) -> list[dict]:
    facts = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for name in z.namelist():
            if not name.lower().endswith(".csv") or "XBRL_TO_CSV" not in name:
                continue
            text = z.read(name).decode("utf-16")
            rows = list(csv.reader(io.StringIO(text), delimiter="\t"))
            for r in rows[1:]:
                if len(r) >= 9:
                    facts.append({"element": r[0], "label": nfkc(r[1]), "context": r[2], "unit": r[6], "value": r[8]})
    return facts


def _pct(v):
    n = parse_number(v)
    if n is None:
        return None
    # Ratios are often stored as a fraction (0.0699) rather than a percentage.
    return round(n * 100, 2) if n <= 1 else round(n, 2)


_HOLDER_FIELDS = {  # jplvh_cor element -> output field. Addresses, birthdates and phone numbers are left out.
    "jplvh_cor:Name": "name",
    "jplvh_cor:IndividualOrCorporation": "holder_type_ja",
    "jplvh_cor:HoldingRatioOfShareCertificatesEtc": "ratio_pct",
    "jplvh_cor:HoldingRatioOfShareCertificatesEtcPerLastReport": "previous_ratio_pct",
    "jplvh_cor:TotalNumberOfStocksEtcHeld": "shares",
    "jplvh_cor:PurposeOfHolding": "purpose_ja",
    "jplvh_cor:ActOfMakingImportantProposalEtc": "important_proposal_ja",
    "jplvh_cor:TotalAmountOfBorrowings": "borrowings_jpy",
    "jplvh_cor:AmountOfOwnFund": "own_funds_jpy",
}
_HOLDER_NUMERIC = {"shares", "borrowings_jpy", "own_funds_jpy"}

# Per-holder context suffixes.  Submitters are '...FilerLargeVolumeHolderNMember'; joint holders
# listed under a submitter are '...JointHolderNMember' (S100VPZJ: Toyota 22.97% as filer, Nikkon
# Holdings 67.68% as JointHolder1; 1,449 of 73,149 cached 5% CSVs use JointHolder).  Matching only
# 'LargeVolumeHolder' dropped every joint holder.
_HOLDER_CONTEXT = re.compile(r"((?:FilerLargeVolumeHolder|JointHolder)\d+Member)$")


def holder_member(context: str) -> str | None:
    """'FilerLargeVolumeHolder1Member' / 'JointHolder2Member' for a per-holder context, else None."""
    m = _HOLDER_CONTEXT.search(context)
    return m.group(1) if m else None


def _holder_order(member: str):
    """Filers first, then joint holders, each by number."""
    return (member.startswith("Joint"), int(re.search(r"(\d+)Member$", member).group(1)))


def large_holding_holders(facts) -> dict:
    """Per-holder rows of a 5% report plus the filer's English name."""
    rows = {}
    filer_names = {}
    for f in facts:
        if f["element"] == "jplvh_cor:FilerNameInEnglishDEI" and f["value"] not in ("", "－"):
            # Joint filings repeat this DEI fact once per holder.  Holder 1 is
            # the submitter; taking the last fact mislabeled the final joint
            # holder as the filer.
            # Joint holders repeat it too; they are never the filer.
            m = re.search(r"FilerLargeVolumeHolder(\d+)Member$", f["context"])
            if m or not holder_member(f["context"]):
                filer_names[int(m.group(1)) if m else 0] = f["value"]
        member = holder_member(f["context"])
        field = _HOLDER_FIELDS.get(f["element"])
        if not member or not field or f["value"] in ("", "－", "-"):
            continue
        row = rows.setdefault(member, {"holder_role": "joint_holder" if member.startswith("Joint") else "filer"})
        if field.endswith("ratio_pct"):
            row[field] = _pct(f["value"])
        elif field in _HOLDER_NUMERIC:
            row[field] = parse_number(f["value"])
        else:
            row[field] = nfkc(f["value"])
    # Stable sort: equal ratios keep filing order (filers, then joint holders).
    holders = sorted((rows[k] for k in sorted(rows, key=_holder_order)), key=lambda r: -(r.get("ratio_pct") or 0))
    for h in holders:
        t = h.get("holder_type_ja", "")
        h["holder_type"] = "individual" if t.startswith("個人") else "corporation" if t.startswith("法人") else None
    filer_en = filer_names[min(filer_names)] if filer_names else None
    return {"filer_name_en": filer_en, "holders": holders}


def large_holding_ratios(facts) -> dict:
    """Total holding ratio and the previous report's ratio. Per-holder facts carry a
    '...FilerLargeVolumeHolderNMember' or '...JointHolderNMember' context; the joint total has a context without 'Member'
    (verified on S100Z59N: total 38.23% vs. holders 30.33/2.08/0.67/...). Some filings have no
    total context (S100Z90M, ten holders): then the total is the sum of the holders' ratios."""
    totals = {"current": None, "previous": None}
    members = {"current": {}, "previous": {}}
    for f in facts:
        label = f["label"]
        if "テキストブロック" in label:
            continue
        if "直前の報告書に記載された" in label and "保有割合" in label:
            key = "previous"
        elif "株券等保有割合" in label:
            key = "current"
        else:
            continue
        v = _pct(f["value"])
        if v is None:
            continue
        member = holder_member(f["context"])
        if member:
            members[key][member] = v  # repeated tables carry the same value per holder
        elif "Member" not in f["context"]:
            totals[key] = v

    def pick(key):
        if totals[key] is not None:
            return totals[key]
        return round(sum(members[key].values()), 2) if members[key] else None

    return {"holding_ratio_pct": pick("current"), "previous_ratio_pct": pick("previous")}
