"""Consent-aware evidence journal backing Memory claims.

Evidence is not prompt-visible by default.  Claims carry stable evidence references; adapters may
replicate a bounded metadata manifest while local source references remain on the originating
device.
"""
from __future__ import annotations

import hashlib
import json
import time

from .memory import MemorySecretRejected, contains_memory_secret, _optional_timestamp

__all__ = ["MemoryEvidence"]

SENSITIVITY = frozenset(("normal", "sensitive", "restricted"))
RETENTION = frozenset(("durable", "session", "ephemeral", "source_owned"))


class MemoryEvidence:
    def __init__(self, memory):
        self.memory = memory
        self.db = memory.db

    def add(self, *, source_type: str, content_hash: str, observed_at: int,
            source_ref: str = "", sensitivity: str = "normal",
            retention: str = "durable", excerpt: str = "") -> dict:
        source_type = str(source_type or "").strip()[:80]
        content_hash = str(content_hash or "").strip().lower()[:128]
        source_ref = str(source_ref or "").strip()[:500]
        sensitivity = str(sensitivity or "normal").lower()
        retention = str(retention or "durable").lower()
        observed_at = _optional_timestamp(observed_at, "observed_at")
        excerpt = " ".join(str(excerpt or "").split())[:500]
        if not source_type or not content_hash or observed_at is None:
            raise ValueError("evidence requires source_type, content_hash and observed_at")
        if sensitivity not in SENSITIVITY or retention not in RETENTION:
            raise ValueError("invalid evidence sensitivity or retention")
        if contains_memory_secret({"source_ref": source_ref, "excerpt": excerpt}):
            raise MemorySecretRejected(
                "credential material belongs in the OS credential vault, not Memory evidence")
        identity = "%s\0%s\0%s" % (source_type, content_hash, observed_at)
        evidence_id = "evi_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        self.db.execute("""INSERT INTO memory_evidence(
            evidence_id,source_type,source_ref,content_hash,observed_at,sensitivity,retention,
            excerpt,origin_device,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(evidence_id) DO UPDATE SET
            sensitivity=excluded.sensitivity,retention=excluded.retention,
            excerpt=CASE WHEN excluded.excerpt<>'' THEN excluded.excerpt ELSE memory_evidence.excerpt END
            """, (evidence_id, source_type, source_ref, content_hash, observed_at, sensitivity,
                   retention, excerpt, self.memory._memory_origin_device, int(time.time())))
        self.db.commit()
        return self.get(evidence_id)

    def get(self, evidence_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM memory_evidence WHERE evidence_id=?", (str(evidence_id),)).fetchone()
        return dict(row) if row else None

    def link(self, memory_id: int, evidence_id: str, *, relation: str = "supports") -> dict:
        claim = self.memory.get_claim(int(memory_id))
        if not claim:
            raise ValueError("unknown memory claim")
        evidence = self.get(evidence_id)
        if not evidence:
            raise ValueError("unknown evidence")
        relation = str(relation or "supports").strip().lower()[:80]
        if not relation or not all(ch.isalnum() or ch in "_-" for ch in relation):
            raise ValueError("invalid evidence relation")
        self.db.execute("""INSERT OR IGNORE INTO memory_claim_evidence(
                           claim_id,evidence_id,relation,created_at) VALUES(?,?,?,?)""",
                        (claim["claim_id"], evidence_id, relation, int(time.time())))
        self._refresh_claim_refs(claim["claim_id"])
        self.db.commit()
        return {"claim_id": claim["claim_id"], "evidence_id": evidence_id,
                "relation": relation}

    def unlink(self, memory_id: int, evidence_id: str) -> bool:
        claim = self.memory.get_claim(int(memory_id))
        if not claim:
            return False
        cur = self.db.execute("""DELETE FROM memory_claim_evidence
                                 WHERE claim_id=? AND evidence_id=?""",
                              (claim["claim_id"], str(evidence_id)))
        if cur.rowcount:
            self._refresh_claim_refs(claim["claim_id"])
            self.db.commit()
            return True
        return False

    def _refresh_claim_refs(self, claim_id: str) -> None:
        ids = [row["evidence_id"] for row in self.db.execute(
            """SELECT evidence_id FROM memory_claim_evidence WHERE claim_id=?
               ORDER BY evidence_id LIMIT 100""", (claim_id,)).fetchall()]
        # evidence_ids_json is semantic, so the Memory outbox trigger emits a new claim revision.
        self.db.execute("UPDATE facts SET evidence_ids_json=? WHERE claim_id=?",
                        (json.dumps(ids, separators=(",", ":")), claim_id))

    def for_claim(self, memory_id: int) -> list[dict]:
        claim = self.memory.get_claim(int(memory_id))
        if not claim:
            return []
        return [dict(row) for row in self.db.execute("""SELECT e.*,ce.relation
            FROM memory_claim_evidence ce JOIN memory_evidence e USING(evidence_id)
            WHERE ce.claim_id=? ORDER BY e.observed_at,e.evidence_id""",
            (claim["claim_id"],)).fetchall()]

    def manifest(self, evidence_ids) -> list[dict]:
        ids = tuple(dict.fromkeys(str(item) for item in evidence_ids
                                  if str(item).startswith("evi_")))[:200]
        if not ids:
            return []
        q = ",".join("?" * len(ids))
        rows = self.db.execute(
            "SELECT * FROM memory_evidence WHERE evidence_id IN (%s) ORDER BY evidence_id" % q,
            ids).fetchall()
        out = []
        for row in rows:
            # Session/ephemeral evidence is intentionally local-lifetime material.  A stable claim
            # may retain the unresolved evidence ID, but its metadata must not outlive that policy
            # merely because a peer requested a delta page.
            if row["retention"] in ("session", "ephemeral"):
                continue
            # Device-local source_ref never crosses this boundary. Restricted evidence proves a
            # link exists but does not send its excerpt.
            out.append({"evidence_id": row["evidence_id"], "source_type": row["source_type"],
                        "content_hash": row["content_hash"], "observed_at": row["observed_at"],
                        "sensitivity": row["sensitivity"], "retention": row["retention"],
                        "excerpt": "" if row["sensitivity"] == "restricted" else row["excerpt"],
                        "origin_device": row["origin_device"]})
        return out
