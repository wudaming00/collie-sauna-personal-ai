"""Production Memory uses temporal claims and a retractable, opt-in relationship graph."""
import pytest

from harness.memory import SqliteMemory


def _memory(tmp_path):
    return SqliteMemory(str(tmp_path / "memory.db"))


def test_as_of_recall_respects_validity_and_overlapping_conflict_versions(tmp_path):
    memory = _memory(tmp_path)
    try:
        london = memory.remember(
            "The office is in London", keys="office location", created_at=100,
            observed_at=100, valid_from=100, valid_to=200,
            conflict_key="owner.office", project="global")
        paris = memory.remember(
            "The office is in Paris", keys="office location", created_at=200,
            observed_at=200, valid_from=200,
            conflict_key="owner.office", project="global")
        assert [row["id"] for row in memory.recall("office location", as_of=150)] == [london]
        assert [row["id"] for row in memory.recall("office location", as_of=250)] == [paris]

        # Imperfect imports can leave two open intervals. The conflict key still admits only the
        # latest version at the requested instant without deleting the historical row.
        old = memory.remember(
            "The preferred venue is North Hall", keys="preferred venue", created_at=300,
            valid_from=300, conflict_key="event.venue", project="global")
        new = memory.remember(
            "The preferred venue is South Hall", keys="preferred venue", created_at=400,
            valid_from=400, conflict_key="event.venue", project="global")
        assert [row["id"] for row in memory.recall("preferred venue", as_of=350)] == [old]
        assert [row["id"] for row in memory.recall("preferred venue", as_of=450)] == [new]

        retroactive = memory.remember(
            "The 2025 launch color was amber", keys="launch color", created_at=500,
            observed_at=500, valid_from=100, valid_to=200,
            conflict_key="launch.color", project="global")
        assert [row["id"] for row in memory.recall(
            "launch color", as_of=150, known_at=600)] == [retroactive]
        memory.remember(
            "Expired historical secret-free note", keys="expired historical", created_at=100,
            valid_from=100, expires_at=300, project="global")
        assert memory.recall("expired historical", as_of=150, known_at=600) == []
        assert memory.get_claim(london)["valid_to"] == 200
        with pytest.raises(ValueError, match="as_of"):
            memory.recall("office", as_of=0)
    finally:
        memory.close()


def test_graph_is_query_gated_bounded_and_retracted_with_its_claim(tmp_path):
    memory = _memory(tmp_path)
    try:
        leads = memory.remember("Alice leads Orion", project="repo", scope="repo")
        uses = memory.remember("Orion uses PostgreSQL", project="repo", scope="repo")
        foreign = memory.remember("Orion owns Hidden", project="other", scope="other")
        proposed = memory.propose("Alice might know Eve", project="repo", scope="repo")
        memory.set_claim_relations(leads, [
            {"subject": "Alice", "predicate": "leads", "object": "Orion",
             "subject_type": "person", "object_type": "project"},
        ])
        memory.set_claim_relations(uses, [
            {"subject": "Orion", "predicate": "uses", "object": "PostgreSQL",
             "subject_type": "project", "object_type": "database"},
        ])
        memory.set_claim_relations(foreign, [
            {"subject": "Orion", "predicate": "owns", "object": "Hidden"},
        ])
        with pytest.raises(ValueError, match="accepted"):
            memory.set_claim_relations(proposed, [
                {"subject": "Alice", "predicate": "knows", "object": "Eve"},
            ])

        assert memory.recall("database architecture", project="repo") == []
        one_hop = memory.graph_expand(["Alice"], project="repo", max_hops=1)
        assert [item["claim_id"] for item in one_hop] == [leads]
        graph = memory.graph_expand(["Alice"], project="repo", max_hops=3)
        assert [item["claim_id"] for item in graph] == [leads, uses]
        recalled = memory.recall(
            "database architecture", project="repo", graph_entities=["Alice"], graph_hops=2)
        assert {row["id"] for row in recalled} == {leads, uses}

        assert memory.invalidate(uses, evidence="stack changed")
        after = memory.graph_expand(["Alice"], project="repo", max_hops=3)
        assert [item["claim_id"] for item in after] == [leads]
        # The derived edge may remain on disk for audit/rebuild bookkeeping, but it is no longer
        # traversable because the authoritative claim is invalidated.
        assert memory.db.execute(
            "SELECT COUNT(*) FROM memory_edges WHERE claim_id=?", (uses,)).fetchone()[0] == 1
    finally:
        memory.close()
