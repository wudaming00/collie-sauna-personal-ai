"""Privacy-gated, versioned replication for the rebuildable Session Memory archive.

Only safe user/assistant episodes cross this boundary.  Local paths, tool/system messages,
embeddings and exact session-journal files never do.  The archive can therefore be rebuilt on a
peer without making indexed historical speech an authoritative Memory claim.
"""
from __future__ import annotations

import hashlib
import json
import math
import secrets
import time

from .memory import contains_memory_secret

SESSION_DELTA_FORMAT = "collie-session-memory-delta/1"
_PAYLOAD_KEYS = frozenset((
    "session_id", "project", "title", "summary", "created_at", "updated_at", "source_updated",
    "omitted_sensitive", "source_hash", "episodes",
))
_EPISODE_KEYS = frozenset((
    "episode_id", "idx", "role", "content", "content_hash", "observed_at",
))


class SessionDeltaError(ValueError):
    pass


def session_source_hash(project, title, summary, episodes) -> str:
    rows = [(int(row["idx"]), str(row["role"]), str(row["content_hash"]))
            for row in episodes]
    return hashlib.sha256(json.dumps({
        "project": str(project), "title": str(title), "summary": str(summary),
        "episodes": rows,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def prepare_connection(archive) -> None:
    db = archive.db
    db.executescript("""
        CREATE TABLE IF NOT EXISTS session_memory_meta(
            key TEXT PRIMARY KEY,value TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS session_memory_changes(
            seq INTEGER PRIMARY KEY AUTOINCREMENT,change_id TEXT NOT NULL UNIQUE,
            session_id TEXT NOT NULL,operation TEXT NOT NULL,base_revision INTEGER NOT NULL,
            revision INTEGER NOT NULL,origin_device TEXT NOT NULL,changed_at INTEGER NOT NULL,
            payload_json TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS session_memory_changes_seq_v1
            ON session_memory_changes(seq);
        CREATE TABLE IF NOT EXISTS session_memory_tombstones(
            session_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,origin_device TEXT NOT NULL,
            deleted_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS session_memory_sync_applied(
            peer_id TEXT NOT NULL,change_id TEXT NOT NULL,applied_at INTEGER NOT NULL,
            PRIMARY KEY(peer_id,change_id));
        CREATE TABLE IF NOT EXISTS session_memory_sync_peers(
            peer_id TEXT PRIMARY KEY,push_cursor INTEGER NOT NULL DEFAULT 0,
            pull_cursor INTEGER NOT NULL DEFAULT 0,updated_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS session_memory_sync_conflicts(
            conflict_id TEXT PRIMARY KEY,peer_id TEXT NOT NULL,change_id TEXT NOT NULL,
            session_id TEXT NOT NULL,local_revision INTEGER NOT NULL,
            remote_base_revision INTEGER NOT NULL,remote_revision INTEGER NOT NULL,
            operation TEXT NOT NULL,local_payload_json TEXT NOT NULL,
            remote_payload_json TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'open',
            created_at INTEGER NOT NULL,resolved_at INTEGER);
    """)
    row = db.execute(
        "SELECT value FROM session_memory_meta WHERE key='device_id'").fetchone()
    device_id = str(row[0]) if row and row[0] else "sessdev_" + secrets.token_hex(12)
    db.execute("INSERT OR REPLACE INTO session_memory_meta(key,value) VALUES('device_id',?)",
               (device_id,))
    archive._session_origin_device = device_id
    db.execute("""UPDATE session_index SET source_updated=CASE
                    WHEN updated_at>0 THEN updated_at ELSE ? END
                  WHERE source_updated<=0""", (time.time(),))
    db.execute("""UPDATE session_index SET revision=1,origin_device=?
                  WHERE revision<1 OR origin_device=''""", (device_id,))
    for session in db.execute(
            "SELECT session_id,project,title,summary FROM session_index").fetchall():
        episodes = db.execute("""SELECT idx,role,content_hash FROM session_episodes
            WHERE session_id=? ORDER BY idx""", (session["session_id"],)).fetchall()
        canonical_hash = session_source_hash(
            session["project"], session["title"], session["summary"], episodes)
        db.execute("UPDATE session_index SET source_hash=? WHERE session_id=?",
                   (canonical_hash, session["session_id"]))
    for tombstone in db.execute(
            "SELECT session_id FROM session_memory_tombstones").fetchall():
        session_id = tombstone["session_id"]
        minimal = json.dumps({"session_id": session_id}, separators=(",", ":"))
        db.execute("""DELETE FROM session_memory_changes
                      WHERE session_id=? AND operation<>'delete'""", (session_id,))
        db.execute("""UPDATE session_memory_sync_conflicts
            SET local_payload_json=?,remote_payload_json=? WHERE session_id=?""",
            (minimal, minimal, session_id))
    db.commit()


class SessionSync:
    def __init__(self, archive):
        self.archive = archive
        self.db = archive.db
        self.device_id = archive._session_origin_device

    def _payload(self, session_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM session_index WHERE session_id=?", (str(session_id),)).fetchone()
        if not row:
            return None
        episodes = self.db.execute("""SELECT episode_id,idx,role,content,content_hash,observed_at
            FROM session_episodes WHERE session_id=? ORDER BY idx""", (str(session_id),)).fetchall()
        return {
            "session_id": row["session_id"], "project": row["project"],
            "title": row["title"], "summary": row["summary"],
            "created_at": int(row["created_at"]), "updated_at": int(row["updated_at"]),
            "source_updated": float(row["source_updated"]),
            "omitted_sensitive": int(row["omitted_sensitive"]),
            "source_hash": row["source_hash"],
            "episodes": [{key: episode[key] for key in _EPISODE_KEYS} for episode in episodes],
        }

    def _append(self, session_id: str, operation: str, base_revision: int,
                revision: int, payload: dict) -> None:
        self.db.execute("""INSERT INTO session_memory_changes(
            change_id,session_id,operation,base_revision,revision,origin_device,changed_at,payload_json)
            VALUES(?,?,?,?,?,?,?,?)""",
            ("schg_" + secrets.token_hex(16), str(session_id), operation,
             int(base_revision), int(revision), self.device_id, int(time.time()),
             json.dumps(payload, ensure_ascii=False, separators=(",", ":"))))

    def record_upsert(self, session_id: str, base_revision: int, revision: int) -> None:
        payload = self._payload(session_id)
        if payload is None:
            raise SessionDeltaError("cannot record a missing session")
        self._append(session_id, "upsert", base_revision, revision, payload)

    def record_delete(self, session_id: str, base_revision: int, revision: int) -> None:
        now = int(time.time())
        self.db.execute("""INSERT OR REPLACE INTO session_memory_tombstones(
            session_id,revision,origin_device,deleted_at) VALUES(?,?,?,?)""",
            (str(session_id), int(revision), self.device_id, now))
        self._append(session_id, "delete", base_revision, revision,
                     {"session_id": str(session_id)})

    def requeue_current(self) -> int:
        count = 0
        rows = self.db.execute(
            "SELECT session_id,revision FROM session_index ORDER BY session_id").fetchall()
        for row in rows:
            base = int(row["revision"])
            revision = base + 1
            self.db.execute("""UPDATE session_index SET revision=?,origin_device=?
                               WHERE session_id=?""",
                            (revision, self.device_id, row["session_id"]))
            self.record_upsert(row["session_id"], base, revision)
            count += 1
        tombstones = self.db.execute(
            "SELECT session_id,revision FROM session_memory_tombstones ORDER BY session_id").fetchall()
        for row in tombstones:
            base = int(row["revision"])
            self.record_delete(row["session_id"], base, base + 1)
            count += 1
        self.db.commit()
        return count

    def peer_cursor(self, peer_id: str) -> dict:
        row = self.db.execute(
            "SELECT * FROM session_memory_sync_peers WHERE peer_id=?", (str(peer_id),)).fetchone()
        return dict(row) if row else {"peer_id": str(peer_id), "push_cursor": 0,
                                      "pull_cursor": 0, "updated_at": 0}

    def set_push_cursor(self, peer_id: str, cursor: int) -> None:
        self.db.execute("""INSERT INTO session_memory_sync_peers(
            peer_id,push_cursor,pull_cursor,updated_at) VALUES(?,?,0,?)
            ON CONFLICT(peer_id) DO UPDATE SET push_cursor=MAX(push_cursor,excluded.push_cursor),
            updated_at=excluded.updated_at""",
            (str(peer_id), max(0, int(cursor)), int(time.time())))
        self.db.commit()

    def changes_since(self, cursor: int = 0, *, allowed_projects=None,
                      limit: int = 200) -> dict:
        cursor = max(0, int(cursor))
        limit = max(1, min(1000, int(limit)))
        rows = self.db.execute("""SELECT * FROM session_memory_changes WHERE seq>?
            ORDER BY seq LIMIT ?""", (cursor, limit)).fetchall()
        allowed = None if allowed_projects is None else {
            str(value) for value in allowed_projects if str(value)}
        changes, withheld = [], 0
        next_cursor = cursor
        for row in rows:
            next_cursor = int(row["seq"])
            payload = json.loads(row["payload_json"])
            if (allowed is not None and row["operation"] == "upsert" and
                    payload.get("project") not in allowed):
                withheld += 1
                continue
            changes.append({
                "change_id": row["change_id"], "session_id": row["session_id"],
                "operation": row["operation"], "base_revision": int(row["base_revision"]),
                "revision": int(row["revision"]), "origin_device": row["origin_device"],
                "changed_at": int(row["changed_at"]), "payload": payload,
            })
        has_more = bool(self.db.execute(
            "SELECT 1 FROM session_memory_changes WHERE seq>? LIMIT 1", (next_cursor,)).fetchone())
        return {"format": SESSION_DELTA_FORMAT, "source_device": self.device_id,
                "from_cursor": cursor, "cursor": next_cursor, "has_more": has_more,
                "withheld": withheld, "changes": changes}

    @staticmethod
    def _validate_payload(operation: str, payload) -> dict:
        if not isinstance(payload, dict):
            raise SessionDeltaError("session payload must be an object")
        if operation == "delete":
            if set(payload) != {"session_id"}:
                raise SessionDeltaError("session delete payload is malformed")
            session_id = str(payload.get("session_id") or "")
            if not session_id or len(session_id) > 160:
                raise SessionDeltaError("invalid session id")
            return {"session_id": session_id}
        if set(payload) != _PAYLOAD_KEYS:
            raise SessionDeltaError("session upsert payload is incomplete or has unknown fields")
        session_id = str(payload.get("session_id") or "")
        project = str(payload.get("project") or "")
        title = str(payload.get("title") or "")
        summary = str(payload.get("summary") or "")
        if (not session_id or len(session_id) > 160 or not project or len(project) > 240 or
                len(title) > 120 or len(summary) > 700):
            raise SessionDeltaError("session metadata exceeds the protocol limits")
        try:
            created_at = int(payload["created_at"])
            updated_at = int(payload["updated_at"])
            source_updated = float(payload["source_updated"])
            omitted = int(payload["omitted_sensitive"])
        except (TypeError, ValueError):
            raise SessionDeltaError("session timestamps and counters must be integers")
        if not math.isfinite(source_updated) or source_updated <= 0:
            raise SessionDeltaError("session source_updated must be finite and positive")
        episodes = payload.get("episodes")
        if not isinstance(episodes, list) or len(episodes) > 10000:
            raise SessionDeltaError("session episodes must be a bounded array")
        clean = []
        for episode in episodes:
            if not isinstance(episode, dict) or set(episode) != _EPISODE_KEYS:
                raise SessionDeltaError("session episode is malformed")
            episode_id = str(episode.get("episode_id") or "")
            role = str(episode.get("role") or "")
            content = str(episode.get("content") or "")
            content_hash = str(episode.get("content_hash") or "")
            try:
                idx = int(episode["idx"])
                observed_at = int(episode["observed_at"])
            except (TypeError, ValueError):
                raise SessionDeltaError("session episode positions must be integers")
            if (not episode_id.startswith("ep_") or role not in ("user", "assistant") or
                    not content or len(content) > 20000 or idx < 0 or
                    hashlib.sha256(content.encode("utf-8")).hexdigest() != content_hash):
                raise SessionDeltaError("session episode failed integrity validation")
            clean.append({"episode_id": episode_id, "idx": idx, "role": role,
                          "content": content, "content_hash": content_hash,
                          "observed_at": observed_at})
        normalized = {"session_id": session_id, "project": project, "title": title,
                      "summary": summary, "created_at": created_at, "updated_at": updated_at,
                      "source_updated": source_updated,
                      "omitted_sensitive": max(0, omitted),
                      "source_hash": str(payload.get("source_hash") or ""), "episodes": clean}
        calculated = session_source_hash(project, title, summary, clean)
        if normalized["source_hash"] != calculated or contains_memory_secret(normalized):
            raise SessionDeltaError("session payload failed hash or sensitive-data validation")
        return normalized

    def _local_revision(self, session_id: str) -> int:
        row = self.db.execute(
            "SELECT revision FROM session_index WHERE session_id=?", (session_id,)).fetchone()
        if row:
            return int(row[0])
        row = self.db.execute(
            "SELECT revision FROM session_memory_tombstones WHERE session_id=?", (session_id,)).fetchone()
        return int(row[0]) if row else 0

    def _apply_upsert(self, payload: dict, revision: int, origin: str) -> None:
        self.archive._replace_synced(payload, revision, origin)
        self.db.execute("DELETE FROM session_memory_tombstones WHERE session_id=?",
                        (payload["session_id"],))

    def _apply_delete(self, session_id: str, revision: int, origin: str) -> None:
        self.archive._delete_index_rows(session_id)
        minimal = json.dumps({"session_id": session_id}, separators=(",", ":"))
        self.db.execute("""DELETE FROM session_memory_changes
            WHERE session_id=? AND operation<>'delete'""", (session_id,))
        self.db.execute("""UPDATE session_memory_sync_conflicts
            SET local_payload_json=?,remote_payload_json=? WHERE session_id=?""",
            (minimal, minimal, session_id))
        self.db.execute("""INSERT OR REPLACE INTO session_memory_tombstones(
            session_id,revision,origin_device,deleted_at) VALUES(?,?,?,?)""",
            (session_id, int(revision), str(origin), int(time.time())))

    def apply_delta(self, delta: dict, *, peer_id: str) -> dict:
        if not isinstance(delta, dict) or delta.get("format") != SESSION_DELTA_FORMAT:
            raise SessionDeltaError("unsupported Session Memory delta format")
        raw_changes = delta.get("changes")
        if not isinstance(raw_changes, list) or len(raw_changes) > 1000:
            raise SessionDeltaError("Session Memory delta changes must be a bounded array")
        normalized = []
        for change in raw_changes:
            if not isinstance(change, dict):
                raise SessionDeltaError("invalid Session Memory change")
            change_id = str(change.get("change_id") or "")
            session_id = str(change.get("session_id") or "")
            operation = str(change.get("operation") or "")
            if not change_id.startswith("schg_") or operation not in ("upsert", "delete"):
                raise SessionDeltaError("invalid Session Memory change identity")
            try:
                base = int(change["base_revision"]); revision = int(change["revision"])
            except (KeyError, TypeError, ValueError):
                raise SessionDeltaError("Session Memory revisions must be integers")
            if base < 0 or revision <= base:
                raise SessionDeltaError("invalid Session Memory revision transition")
            payload = self._validate_payload(operation, change.get("payload"))
            if session_id != payload["session_id"]:
                raise SessionDeltaError("Session Memory payload identity mismatch")
            normalized.append((change_id, session_id, operation, base, revision,
                               str(change.get("origin_device") or
                                   delta.get("source_device") or "remote"), payload))
        applied = replayed = conflicted = 0
        with self.archive._lock:
            for change_id, session_id, operation, base, revision, origin, payload in normalized:
                if self.db.execute("""SELECT 1 FROM session_memory_sync_applied
                    WHERE peer_id=? AND change_id=?""", (str(peer_id), change_id)).fetchone():
                    replayed += 1
                    continue
                local_revision = self._local_revision(session_id)
                if local_revision != base and not (operation == "delete" and local_revision == 0):
                    local_payload = self._payload(session_id) or {"session_id": session_id}
                    self.db.execute("""INSERT INTO session_memory_sync_conflicts(
                        conflict_id,peer_id,change_id,session_id,local_revision,
                        remote_base_revision,remote_revision,operation,local_payload_json,
                        remote_payload_json,status,created_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,'open',?)""",
                        ("sconf_" + secrets.token_hex(12), str(peer_id), change_id, session_id,
                         local_revision, base, revision, operation,
                         json.dumps(local_payload, ensure_ascii=False),
                         json.dumps(payload, ensure_ascii=False), int(time.time())))
                    conflicted += 1
                elif operation == "upsert":
                    self._apply_upsert(payload, revision, origin); applied += 1
                else:
                    self._apply_delete(session_id, revision, origin); applied += 1
                self.db.execute("""INSERT OR IGNORE INTO session_memory_sync_applied(
                    peer_id,change_id,applied_at) VALUES(?,?,?)""",
                    (str(peer_id), change_id, int(time.time())))
                self.db.commit()
            pull_cursor = max(0, int(delta.get("cursor") or 0))
            self.db.execute("""INSERT INTO session_memory_sync_peers(
                peer_id,push_cursor,pull_cursor,updated_at) VALUES(?,0,?,?)
                ON CONFLICT(peer_id) DO UPDATE SET
                pull_cursor=MAX(pull_cursor,excluded.pull_cursor),updated_at=excluded.updated_at""",
                (str(peer_id), pull_cursor, int(time.time())))
            self.db.commit()
        return {"applied": applied, "replayed": replayed, "conflicts": conflicted,
                "cursor": int(delta.get("cursor") or 0)}

    def conflicts(self, *, status: str = "open", limit: int = 100) -> list[dict]:
        rows = self.db.execute("""SELECT * FROM session_memory_sync_conflicts
            WHERE status=? ORDER BY created_at DESC LIMIT ?""",
            (str(status), max(1, min(500, int(limit))))).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["local_payload"] = json.loads(item.pop("local_payload_json"))
            item["remote_payload"] = json.loads(item.pop("remote_payload_json"))
            out.append(item)
        return out

    def resolve_conflict(self, conflict_id: str, resolution: str) -> dict:
        if resolution not in ("local", "remote"):
            raise SessionDeltaError("Session Memory conflict resolution must be local or remote")
        row = self.db.execute("""SELECT * FROM session_memory_sync_conflicts
            WHERE conflict_id=?""", (str(conflict_id),)).fetchone()
        if not row:
            raise SessionDeltaError("unknown Session Memory conflict")
        if row["status"] != "open":
            return {"conflict_id": row["conflict_id"], "status": row["status"]}
        session_id = row["session_id"]
        local_revision = self._local_revision(session_id)
        revision = max(local_revision, int(row["remote_revision"])) + 1
        with self.archive._lock:
            if resolution == "remote":
                operation = row["operation"]
                payload = self._validate_payload(
                    operation, json.loads(row["remote_payload_json"]))
                if operation == "upsert":
                    self._apply_upsert(payload, revision, self.device_id)
                else:
                    self._apply_delete(session_id, revision, self.device_id)
            else:
                payload = self._payload(session_id)
                operation = "upsert" if payload is not None else "delete"
                if payload is not None:
                    self.db.execute("""UPDATE session_index SET revision=?,origin_device=?
                        WHERE session_id=?""", (revision, self.device_id, session_id))
                    payload = self._payload(session_id)
                else:
                    payload = {"session_id": session_id}
                    self._apply_delete(session_id, revision, self.device_id)
            self._append(session_id, operation, local_revision, revision, payload)
            status = "resolved_" + resolution
            self.db.execute("""UPDATE session_memory_sync_conflicts
                SET status=?,resolved_at=? WHERE conflict_id=?""",
                (status, int(time.time()), row["conflict_id"]))
            self.db.commit()
        return {"conflict_id": row["conflict_id"], "session_id": session_id,
                "status": status, "revision": revision}

    def status(self) -> dict:
        return {"format": SESSION_DELTA_FORMAT, "device_id": self.device_id,
                "sessions": int(self.db.execute("SELECT COUNT(*) FROM session_index").fetchone()[0]),
                "tombstones": int(self.db.execute(
                    "SELECT COUNT(*) FROM session_memory_tombstones").fetchone()[0]),
                "pending_changes": int(self.db.execute(
                    "SELECT COUNT(*) FROM session_memory_changes").fetchone()[0]),
                "open_conflicts": int(self.db.execute("""SELECT COUNT(*)
                    FROM session_memory_sync_conflicts WHERE status='open'""").fetchone()[0])}
