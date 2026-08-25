"""The Sauna-shaped Memory cards remain projections over Collie's trust model."""
import os

from harness.memory import SqliteMemory
from harness.memory_cards import MemoryCardProjector
from harness.personal_state import PersonalState


def test_cards_include_only_live_trusted_claims_and_keep_trust_metadata(tmp_path):
    state = PersonalState(str(tmp_path / "personal.db"))
    memory = SqliteMemory(str(tmp_path / "memory.db"))
    try:
        preference = memory.set_preference("editor", "VS Code")
        rule = memory.propose("Always run tests before release", project="global",
                              kind="procedure", subject="project", confidence=0.8)
        assert memory.promote(rule, status="verified", source="local_review")
        memory.propose("The owner might like red", project="global",
                       kind="preference", subject="owner")
        memory.remember("Unmapped project observation", project="global",
                        kind="observation", subject="project")
        state.add_person("Jordan", role="Product Lead", org="Sauna", notes="Product contact")
        state.record_activity("note_added", "Captured architecture notes", actor="user")

        out = MemoryCardProjector(state, memory, output_dir=str(tmp_path / "cards")).render()

        preferences = out["cards"]["user_preferences"]["text"]
        rules = out["cards"]["rules"]["text"]
        relationships = out["cards"]["user_relationships"]["text"]
        recent = out["cards"]["recent_activity"]["text"]
        assert "editor = VS Code" in preferences
        assert "The owner might like red" not in preferences
        assert "Always run tests before release" in rules
        assert "verified" in rules and "claim `%s`" % rule in rules
        assert "Jordan" in relationships and "Product contact" in relationships
        assert "Captured architecture notes" in recent
        assert out["stats"] == {"considered": 4, "included_claims": 2, "untrusted": 1,
                                "inactive": 0, "unclassified": 1}
        assert out["cards"]["user_preferences"]["count"] == 1
        assert out["cards"]["rules"]["count"] == 1
        assert set(out["cards"]) == {
            "user_preferences", "rules", "user_profile", "your_tools",
            "assistant_identity", "user_relationships", "recent_activity",
        }
        for card in out["cards"].values():
            assert os.path.isfile(card["path"])
            assert "Read-only projection" in card["text"]
        assert memory.get_claim(preference)["status"] == "attested"
    finally:
        memory.close()
        state.close()


def test_superseded_and_expired_claims_do_not_reappear_in_cards(tmp_path):
    state = PersonalState(str(tmp_path / "personal.db"))
    memory = SqliteMemory(str(tmp_path / "memory.db"))
    try:
        old = memory.set_preference("theme", "light")
        memory.set_preference("theme", "dark")
        memory.remember("Temporary preference", project="global", kind="preference",
                        subject="owner", expires_at=1)
        out = MemoryCardProjector(state, memory, output_dir=str(tmp_path / "cards")).project()
        text = out["cards"]["user_preferences"]["text"]
        assert "theme = dark" in text
        assert "theme = light" not in text
        assert "Temporary preference" not in text
        assert out["stats"]["inactive"] == 2
        assert memory.get_claim(old)["superseded_by"] is not None
    finally:
        memory.close()
        state.close()
