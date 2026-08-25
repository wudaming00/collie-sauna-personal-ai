from pathlib import Path

from harness import catalog, settings
from harness.providers import provider_default_model


ROOT = Path(__file__).resolve().parents[1]


def test_web_settings_exposes_official_agent_sdk_provider():
    provider = next(row for row in settings.SCHEMA if row["key"] == "PROVIDER")
    options = {
        option["value"]: option
        for option in provider["options"] if isinstance(option, dict)
    }

    assert "claude-agent-sdk" in options
    assert "Claude Agent SDK" in options["claude-agent-sdk"]["label"]
    assert "anthropic-oauth" not in options
    assert provider_default_model("claude-agent-sdk") == "claude-opus-5"


def test_model_catalog_offers_opus_through_official_agent_sdk(monkeypatch):
    monkeypatch.setattr(catalog.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(catalog.shutil, "which", lambda name: "C:/bin/claude.exe")

    entries = {entry["id"]: entry for entry in catalog.list_entries(discover_live=False)}
    entry = entries["claude-agent-sdk:claude-opus-5"]

    assert entry["auth"] == "ok"
    assert entry["kind"] == "subscription"
    assert entry["via"] == "Official Agent SDK · Collie tools"
    assert "overnight" in entry["tags"]


def test_web_onboarding_uses_the_sdk_provider_default():
    page = (ROOT / "harness/webui/index.html").read_text(encoding="utf-8")

    assert '"claude-agent-sdk": "claude-opus-5"' in page
    assert '"claude-agent-sdk": "Claude Agent SDK · Collie tools"' in page
    assert '"claude-cli": "Claude Code · official CLI"' in page
    assert "Claude direct" not in page
