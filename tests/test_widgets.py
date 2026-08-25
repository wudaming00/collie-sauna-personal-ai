"""The widget manifest is the trust boundary: it is a file a person may have been handed.

Everything here is about what a manifest is NOT allowed to do. The rendering side has no eval and
builds no markup, so the remaining risk is all in what gets accepted and what gets fetched — which
is exactly what these assert.
"""
import json
import os

import pytest

from harness import widgets


@pytest.fixture()
def wdir(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    d = tmp_path / "widgets"
    d.mkdir()
    return d


def write(d, name, obj):
    (d / name).write_text(json.dumps(obj), encoding="utf-8")


GOOD = {"id": "stocks", "title": "Stocks", "each": ["AAPL"],
        "url": "https://example.invalid/{item}", "label": "{item}", "value": "a.b"}


def test_a_well_formed_manifest_is_accepted(wdir):
    write(wdir, "stocks.json", GOOD)
    got = widgets.custom()
    assert [w["id"] for w in got] == ["stocks"]
    assert got[0]["title"] == "Stocks"


def test_plain_http_is_refused(wdir):
    write(wdir, "x.json", dict(GOOD, id="x", url="http://example.invalid/a"))
    assert widgets.custom() == []


def test_a_manifest_cannot_impersonate_a_builtin(wdir):
    # otherwise dropping a file called clock.json silently replaces the real clock
    write(wdir, "clock.json", dict(GOOD, id="clock"))
    assert widgets.custom() == []


def test_a_manifest_with_nothing_to_draw_is_refused(wdir):
    write(wdir, "empty.json", {"id": "empty", "url": "https://example.invalid/"})
    assert widgets.custom() == []


def test_a_broken_file_does_not_take_the_others_down(wdir):
    (wdir / "bad.json").write_text("{not json", encoding="utf-8")
    write(wdir, "stocks.json", GOOD)
    assert [w["id"] for w in widgets.custom()] == ["stocks"]


def test_ids_are_constrained(wdir):
    write(wdir, "a.json", dict(GOOD, id="../../etc/passwd"))
    write(wdir, "b.json", dict(GOOD, id="Has Spaces"))
    assert widgets.custom() == []


def test_refresh_is_clamped(wdir):
    write(wdir, "fast.json", dict(GOOD, id="fast", refresh=0.001))
    write(wdir, "slow.json", dict(GOOD, id="slow", refresh=10 ** 9))
    got = {w["id"]: w["refresh"] for w in widgets.custom()}
    assert got["fast"] == widgets.TTL_MIN and got["slow"] == widgets.TTL_MAX


def test_each_is_bounded(wdir):
    write(wdir, "many.json", dict(GOOD, id="many", each=[str(i) for i in range(50)]))
    assert len(widgets.custom()[0]["each"]) <= 8


def test_catalog_merges_placement_and_never_loses_an_installed_widget(wdir):
    write(wdir, "stocks.json", GOOD)
    cat = widgets.catalog({"widgets": {"clock": {"on": True, "slot": "tr"},
                                       "stocks": {"on": True, "slot": "bl"}}})
    by_id = {w["id"]: w for w in cat}
    assert by_id["clock"]["kind"] == "builtin" and by_id["clock"]["on"] is True
    assert by_id["stocks"]["kind"] == "custom" and by_id["stocks"]["slot"] == "bl"
    assert by_id["brand"]["on"] is False               # absent from the config means not placed


def test_unknown_widget_reports_rather_than_raises(wdir):
    out = widgets.data("nope")
    assert out["ok"] is False and "unknown" in out["error"]


def test_a_dead_url_degrades_instead_of_throwing(wdir):
    write(wdir, "dead.json", dict(GOOD, id="dead", url="https://127.0.0.1:1/{item}"))
    out = widgets.data("dead")
    assert out["ok"] is False and out["rows"] == [] and out["title"] == "Stocks"


def test_paths_walk_dicts_and_lists_and_stop_at_a_wrong_turn():
    doc = {"chart": {"result": [{"meta": {"price": 3.5}}]}}
    assert widgets._dig(doc, "chart.result.0.meta.price") == 3.5
    assert widgets._dig(doc, "chart.result.9.meta.price") is None
    assert widgets._dig(doc, "chart.nope.price") is None
    assert widgets._dig(doc, "chart.result.0.meta.price.deeper") is None


def test_numbers_are_formatted_for_reading():
    assert widgets._fmt(3.14159) == "3.14"
    assert widgets._fmt(1234567.0) == "1,234,567"
    assert widgets._fmt("AAPL") == "AAPL"
