"""Static UI contracts for the Settings Memory inspector."""

from pathlib import Path


HTML = (Path(__file__).resolve().parents[1] / "harness" / "webui" / "index.html").read_text(encoding="utf-8")


def _memory_code():
    return HTML.split("// ---- reviewable Memory profile", 1)[1].split("function workIdentityRender", 1)[0]


def test_memory_panel_exposes_trusted_profile_and_quarantined_proposals():
    assert 'id="memoryInspector" aria-labelledby="memoryInspectorTitle"' in HTML
    assert 'id="memoryProfile" aria-busy="true"' in HTML
    code = _memory_code()
    assert 'memorySnapshot.profile' in code
    assert 'row.kind === "preference"' in code
    assert 'row.kind === "habit"' in code
    assert 'row.status === "proposed"' in code
    assert '"Confirmed preferences"' in code
    assert '"Verified habits"' in code
    assert '"Pending proposals"' in code
    assert '"Proposals never steer Collie until you confirm them."' in code


def test_memory_actions_use_authenticated_lifecycle_endpoints_only():
    code = _memory_code()
    assert 'fetch("/api/memory?token=" + encodeURIComponent(CT)' in code
    assert 'memoryPost("/api/memory/preference"' in code
    assert 'memoryPost("/api/memory/review"' in code
    for action in ('"attest"', '"reject"', '"invalidate"'):
        assert action in code
    assert 'data-memory-action' in code
    assert 'window.confirm(t("Forget this memory?"))' in code
    assert "/api/route" not in code
    assert "readRunConfig" not in code
    assert "send()" not in code


def test_memory_server_values_are_rendered_as_text_and_controls_are_mobile_sized():
    code = _memory_code()
    assert "name.textContent = row.attribute" in code
    assert "value.textContent = memoryValueText(row)" in code
    assert "meta.textContent = memoryMetaText(row, project)" in code
    assert ".memory-action,.memory-refresh,.memory-save { min-height:44px; }" in HTML
    assert ".memory-form { grid-template-columns:1fr; }" in HTML


def test_memory_copy_is_localized_for_simplified_and_traditional_chinese():
    for key in (
        '"Memory you control"',
        '"Confirmed preferences"',
        '"Verified habits"',
        '"Pending proposals"',
        '"Forget this memory?"',
    ):
        assert HTML.count(key) >= 3, key
