"""Static guardrails for the authoritative Needs You and desktop-capsule boundaries."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "harness" / "webui" / "index.html").read_text("utf-8")
SERVER = (ROOT / "harness" / "webapp.py").read_text("utf-8")


def test_needs_you_consumes_independent_paginated_total_not_default_mission_page():
    assert 'path == "/api/needs-you"' in SERVER
    assert '"items": page, "total": total, "has_more": has_more' in SERVER
    assert "Handler._needs_you_all()" in SERVER
    assert "while True:" in SERVER and "svc.missions(limit=201" in SERVER
    assert 'fetch("/api/needs-you?limit=50"' in HTML
    assert "NEEDS_YOU_TOTAL" in HTML and "NEEDS_YOU_NEXT" in HTML
    assert 'more.textContent = t("Load more decisions")' in HTML
    assert 'fetch("/api/approvals?token="' not in HTML


def test_decision_cards_explain_exact_consequences_and_avoid_generic_allow_copy():
    for field in ("reason", "impact_summary", "approve_effect", "reject_effect",
                  "payload_sha256", "nonce"):
        assert field in SERVER
    for label in ("Why Collie is asking", "Impact", "If approved", "If declined"):
        assert label in HTML
    assert 't("Approve once")' in HTML
    assert 't("Approve for this run")' in HTML
    assert 't("Decline this request")' in HTML
    assert '<button data-a="allow">Allow</button>' not in HTML


def test_capsule_group_shortcut_and_voice_privacy_are_truthful():
    assert 'class="capsule-chips" role="group"' in HTML
    assert 'summon.removeAttribute("aria-keyshortcuts")' in HTML
    for shortcut in ("Control+Shift+Space", "Meta+Shift+Space", "Alt+Space"):
        assert shortcut in HTML
    assert "COMMAND_HOST_STATUS" in HTML and "commandHostStatusText" in HTML
    assert "Registered by the desktop host" in HTML
    assert "may use cloud recognition" in HTML
    assert "Turn off Voice input to deny microphone access" in HTML


def test_unchanged_needs_poll_does_not_duplicate_render_or_drop_focus():
    refresh = HTML.split("function refreshMissionData(showLoading)", 1)[1].split(
        "function loadHomeOverview", 1)[0]
    assert 'if ($("needsList")) renderNeedsYouPage();' not in refresh
    assert "NEEDS_RENDER_SIGNATURE" in HTML
    assert "signature !== NEEDS_RENDER_SIGNATURE" in HTML
    assert 'getAttribute("data-needs-id")' in HTML
    assert 'getAttribute("data-needs-action")' in HTML
