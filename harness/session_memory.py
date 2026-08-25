"""Indexed episodic/session memory over Collie's durable conversation threads.

The JSON session remains the exact transcript and recovery journal.  This database is a
rebuildable search index: recent summaries for orientation, hybrid fragments for discovery, and
an exact-thread seam for high-stakes recall.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time

from .embeddings import cosine
from .memory import contains_memory_secret, _fts_terms
from .providers import content_text

__all__ = ["SessionMemory", "default_path", "default_session_memory"]


def default_path() -> str:
    override = os.environ.get("COLLIE_SESSION_MEMORY_DB")
    if override:
        return override
    sessions_dir = os.environ.get("COLLIE_SESSIONS_DIR")
    if sessions_dir:
        return os.path.join(os.path.dirname(os.path.abspath(sessions_dir)), "session_memory.db")
    root = os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie")
    return os.path.join(root, "session_memory.db")


class SessionMemory:
    def __init__(self, path: str | None = None, *, embedder=None):
        self.path = path or default_path()
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.embedder = embedder
        self.embed_model = getattr(embedder, "name", "") if embedder else ""
        self.db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=30000")
        except sqlite3.OperationalError:
            pass
        self.has_fts = True
        self._init_schema()
        from .session_sync import prepare_connection
        prepare_connection(self)

    def _init_schema(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS session_index(
                session_id TEXT PRIMARY KEY,project TEXT NOT NULL,cwd TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',summary TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,
                source_updated REAL NOT NULL DEFAULT 0,
                message_count INTEGER NOT NULL DEFAULT 0,omitted_sensitive INTEGER NOT NULL DEFAULT 0,
                source_hash TEXT NOT NULL DEFAULT '',revision INTEGER NOT NULL DEFAULT 0,
                origin_device TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS session_index_project_v1
                ON session_index(project,updated_at DESC);
            CREATE TABLE IF NOT EXISTS session_episodes(
                episode_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,idx INTEGER NOT NULL,
                role TEXT NOT NULL,content TEXT NOT NULL,content_hash TEXT NOT NULL,
                observed_at INTEGER NOT NULL,embed_model TEXT NOT NULL DEFAULT '',
                embedding TEXT NOT NULL DEFAULT '[]',UNIQUE(session_id,idx));
            CREATE INDEX IF NOT EXISTS session_episodes_session_v1
                ON session_episodes(session_id,idx);
        """)
        try:
            self.db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS session_episodes_fts
                USING fts5(content,session_id UNINDEXED,episode_id UNINDEXED,role UNINDEXED)""")
        except sqlite3.OperationalError:
            self.has_fts = False
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(session_index)")}
        if "revision" not in columns:
            self.db.execute(
                "ALTER TABLE session_index ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
        if "origin_device" not in columns:
            self.db.execute(
                "ALTER TABLE session_index ADD COLUMN origin_device TEXT NOT NULL DEFAULT ''")
        if "source_updated" not in columns:
            self.db.execute(
                "ALTER TABLE session_index ADD COLUMN source_updated REAL NOT NULL DEFAULT 0")
        self.db.commit()

    @staticmethod
    def _message_text(message) -> str:
        try:
            return " ".join(content_text(message.get("content", "")).split())[:20000]
        except Exception:
            return ""

    def ingest(self, session_id: str, messages, *, project: str = "global", cwd: str = "",
               title: str = "", updated_at: int | float | None = None,
               summary: str = "") -> dict:
        session_id = str(session_id or "").strip()
        if not session_id or len(session_id) > 160:
            raise ValueError("valid session_id required")
        source_updated = float(updated_at or time.time())
        updated = int(source_updated)
        prepared, omitted = [], 0
        for index, message in enumerate(messages or []):
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "")
            if role not in ("user", "assistant"):
                continue
            text = self._message_text(message)
            if not text:
                continue
            if contains_memory_secret(text):
                omitted += 1
                continue
            content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            raw = "%s\0%s\0%s\0%s" % (session_id, index, role, content_hash)
            episode_id = "ep_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
            embedding = "[]"
            if self.embedder:
                try:
                    embedding = json.dumps(self.embedder.embed(text, kind="passage"))
                except Exception:
                    embedding = "[]"
            prepared.append((episode_id, session_id, index, role, text, content_hash,
                             updated, self.embed_model, embedding))
        first_user = next((row[4] for row in prepared if row[3] == "user"), "")
        last_assistant = next((row[4] for row in reversed(prepared) if row[3] == "assistant"), "")
        title = " ".join(str(title or first_user or "Untitled session").split())[:120]
        if not summary:
            parts = [first_user[:220]]
            if last_assistant and last_assistant != first_user:
                parts.append(last_assistant[:320])
            summary = " — ".join(part for part in parts if part)
        summary = " ".join(str(summary or "").split())[:700]
        project = str(project or "global")
        from .session_sync import session_source_hash
        source_hash = session_source_hash(project, title, summary, [
            {"idx": row[2], "role": row[3], "content_hash": row[5]} for row in prepared])
        with self._lock:
            prior = self.db.execute(
                """SELECT created_at,source_hash,source_updated,revision
                   FROM session_index WHERE session_id=?""",
                (session_id,)).fetchone()
            if prior and source_updated < float(prior["source_updated"] or 0):
                return self.get_session(session_id)
            if prior and prior["source_hash"] == source_hash:
                return self.get_session(session_id)
            created = int(prior["created_at"]) if prior else updated
            base_revision = int(prior["revision"] or 0) if prior else 0
            revision = base_revision + 1
            try:
                self.db.execute("BEGIN IMMEDIATE")
                if self.has_fts:
                    self.db.execute("DELETE FROM session_episodes_fts WHERE session_id=?",
                                    (session_id,))
                self.db.execute("DELETE FROM session_episodes WHERE session_id=?", (session_id,))
                self.db.executemany("""INSERT INTO session_episodes(
                    episode_id,session_id,idx,role,content,content_hash,observed_at,embed_model,embedding)
                    VALUES(?,?,?,?,?,?,?,?,?)""", prepared)
                if self.has_fts:
                    self.db.executemany("""INSERT INTO session_episodes_fts(
                        content,session_id,episode_id,role) VALUES(?,?,?,?)""",
                        [(row[4], row[1], row[0], row[3]) for row in prepared])
                self.db.execute("""INSERT INTO session_index(
                    session_id,project,cwd,title,summary,created_at,updated_at,source_updated,message_count,
                    omitted_sensitive,source_hash,revision,origin_device)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(session_id) DO UPDATE SET project=excluded.project,cwd=excluded.cwd,
                    title=excluded.title,summary=excluded.summary,updated_at=excluded.updated_at,
                    source_updated=excluded.source_updated,
                    message_count=excluded.message_count,
                    omitted_sensitive=excluded.omitted_sensitive,source_hash=excluded.source_hash,
                    revision=excluded.revision,origin_device=excluded.origin_device""",
                    (session_id, project, str(cwd or ""), title, summary,
                     created, updated, source_updated, len(prepared), omitted, source_hash, revision,
                     self._session_origin_device))
                self.session_sync().record_upsert(session_id, base_revision, revision)
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        return self.get_session(session_id)

    def ingest_saved(self, session: dict) -> dict:
        return self.ingest(
            session.get("id"), session.get("messages") or [],
            project=session.get("project") or "global", cwd=session.get("cwd") or "",
            title=session.get("title") or "", updated_at=session.get("updated") or time.time(),
            summary=session.get("summary") or "")

    def get_session(self, session_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM session_index WHERE session_id=?", (str(session_id),)).fetchone()
        return dict(row) if row else None

    def recent_threads(self, *, project: str = "", limit: int = 6,
                       exclude_session: str = "") -> list[dict]:
        sql, params = "SELECT * FROM session_index WHERE 1=1", []
        if project:
            sql += " AND project IN (?, 'global')"; params.append(str(project))
        if exclude_session:
            sql += " AND session_id<>?"; params.append(str(exclude_session))
        sql += " ORDER BY updated_at DESC LIMIT ?"; params.append(max(1, min(50, int(limit))))
        return [dict(row) for row in self.db.execute(sql, params).fetchall()]

    def _lexical(self, query: str, project: str, limit: int) -> list[str]:
        if self.has_fts:
            match = " OR ".join(_fts_terms(query))
            if match:
                try:
                    rows = self.db.execute("""SELECT f.episode_id,bm25(session_episodes_fts) score
                        FROM session_episodes_fts f JOIN session_index s USING(session_id)
                        WHERE session_episodes_fts MATCH ?
                          AND (s.project=? OR s.project='global') ORDER BY score LIMIT ?""",
                        (match, project, limit)).fetchall()
                    return [row["episode_id"] for row in rows]
                except sqlite3.OperationalError:
                    pass
        tokens = [token for token in query.split() if len(token) > 2][:4]
        if not tokens:
            return []
        clause = " OR ".join("e.content LIKE ?" for _ in tokens)
        rows = self.db.execute("""SELECT e.episode_id FROM session_episodes e
            JOIN session_index s USING(session_id) WHERE (s.project=? OR s.project='global')
            AND (%s) ORDER BY e.observed_at DESC LIMIT ?""" % clause,
            (project, *["%%%s%%" % token for token in tokens], limit)).fetchall()
        return [row["episode_id"] for row in rows]

    def _dense(self, query: str, project: str, limit: int) -> list[str]:
        if not self.embedder:
            return []
        try:
            vector = self.embedder.embed(query, kind="query")
        except Exception:
            return []
        rows = self.db.execute("""SELECT e.episode_id,e.embedding FROM session_episodes e
            JOIN session_index s USING(session_id)
            WHERE (s.project=? OR s.project='global') AND e.embed_model=?""",
            (project, self.embed_model)).fetchall()
        scored = []
        for row in rows:
            try:
                scored.append((row["episode_id"], cosine(vector, json.loads(row["embedding"]))))
            except Exception:
                pass
        return [episode_id for episode_id, _score in
                sorted(scored, key=lambda item: item[1], reverse=True)[:limit]]

    def search(self, query: str, *, project: str = "global", limit: int = 12,
               exclude_session: str = "") -> list[dict]:
        limit = max(1, min(100, int(limit)))
        lexical = self._lexical(query, project, max(limit, 30))
        dense = self._dense(query, project, max(limit, 30))
        scores = {}
        sources = {}
        for source, ranked in (("lexical", lexical), ("dense", dense)):
            for position, episode_id in enumerate(ranked):
                scores[episode_id] = scores.get(episode_id, 0.0) + 1.0 / (60 + position + 1)
                sources.setdefault(episode_id, []).append(source)
        ranked = [item[0] for item in sorted(scores.items(), key=lambda item: item[1], reverse=True)]
        if not ranked:
            return []
        q = ",".join("?" * len(ranked))
        rows = self.db.execute("""SELECT e.*,s.title,s.summary,s.project,s.updated_at
            FROM session_episodes e JOIN session_index s USING(session_id)
            WHERE e.episode_id IN (%s)""" % q, ranked).fetchall()
        by_id = {row["episode_id"]: row for row in rows}
        out = []
        for episode_id in ranked:
            row = by_id.get(episode_id)
            if not row or (exclude_session and row["session_id"] == exclude_session):
                continue
            item = dict(row)
            item.pop("embedding", None)
            item["score"] = round(scores[episode_id], 6)
            item["retrieval_sources"] = sources[episode_id]
            out.append(item)
            if len(out) >= limit:
                break
        return out

    def related(self, query: str, *, project: str = "global", current_session: str = "",
                thread_limit: int = 4, fragment_limit: int = 8) -> dict:
        return {"recent_threads": self.recent_threads(
                    project=project, limit=thread_limit, exclude_session=current_session),
                "fragments": self.search(
                    query, project=project, limit=fragment_limit,
                    exclude_session=current_session)}

    def open_thread(self, session_id: str) -> dict | None:
        from . import sessions
        checked = sessions.load_checked(str(session_id))
        if checked.get("status") == "ok":
            return checked.get("session")
        meta = self.get_session(session_id)
        if not meta:
            return None
        episodes = self.db.execute("""SELECT role,content,idx,observed_at
            FROM session_episodes WHERE session_id=? ORDER BY idx""",
            (str(session_id),)).fetchall()
        return {"id": str(session_id), "project": meta["project"], "title": meta["title"],
                "summary": meta["summary"], "updated": meta["updated_at"],
                "messages": [{"role": row["role"], "content": row["content"]}
                             for row in episodes],
                "archive_only": True,
                "notice": "Replicated safe dialogue; tool/system and sensitive messages omitted."}

    def delete(self, session_id: str) -> bool:
        with self._lock:
            exists = self.get_session(session_id)
            if not exists:
                return False
            try:
                self.db.execute("BEGIN IMMEDIATE")
                base_revision = int(exists.get("revision") or 0)
                self._delete_index_rows(str(session_id))
                self.session_sync().record_delete(
                    str(session_id), base_revision, base_revision + 1)
                minimal = json.dumps({"session_id": str(session_id)}, separators=(",", ":"))
                self.db.execute("""DELETE FROM session_memory_changes
                    WHERE session_id=? AND operation<>'delete'""", (str(session_id),))
                self.db.execute("""UPDATE session_memory_sync_conflicts
                    SET local_payload_json=?,remote_payload_json=? WHERE session_id=?""",
                    (minimal, minimal, str(session_id)))
                self.db.commit()
                return True
            except Exception:
                self.db.rollback()
                raise

    def _delete_index_rows(self, session_id: str) -> None:
        if self.has_fts:
            self.db.execute("DELETE FROM session_episodes_fts WHERE session_id=?", (session_id,))
        self.db.execute("DELETE FROM session_episodes WHERE session_id=?", (session_id,))
        self.db.execute("DELETE FROM session_index WHERE session_id=?", (session_id,))

    def _replace_synced(self, payload: dict, revision: int, origin_device: str) -> None:
        """Replace one derived archive thread from an already validated wire payload."""
        session_id = payload["session_id"]
        prepared = []
        for episode in payload["episodes"]:
            text = episode["content"]
            embedding = "[]"
            if self.embedder:
                try:
                    embedding = json.dumps(self.embedder.embed(text, kind="passage"))
                except Exception:
                    pass
            prepared.append((episode["episode_id"], session_id, int(episode["idx"]),
                             episode["role"], text, episode["content_hash"],
                             int(episode["observed_at"]), self.embed_model, embedding))
        self._delete_index_rows(session_id)
        self.db.executemany("""INSERT INTO session_episodes(
            episode_id,session_id,idx,role,content,content_hash,observed_at,embed_model,embedding)
            VALUES(?,?,?,?,?,?,?,?,?)""", prepared)
        if self.has_fts:
            self.db.executemany("""INSERT INTO session_episodes_fts(
                content,session_id,episode_id,role) VALUES(?,?,?,?)""",
                [(row[4], row[1], row[0], row[3]) for row in prepared])
        self.db.execute("""INSERT INTO session_index(
            session_id,project,cwd,title,summary,created_at,updated_at,source_updated,message_count,
            omitted_sensitive,source_hash,revision,origin_device)
            VALUES(?,?,'',?,?,?,?,?,?,?,?,?,?)""",
            (session_id, payload["project"], payload["title"], payload["summary"],
             int(payload["created_at"]), int(payload["updated_at"]),
             float(payload["source_updated"]), len(prepared),
             int(payload["omitted_sensitive"]), payload["source_hash"], int(revision),
             str(origin_device)))

    def session_sync(self):
        from .session_sync import SessionSync
        return SessionSync(self)

    def close(self):
        self.db.close()


_SINGLETON = None
_SINGLETON_PATH = None
_SINGLETON_LOCK = threading.Lock()


def default_session_memory(*, embedder=None, path: str | None = None) -> SessionMemory:
    global _SINGLETON, _SINGLETON_PATH
    path = path or default_path()
    with _SINGLETON_LOCK:
        if _SINGLETON is None or _SINGLETON_PATH != path:
            if _SINGLETON is not None:
                try:
                    _SINGLETON.close()
                except Exception:
                    pass
            _SINGLETON = SessionMemory(path, embedder=embedder)
            _SINGLETON_PATH = path
        elif embedder is not None and _SINGLETON.embedder is None:
            _SINGLETON.embedder = embedder
            _SINGLETON.embed_model = getattr(embedder, "name", "")
        return _SINGLETON
