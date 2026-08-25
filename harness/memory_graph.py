"""Retractable, query-gated relationship index over authoritative Memory claims.

This graph never owns truth.  An edge is usable only while its supporting claim passes the same
status, scope, device, supersession and temporal admission checks as ordinary recall.  Deleting
this entire index loses no memory; it can be rebuilt from claim-backed extraction receipts.
"""
from __future__ import annotations

from collections import defaultdict, deque
import hashlib
import json
import re
import time
import unicodedata

from .memory import (MemorySecretRejected, RECALLABLE_STATUSES, contains_memory_secret,
                     _optional_timestamp, _short_identity)

__all__ = ["MemoryGraph"]


def _normalize(value) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    return re.sub(r"\s+", " ", value)[:240]


def _entity_id(normalized: str, entity_type: str) -> str:
    digest = hashlib.sha256((entity_type + "\0" + normalized).encode("utf-8")).hexdigest()[:32]
    return "ent_" + digest


class MemoryGraph:
    def __init__(self, memory):
        self.memory = memory
        self.db = memory.db

    def _entity(self, display_name, entity_type="entity") -> str:
        display = re.sub(r"\s+", " ", str(display_name or "")).strip()[:240]
        normalized = _normalize(display)
        entity_type = _short_identity(entity_type or "entity").lower() or "entity"
        if not normalized:
            raise ValueError("graph entity name is required")
        entity_id = _entity_id(normalized, entity_type)
        self.db.execute(
            """INSERT INTO memory_entities(entity_id,normalized,display_name,entity_type,created_at)
               VALUES(?,?,?,?,?) ON CONFLICT(entity_id) DO UPDATE SET
               display_name=excluded.display_name""",
            (entity_id, normalized, display, entity_type, int(time.time())))
        return entity_id

    @staticmethod
    def _prepare(relations):
        if not isinstance(relations, (list, tuple)) or len(relations) > 100:
            raise ValueError("relations must be an array of at most 100 edges")
        prepared = []
        for relation in relations:
            if not isinstance(relation, dict):
                raise ValueError("each relation must be an object")
            subject = str(relation.get("subject") or "").strip()
            predicate = _normalize(relation.get("predicate"))[:160]
            object_ = str(relation.get("object") or "").strip()
            if not subject or not predicate or not object_:
                raise ValueError("relation subject, predicate and object are required")
            if contains_memory_secret(relation):
                raise MemorySecretRejected(
                    "credential material belongs in the OS credential vault, not Memory graph")
            prepared.append({"subject": subject, "predicate": predicate, "object": object_,
                             "subject_type": str(relation.get("subject_type") or "entity"),
                             "object_type": str(relation.get("object_type") or "entity")})
        return prepared

    def _replace_edges(self, memory_id: int, relations) -> int:
        prepared = self._prepare(relations)
        self.db.execute("DELETE FROM memory_edges WHERE claim_id=?", (int(memory_id),))
        for relation in prepared:
            subject_id = self._entity(relation["subject"], relation["subject_type"])
            object_id = self._entity(relation["object"], relation["object_type"])
            raw = "%s\0%s\0%s\0%s" % (
                memory_id, subject_id, relation["predicate"], object_id)
            edge_id = "edge_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
            self.db.execute(
                """INSERT OR REPLACE INTO memory_edges(
                       edge_id,claim_id,subject_id,predicate,object_id,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (edge_id, int(memory_id), subject_id, relation["predicate"], object_id,
                 int(time.time())))
        return len(prepared)

    def set_claim_relations(self, memory_id: int, relations,
                            *, extraction_receipt: dict | None = None) -> int:
        """Replace all graph edges supported by one accepted claim."""
        claim = self.memory.get_claim(int(memory_id))
        if not claim:
            raise ValueError("unknown memory claim")
        if claim.get("status") not in RECALLABLE_STATUSES:
            raise ValueError("only an accepted memory claim may support graph edges")
        if claim.get("superseded_by") is not None:
            raise ValueError("a superseded memory claim may not support new graph edges")
        if self.db.in_transaction:
            raise RuntimeError("finish the current Memory transaction before deriving graph edges")
        prepared = self._prepare(relations)

        savepoint = "memory_graph_write"
        self.db.execute("SAVEPOINT " + savepoint)
        try:
            self._replace_edges(memory_id, prepared)
            relations_json = json.dumps(prepared, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":"))
            self.db.execute("UPDATE facts SET relations_json=? WHERE id=?",
                            (relations_json, int(memory_id)))
            receipt = extraction_receipt if isinstance(extraction_receipt, dict) else {}
            extractor = str(receipt.get("extractor") or "host")[:120]
            model = str(receipt.get("model") or "")[:160]
            input_hash = str(receipt.get("input_hash") or hashlib.sha256(
                str(claim.get("text") or "").encode("utf-8")).hexdigest())[:128]
            raw_receipt = "%s\0%s\0%s\0%s" % (
                claim["claim_id"], extractor, input_hash, relations_json)
            extraction_id = "gext_" + hashlib.sha256(raw_receipt.encode("utf-8")).hexdigest()[:32]
            self.db.execute("""INSERT OR REPLACE INTO memory_graph_extractions(
                extraction_id,claim_id,extractor,model,input_hash,relations_json,status,created_at)
                VALUES(?,?,?,?,?,?,'accepted',?)""",
                (extraction_id, claim["claim_id"], extractor, model, input_hash, relations_json,
                 int(time.time())))
            self.db.execute("RELEASE SAVEPOINT " + savepoint)
        except Exception:
            self.db.execute("ROLLBACK TO SAVEPOINT " + savepoint)
            self.db.execute("RELEASE SAVEPOINT " + savepoint)
            raise
        self.db.commit()
        return len(prepared)

    def expand(self, entities, *, project: str = "global", allowed_scopes=None,
               device_id: str = "", as_of: int | None = None,
               known_at: int | None = None,
               max_hops: int = 3, max_nodes: int = 100) -> list[dict]:
        """Traverse a bounded undirected support graph and rank claims by shortest hop."""
        if isinstance(entities, str):
            entities = (entities,)
        seeds = tuple(dict.fromkeys(_normalize(value) for value in (entities or ()) if _normalize(value)))
        if not seeds:
            return []
        max_hops = max(1, min(3, int(max_hops or 1)))
        max_nodes = max(1, min(1000, int(max_nodes or 100)))
        as_of = _optional_timestamp(as_of, "as_of") or int(time.time())
        known_at = _optional_timestamp(known_at, "known_at") or int(time.time())
        device_id = _short_identity(device_id)
        projects, scopes, _legacy = self.memory._read_boundary(project, allowed_scopes)
        if not scopes:
            return []
        statuses = tuple(RECALLABLE_STATUSES)
        pq, sq, tq = ((",".join("?" * len(values))) for values in
                      (projects, scopes, statuses))
        rows = self.db.execute(
            """SELECT e.claim_id,e.predicate,e.subject_id,e.object_id,
                      subject.normalized AS subject_name,object.normalized AS object_name
               FROM memory_edges e
               JOIN memory_entities subject ON subject.entity_id=e.subject_id
               JOIN memory_entities object ON object.entity_id=e.object_id
               JOIN facts f ON f.id=e.claim_id
               WHERE f.project IN (%s) AND f.scope IN (%s)
                 AND f.status IN (%s) AND f.superseded_by IS NULL
                 AND (f.device_id='' OR f.device_id=?)
                 AND (f.expires_at IS NULL OR f.expires_at>?)
                 AND f.created_at<=?
                 AND (f.valid_from IS NULL OR f.valid_from<=?)
                 AND (f.valid_to IS NULL OR f.valid_to>?)""" % (pq, sq, tq),
            (*projects, *scopes, *statuses, device_id, known_at, known_at,
             as_of, as_of)).fetchall()

        adjacency = defaultdict(list)
        for row in rows:
            a, b = row["subject_id"], row["object_id"]
            evidence = (int(row["claim_id"]), row["predicate"],
                        row["subject_name"], row["object_name"])
            adjacency[a].append((b, evidence))
            adjacency[b].append((a, evidence))
        names_q = ",".join("?" * len(seeds))
        seed_ids = [row["entity_id"] for row in self.db.execute(
            "SELECT entity_id FROM memory_entities WHERE normalized IN (%s)" % names_q,
            seeds).fetchall()]
        queue = deque((seed_id, 0) for seed_id in seed_ids if seed_id in adjacency)
        visited = {node for node, _distance in queue}
        support = {}
        while queue and len(visited) <= max_nodes:
            node, distance = queue.popleft()
            if distance >= max_hops:
                continue
            for neighbor, evidence in adjacency.get(node, ()):
                claim_id, predicate, subject, object_ = evidence
                hop = distance + 1
                prior = support.get(claim_id)
                if prior is None or hop < prior["hop"]:
                    support[claim_id] = {"claim_id": claim_id, "hop": hop,
                                         "predicate": predicate,
                                         "subject": subject, "object": object_}
                if neighbor not in visited and len(visited) < max_nodes:
                    visited.add(neighbor)
                    queue.append((neighbor, hop))
        return sorted(support.values(), key=lambda item: (item["hop"], item["claim_id"]))
