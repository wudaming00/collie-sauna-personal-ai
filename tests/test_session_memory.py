"""Session Memory indexes safe dialogue while exact threads remain in the session journal."""
import json
import sqlite3

import pytest

from harness import sessions
from harness.session_memory import SessionMemory
from harness.session_sync import SessionDeltaError


def test_session_archive_search_update_sensitive_omission_and_delete(monkeypatch, tmp_path):
    session_dir = tmp_path / "sessions"
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(session_dir))
    monkeypatch.setenv("COLLIE_SESSION_MEMORY_DB", str(tmp_path / "session_memory.db"))
    sid = "session-one"
    sessions.save(sid, [
        {"role": "user", "content": "We decided Orion uses PostgreSQL"},
        {"role": "tool", "content": "raw tool output should not be indexed"},
        {"role": "assistant", "content": "The database decision is recorded"},
        {"role": "user", "content": "password = do-not-store-this-value"},
    ], project="repo", answer="recorded")

    archive = SessionMemory(str(tmp_path / "session_memory.db"))
    try:
        meta = archive.get_session(sid)
        assert meta["message_count"] == 2 and meta["omitted_sensitive"] == 1
        hits = archive.search("Orion PostgreSQL", project="repo")
        assert hits[0]["session_id"] == sid
        assert all("do-not-store" not in hit["content"] for hit in hits)
        assert archive.open_thread(sid)["messages"][-1]["content"] == \
            "password = do-not-store-this-value"
    finally:
        archive.close()

    sessions.append_exchange(sid, "What did we choose?", "PostgreSQL", project="repo")
    archive = SessionMemory(str(tmp_path / "session_memory.db"))
    try:
        assert archive.get_session(sid)["message_count"] == 4
        assert len(archive.search("PostgreSQL", project="repo")) >= 2
    finally:
        archive.close()
    assert sessions.delete(sid)
    archive = SessionMemory(str(tmp_path / "session_memory.db"))
    try:
        assert archive.get_session(sid) is None
    finally:
        archive.close()


def test_session_delta_replication_is_safe_idempotent_and_deletes(tmp_path):
    source = SessionMemory(str(tmp_path / "source.db"))
    target = SessionMemory(str(tmp_path / "target.db"))
    try:
        source.ingest("shared", [
            {"role": "user", "content": "Orion chose PostgreSQL"},
            {"role": "tool", "content": "tool output never crosses the wire"},
            {"role": "assistant", "content": "Decision recorded"},
            {"role": "user", "content": "password = never-replicate-this"},
        ], project="repo", cwd="C:/private/repo", updated_at=100)
        delta = source.session_sync().changes_since(0, allowed_projects=["repo"])
        wire = json.dumps(delta)
        assert "C:/private" not in wire and "tool output" not in wire
        assert "never-replicate" not in wire
        result = target.session_sync().apply_delta(delta, peer_id="source")
        assert result == {"applied": 1, "replayed": 0, "conflicts": 0,
                          "cursor": delta["cursor"]}
        assert target.get_session("shared")["revision"] == 1
        assert target.open_thread("shared")["archive_only"] is True
        assert [row["role"] for row in target.open_thread("shared")["messages"]] == [
            "user", "assistant"]
        assert target.session_sync().apply_delta(delta, peer_id="source")["replayed"] == 1

        source.ingest("shared", [
            {"role": "user", "content": "Orion chose PostgreSQL"},
            {"role": "assistant", "content": "Decision recorded and approved"},
        ], project="repo", updated_at=101)
        update = source.session_sync().changes_since(delta["cursor"], allowed_projects=["repo"])
        assert target.session_sync().apply_delta(update, peer_id="source")["applied"] == 1
        assert "approved" in target.open_thread("shared")["messages"][-1]["content"]

        assert source.delete("shared")
        deletion = source.session_sync().changes_since(
            update["cursor"], allowed_projects=["repo"])
        assert "Orion" not in json.dumps(deletion)
        assert target.session_sync().apply_delta(deletion, peer_id="source")["applied"] == 1
        assert target.get_session("shared") is None
        assert target.session_sync().status()["tombstones"] == 1
        assert source.db.execute(
            "SELECT COUNT(*) FROM session_memory_changes WHERE operation='upsert'").fetchone()[0] == 0

        late = SessionMemory(str(tmp_path / "late.db"))
        try:
            applied = late.session_sync().apply_delta(deletion, peer_id="source")
            assert applied["applied"] == 1 and applied["conflicts"] == 0
        finally:
            late.close()
    finally:
        source.close(); target.close()


def test_session_delta_scope_validation_and_conflict_preservation(tmp_path):
    left = SessionMemory(str(tmp_path / "left.db"))
    right = SessionMemory(str(tmp_path / "right.db"))
    try:
        left.ingest("global", [{"role": "user", "content": "Global thread"}],
                    project="global", updated_at=1)
        left.ingest("private", [{"role": "user", "content": "Private thread"}],
                    project="repo", updated_at=1)
        scoped = left.session_sync().changes_since(0, allowed_projects=["global"])
        assert len(scoped["changes"]) == 1 and scoped["withheld"] == 1
        bad = json.loads(json.dumps(scoped))
        bad["changes"][0]["change_id"] += "x"
        bad["changes"][0]["payload"]["cwd"] = "C:/leak"
        with pytest.raises(SessionDeltaError):
            right.session_sync().apply_delta(bad, peer_id="bad")

        initial = left.session_sync().changes_since(0, allowed_projects=["repo"])
        private_change = next(change for change in initial["changes"]
                              if change["session_id"] == "private")
        private_page = {**initial, "changes": [private_change]}
        assert right.session_sync().apply_delta(private_page, peer_id="left")["applied"] == 1
        left.ingest("private", [{"role": "user", "content": "Left edit"}],
                    project="repo", updated_at=2)
        right.ingest("private", [{"role": "user", "content": "Right edit"}],
                     project="repo", updated_at=2)
        update = left.session_sync().changes_since(initial["cursor"], allowed_projects=["repo"])
        result = right.session_sync().apply_delta(update, peer_id="left")
        assert result["conflicts"] == 1
        assert right.open_thread("private")["messages"][0]["content"] == "Right edit"
        conflict = right.session_sync().conflicts()[0]
        assert conflict["remote_payload"]["episodes"][0]["content"] == "Left edit"
        resolved = right.session_sync().resolve_conflict(conflict["conflict_id"], "remote")
        assert resolved["status"] == "resolved_remote" and resolved["revision"] == 3
        assert right.open_thread("private")["messages"][0]["content"] == "Left edit"
        assert right.session_sync().status()["open_conflicts"] == 0
    finally:
        left.close(); right.close()


def test_session_archive_rejects_a_late_stale_projection(tmp_path):
    archive = SessionMemory(str(tmp_path / "ordering.db"))
    try:
        archive.ingest("race", [{"role": "user", "content": "first"}], updated_at=10.1)
        archive.ingest("race", [{"role": "user", "content": "newest"}], updated_at=10.3)
        archive.ingest("race", [{"role": "user", "content": "stale"}], updated_at=10.2)
        assert archive.open_thread("race")["messages"][0]["content"] == "newest"
        assert archive.get_session("race")["revision"] == 2
    finally:
        archive.close()


def test_legacy_session_archive_migrates_and_can_be_requeued(tmp_path):
    path = tmp_path / "legacy.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE session_index(
            session_id TEXT PRIMARY KEY,project TEXT NOT NULL,cwd TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',summary TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0,omitted_sensitive INTEGER NOT NULL DEFAULT 0,
            source_hash TEXT NOT NULL DEFAULT '');
        CREATE TABLE session_episodes(
            episode_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,idx INTEGER NOT NULL,
            role TEXT NOT NULL,content TEXT NOT NULL,content_hash TEXT NOT NULL,
            observed_at INTEGER NOT NULL,embed_model TEXT NOT NULL DEFAULT '',
            embedding TEXT NOT NULL DEFAULT '[]',UNIQUE(session_id,idx));
        INSERT INTO session_index VALUES(
            'legacy','global','','Old thread','Old summary',100,101,0,0,'old-hash');
    """)
    db.commit(); db.close()
    archive = SessionMemory(str(path))
    try:
        row = archive.get_session("legacy")
        assert row["revision"] == 1 and row["origin_device"].startswith("sessdev_")
        assert row["source_updated"] == 101
        assert archive.session_sync().requeue_current() == 1
        delta = archive.session_sync().changes_since(0)
        assert delta["changes"][0]["revision"] == 2
    finally:
        archive.close()
