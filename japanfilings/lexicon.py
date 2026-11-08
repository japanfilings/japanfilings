"""Word lists kept out of the repository. CI writes them from the LEXICON_JSON secret; see docs/DEPLOY.md."""
import json
import os

_PATH = os.path.join(os.path.dirname(__file__), "private", "lexicon.json")


def load(section):
    raw = os.environ.get("LEXICON_JSON")
    if raw:
        return json.loads(raw)[section]
    if not os.path.exists(_PATH):
        raise RuntimeError(f"Word lists not found: set LEXICON_JSON or create {_PATH}")
    with open(_PATH, encoding="utf-8") as f:
        return json.load(f)[section]
