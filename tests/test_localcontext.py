"""Device context: bounded, settings-gated, a value not a recording."""
import sys
import time

import pytest

from harness import localcontext as lc
from harness.personal_state import PersonalState


def test_chips_describe_the_moment():
    snap = {"foreground": {"app": "Chrome", "title": "Sauna - Google Chrome"},
            "selection": {"state": "ok", "text": "Sauna is the person-level intelligence layer"},
            "project": {"name": "Collie", "source": "window"}, "self": False}
    labels = [c["label"] for c in lc.chips(snap)]
    assert labels[0] == "Chrome · Sauna"
    assert "Selected text · 6 words" in labels
    assert "Project · Collie" in labels
    # while the selection read is still in flight the chip says so instead of lying
    snap["selection"] = {"state": "pending", "text": ""}
    assert any("checking" in l for l in [c["label"] for c in lc.chips(snap)])
    # Collie's own window is not context about the person
    snap["self"] = True
    assert not any(c["kind"] == "app" for c in lc.chips(snap))


def test_infer_project_prefers_the_personal_state(tmp_path):
    s = PersonalState(str(tmp_path / "p.db"))
    try:
        s.upsert_project("Collie", path="C:\\work\\collie-uiux-rebuild")
        fg = {"title": "index.html - collie-uiux-rebuild - Visual Studio Code"}
        p = lc.infer_project(fg, state=s)
        assert p["name"] == "Collie" and p["source"] == "window"
        p2 = lc.infer_project({"title": "README.md - some-repo - Visual Studio Code"}, state=s)
        assert p2["name"] == "some-repo"
        p3 = lc.infer_project(None, cwd=str(tmp_path), state=s)
        assert p3 and p3["source"] == "cwd"
    finally:
        s.close()


def test_snapshot_with_everything_off_is_still_a_value():
    snap = lc.snapshot(active_window=False, selection_text=False, clipboard_text=False, browser_tab=False)
    assert snap["foreground"] is None and snap["selection"] is None and snap["clipboard"] is None
    assert "at" in snap and "platform" in snap


def test_selection_read_is_cached_per_window(monkeypatch):
    calls = []

    def slow_selection(fg):
        calls.append(fg)
        time.sleep(0.3)
        return "picked text"

    monkeypatch.setattr(lc, "selection", slow_selection)
    lc._SEL_CACHE.clear()
    fg = {"hwnd": 4242, "pid": 1, "title": "t"}
    first = lc._selection_with_timeout(fg, wait=0.05)
    assert first["state"] == "pending"
    second = lc._selection_with_timeout(fg, wait=1.0)
    assert second["state"] == "ok" and second["text"] == "picked text"
    assert len(calls) == 1, "the same window must reuse the in-flight read"


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows foreground read")
def test_windows_foreground_reads_something():
    fg = lc.foreground()
    # a headless CI session may have no foreground window at all; when there is one it is described
    if fg is not None:
        assert "app" in fg and "title" in fg and fg["hwnd"]


def test_browser_suffix_is_stripped():
    assert lc._strip_browser_suffix("Sauna - Google Chrome") == "Sauna"
    assert lc._strip_browser_suffix("Plain title") == "Plain title"
