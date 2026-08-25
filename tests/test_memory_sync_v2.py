"""Typed Memory claims replicate by stable IDs, evidence manifests, and tombstones."""
import json

from harness.memory import SqliteMemory
from harness.memory_sync import MemoryDeltaError


def _sync(source, target, cursor=0):
    delta = source.memory_sync().changes_since(cursor, allowed_scopes=["global", "repo"])
    return delta, target.memory_sync().apply_delta(delta, peer_id="source")


def test_claim_create_update_evidence_delete_and_replay(tmp_path):
    source = SqliteMemory(str(tmp_path / "source.db"))
    target = SqliteMemory(str(tmp_path / "target.db"))
    try:
        local_id = source.remember("Office is Paris", project="global", conflict_key="office")
        evidence = source.evidence_store().add(
            source_type="calendar", source_ref="C:/private/calendar.ics",
            content_hash="abc123", observed_at=100, excerpt="Office calendar")
        source.evidence_store().link(local_id, evidence["evidence_id"])
        ephemeral = source.evidence_store().add(
            source_type="screen", content_hash="screen123", observed_at=101,
            retention="ephemeral", excerpt="One-frame observation")
        source.evidence_store().link(local_id, ephemeral["evidence_id"])
        first, result = _sync(source, target)
        assert result["applied"] == 3 and result["conflicts"] == 0
        remote = target.list_claims()[0]
        assert remote["claim_id"] == source.get_claim(local_id)["claim_id"]
        assert remote["revision"] == 3
        linked = target.evidence_store().for_claim(remote["id"])
        assert linked[0]["evidence_id"] == evidence["evidence_id"]
        assert linked[0]["source_ref"] == ""
        assert first["evidence"][0]["content_hash"] == "abc123"
        assert all(row["evidence_id"] != ephemeral["evidence_id"] for row in first["evidence"])

        replay = target.memory_sync().apply_delta(first, peer_id="source")
        assert replay["replayed"] == 3 and replay["applied"] == 0

        assert source.invalidate(local_id, evidence="calendar corrected")
        second, result = _sync(source, target, first["cursor"])
        assert result["applied"] == 1
        assert target.list_claims()[0]["status"] == "invalidated"

        assert source.erase_claim(local_id)
        third, result = _sync(source, target, second["cursor"])
        assert result["applied"] == 1 and target.list_claims() == []
        assert target.memory_sync().status()["tombstones"] == 1
        assert third["changes"][0]["operation"] == "delete"
        assert "Office is Paris" not in json.dumps(third)
        assert source.db.execute(
            "SELECT COUNT(*) FROM memory_evidence").fetchone()[0] == 0
        retained = source.db.execute("""SELECT payload_json FROM memory_claim_changes
            WHERE claim_id=?""", (remote["claim_id"],)).fetchall()
        assert retained and all("Office is Paris" not in row[0] for row in retained)

        never_saw_claim = SqliteMemory(str(tmp_path / "late-peer.db"))
        try:
            late = never_saw_claim.memory_sync().apply_delta(third, peer_id="source")
            assert late["applied"] == 1 and late["conflicts"] == 0
            assert never_saw_claim.memory_sync().status()["tombstones"] == 1
        finally:
            never_saw_claim.close()
    finally:
        source.close(); target.close()


def test_divergent_claim_changes_are_preserved_and_remote_resolution_reemits(tmp_path):
    left = SqliteMemory(str(tmp_path / "left.db"))
    right = SqliteMemory(str(tmp_path / "right.db"))
    try:
        left_id = left.remember("Deploy on Tuesday", project="global")
        initial, _ = _sync(left, right)
        right_id = right.list_claims()[0]["id"]
        assert left.invalidate(left_id, evidence="left correction")
        assert right.invalidate(right_id, evidence="right correction")
        delta = left.memory_sync().changes_since(initial["cursor"], allowed_scopes=["global"])
        result = right.memory_sync().apply_delta(delta, peer_id="left")
        assert result["conflicts"] == 1
        assert right.get_claim(right_id)["review_evidence"] == "right correction"
        conflict = right.memory_sync().conflicts()[0]
        resolved = right.memory_sync().resolve_conflict(conflict["conflict_id"], "remote")
        assert resolved["status"] == "resolved_remote"
        claim = right.get_claim(right_id)
        assert claim["review_evidence"] == "left correction"
        assert claim["revision"] == 3
        assert right.memory_sync().status()["open_conflicts"] == 0
        assert right.memory_sync().changes_since(0, allowed_scopes=["global"])["changes"]
    finally:
        left.close(); right.close()


def test_memory_delta_scope_and_payload_allow_lists_fail_closed(tmp_path):
    memory = SqliteMemory(str(tmp_path / "memory.db"))
    other = SqliteMemory(str(tmp_path / "other.db"))
    try:
        memory.remember("global fact", project="global")
        memory.remember("private mission fact", project="repo", scope="mission:secret")
        delta = memory.memory_sync().changes_since(0, allowed_scopes=["global"])
        assert len(delta["changes"]) == 1 and delta["withheld"] == 1
        bad = json.loads(json.dumps(delta))
        bad["changes"][0]["change_id"] += "bad"
        bad["changes"][0]["payload"]["sql_table"] = "facts"
        try:
            other.memory_sync().apply_delta(bad, peer_id="bad")
            assert False, "unknown columns must be rejected"
        except MemoryDeltaError:
            pass
    finally:
        memory.close(); other.close()


def test_claim_relations_and_extraction_receipt_rebuild_on_peer(tmp_path):
    source = SqliteMemory(str(tmp_path / "source-graph.db"))
    target = SqliteMemory(str(tmp_path / "target-graph.db"))
    try:
        local_id = source.remember("Alice leads Orion", project="global")
        source.set_claim_relations(local_id, [
            {"subject": "Alice", "predicate": "leads", "object": "Orion",
             "subject_type": "person", "object_type": "project"},
        ], extraction_receipt={"extractor": "relation-worker", "model": "test-model",
                               "input_hash": "input-hash"})
        delta = source.memory_sync().changes_since(0, allowed_scopes=["global"])
        assert len(delta["graph_extractions"]) == 1
        result = target.memory_sync().apply_delta(delta, peer_id="source")
        assert result["applied"] == 2 and result["conflicts"] == 0
        remote = target.list_claims()[0]
        assert remote["relations"][0]["predicate"] == "leads"
        assert target.graph_expand(["Alice"], project="global")[0]["claim_id"] == remote["id"]
        receipt = target.db.execute(
            "SELECT * FROM memory_graph_extractions WHERE claim_id=?",
            (remote["claim_id"],)).fetchone()
        assert receipt["extractor"] == "relation-worker" and receipt["model"] == "test-model"

        assert source.invalidate(local_id, evidence="relationship changed")
        update = source.memory_sync().changes_since(delta["cursor"], allowed_scopes=["global"])
        assert target.memory_sync().apply_delta(update, peer_id="source")["applied"] == 1
        assert target.graph_expand(["Alice"], project="global") == []
    finally:
        source.close(); target.close()
