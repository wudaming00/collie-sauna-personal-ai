"""Versioned, conflict-preserving delta replication for typed Memory claims.

The local SQLite row id never crosses the wire.  Triggers capture semantic writes atomically;
access counters, embeddings and derived graph rows are intentionally absent because peers rebuild
those indexes locally.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import secrets
import time


MEMORY_DELTA_FORMAT = "collie-memory-delta/2"
DELETE_COLUMNS = ("project", "scope", "kind", "subject", "device_id")

SYNC_COLUMNS = (
    "project", "text", "keys", "importance", "created_at", "status", "source", "evidence",
    "provenance", "scope", "review_source", "review_evidence", "review_provenance",
    "reviewed_at", "kind", "subject", "confidence", "observations", "expires_at", "device_id",
    "mission_id", "attribute", "value_json", "valid_from", "valid_to", "observed_at",
    "conflict_key", "supersedes_claim_id",
    "evidence_ids_json",
    "context_json", "counter_observations", "relations_json",
)


class MemoryDeltaError(ValueError):
    pass


def _minimal_delete_payload(payload) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    return {column: payload.get(column) or "" for column in DELETE_COLUMNS}


def _change_id() -> str:
    return "mchg_" + secrets.token_hex(16)


def prepare_connection(memory) -> None:
    """Install connection-local functions before sync triggers are created."""
    memory._memory_sync_suppressed = 0
    memory._memory_sync_origin = ""
    memory._memory_hlc_millis = 0
    memory._memory_hlc_counter = 0
    memory.db.execute("""CREATE TABLE IF NOT EXISTS memory_meta(
        key TEXT PRIMARY KEY,value TEXT NOT NULL DEFAULT '')""")
    row = memory.db.execute(
        "SELECT value FROM memory_meta WHERE key='device_id'").fetchone()
    device_id = str(row[0]) if row and row[0] else "memdev_" + secrets.token_hex(12)
    if not row or not row[0]:
        memory.db.execute(
            "INSERT OR REPLACE INTO memory_meta(key,value) VALUES('device_id',?)", (device_id,))
    memory._memory_origin_device = device_id

    def origin():
        return memory._memory_sync_origin or memory._memory_origin_device

    def hlc():
        millis = int(time.time_ns() // 1_000_000)
        if millis > memory._memory_hlc_millis:
            memory._memory_hlc_millis, memory._memory_hlc_counter = millis, 0
        else:
            memory._memory_hlc_counter += 1
        return "%013d:%06d:%s" % (
            memory._memory_hlc_millis, memory._memory_hlc_counter, origin())

    def json_pairs(*items):
        if len(items) % 2:
            raise ValueError("collie_memory_json needs key/value pairs")
        return json.dumps({str(items[i]): items[i + 1] for i in range(0, len(items), 2)},
                          ensure_ascii=False, separators=(",", ":"))

    memory.db.create_function("collie_memory_sync_suppress", 0,
                              lambda: int(memory._memory_sync_suppressed > 0))
    memory.db.create_function("collie_memory_origin", 0, origin)
    memory.db.create_function("collie_memory_hlc", 0, hlc)
    memory.db.create_function("collie_memory_change_id", 0, _change_id)
    memory.db.create_function("collie_memory_claim_id", 0,
                              lambda: "mem_" + secrets.token_hex(16))
    memory.db.create_function("collie_memory_json", -1, json_pairs)
    memory.db.commit()


def _payload_sql(prefix: str) -> str:
    args = []
    for column in SYNC_COLUMNS:
        if column == "supersedes_claim_id" and prefix == "NEW":
            value = ("COALESCE((SELECT claim_id FROM facts successor "
                     "WHERE successor.id=NEW.superseded_by),NEW.supersedes_claim_id,'')")
        else:
            value = '%s."%s"' % (prefix, column)
        args.extend(("'%s'" % column, value))
    return "collie_memory_json(%s)" % ",".join(args)


def _semantic_changed() -> str:
    checks = []
    # SQLite's IS operator is NULL-safe and works for scalars.
    for column in SYNC_COLUMNS:
        if column == "supersedes_claim_id":
            continue
        checks.append('NOT (OLD."%s" IS NEW."%s")' % (column, column))
    checks.append("NOT (OLD.superseded_by IS NEW.superseded_by)")
    return " OR ".join(checks)


def install(memory) -> None:
    db = memory.db
    cols = {row[1] for row in db.execute("PRAGMA table_info(facts)")}
    for name, decl in (
            ("claim_id", "TEXT NOT NULL DEFAULT ''"),
            ("revision", "INTEGER NOT NULL DEFAULT 0"),
            ("origin_device", "TEXT NOT NULL DEFAULT ''"),
            ("updated_at", "INTEGER"),
            ("deleted_at", "INTEGER"),
            ("supersedes_claim_id", "TEXT NOT NULL DEFAULT ''")):
        if name not in cols:
            db.execute("ALTER TABLE facts ADD COLUMN %s %s" % (name, decl))
            cols.add(name)
    db.executescript("""
        CREATE UNIQUE INDEX IF NOT EXISTS facts_claim_id_v5 ON facts(claim_id) WHERE claim_id<>'';
        CREATE TABLE IF NOT EXISTS memory_claim_changes(
            seq INTEGER PRIMARY KEY AUTOINCREMENT,change_id TEXT NOT NULL UNIQUE,
            claim_id TEXT NOT NULL,operation TEXT NOT NULL,base_revision INTEGER NOT NULL,
            revision INTEGER NOT NULL,origin_device TEXT NOT NULL,hlc TEXT NOT NULL,
            changed_at INTEGER NOT NULL,payload_json TEXT NOT NULL DEFAULT '{}');
        CREATE INDEX IF NOT EXISTS memory_claim_changes_seq_v1 ON memory_claim_changes(seq);
        CREATE TABLE IF NOT EXISTS memory_claim_tombstones(
            claim_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,origin_device TEXT NOT NULL,
            deleted_at INTEGER NOT NULL,payload_json TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS memory_sync_applied(
            peer_id TEXT NOT NULL,change_id TEXT NOT NULL,applied_at INTEGER NOT NULL,
            PRIMARY KEY(peer_id,change_id));
        CREATE TABLE IF NOT EXISTS memory_sync_peers(
            peer_id TEXT PRIMARY KEY,push_cursor INTEGER NOT NULL DEFAULT 0,
            pull_cursor INTEGER NOT NULL DEFAULT 0,updated_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS memory_sync_conflicts(
            conflict_id TEXT PRIMARY KEY,peer_id TEXT NOT NULL,change_id TEXT NOT NULL,
            claim_id TEXT NOT NULL,local_revision INTEGER NOT NULL,remote_base_revision INTEGER NOT NULL,
            remote_revision INTEGER NOT NULL,operation TEXT NOT NULL,
            local_payload_json TEXT NOT NULL,remote_payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',created_at INTEGER NOT NULL,resolved_at INTEGER);
    """)

    for trigger in ("memory_claim_bootstrap_v1", "memory_claim_insert_v1",
                    "memory_claim_bootstrap_change_v1", "memory_claim_update_v1",
                    "memory_claim_delete_v1"):
        db.execute("DROP TRIGGER IF EXISTS " + trigger)
    payload_new = _payload_sql("NEW")
    payload_delete = "collie_memory_json(%s)" % ",".join(
        item for column in DELETE_COLUMNS for item in ("'%s'" % column, 'OLD."%s"' % column))
    update_columns = ",".join(
        column for column in SYNC_COLUMNS if column != "supersedes_claim_id") + ",superseded_by"
    db.executescript("""
        CREATE TRIGGER memory_claim_bootstrap_v1 AFTER INSERT ON facts
        WHEN NEW.claim_id=''
        BEGIN
          UPDATE facts SET claim_id=collie_memory_claim_id(),revision=1,
            origin_device=collie_memory_origin(),updated_at=CAST(strftime('%%s','now') AS INTEGER),
            supersedes_claim_id=COALESCE((SELECT claim_id FROM facts WHERE id=NEW.superseded_by),'')
          WHERE id=NEW.id;
        END;

        CREATE TRIGGER memory_claim_insert_v1 AFTER INSERT ON facts
        WHEN NEW.claim_id<>'' AND collie_memory_sync_suppress()=0
        BEGIN
          INSERT INTO memory_claim_changes(change_id,claim_id,operation,base_revision,revision,
            origin_device,hlc,changed_at,payload_json)
          VALUES(collie_memory_change_id(),NEW.claim_id,'upsert',0,
            CASE WHEN NEW.revision<1 THEN 1 ELSE NEW.revision END,collie_memory_origin(),
            collie_memory_hlc(),CAST(strftime('%%s','now') AS INTEGER),%s);
        END;

        CREATE TRIGGER memory_claim_bootstrap_change_v1 AFTER UPDATE OF claim_id ON facts
        WHEN OLD.claim_id='' AND NEW.claim_id<>'' AND collie_memory_sync_suppress()=0
        BEGIN
          INSERT INTO memory_claim_changes(change_id,claim_id,operation,base_revision,revision,
            origin_device,hlc,changed_at,payload_json)
          VALUES(collie_memory_change_id(),NEW.claim_id,'upsert',0,NEW.revision,
            collie_memory_origin(),collie_memory_hlc(),
            CAST(strftime('%%s','now') AS INTEGER),%s);
        END;

        CREATE TRIGGER memory_claim_update_v1 AFTER UPDATE OF %s ON facts
        WHEN NEW.claim_id<>'' AND collie_memory_sync_suppress()=0 AND (%s)
        BEGIN
          UPDATE facts SET revision=OLD.revision+1,origin_device=collie_memory_origin(),
            updated_at=CAST(strftime('%%s','now') AS INTEGER),
            supersedes_claim_id=COALESCE((SELECT claim_id FROM facts WHERE id=NEW.superseded_by),'')
          WHERE id=NEW.id;
          INSERT INTO memory_claim_changes(change_id,claim_id,operation,base_revision,revision,
            origin_device,hlc,changed_at,payload_json)
          VALUES(collie_memory_change_id(),NEW.claim_id,'upsert',OLD.revision,OLD.revision+1,
            collie_memory_origin(),collie_memory_hlc(),
            CAST(strftime('%%s','now') AS INTEGER),%s);
        END;

        CREATE TRIGGER memory_claim_delete_v1 AFTER DELETE ON facts
        WHEN OLD.claim_id<>''
        BEGIN
          INSERT OR REPLACE INTO memory_claim_tombstones(
            claim_id,revision,origin_device,deleted_at,payload_json)
          VALUES(OLD.claim_id,OLD.revision+1,collie_memory_origin(),
            CAST(strftime('%%s','now') AS INTEGER),%s);
          INSERT INTO memory_claim_changes(change_id,claim_id,operation,base_revision,revision,
            origin_device,hlc,changed_at,payload_json)
          SELECT collie_memory_change_id(),OLD.claim_id,'delete',OLD.revision,OLD.revision+1,
            collie_memory_origin(),collie_memory_hlc(),
            CAST(strftime('%%s','now') AS INTEGER),%s
          WHERE collie_memory_sync_suppress()=0;
        END;
    """ % (payload_new, payload_new, update_columns, _semantic_changed(), payload_new,
             payload_delete, payload_delete))

    # Builds before Memory v2 kept complete deleted claims in tombstone/outbox payloads.  Scrub
    # them during migration so a physical erasure cannot survive as a hidden audit copy.
    for row in db.execute(
            "SELECT claim_id,payload_json FROM memory_claim_tombstones").fetchall():
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        minimal_json = json.dumps(_minimal_delete_payload(payload), separators=(",", ":"))
        claim_id = row["claim_id"]
        db.execute("UPDATE memory_claim_tombstones SET payload_json=? WHERE claim_id=?",
                   (minimal_json, claim_id))
        evidence_ids = [item["evidence_id"] for item in db.execute(
            "SELECT evidence_id FROM memory_claim_evidence WHERE claim_id=?",
            (claim_id,)).fetchall()]
        db.execute("DELETE FROM memory_claim_evidence WHERE claim_id=?", (claim_id,))
        db.execute("DELETE FROM memory_graph_extractions WHERE claim_id=?", (claim_id,))
        db.execute("""DELETE FROM memory_claim_changes
                      WHERE claim_id=? AND operation<>'delete'""", (claim_id,))
        db.execute("""UPDATE memory_sync_conflicts
            SET local_payload_json=?,remote_payload_json=? WHERE claim_id=?""",
            (minimal_json, minimal_json, claim_id))
        for evidence_id in evidence_ids:
            db.execute("""DELETE FROM memory_evidence WHERE evidence_id=? AND NOT EXISTS(
                SELECT 1 FROM memory_claim_evidence WHERE evidence_id=?)""",
                (evidence_id, evidence_id))
    for row in db.execute("""SELECT seq,payload_json FROM memory_claim_changes
                              WHERE operation='delete'""").fetchall():
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        db.execute("UPDATE memory_claim_changes SET payload_json=? WHERE seq=?",
                   (json.dumps(_minimal_delete_payload(payload), separators=(",", ":")),
                    int(row["seq"])))

    missing = db.execute("SELECT id FROM facts WHERE claim_id='' ORDER BY id").fetchall()
    if missing:
        missing_ids = [int(row["id"]) for row in missing]
        memory._memory_sync_suppressed += 1
        try:
            for row in missing:
                db.execute("""UPDATE facts SET claim_id=?,revision=1,origin_device=?,updated_at=?
                              WHERE id=?""",
                           ("mem_" + secrets.token_hex(16), memory._memory_origin_device,
                            int(time.time()), int(row["id"])))
            db.execute("""UPDATE facts SET supersedes_claim_id=COALESCE(
                           (SELECT successor.claim_id FROM facts successor
                            WHERE successor.id=facts.superseded_by),'')""")
        finally:
            memory._memory_sync_suppressed -= 1
        q = ",".join("?" * len(missing_ids))
        for row in db.execute(
                "SELECT * FROM facts WHERE id IN (%s) ORDER BY id" % q,
                missing_ids).fetchall():
            db.execute("""INSERT INTO memory_claim_changes(
                change_id,claim_id,operation,base_revision,revision,origin_device,hlc,changed_at,
                payload_json) VALUES(?,?,?,?,?,?,?,?,?)""",
                (_change_id(), row["claim_id"], "upsert", 0, int(row["revision"] or 1),
                 row["origin_device"] or memory._memory_origin_device,
                 "%013d:000000:%s" % (int(time.time() * 1000), memory._memory_origin_device),
                 int(time.time()), json.dumps(payload_from_row(row), ensure_ascii=False,
                                              separators=(",", ":"))))
    db.commit()


def payload_from_row(row) -> dict:
    keys = row.keys() if hasattr(row, "keys") else row
    return {column: row[column] if column in keys else None for column in SYNC_COLUMNS}


@contextmanager
def _remote(memory, origin: str):
    previous = memory._memory_sync_origin
    memory._memory_sync_origin = str(origin or "remote")[:200]
    memory._memory_sync_suppressed += 1
    try:
        yield
    finally:
        memory._memory_sync_suppressed = max(0, memory._memory_sync_suppressed - 1)
        memory._memory_sync_origin = previous


class MemorySync:
    def __init__(self, memory):
        self.memory = memory
        self.db = memory.db

    @property
    def device_id(self) -> str:
        return self.memory._memory_origin_device

    def status(self) -> dict:
        latest = self.db.execute(
            "SELECT COALESCE(MAX(seq),0) FROM memory_claim_changes").fetchone()[0]
        conflicts = self.db.execute(
            "SELECT COUNT(*) FROM memory_sync_conflicts WHERE status='open'").fetchone()[0]
        return {"format": MEMORY_DELTA_FORMAT, "device_id": self.device_id,
                "latest_cursor": int(latest or 0), "open_conflicts": int(conflicts or 0),
                "claims": int(self.db.execute("SELECT COUNT(*) FROM facts").fetchone()[0]),
                "tombstones": int(self.db.execute(
                    "SELECT COUNT(*) FROM memory_claim_tombstones").fetchone()[0])}

    def peer_cursor(self, peer_id: str) -> dict:
        row = self.db.execute(
            "SELECT * FROM memory_sync_peers WHERE peer_id=?", (str(peer_id),)).fetchone()
        return dict(row) if row else {"peer_id": str(peer_id), "push_cursor": 0,
                                      "pull_cursor": 0, "updated_at": 0}

    def set_push_cursor(self, peer_id: str, cursor: int) -> None:
        now = int(time.time())
        self.db.execute("""INSERT INTO memory_sync_peers(peer_id,push_cursor,pull_cursor,updated_at)
            VALUES(?,?,0,?) ON CONFLICT(peer_id) DO UPDATE SET
            push_cursor=MAX(push_cursor,excluded.push_cursor),updated_at=excluded.updated_at""",
            (str(peer_id), max(0, int(cursor)), now))
        self.db.commit()

    def changes_since(self, cursor: int = 0, *, allowed_scopes=None,
                      include_profile: bool = True, limit: int = 500) -> dict:
        cursor = max(0, int(cursor or 0))
        limit = max(1, min(1000, int(limit or 500)))
        scopes = None if allowed_scopes is None else {
            str(scope) for scope in allowed_scopes if str(scope)}
        rows = self.db.execute(
            "SELECT * FROM memory_claim_changes WHERE seq>? ORDER BY seq LIMIT ?",
            (cursor, limit + 1)).fetchall()
        page, raw = rows[:limit], rows[:limit]
        changes = []
        withheld = 0
        evidence_ids = set()
        claim_ids = set()
        relation_versions = {}
        for row in page:
            payload = json.loads(row["payload_json"] or "{}")
            if scopes is not None and str(payload.get("scope") or "") not in scopes:
                withheld += 1
                continue
            if not include_profile and payload.get("kind") in ("preference", "habit", "identity"):
                withheld += 1
                continue
            changes.append({"change_id": row["change_id"], "claim_id": row["claim_id"],
                            "operation": row["operation"],
                            "base_revision": int(row["base_revision"]),
                            "revision": int(row["revision"]),
                            "origin_device": row["origin_device"], "hlc": row["hlc"],
                            "changed_at": int(row["changed_at"]), "payload": payload})
            claim_ids.add(row["claim_id"])
            if row["operation"] == "upsert":
                relation_versions.setdefault(row["claim_id"], set()).add(
                    str(payload.get("relations_json") or "[]"))
            try:
                evidence_ids.update(item for item in json.loads(
                    payload.get("evidence_ids_json") or "[]") if str(item).startswith("evi_"))
            except (TypeError, ValueError):
                pass
        next_cursor = int(raw[-1]["seq"]) if raw else cursor
        has_more = len(rows) > limit
        evidence = self.memory.evidence_store().manifest(evidence_ids)
        graph_extractions = []
        if claim_ids:
            q = ",".join("?" * len(claim_ids))
            extraction_rows = self.db.execute("""SELECT extraction_id,claim_id,extractor,model,input_hash,
                relations_json,status,created_at FROM memory_graph_extractions
                WHERE claim_id IN (%s) ORDER BY extraction_id""" % q,
                tuple(sorted(claim_ids))).fetchall()
            graph_extractions = [dict(row) for row in extraction_rows
                                 if row["relations_json"] in
                                 relation_versions.get(row["claim_id"], set())]
        return {"format": MEMORY_DELTA_FORMAT, "source_device": self.device_id,
                "from_cursor": cursor, "cursor": next_cursor,
                "has_more": has_more, "withheld": withheld,
                "evidence": evidence, "graph_extractions": graph_extractions,
                "changes": changes}

    def requeue_current(self, *, kinds=None, allowed_scopes=None) -> int:
        kinds = {str(kind) for kind in kinds} if kinds is not None else None
        scopes = {str(scope) for scope in allowed_scopes} if allowed_scopes is not None else None
        rows = self.db.execute("SELECT claim_id,kind,scope FROM facts ORDER BY id").fetchall()
        count = 0
        for row in rows:
            if kinds is not None and row["kind"] not in kinds:
                continue
            if scopes is not None and row["scope"] not in scopes:
                continue
            count += int(self._touch_claim(row["claim_id"]))
        self.db.commit()
        return count

    def _local_payload(self, claim_id: str) -> tuple[int, dict] | None:
        row = self.db.execute("SELECT * FROM facts WHERE claim_id=?", (claim_id,)).fetchone()
        if row:
            return int(row["revision"] or 0), payload_from_row(row)
        tomb = self.db.execute(
            "SELECT * FROM memory_claim_tombstones WHERE claim_id=?", (claim_id,)).fetchone()
        if tomb:
            return int(tomb["revision"]), json.loads(tomb["payload_json"] or "{}")
        return None

    def apply_delta(self, delta: dict, *, peer_id: str = "sauna") -> dict:
        from .memory import (_CONTEXT_KEYS, MEMORY_KINDS, MEMORY_STATUSES, MEMORY_SUBJECTS,
                             contains_memory_secret)
        if not isinstance(delta, dict) or delta.get("format") != MEMORY_DELTA_FORMAT:
            raise MemoryDeltaError("unsupported Memory delta format")
        changes = delta.get("changes")
        if not isinstance(changes, list) or len(changes) > 1000:
            raise MemoryDeltaError("changes must be an array of at most 1000 items")
        # Validate the entire page before the first database mutation.  Operational failures can
        # still be retried by change_id, but an unknown/malformed later item must not make a
        # partially trusted page visible.
        for change in changes:
            if not isinstance(change, dict):
                raise MemoryDeltaError("invalid Memory change")
            operation = str(change.get("operation") or "")
            claim_id = str(change.get("claim_id") or "")
            if (not str(change.get("change_id") or "") or not claim_id.startswith("mem_") or
                    operation not in ("upsert", "delete")):
                raise MemoryDeltaError("invalid Memory change identity or operation")
            try:
                base = int(change.get("base_revision")); revision = int(change.get("revision"))
            except (TypeError, ValueError):
                raise MemoryDeltaError("Memory revisions must be integers")
            if revision <= base or base < 0:
                raise MemoryDeltaError("invalid Memory revision transition")
            payload = change.get("payload") or {}
            if not isinstance(payload, dict) or set(payload) - set(SYNC_COLUMNS):
                raise MemoryDeltaError("Memory payload contains unknown columns")
            if contains_memory_secret(payload):
                raise MemoryDeltaError("Memory payload contains credential material")
            if operation == "upsert":
                if set(payload) != set(SYNC_COLUMNS):
                    raise MemoryDeltaError("Memory upsert payload is incomplete")
                if (payload.get("status") not in MEMORY_STATUSES or
                        payload.get("kind") not in MEMORY_KINDS or
                        payload.get("subject") not in MEMORY_SUBJECTS):
                    raise MemoryDeltaError("Memory payload has invalid typed fields")
                try:
                    evidence_ids = json.loads(payload.get("evidence_ids_json") or "[]")
                    context = json.loads(payload.get("context_json") or "{}")
                    relations = json.loads(payload.get("relations_json") or "[]")
                except (TypeError, ValueError):
                    raise MemoryDeltaError("Memory JSON fields are malformed")
                relation_keys = {"subject", "predicate", "object", "subject_type", "object_type"}
                if (not isinstance(evidence_ids, list) or len(evidence_ids) > 100 or
                        any(not str(item).startswith("evi_") for item in evidence_ids) or
                        not isinstance(context, dict) or set(context) - _CONTEXT_KEYS or
                        not isinstance(relations, list) or len(relations) > 100 or
                        any(not isinstance(item, dict) or set(item) != relation_keys
                            for item in relations)):
                    raise MemoryDeltaError("Memory evidence, context or relation fields are malformed")
                try:
                    from .memory_graph import MemoryGraph
                    MemoryGraph(self.memory)._prepare(relations)
                except (TypeError, ValueError) as exc:
                    raise MemoryDeltaError("Memory relations failed validation") from exc
            elif set(payload) != set(DELETE_COLUMNS):
                raise MemoryDeltaError("Memory delete payload is malformed")
        evidence_rows = delta.get("evidence") or []
        if not isinstance(evidence_rows, list) or len(evidence_rows) > 500:
            raise MemoryDeltaError("evidence must be an array of at most 500 items")
        allowed_evidence = {"evidence_id", "source_type", "content_hash", "observed_at",
                            "sensitivity", "retention", "excerpt", "origin_device"}
        validated_evidence = []
        for evidence in evidence_rows:
            if (not isinstance(evidence, dict) or set(evidence) - allowed_evidence or
                    not str(evidence.get("evidence_id") or "").startswith("evi_")):
                raise MemoryDeltaError("invalid evidence manifest entry")
            if contains_memory_secret(evidence):
                raise MemoryDeltaError("evidence manifest contains credential material")
            if evidence.get("sensitivity") not in ("normal", "sensitive", "restricted") or \
                    evidence.get("retention") not in ("durable", "session", "ephemeral", "source_owned"):
                raise MemoryDeltaError("invalid evidence policy")
            try:
                int(evidence.get("observed_at"))
            except (TypeError, ValueError):
                raise MemoryDeltaError("invalid evidence observed_at")
            validated_evidence.append(evidence)
        graph_rows = delta.get("graph_extractions") or []
        if not isinstance(graph_rows, list) or len(graph_rows) > 500:
            raise MemoryDeltaError("graph_extractions must be an array of at most 500 items")
        allowed_graph = {"extraction_id", "claim_id", "extractor", "model", "input_hash",
                         "relations_json", "status", "created_at"}
        graph_by_claim = {}
        changed_claims = {str(change.get("claim_id") or "") for change in changes
                          if isinstance(change, dict)}
        changed_relation_versions = {
            (str(change.get("claim_id") or ""),
             str((change.get("payload") or {}).get("relations_json") or "[]"))
            for change in changes if isinstance(change, dict) and
            change.get("operation") == "upsert" and isinstance(change.get("payload"), dict)}
        for extraction in graph_rows:
            if (not isinstance(extraction, dict) or set(extraction) != allowed_graph or
                    not str(extraction.get("extraction_id") or "").startswith("gext_") or
                    not str(extraction.get("claim_id") or "").startswith("mem_") or
                    extraction.get("status") != "accepted" or
                    extraction.get("claim_id") not in changed_claims or
                    (extraction.get("claim_id"), extraction.get("relations_json")) not in
                    changed_relation_versions or
                    contains_memory_secret(extraction)):
                raise MemoryDeltaError("invalid graph extraction receipt")
            try:
                relations = json.loads(extraction.get("relations_json") or "[]")
                created_at = int(extraction.get("created_at"))
            except (TypeError, ValueError):
                raise MemoryDeltaError("malformed graph extraction receipt")
            if not isinstance(relations, list) or len(relations) > 100:
                raise MemoryDeltaError("graph extraction relations are malformed")
            normalized = dict(extraction)
            normalized["created_at"] = created_at
            graph_by_claim.setdefault(extraction["claim_id"], []).append(normalized)
        for evidence in validated_evidence:
            self.db.execute("""INSERT OR IGNORE INTO memory_evidence(
                evidence_id,source_type,source_ref,content_hash,observed_at,sensitivity,retention,
                excerpt,origin_device,created_at) VALUES(?,?,'',?,?,?,?,?,?,?)""",
                (evidence["evidence_id"], str(evidence.get("source_type") or "remote")[:80],
                 str(evidence.get("content_hash") or "")[:128],
                 int(evidence.get("observed_at") or time.time()), evidence["sensitivity"],
                 evidence["retention"], str(evidence.get("excerpt") or "")[:500],
                 str(evidence.get("origin_device") or "remote")[:200], int(time.time())))
        applied = replayed = conflicted = 0
        for change in changes:
            if not isinstance(change, dict):
                raise MemoryDeltaError("invalid Memory change")
            change_id = str(change.get("change_id") or "")
            claim_id = str(change.get("claim_id") or "")
            operation = str(change.get("operation") or "")
            origin = str(change.get("origin_device") or delta.get("source_device") or "remote")
            if (not change_id or not claim_id.startswith("mem_") or operation not in
                    ("upsert", "delete")):
                raise MemoryDeltaError("invalid Memory change identity or operation")
            prior = self.db.execute("""SELECT 1 FROM memory_sync_applied
                                       WHERE peer_id=? AND change_id=?""",
                                    (peer_id, change_id)).fetchone()
            if prior:
                replayed += 1
                continue
            try:
                base = int(change.get("base_revision"))
                revision = int(change.get("revision"))
            except (TypeError, ValueError):
                raise MemoryDeltaError("Memory revisions must be integers")
            if revision <= base or base < 0:
                raise MemoryDeltaError("invalid Memory revision transition")
            payload = change.get("payload") or {}
            if not isinstance(payload, dict) or set(payload) - set(SYNC_COLUMNS):
                raise MemoryDeltaError("Memory payload contains unknown columns")
            if contains_memory_secret(payload):
                raise MemoryDeltaError("Memory payload contains credential material")
            if operation == "upsert":
                if set(payload) != set(SYNC_COLUMNS):
                    raise MemoryDeltaError("Memory upsert payload is incomplete")
                if payload.get("status") not in MEMORY_STATUSES or payload.get("kind") not in MEMORY_KINDS \
                        or payload.get("subject") not in MEMORY_SUBJECTS:
                    raise MemoryDeltaError("Memory payload has invalid typed fields")
                try:
                    evidence_ids = json.loads(payload.get("evidence_ids_json") or "[]")
                    context = json.loads(payload.get("context_json") or "{}")
                    relations = json.loads(payload.get("relations_json") or "[]")
                except (TypeError, ValueError):
                    raise MemoryDeltaError("Memory JSON fields are malformed")
                if (not isinstance(evidence_ids, list) or
                        any(not str(item).startswith("evi_") for item in evidence_ids) or
                        not isinstance(context, dict) or not isinstance(relations, list)):
                    raise MemoryDeltaError("Memory evidence or context fields are malformed")
            local = self._local_payload(claim_id)
            local_revision = local[0] if local else 0
            # A tombstone is safe to install on a peer that never received the prior private
            # revisions.  Requiring base==0 there would turn deletion into a conflict and risk a
            # later stale resurrection.
            if local_revision != base and not (operation == "delete" and local is None):
                conflict_id = "mconf_" + secrets.token_hex(12)
                self.db.execute("""INSERT INTO memory_sync_conflicts(
                    conflict_id,peer_id,change_id,claim_id,local_revision,remote_base_revision,
                    remote_revision,operation,local_payload_json,remote_payload_json,status,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,'open',?)""",
                    (conflict_id, peer_id, change_id, claim_id, local_revision, base, revision,
                     operation, json.dumps(local[1] if local else {}, ensure_ascii=False),
                     json.dumps(payload, ensure_ascii=False), int(time.time())))
                self._mark_applied(peer_id, change_id)
                self.db.commit()
                conflicted += 1
                continue
            with _remote(self.memory, origin):
                if operation == "delete":
                    self._apply_delete(claim_id, revision, origin, payload)
                else:
                    self._apply_upsert(claim_id, revision, origin, payload)
                    for extraction in graph_by_claim.get(claim_id, ()):
                        if extraction["relations_json"] != payload.get("relations_json"):
                            continue
                        self.db.execute("""INSERT OR IGNORE INTO memory_graph_extractions(
                            extraction_id,claim_id,extractor,model,input_hash,relations_json,
                            status,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                            (extraction["extraction_id"], extraction["claim_id"],
                             str(extraction["extractor"])[:120], str(extraction["model"])[:160],
                             str(extraction["input_hash"])[:128], extraction["relations_json"],
                             "accepted", extraction["created_at"]))
            self._mark_applied(peer_id, change_id)
            applied += 1
            self.db.commit()
        self._resolve_supersession_links()
        pull_cursor = max(0, int(delta.get("cursor") or 0))
        self.db.execute("""INSERT INTO memory_sync_peers(
            peer_id,push_cursor,pull_cursor,updated_at) VALUES(?,0,?,?)
            ON CONFLICT(peer_id) DO UPDATE SET
            pull_cursor=MAX(pull_cursor,excluded.pull_cursor),updated_at=excluded.updated_at""",
            (str(peer_id), pull_cursor, int(time.time())))
        self.db.commit()
        return {"applied": applied, "replayed": replayed, "conflicts": conflicted,
                "cursor": int(delta.get("cursor") or 0)}

    def _mark_applied(self, peer_id, change_id):
        self.db.execute("""INSERT OR IGNORE INTO memory_sync_applied(peer_id,change_id,applied_at)
                           VALUES(?,?,?)""", (str(peer_id), str(change_id), int(time.time())))

    def _apply_delete(self, claim_id, revision, origin, payload):
        row = self.db.execute("SELECT * FROM facts WHERE claim_id=?", (claim_id,)).fetchone()
        if row:
            evidence_ids = [item["evidence_id"] for item in self.db.execute(
                "SELECT evidence_id FROM memory_claim_evidence WHERE claim_id=?",
                (claim_id,)).fetchall()]
            self.db.execute("DELETE FROM memory_edges WHERE claim_id=?", (int(row["id"]),))
            self.db.execute("DELETE FROM memory_graph_extractions WHERE claim_id=?", (claim_id,))
            self.db.execute("DELETE FROM memory_claim_evidence WHERE claim_id=?", (claim_id,))
            for evidence_id in evidence_ids:
                self.db.execute("""DELETE FROM memory_evidence WHERE evidence_id=? AND NOT EXISTS(
                    SELECT 1 FROM memory_claim_evidence WHERE evidence_id=?)""",
                    (evidence_id, evidence_id))
            if self.memory.has_fts:
                try:
                    self.db.execute("""INSERT INTO facts_fts(facts_fts,rowid,text,keys)
                                       VALUES('delete',?,?,?)""",
                                    (row["id"], row["text"] or "", row["keys"] or ""))
                except Exception:
                    pass
            self.db.execute("DELETE FROM facts WHERE id=?", (int(row["id"]),))
        minimal_json = json.dumps(_minimal_delete_payload(payload), ensure_ascii=False,
                                  separators=(",", ":"))
        self.db.execute("""DELETE FROM memory_claim_changes
                           WHERE claim_id=? AND operation<>'delete'""", (claim_id,))
        self.db.execute("""UPDATE memory_sync_conflicts
            SET local_payload_json=?,remote_payload_json=? WHERE claim_id=?""",
            (minimal_json, minimal_json, claim_id))
        self.db.execute("""INSERT OR REPLACE INTO memory_claim_tombstones(
            claim_id,revision,origin_device,deleted_at,payload_json) VALUES(?,?,?,?,?)""",
            (claim_id, revision, origin, int(time.time()),
             minimal_json))

    def _apply_upsert(self, claim_id, revision, origin, payload):
        row = self.db.execute("SELECT * FROM facts WHERE claim_id=?", (claim_id,)).fetchone()
        values = {column: payload.get(column) for column in SYNC_COLUMNS}
        values["supersedes_claim_id"] = str(values.get("supersedes_claim_id") or "")
        text, keys = str(values.get("text") or ""), str(values.get("keys") or "")
        embedding = json.dumps(self.memory.embedder.embed(
            text + " " + keys, kind="passage")) if self.memory.embedder else "[]"
        if row:
            if self.memory.has_fts:
                try:
                    self.db.execute("""INSERT INTO facts_fts(facts_fts,rowid,text,keys)
                                       VALUES('delete',?,?,?)""",
                                    (row["id"], row["text"] or "", row["keys"] or ""))
                except Exception:
                    pass
            assignments = ",".join('"%s"=?' % column for column in SYNC_COLUMNS)
            self.db.execute("""UPDATE facts SET %s,revision=?,origin_device=?,updated_at=?,
                embed_model=?,embedding=?,deleted_at=NULL WHERE claim_id=?""" % assignments,
                (*[values[column] for column in SYNC_COLUMNS], revision, origin, int(time.time()),
                 self.memory.embed_model, embedding, claim_id))
            row_id = int(row["id"])
        else:
            columns = ",".join('"%s"' % column for column in SYNC_COLUMNS)
            placeholders = ",".join("?" * len(SYNC_COLUMNS))
            cur = self.db.execute("""INSERT INTO facts(%s,claim_id,revision,origin_device,
                updated_at,embed_model,embedding,access_count,last_access,superseded_by,deleted_at)
                VALUES(%s,?,?,?,?,?,?,0,NULL,NULL,NULL)""" % (columns, placeholders),
                (*[values[column] for column in SYNC_COLUMNS], claim_id, revision, origin,
                 int(time.time()), self.memory.embed_model, embedding))
            row_id = int(cur.lastrowid)
        self.db.execute("DELETE FROM memory_claim_tombstones WHERE claim_id=?", (claim_id,))
        if self.memory.has_fts:
            self.db.execute("INSERT INTO facts_fts(rowid,text,keys) VALUES(?,?,?)",
                            (row_id, text, keys))
        self.db.execute("DELETE FROM memory_claim_evidence WHERE claim_id=?", (claim_id,))
        try:
            evidence_ids = json.loads(values.get("evidence_ids_json") or "[]")
        except (TypeError, ValueError):
            evidence_ids = []
        for evidence_id in evidence_ids[:100] if isinstance(evidence_ids, list) else ():
            if self.db.execute("SELECT 1 FROM memory_evidence WHERE evidence_id=?",
                               (str(evidence_id),)).fetchone():
                self.db.execute("""INSERT OR IGNORE INTO memory_claim_evidence(
                    claim_id,evidence_id,relation,created_at) VALUES(?,?,'supports',?)""",
                    (claim_id, str(evidence_id), int(time.time())))
        try:
            relations = json.loads(values.get("relations_json") or "[]")
        except (TypeError, ValueError):
            relations = []
        from .memory_graph import MemoryGraph
        MemoryGraph(self.memory)._replace_edges(row_id, relations if isinstance(relations, list) else [])

    def _resolve_supersession_links(self):
        self.db.execute("""UPDATE facts SET superseded_by=(
            SELECT successor.id FROM facts successor
            WHERE successor.claim_id=facts.supersedes_claim_id)
            WHERE supersedes_claim_id<>''""")

    def conflicts(self, *, status: str = "open", limit: int = 100) -> list[dict]:
        rows = self.db.execute("""SELECT * FROM memory_sync_conflicts WHERE status=?
                                  ORDER BY created_at DESC LIMIT ?""",
                               (str(status), max(1, min(500, int(limit))))).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["local_payload"] = json.loads(item.pop("local_payload_json") or "{}")
            item["remote_payload"] = json.loads(item.pop("remote_payload_json") or "{}")
            out.append(item)
        return out

    def _touch_claim(self, claim_id: str) -> bool:
        row = self.db.execute("SELECT * FROM facts WHERE claim_id=?", (claim_id,)).fetchone()
        if not row:
            tomb = self.db.execute(
                "SELECT * FROM memory_claim_tombstones WHERE claim_id=?", (claim_id,)).fetchone()
            if not tomb:
                return False
            base, revision = int(tomb["revision"]), int(tomb["revision"]) + 1
            payload_json = tomb["payload_json"] or "{}"
            self.db.execute("""UPDATE memory_claim_tombstones SET revision=?,origin_device=?,
                               deleted_at=? WHERE claim_id=?""",
                            (revision, self.device_id, int(time.time()), claim_id))
            operation = "delete"
        else:
            base, revision = int(row["revision"]), int(row["revision"]) + 1
            payload_json = json.dumps(payload_from_row(row), ensure_ascii=False,
                                      separators=(",", ":"))
            with _remote(self.memory, self.device_id):
                self.db.execute("""UPDATE facts SET revision=?,origin_device=?,updated_at=?
                                   WHERE claim_id=?""",
                                (revision, self.device_id, int(time.time()), claim_id))
            operation = "upsert"
        self.db.execute("""INSERT INTO memory_claim_changes(
            change_id,claim_id,operation,base_revision,revision,origin_device,hlc,changed_at,payload_json)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (_change_id(), claim_id, operation, base, revision, self.device_id,
             "%013d:000000:%s" % (int(time.time() * 1000), self.device_id),
             int(time.time()), payload_json))
        return True

    def resolve_conflict(self, conflict_id: str, resolution: str) -> dict | None:
        if resolution not in ("local", "remote"):
            raise MemoryDeltaError("resolution must be local or remote")
        row = self.db.execute("""SELECT * FROM memory_sync_conflicts
                                  WHERE conflict_id=? AND status='open'""",
                              (str(conflict_id),)).fetchone()
        if not row:
            return None
        claim_id = row["claim_id"]
        if resolution == "remote":
            payload = json.loads(row["remote_payload_json"] or "{}")
            current = self._local_payload(claim_id)
            local_revision = current[0] if current else 0
            with _remote(self.memory, self.device_id):
                if row["operation"] == "delete":
                    self._apply_delete(claim_id, local_revision, self.device_id, payload)
                else:
                    self._apply_upsert(claim_id, local_revision, self.device_id, payload)
        self._touch_claim(claim_id)
        now = int(time.time())
        self.db.execute("""UPDATE memory_sync_conflicts SET status=?,resolved_at=?
                           WHERE conflict_id=?""",
                        ("resolved_" + resolution, now, str(conflict_id)))
        self._resolve_supersession_links()
        self.db.commit()
        return {"conflict_id": str(conflict_id), "claim_id": claim_id,
                "resolution": resolution, "status": "resolved_" + resolution,
                "resolved_at": now}
