def test_event_payload_may_carry_its_own_kind_field():
    """Checkpoint/tool metadata named ``kind`` must not collide with the event channel."""
    from harness.loop import Harness

    seen = []
    harness = Harness.__new__(Harness)
    harness.emit = lambda event_kind, data: seen.append((event_kind, data))
    harness._emit("checkpoint", ok=True, kind="git", ref="abc123")
    assert seen == [("checkpoint", {"ok": True, "kind": "git", "ref": "abc123"})]
