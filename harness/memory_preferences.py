"""Contextual preference resolution with deterministic precedence and receipts."""
from __future__ import annotations

import json
import secrets
import time

from .memory import _context_json, _context_value, _confidence, _short_identity

__all__ = ["PreferenceResolver"]


class PreferenceResolver:
    def __init__(self, memory):
        self.memory = memory
        self.db = memory.db
        self.db.execute("""CREATE TABLE IF NOT EXISTS memory_preference_receipts(
            receipt_id TEXT PRIMARY KEY,attribute TEXT NOT NULL,context_json TEXT NOT NULL,
            source TEXT NOT NULL,claim_id TEXT NOT NULL DEFAULT '',value_json TEXT NOT NULL,
            candidates_json TEXT NOT NULL,reason TEXT NOT NULL,created_at INTEGER NOT NULL)""")
        self.db.commit()

    @staticmethod
    def _matches(stored: dict, actual: dict) -> bool:
        for key, expected in stored.items():
            actual_value = actual.get(key)
            if isinstance(expected, list):
                if actual_value not in expected:
                    return False
            elif str(actual_value or "") != str(expected):
                return False
        return True

    def resolve(self, attribute: str, *, context: dict | None = None,
                project: str = "global", device_id: str = "", default=None,
                current_request_value=None, policy_override=None,
                min_confidence: float = 0.5) -> dict:
        attribute = str(attribute or "").strip()[:160]
        if not attribute:
            raise ValueError("preference attribute required")
        actual = _context_value(_context_json(context))
        device_id = _short_identity(device_id)
        min_confidence = _confidence(min_confidence)
        candidates = []
        source = "default"
        chosen = None
        reason = "no applicable accepted preference"

        if policy_override is not None:
            source, chosen, reason = "policy", policy_override, "policy boundary overrides defaults"
        elif current_request_value is not None:
            source, chosen, reason = ("current_request", current_request_value,
                                      "the current explicit request overrides remembered defaults")
        else:
            projects, scopes, legacy = self.memory._read_boundary(project)
            pq, sq = ",".join("?" * len(projects)), ",".join("?" * len(scopes))
            now = int(time.time())
            rows = self.db.execute("""SELECT * FROM facts
                WHERE project IN (%s) AND scope IN (%s) AND attribute=?
                  AND kind IN ('preference','habit') AND status IN ('attested','verified')
                  AND superseded_by IS NULL AND (device_id='' OR device_id=?)
                  AND (expires_at IS NULL OR expires_at>?) AND created_at<=?
                  AND (valid_from IS NULL OR valid_from<=?)
                  AND (valid_to IS NULL OR valid_to>?)""" % (pq, sq),
                (*projects, *scopes, attribute, device_id, now, now, now, now)).fetchall()
            for row in rows:
                stored = _context_value(row["context_json"])
                if not self._matches(stored, actual):
                    continue
                effective_confidence = max(
                    0.0, float(row["confidence"] or 0) - 0.15 * int(row["counter_observations"] or 0))
                if effective_confidence < min_confidence:
                    continue
                try:
                    value = json.loads(row["value_json"])
                except Exception:
                    value = row["text"]
                priority = (
                    1 if row["kind"] == "preference" else 0,
                    1 if row["project"] in (project, legacy) else 0,
                    1 if device_id and row["device_id"] == device_id else 0,
                    len(stored), effective_confidence,
                    int(row["reviewed_at"] or row["observed_at"] or row["created_at"] or 0),
                    int(row["id"]),
                )
                candidates.append({"claim_id": row["claim_id"], "local_id": row["id"],
                                   "kind": row["kind"], "value": value, "context": stored,
                                   "confidence": effective_confidence, "priority": priority})
            if candidates:
                best = max(candidates, key=lambda item: item["priority"])
                chosen, source = best["value"], best["kind"]
                reason = "deterministic precedence selected %s" % best["claim_id"]
            else:
                chosen = default

        claim_id = ""
        if candidates and source in ("preference", "habit"):
            claim_id = max(candidates, key=lambda item: item["priority"])["claim_id"]
        receipt_id = "prefret_" + secrets.token_hex(12)
        self.db.execute("""INSERT INTO memory_preference_receipts(
            receipt_id,attribute,context_json,source,claim_id,value_json,candidates_json,reason,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (receipt_id, attribute, _context_json(actual), source, claim_id,
             json.dumps(chosen, ensure_ascii=False, separators=(",", ":")),
             json.dumps(candidates, ensure_ascii=False, separators=(",", ":")),
             reason, int(time.time())))
        self.db.commit()
        return {"attribute": attribute, "value": chosen, "source": source,
                "claim_id": claim_id, "reason": reason, "context": actual,
                "receipt_id": receipt_id, "candidates": candidates}

    def counter_observation(self, memory_id: int, *, amount: int = 1) -> dict:
        row = self.memory.get_claim(int(memory_id))
        if not row or row.get("kind") not in ("preference", "habit"):
            raise ValueError("unknown preference or habit claim")
        amount = max(1, min(20, int(amount or 1)))
        self.db.execute("""UPDATE facts SET counter_observations=counter_observations+?,
                           observed_at=? WHERE id=?""",
                        (amount, int(time.time()), int(memory_id)))
        self.db.commit()
        return self.memory.get_claim(int(memory_id))

    def receipt(self, receipt_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM memory_preference_receipts WHERE receipt_id=?",
            (str(receipt_id),)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["context"] = json.loads(item.pop("context_json") or "{}")
        item["value"] = json.loads(item.pop("value_json") or "null")
        item["candidates"] = json.loads(item.pop("candidates_json") or "[]")
        return item
