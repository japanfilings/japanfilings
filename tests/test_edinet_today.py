from datetime import datetime, timedelta, timezone

import pytest

from japanfilings import edinet


def _not_found(*a, **k):
    raise edinet.EdinetError({"metadata": {"status": "404", "message": "Not Found"}})


def test_no_list_yet_for_today_is_empty(monkeypatch):
    monkeypatch.setattr(edinet, "_get", _not_found)
    today = datetime.now(timezone(timedelta(hours=9))).date()
    assert edinet.documents(today.isoformat()) == []
    assert edinet.documents((today + timedelta(days=1)).isoformat()) == []


def test_not_found_for_a_past_day_still_fails(monkeypatch):
    monkeypatch.setattr(edinet, "_get", _not_found)
    with pytest.raises(edinet.EdinetError):
        edinet.documents("2026-11-10")


def test_other_errors_for_today_still_fail(monkeypatch):
    def bad(*a, **k):
        raise edinet.EdinetError({"metadata": {"status": "401"}})
    monkeypatch.setattr(edinet, "_get", bad)
    with pytest.raises(edinet.EdinetError):
        edinet.documents(datetime.now(timezone(timedelta(hours=9))).date().isoformat())
