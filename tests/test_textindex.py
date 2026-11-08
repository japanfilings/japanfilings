"""Policy-uncertainty windows and results tone on hand-written Japanese filing text."""
from japanfilings import textindex


def test_policy_uncertainty_needs_economy_and_policy_words_nearby():
    risks = ("米国の関税政策の動向により世界経済の先行きは不透明です。当社は輸出比率が高くなっています。"
             "原材料価格の動向は不透明です。")
    out = textindex.analyze({"business_risks": risks})
    assert out["uncertainty_sentences"] == 2
    # both uncertainty sentences sit inside one window with 経済 and 関税, so both count, attributed to trade
    assert out["policy_uncertainty_mentions"] == 2 and out["policy_uncertainty_by_area"]["trade"] == 2


def test_uncertainty_without_policy_words_is_not_policy_uncertainty():
    out = textindex.analyze({"business_risks": "原材料価格の動向は不透明です。需要は回復しています。"})
    assert out["uncertainty_sentences"] == 1 and out["policy_uncertainty_mentions"] == 0


def test_results_tone():
    out = textindex.analyze({"md_and_a": "売上高は増収となり、営業利益は過去最高となりました。海外事業は低迷しました。"})
    assert out["results_tone_words"] == {"positive": 2, "negative": 1}
    assert out["results_tone"] == round(1 / 3, 3)
