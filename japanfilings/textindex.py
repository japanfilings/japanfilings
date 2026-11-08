"""Text indicators from the narrative sections of annual reports."""
import re

from .lexicon import load

SECTIONS = {  # jpcrp_cor element -> section key
    "jpcrp_cor:BusinessPolicyBusinessEnvironmentIssuesToAddressEtcTextBlock": "policy_and_issues",
    "jpcrp_cor:BusinessRisksTextBlock": "business_risks",
    "jpcrp_cor:ManagementAnalysisOfFinancialPositionOperatingResultsAndCashFlowsTextBlock": "md_and_a",
}

_LEX = load("text")
UNCERTAINTY, ECONOMY = re.compile(_LEX["uncertainty"]), re.compile(_LEX["economy"])
POLICY = {k: re.compile(v) for k, v in _LEX["policy"].items()}
POSITIVE, NEGATIVE = re.compile(_LEX["positive"]), re.compile(_LEX["negative"])

_SENT = re.compile(r"(?<=[。．！？])")
_SPACE = re.compile(r"\s+")


def sentences(text):
    text = _SPACE.sub(" ", text or "")
    return [s.strip() for s in _SENT.split(text) if len(s.strip()) > 4]


def analyze(sections: dict) -> dict:
    """`sections` maps section key -> text. Returns counts and rates; rates are per 100 sentences."""
    parts = [sentences(sections.get(k)) for k in [*SECTIONS.values(), "transcript"]]
    n = sum(map(len, parts))
    if not n:
        return {}
    unc, epu_windows, by_area, examples = 0, 0, {k: 0 for k in POLICY}, []
    for sents in parts:  # the three-sentence window stays inside one section
        for i, sent in enumerate(sents):
            if not UNCERTAINTY.search(sent):
                continue
            unc += 1
            window = "".join(sents[max(0, i - 3): i + 4])
            if not ECONOMY.search(window):
                continue
            areas = [k for k, rx in POLICY.items() if rx.search(window)]
            if not areas:
                continue
            epu_windows += 1
            for a in areas:
                by_area[a] += 1
            if len(examples) < 3:
                examples.append({"sentence_ja": sent[:200], "areas": areas})
    mdna = " ".join(sentences(sections.get("md_and_a") or sections.get("transcript")))
    pos, neg = len(POSITIVE.findall(mdna)), len(NEGATIVE.findall(mdna))
    return {
        "sentences": n,
        "uncertainty_sentences": unc,
        "uncertainty_per_100": round(100 * unc / n, 2),
        "policy_uncertainty_mentions": epu_windows,
        "policy_uncertainty_per_100": round(100 * epu_windows / n, 2),
        "policy_uncertainty_by_area": by_area,
        "results_tone": round((pos - neg) / (pos + neg), 3) if pos + neg else None,
        "results_tone_words": {"positive": pos, "negative": neg},
        "examples": examples,
    }


def from_facts(facts) -> dict:
    sections = {}
    for f in facts:
        key = SECTIONS.get(f["element"])
        if key and f["value"] not in ("", "－"):
            sections[key] = sections.get(key, "") + " " + f["value"]
    return analyze(sections)
