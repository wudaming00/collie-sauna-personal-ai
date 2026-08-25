"""Remembered defaults apply only in matching context and never beat live/policy input."""
from harness.memory import SqliteMemory


def test_context_specific_precedence_counter_evidence_and_receipts(tmp_path):
    memory = SqliteMemory(str(tmp_path / "memory.db"))
    try:
        general = memory.set_preference("response.tone", "detailed")
        email = memory.set_preference(
            "response.tone", "brief", context={"channel": "email"})
        assert memory.resolve_preference(
            "response.tone", context={"channel": "email"})["value"] == "brief"
        slack = memory.resolve_preference(
            "response.tone", context={"channel": "slack"})
        assert slack["value"] == "detailed" and slack["claim_id"] == memory.get_claim(general)["claim_id"]
        live = memory.resolve_preference(
            "response.tone", context={"channel": "email"}, current_request_value="long")
        assert live["source"] == "current_request" and live["value"] == "long"
        policy = memory.resolve_preference(
            "response.tone", context={"channel": "email"}, current_request_value="long",
            policy_override="safe")
        assert policy["source"] == "policy" and policy["value"] == "safe"

        memory.preference_resolver().counter_observation(email, amount=4)
        corrected = memory.resolve_preference(
            "response.tone", context={"channel": "email"})
        assert corrected["value"] == "detailed"
        receipt = memory.preference_resolver().receipt(corrected["receipt_id"])
        assert receipt["attribute"] == "response.tone" and receipt["value"] == "detailed"
        assert memory.trusted_profile("global")["response.tone"]["value"] == "detailed"
    finally:
        memory.close()
