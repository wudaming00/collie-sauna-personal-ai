"""Personal State Model — the structured, local-first record of the person behind this Collie.

Collie already knows how to *do* things (runs, missions, tools) and how to *remember* claims
(``memory.py``).  What it did not have is a typed model of the person's own world: projects,
goals, tasks, commitments, calendar events, notes, an AI-maintained journal, the activity stream
that feeds it, learned workflows, and the suggestions that come out of them.  This module is that
model.  It is deliberately small, SQLite-backed, and canonical: Markdown files are *projections*
rendered from it (``render_views``), never the source of truth.

Design rules
------------
* Local by default.  The database lives under the Collie state dir (``~/.collie/personal.db``,
  ``COLLIE_STATE_DIR`` honoured, ``COLLIE_PERSONAL_DB`` overrides for tests).  Nothing here talks
  to the network; ``sauna.py`` decides what (if anything) leaves the machine.
* Structured first, Markdown second.  ``today.md`` / ``profile.md`` / ``recent_activity.md`` /
  ``project_summary.md`` are regenerated read-only views so a person can inspect what the AI
  believes; the state machine never parses them back.
* Append-only activity.  Everything the user or Collie does lands in ``activities`` with an actor
  and a source; journal entries are *compressions* of that stream, so the model can understand
  continuity without replaying raw history.
* Hierarchical summaries.  ``activities`` → daily ``journal`` → weekly ``summaries`` →
  ``project_timeline`` → (decisions) long-term memory.  Each level is derivable from the one below.

Nothing in this module requires a model; an optional narrator callable can improve journal prose.
"""
from __future__ import annotations

import datetime as _dt
from contextlib import contextmanager
import json
import os
import re
import secrets
import sqlite3
import threading
import time

from .personal_core import (COMMITMENT_STATUSES, ENTITY_BY_TYPE, ENTITY_SPECS,
                            PERSONAL_CORE_SCHEMA_VERSION, PERSONAL_DELTA_FORMAT)

__all__ = [
    "PersonalState", "default_path", "state_dir", "new_id", "TASK_STATUSES", "GOAL_STATUSES",
    "day_key", "week_key", "valid_day", "SyncDeltaError",
]

TASK_STATUSES = COMMITMENT_STATUSES
GOAL_STATUSES = ("active", "done", "paused")
EVENT_KINDS = ("meeting", "interview", "deadline", "call", "block", "other")
ACTIVITY_ACTORS = ("user", "collie", "sauna", "workflow", "system")

_PREFIX = {
    "project": "prj", "goal": "gol", "task": "tsk", "event": "evt", "note": "nte",
    "person": "per", "workflow": "wf", "suggestion": "sug", "cloud_task": "ct",
    "summary": "sum", "device": "dev",
}


def state_dir() -> str:
    return os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie")


def default_path() -> str:
    override = os.environ.get("COLLIE_PERSONAL_DB")
    if override:
        return override
    return os.path.join(state_dir(), "personal.db")


def new_id(kind: str) -> str:
    return "%s_%s" % (_PREFIX.get(kind, kind[:3]), secrets.token_hex(5))


_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def valid_day(day: str) -> bool:
    """A journal key has to be a real calendar day: it is parsed on every render and roll-up, so a
    single bad row from an imported snapshot would otherwise break those surfaces permanently."""
    if not _DAY_RE.match(str(day or "")):
        return False
    try:
        _dt.datetime.strptime(day, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def day_key(ts: float | None = None) -> str:
    return _dt.datetime.fromtimestamp(ts if ts is not None else time.time()).strftime("%Y-%m-%d")


def week_key(ts: float | None = None) -> str:
    d = _dt.datetime.fromtimestamp(ts if ts is not None else time.time())
    iso = d.isocalendar()
    return "%d-W%02d" % (iso[0], iso[1])


def _now() -> int:
    return int(time.time())


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


class SyncDeltaError(ValueError):
    """A remote Personal AI delta failed structural or trust-boundary validation."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS projects(
    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'project',
    status TEXT NOT NULL DEFAULT 'active', summary TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS goals(
    id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
    project_id TEXT NOT NULL DEFAULT '', due_at INTEGER, summary TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(
    id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
    project_id TEXT NOT NULL DEFAULT '', goal_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '', due_at INTEGER, order_key INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'user', notes TEXT NOT NULL DEFAULT '',
    done_at INTEGER, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS events(
    id TEXT PRIMARY KEY, title TEXT NOT NULL, start_at INTEGER NOT NULL, end_at INTEGER,
    all_day INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL DEFAULT 'meeting',
    location TEXT NOT NULL DEFAULT '', project_id TEXT NOT NULL DEFAULT '',
    goal_id TEXT NOT NULL DEFAULT '', external_ref TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS notes(
    id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT '', goal_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'user', pinned INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS people(
    id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL DEFAULT '',
    org TEXT NOT NULL DEFAULT '', project_id TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS relations(
    src_type TEXT NOT NULL, src_id TEXT NOT NULL, dst_type TEXT NOT NULL, dst_id TEXT NOT NULL,
    rel TEXT NOT NULL DEFAULT 'related', created_at INTEGER NOT NULL,
    PRIMARY KEY(src_type, src_id, dst_type, dst_id, rel));
CREATE TABLE IF NOT EXISTS activities(
    id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL, actor TEXT NOT NULL DEFAULT 'collie',
    kind TEXT NOT NULL, summary TEXT NOT NULL, detail_json TEXT NOT NULL DEFAULT '{}',
    project_id TEXT NOT NULL DEFAULT '', task_id TEXT NOT NULL DEFAULT '',
    goal_id TEXT NOT NULL DEFAULT '', run_id TEXT NOT NULL DEFAULT '',
    session TEXT NOT NULL DEFAULT '', device_id TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS activities_at ON activities(at);
CREATE INDEX IF NOT EXISTS activities_project ON activities(project_id, at);
CREATE TABLE IF NOT EXISTS journal(
    day TEXT PRIMARY KEY, happened_json TEXT NOT NULL DEFAULT '[]',
    decisions_json TEXT NOT NULL DEFAULT '[]', open_loops_json TEXT NOT NULL DEFAULT '[]',
    next_json TEXT NOT NULL DEFAULT '[]', narrative TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'auto', generated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS summaries(
    id TEXT PRIMARY KEY, scope TEXT NOT NULL, key TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
    facts_json TEXT NOT NULL DEFAULT '{}', period_start INTEGER, period_end INTEGER,
    generated_at INTEGER NOT NULL, UNIQUE(scope, key));
CREATE TABLE IF NOT EXISTS workflows(
    id TEXT PRIMARY KEY, name TEXT NOT NULL, trigger TEXT NOT NULL DEFAULT '',
    steps_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'observed',
    observations INTEGER NOT NULL DEFAULT 0, confidence REAL NOT NULL DEFAULT 0.3,
    source TEXT NOT NULL DEFAULT 'learned', accepted INTEGER NOT NULL DEFAULT 0,
    rejected INTEGER NOT NULL DEFAULT 0, last_used_at INTEGER,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS workflow_observations(
    id INTEGER PRIMARY KEY AUTOINCREMENT, workflow_id TEXT NOT NULL DEFAULT '',
    prev_kind TEXT NOT NULL, next_kind TEXT NOT NULL, goal_id TEXT NOT NULL DEFAULT '',
    at INTEGER NOT NULL, run_id TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS suggestions(
    id TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'next_step', title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '', task_id TEXT NOT NULL DEFAULT '',
    workflow_id TEXT NOT NULL DEFAULT '', goal_id TEXT NOT NULL DEFAULT '',
    action_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'open',
    confidence REAL NOT NULL DEFAULT 0.5, source TEXT NOT NULL DEFAULT 'workflow',
    created_at INTEGER NOT NULL, resolved_at INTEGER);
CREATE TABLE IF NOT EXISTS devices(
    device_id TEXT PRIMARY KEY, name TEXT NOT NULL, platform TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'desktop', this_device INTEGER NOT NULL DEFAULT 0,
    runtime_json TEXT NOT NULL DEFAULT '{}', last_seen INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS cloud_tasks(
    id TEXT PRIMARY KEY, title TEXT NOT NULL, runtime TEXT NOT NULL DEFAULT 'sauna-cloud',
    status TEXT NOT NULL DEFAULT 'scheduled', scheduled_for INTEGER, deliver_at INTEGER,
    detail_json TEXT NOT NULL DEFAULT '{}', result TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
"""


class PersonalState:
    """One person's structured state.  Thread-safe for the webapp's threaded handlers."""

    def __init__(self, path: str | None = None):
        self.path = path or default_path()
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        self._lock = threading.RLock()
        self._sync_suppressed = 0
        self._sync_origin = ""
        self._hlc_millis = 0
        self._hlc_counter = 0
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        # The sync triggers use connection-local functions instead of mutable SQL flags.  A remote
        # apply can therefore suppress echo atomically on this connection without leaving a global
        # switch behind after a crash.
        self._db.create_function("collie_sync_suppress", 0, lambda: int(self._sync_suppressed > 0))
        self._db.create_function("collie_sync_origin", 0, self._current_sync_origin)
        self._db.create_function("collie_hlc", 0, self._next_hlc)
        self._db.create_function("collie_json", -1, self._json_pairs)
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        self._db.execute("PRAGMA busy_timeout=30000")
        self._db.executescript(_SCHEMA)
        existing_device = self._db.execute("SELECT value FROM meta WHERE key='device_id'").fetchone()
        self._origin_device = str(existing_device[0]) if existing_device and existing_device[0] else new_id("device")
        if not existing_device or not existing_device[0]:
            self._db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('device_id',?)",
                             (self._origin_device,))
        self._install_personal_core_schema()
        self._db.commit()

    # ---------------------------------------------------------- schema / sync substrate
    def _current_sync_origin(self) -> str:
        return self._sync_origin or self._origin_device

    def _next_hlc(self) -> str:
        """Return a lexically sortable Hybrid Logical Clock stamp for this database connection."""
        millis = int(time.time_ns() // 1_000_000)
        if millis > self._hlc_millis:
            self._hlc_millis, self._hlc_counter = millis, 0
        else:
            self._hlc_counter += 1
        return "%013d:%06d:%s" % (self._hlc_millis, self._hlc_counter,
                                   self._current_sync_origin())

    @staticmethod
    def _json_pairs(*items) -> str:
        if len(items) % 2:
            raise ValueError("collie_json requires key/value pairs")
        return json.dumps({str(items[i]): items[i + 1] for i in range(0, len(items), 2)},
                          ensure_ascii=False, separators=(",", ":"))

    @contextmanager
    def _remote_apply(self, origin: str):
        previous = self._sync_origin
        self._sync_origin = str(origin or "remote")[:160]
        self._sync_suppressed += 1
        try:
            yield
        finally:
            self._sync_suppressed = max(0, self._sync_suppressed - 1)
            self._sync_origin = previous

    @staticmethod
    def _trigger_payload(spec, prefix: str) -> str:
        args = []
        for column in spec.columns:
            args.extend(("'%s'" % column, '%s."%s"' % (prefix, column)))
        return "collie_json(%s)" % ",".join(args)

    def _sync_trigger_sql(self, spec) -> str:
        """Generate atomic change-capture triggers from the allow-listed domain spec."""
        table, key, entity = spec.table, spec.key, spec.entity_type
        new_key, old_key = 'CAST(NEW."%s" AS TEXT)' % key, 'CAST(OLD."%s" AS TEXT)' % key
        now = "CAST(strftime('%s','now') AS INTEGER)"
        upsert_version = """
          INSERT INTO entity_versions(entity_type,entity_id,revision,origin_device,hlc,deleted_at,changed_at)
          VALUES('%s',%s,1,collie_sync_origin(),collie_hlc(),NULL,%s)
          ON CONFLICT(entity_type,entity_id) DO UPDATE SET
            revision=entity_versions.revision+1,
            origin_device=excluded.origin_device,hlc=excluded.hlc,deleted_at=NULL,
            changed_at=excluded.changed_at;
        """ % (entity, new_key, now)
        record_upsert = """
          INSERT INTO sync_changes(change_id,entity_type,entity_id,operation,base_revision,revision,
                                   origin_device,hlc,changed_at,payload_json)
          SELECT 'chg_'||lower(hex(randomblob(16))),entity_type,entity_id,'upsert',
                 CASE WHEN revision>0 THEN revision-1 ELSE 0 END,revision,origin_device,hlc,changed_at,%s
          FROM entity_versions WHERE entity_type='%s' AND entity_id=%s;
        """ % (self._trigger_payload(spec, "NEW"), entity, new_key)
        delete_version = """
          INSERT INTO entity_versions(entity_type,entity_id,revision,origin_device,hlc,deleted_at,changed_at)
          VALUES('%s',%s,1,collie_sync_origin(),collie_hlc(),%s,%s)
          ON CONFLICT(entity_type,entity_id) DO UPDATE SET
            revision=entity_versions.revision+1,
            origin_device=excluded.origin_device,hlc=excluded.hlc,deleted_at=excluded.deleted_at,
            changed_at=excluded.changed_at;
        """ % (entity, old_key, now, now)
        record_delete = """
          INSERT INTO sync_changes(change_id,entity_type,entity_id,operation,base_revision,revision,
                                   origin_device,hlc,changed_at,payload_json)
          SELECT 'chg_'||lower(hex(randomblob(16))),entity_type,entity_id,'delete',
                 CASE WHEN revision>0 THEN revision-1 ELSE 0 END,revision,origin_device,hlc,changed_at,%s
          FROM entity_versions WHERE entity_type='%s' AND entity_id=%s;
        """ % (self._trigger_payload(spec, "OLD"), entity, old_key)
        return """
          CREATE TRIGGER IF NOT EXISTS pcore_%s_insert AFTER INSERT ON "%s"
          WHEN collie_sync_suppress()=0 BEGIN %s %s END;
          CREATE TRIGGER IF NOT EXISTS pcore_%s_update AFTER UPDATE ON "%s"
          WHEN collie_sync_suppress()=0 BEGIN %s %s END;
          CREATE TRIGGER IF NOT EXISTS pcore_%s_delete AFTER DELETE ON "%s"
          WHEN collie_sync_suppress()=0 BEGIN %s %s END;
        """ % (table, table, upsert_version, record_upsert,
                 table, table, upsert_version, record_upsert,
                 table, table, delete_version, record_delete)

    def _install_personal_core_schema(self) -> None:
        current = int(self._db.execute("PRAGMA user_version").fetchone()[0])
        if current > PERSONAL_CORE_SCHEMA_VERSION:
            raise RuntimeError("personal.db schema %d is newer than this Collie supports (%d)" %
                               (current, PERSONAL_CORE_SCHEMA_VERSION))
        self._db.executescript("""
          CREATE TABLE IF NOT EXISTS schema_migrations(
            version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL,
            description TEXT NOT NULL, checksum TEXT NOT NULL DEFAULT '');
          CREATE TABLE IF NOT EXISTS entity_versions(
            entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, revision INTEGER NOT NULL,
            origin_device TEXT NOT NULL, hlc TEXT NOT NULL, deleted_at INTEGER,
            changed_at INTEGER NOT NULL,
            PRIMARY KEY(entity_type,entity_id));
          CREATE TABLE IF NOT EXISTS sync_changes(
            seq INTEGER PRIMARY KEY AUTOINCREMENT, change_id TEXT NOT NULL UNIQUE,
            entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
            operation TEXT NOT NULL CHECK(operation IN ('upsert','delete')),
            base_revision INTEGER NOT NULL, revision INTEGER NOT NULL,
            origin_device TEXT NOT NULL, hlc TEXT NOT NULL, changed_at INTEGER NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}');
          CREATE INDEX IF NOT EXISTS sync_changes_cursor ON sync_changes(seq);
          CREATE INDEX IF NOT EXISTS sync_changes_entity ON sync_changes(entity_type,entity_id,seq);
          CREATE TABLE IF NOT EXISTS sync_applied(
            change_id TEXT PRIMARY KEY, peer_id TEXT NOT NULL, applied_at INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS sync_peers(
            peer_id TEXT PRIMARY KEY, push_cursor INTEGER NOT NULL DEFAULT 0,
            pull_cursor INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS sync_conflicts(
            conflict_id TEXT PRIMARY KEY, remote_change_id TEXT NOT NULL UNIQUE,
            entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
            local_revision INTEGER NOT NULL, remote_base_revision INTEGER NOT NULL,
            local_json TEXT NOT NULL, remote_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open', created_at INTEGER NOT NULL,
            resolved_at INTEGER);
          CREATE INDEX IF NOT EXISTS sync_conflicts_open ON sync_conflicts(status,created_at);
        """)
        for spec in ENTITY_SPECS:
            self._db.executescript(self._sync_trigger_sql(spec))

        migrated = self._db.execute(
            "SELECT 1 FROM schema_migrations WHERE version=?",
            (PERSONAL_CORE_SCHEMA_VERSION,)).fetchone()
        if not migrated:
            # Existing installs start with one synthetic revision per object so delta sync can
            # bootstrap without shipping a full replacement snapshot.
            now = _now()
            for spec in ENTITY_SPECS:
                rows = self._db.execute('SELECT %s FROM "%s"' %
                                        (",".join('"%s"' % c for c in spec.columns), spec.table)).fetchall()
                for row in rows:
                    payload = {c: row[c] for c in spec.columns}
                    entity_id = str(payload[spec.key])
                    hlc = self._next_hlc()
                    self._db.execute(
                        "INSERT OR IGNORE INTO entity_versions(entity_type,entity_id,revision,origin_device,hlc,"
                        "deleted_at,changed_at) VALUES(?,?,?,?,?,NULL,?)",
                        (spec.entity_type, entity_id, 1, self._origin_device, hlc, now))
                    self._db.execute(
                        "INSERT INTO sync_changes(change_id,entity_type,entity_id,operation,base_revision,revision,"
                        "origin_device,hlc,changed_at,payload_json) VALUES(?,?,?,?,0,1,?,?,?,?)",
                        ("chg_" + secrets.token_hex(16), spec.entity_type, entity_id, "upsert",
                         self._origin_device, hlc, now,
                         json.dumps(payload, ensure_ascii=False, separators=(",", ":"))))
            self._db.execute(
                "INSERT INTO schema_migrations(version,applied_at,description,checksum) VALUES(?,?,?,?)",
                (PERSONAL_CORE_SCHEMA_VERSION, now,
                 "versioned entities, delta log, tombstones, peer cursors and conflict tray",
                 "personal-core-v1"))
        self._db.execute("PRAGMA user_version=%d" % PERSONAL_CORE_SCHEMA_VERSION)

    # ------------------------------------------------------------------ plumbing
    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

    def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def _row(self, sql: str, args: tuple = ()) -> dict | None:
        rows = self._rows(sql, args)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            try:
                self._db.close()
            except Exception:
                pass

    # ---------------------------------------------------------------------- meta
    def get_meta(self, key: str, default: str = "") -> str:
        row = self._row("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else default

    def set_meta(self, key: str, value) -> None:
        self._exec("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, "" if value is None else str(value)))

    # ------------------------------------------------------------------ projects
    def upsert_project(self, name: str, *, kind: str = "project", summary: str = "",
                       path: str = "", status: str = "active", project_id: str = "") -> dict:
        now = _now()
        existing = None
        if project_id:
            # An explicit id addresses one exact row. Falling back to a name match here let the demo
            # seed adopt — and permanently overwrite — a real project the person happened to call
            # "Collie", which reset() could then never undo because no demo_ row existed.
            existing = self._row("SELECT * FROM projects WHERE id=?", (project_id,))
        else:
            existing = self._row("SELECT * FROM projects WHERE lower(name)=lower(?)", (name,))
        if existing:
            self._exec("UPDATE projects SET name=?, kind=?, status=?, summary=COALESCE(NULLIF(?,''),summary), "
                       "path=COALESCE(NULLIF(?,''),path), updated_at=? WHERE id=?",
                       (name, kind or existing["kind"], status or existing["status"], summary, path, now,
                        existing["id"]))
            return self.project(existing["id"])
        pid = project_id or new_id("project")
        self._exec("INSERT INTO projects(id,name,kind,status,summary,path,created_at,updated_at) "
                   "VALUES(?,?,?,?,?,?,?,?)", (pid, name, kind, status, summary, path, now, now))
        return self.project(pid)

    def project(self, project_id: str) -> dict | None:
        return self._row("SELECT * FROM projects WHERE id=?", (project_id,))

    def projects(self, status: str | None = "active") -> list[dict]:
        if status:
            return self._rows("SELECT * FROM projects WHERE status=? ORDER BY updated_at DESC", (status,))
        return self._rows("SELECT * FROM projects ORDER BY updated_at DESC")

    def find_project(self, text: str) -> dict | None:
        """Best-effort project match for a free-text label, window title, or path."""
        text_l = (text or "").lower()
        if not text_l:
            return None
        best = None
        for p in self.projects(None):
            name = p["name"].lower()
            if name and (name in text_l or (p["path"] and p["path"].lower() in text_l)):
                if best is None or len(name) > len(best["name"]):
                    best = p
        return best

    # --------------------------------------------------------------------- goals
    def add_goal(self, title: str, *, project_id: str = "", due_at: int | None = None,
                 summary: str = "", goal_id: str = "") -> dict:
        now = _now()
        gid = goal_id or new_id("goal")
        self._exec("INSERT OR REPLACE INTO goals(id,title,status,project_id,due_at,summary,created_at,updated_at) "
                   "VALUES(?,?,?,?,?,?,COALESCE((SELECT created_at FROM goals WHERE id=?),?),?)",
                   (gid, title, "active", project_id, due_at, summary, gid, now, now))
        return self.goal(gid)

    def goal(self, goal_id: str) -> dict | None:
        row = self._row("SELECT * FROM goals WHERE id=?", (goal_id,))
        if row:
            row["progress"] = self.goal_progress(goal_id)
        return row

    def goals(self, status: str | None = "active") -> list[dict]:
        rows = (self._rows("SELECT * FROM goals WHERE status=? ORDER BY COALESCE(due_at, 1<<40), created_at", (status,))
                if status else self._rows("SELECT * FROM goals ORDER BY created_at"))
        for r in rows:
            r["progress"] = self.goal_progress(r["id"])
        return rows

    def set_goal_status(self, goal_id: str, status: str) -> None:
        if status not in GOAL_STATUSES:
            raise ValueError("unknown goal status: %s" % status)
        self._exec("UPDATE goals SET status=?, updated_at=? WHERE id=?", (status, _now(), goal_id))

    def goal_progress(self, goal_id: str) -> float:
        row = self._row("SELECT COUNT(*) AS n, SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS d "
                        "FROM tasks WHERE goal_id=? AND status!='dropped'", (goal_id,))
        if not row or not row["n"]:
            return 0.0
        return (row["d"] or 0) / float(row["n"])

    # --------------------------------------------------------------------- tasks
    def add_task(self, title: str, *, project_id: str = "", goal_id: str = "", kind: str = "",
                 status: str = "open", due_at: int | None = None, source: str = "user",
                 notes: str = "", order_key: int | None = None, task_id: str = "") -> dict:
        if status not in TASK_STATUSES:
            raise ValueError("unknown task status: %s" % status)
        now = _now()
        tid = task_id or new_id("task")
        if order_key is None:
            row = self._row("SELECT COALESCE(MAX(order_key),0)+1 AS k FROM tasks WHERE goal_id=?", (goal_id,))
            order_key = int(row["k"]) if row else 1
        self._exec("INSERT OR REPLACE INTO tasks(id,title,status,project_id,goal_id,kind,due_at,order_key,source,"
                   "notes,done_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,"
                   "COALESCE((SELECT created_at FROM tasks WHERE id=?),?),?)",
                   (tid, title, status, project_id, goal_id, kind or _infer_kind(title), due_at, order_key,
                    source, notes, now if status == "done" else None, tid, now, now))
        self.record_activity("task_created", "Added task: %s" % title, actor=source if source in ACTIVITY_ACTORS else "user",
                             project_id=project_id, task_id=tid, goal_id=goal_id)
        return self.task(tid)

    def task(self, task_id: str) -> dict | None:
        return self._row("SELECT * FROM tasks WHERE id=?", (task_id,))

    def completed_since(self, since: int, *, limit: int = 200) -> list[dict]:
        """Tasks completed after ``since``, newest first — the query behind "done today".

        ``tasks(status="done")`` orders by order_key ASC, so its LIMIT keeps the OLDEST completions
        and the count read zero once a few hundred tasks had ever been finished.
        """
        return self._rows("SELECT * FROM tasks WHERE status='done' AND done_at IS NOT NULL AND done_at >= ? "
                          "ORDER BY done_at DESC LIMIT ?", (int(since), int(limit)))

    def tasks(self, *, goal_id: str = "", project_id: str = "", status: str | None = None,
              include_done: bool = True, limit: int = 500) -> list[dict]:
        sql, args = "SELECT * FROM tasks WHERE 1=1", []
        if goal_id:
            sql += " AND goal_id=?"; args.append(goal_id)
        if project_id:
            sql += " AND project_id=?"; args.append(project_id)
        if status:
            sql += " AND status=?"; args.append(status)
        elif not include_done:
            sql += " AND status NOT IN ('done','dropped')"
        sql += " ORDER BY CASE status WHEN 'doing' THEN 0 WHEN 'next' THEN 1 WHEN 'open' THEN 2 WHEN 'done' THEN 3 ELSE 4 END, order_key, created_at LIMIT ?"
        args.append(int(limit))
        return self._rows(sql, tuple(args))

    def update_task(self, task_id: str, **fields) -> dict | None:
        allowed = {"title", "status", "project_id", "goal_id", "kind", "due_at", "order_key", "notes"}
        sets, args = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError("cannot update task.%s" % k)
            if k == "status" and v not in TASK_STATUSES:
                raise ValueError("unknown task status: %s" % v)
            sets.append("%s=?" % k); args.append(v)
        if not sets:
            return self.task(task_id)
        if fields.get("status") == "done":
            sets.append("done_at=?"); args.append(_now())
        elif "status" in fields:
            # Reopening a completed task must also undo the completion timestamp.  Keeping it made
            # exported state disagree with the visible status and let later roll-ups count a task
            # as both open and historically completed at the same time.
            sets.append("done_at=?"); args.append(None)
        sets.append("updated_at=?"); args.append(_now()); args.append(task_id)
        self._exec("UPDATE tasks SET %s WHERE id=?" % ", ".join(sets), tuple(args))
        return self.task(task_id)

    def complete_task(self, task_id: str, *, actor: str = "user", run_id: str = "", session: str = "",
                      evidence: dict | None = None) -> dict | None:
        t = self.task(task_id)
        if not t:
            return None
        if t["status"] != "done":
            self.update_task(task_id, status="done")
            detail = dict(evidence or {})
            self.record_activity("task_done", "Completed: %s" % t["title"], actor=actor,
                                 project_id=t["project_id"], task_id=task_id, goal_id=t["goal_id"],
                                 run_id=run_id, session=session, detail=detail)
            if t["goal_id"]:
                self._touch_goal(t["goal_id"])
            if t["project_id"]:
                self._exec("UPDATE projects SET updated_at=? WHERE id=?", (_now(), t["project_id"]))
        return self.task(task_id)

    def _touch_goal(self, goal_id: str) -> None:
        g = self.goal(goal_id)
        if g and g["status"] == "active" and g["progress"] >= 1.0:
            self._exec("UPDATE goals SET updated_at=? WHERE id=?", (_now(), goal_id))

    def next_task(self, goal_id: str) -> dict | None:
        """The first unfinished step of a goal (workflow order = order_key)."""
        rows = self.tasks(goal_id=goal_id, include_done=False)
        return rows[0] if rows else None

    def match_task(self, text: str, *, project_id: str = "", min_score: float = 0.34) -> tuple[dict | None, float]:
        """Fuzzy-match free text (a prompt, a run label) to one open task. Returns (task, score)."""
        words = _words(text)
        if not words:
            return None, 0.0
        best, best_score = None, 0.0
        for t in self.tasks(include_done=False):
            if project_id and t["project_id"] and t["project_id"] != project_id:
                continue
            tw = _words(t["title"])
            if not tw:
                continue
            inter = len(words & tw)
            score = inter / float(len(tw)) * 0.7 + inter / float(len(words | tw)) * 0.3
            if score > best_score:
                best, best_score = t, score
        if best_score < min_score:
            return None, best_score
        return best, round(best_score, 3)

    # -------------------------------------------------------------------- events
    def add_event(self, title: str, start_at: int, *, end_at: int | None = None, all_day: bool = False,
                  kind: str = "meeting", location: str = "", project_id: str = "", goal_id: str = "",
                  external_ref: str = "", notes: str = "", event_id: str = "") -> dict:
        title = str(title or "").strip()
        if not title:
            raise ValueError("event title required")
        if kind not in EVENT_KINDS:
            raise ValueError("unknown event kind: %s" % kind)
        start_at = int(start_at)
        end_at = int(end_at) if end_at not in (None, "") else None
        if end_at is not None and end_at < start_at:
            raise ValueError("event end_at must not be before start_at")
        now = _now()
        eid = event_id or new_id("event")
        self._exec("INSERT OR REPLACE INTO events(id,title,start_at,end_at,all_day,kind,location,project_id,goal_id,"
                   "external_ref,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,"
                   "COALESCE((SELECT created_at FROM events WHERE id=?),?),?)",
                   (eid, title, start_at, end_at, 1 if all_day else 0, kind, location, project_id, goal_id,
                    external_ref, notes, eid, now, now))
        return self.event(eid)

    def update_event(self, event_id: str, **fields) -> dict | None:
        """Correct one calendar event without replacing its identity or relations."""
        allowed = {"title", "start_at", "end_at", "all_day", "kind", "location", "project_id",
                   "goal_id", "external_ref", "notes"}
        current = self._row("SELECT * FROM events WHERE id=?", (event_id,))
        if not current:
            return None
        sets, args = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError("cannot update event.%s" % key)
            if key == "title":
                value = str(value or "").strip()
                if not value:
                    raise ValueError("event title required")
            elif key in ("start_at", "end_at"):
                value = int(value) if value not in (None, "") else None
                if key == "start_at" and not value:
                    raise ValueError("event start_at required")
            elif key == "all_day":
                value = 1 if bool(value) else 0
            elif key == "kind":
                value = str(value or "meeting")
                if value not in EVENT_KINDS:
                    raise ValueError("unknown event kind: %s" % value)
            elif key in ("location", "project_id", "goal_id", "external_ref", "notes"):
                value = str(value or "")
            sets.append("%s=?" % key)
            args.append(value)
        if not sets:
            return self.event(event_id)
        proposed = dict(current)
        proposed.update(fields)
        start = int(proposed.get("start_at") or 0)
        end = proposed.get("end_at")
        if end not in (None, "") and int(end) < start:
            raise ValueError("event end_at must not be before start_at")
        sets.append("updated_at=?")
        args.extend((_now(), event_id))
        self._exec("UPDATE events SET %s WHERE id=?" % ", ".join(sets), tuple(args))
        return self.event(event_id)

    def delete_event(self, event_id: str) -> dict | None:
        """Delete one event and its graph edges; unrelated personal state is untouched."""
        event = self.event(event_id)
        if not event:
            return None
        with self._lock:
            self._db.execute(
                "DELETE FROM relations WHERE (src_type='event' AND src_id=?) OR "
                "(dst_type='event' AND dst_id=?)", (event_id, event_id))
            self._db.execute("DELETE FROM events WHERE id=?", (event_id,))
            self._db.commit()
        return event

    def event(self, event_id: str) -> dict | None:
        row = self._row("SELECT * FROM events WHERE id=?", (event_id,))
        return self._enrich_event(row) if row else None

    def events(self, *, since: int | None = None, until: int | None = None, limit: int = 200) -> list[dict]:
        sql, args = "SELECT * FROM events WHERE 1=1", []
        if since is not None:
            sql += " AND COALESCE(end_at, start_at) >= ?"; args.append(int(since))
        if until is not None:
            sql += " AND start_at <= ?"; args.append(int(until))
        sql += " ORDER BY start_at LIMIT ?"; args.append(int(limit))
        return [self._enrich_event(r) for r in self._rows(sql, tuple(args))]

    def upcoming(self, *, now: int | None = None, days: int = 14, limit: int = 10) -> list[dict]:
        now = int(now if now is not None else _now())
        return self.events(since=now - 3600, until=now + days * 86400, limit=limit)

    def _enrich_event(self, row: dict) -> dict:
        """The semantic layer: what the event means against goals and commitments."""
        row = dict(row)
        goal = self.goal(row["goal_id"]) if row.get("goal_id") else None
        project = self.project(row["project_id"]) if row.get("project_id") else None
        row["goal"] = goal
        row["project"] = project
        if goal:
            tasks = self.tasks(goal_id=goal["id"])
            row["preparation"] = goal["progress"]
            row["remaining"] = [t for t in tasks if t["status"] not in ("done", "dropped")]
            row["done"] = [t for t in tasks if t["status"] == "done"]
        else:
            row["preparation"] = None
            row["remaining"], row["done"] = [], []
        row["related"] = self.related("event", row["id"])
        return row

    # --------------------------------------------------------------------- notes
    def add_note(self, body: str, *, title: str = "", project_id: str = "", goal_id: str = "",
                 source: str = "user", pinned: bool = False, related: list[tuple[str, str]] | None = None,
                 note_id: str = "") -> dict:
        now = _now()
        nid = note_id or new_id("note")
        title = title or _clip(body.strip().splitlines()[0] if body.strip() else "Note", 80)
        self._exec("INSERT OR REPLACE INTO notes(id,title,body,project_id,goal_id,source,pinned,created_at,updated_at) "
                   "VALUES(?,?,?,?,?,?,?,COALESCE((SELECT created_at FROM notes WHERE id=?),?),?)",
                   (nid, title, body, project_id, goal_id, source, 1 if pinned else 0, nid, now, now))
        for (dst_type, dst_id) in related or []:
            self.link("note", nid, dst_type, dst_id)
        self.record_activity("note", "Saved note: %s" % title, actor=source if source in ACTIVITY_ACTORS else "user",
                             project_id=project_id, goal_id=goal_id, detail={"note_id": nid})
        return self.note(nid)

    def note(self, note_id: str) -> dict | None:
        row = self._row("SELECT * FROM notes WHERE id=?", (note_id,))
        if row:
            row["related"] = self.related("note", note_id)
        return row

    def notes(self, *, project_id: str = "", goal_id: str = "", query: str = "", limit: int = 100) -> list[dict]:
        sql, args = "SELECT * FROM notes WHERE 1=1", []
        if project_id:
            sql += " AND project_id=?"; args.append(project_id)
        if goal_id:
            sql += " AND goal_id=?"; args.append(goal_id)
        if query:
            sql += " AND (lower(title) LIKE ? OR lower(body) LIKE ?)"
            q = "%" + query.lower() + "%"; args += [q, q]
        sql += " ORDER BY pinned DESC, updated_at DESC LIMIT ?"; args.append(int(limit))
        rows = self._rows(sql, tuple(args))
        for r in rows:
            r["related"] = self.related("note", r["id"])
        return rows

    def find_note(self, text: str) -> dict | None:
        """A note whose title is contained in the text (e.g. 'add this to my Sauna interview notes')."""
        text_l = (text or "").lower()
        best = None
        for n in self.notes(limit=500):
            t = (n["title"] or "").lower()
            if len(t) >= 4 and t in text_l and (best is None or len(t) > len(best["title"])):
                best = n
        return best

    def append_note(self, note_id: str, text: str, *, source: str = "user") -> dict | None:
        n = self.note(note_id)
        if not n:
            return None
        body = (n["body"].rstrip() + "\n\n" + text.strip()).strip()
        self._exec("UPDATE notes SET body=?, updated_at=? WHERE id=?", (body, _now(), note_id))
        self.record_activity("note", "Added to note: %s" % n["title"], actor=source if source in ACTIVITY_ACTORS else "user",
                             project_id=n["project_id"], goal_id=n["goal_id"], detail={"note_id": note_id})
        return self.note(note_id)

    def update_note(self, note_id: str, **fields) -> dict | None:
        """Correct a note in place so links and sync identity survive the edit."""
        allowed = {"title", "body", "project_id", "goal_id", "source", "pinned"}
        current = self._row("SELECT * FROM notes WHERE id=?", (note_id,))
        if not current:
            return None
        sets, args = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError("cannot update note.%s" % key)
            if key == "body":
                value = str(value or "").strip()
                if not value:
                    raise ValueError("note text required")
            elif key == "title":
                value = str(value or "").strip()
            elif key in ("project_id", "goal_id", "source"):
                value = str(value or "")
            elif key == "pinned":
                value = 1 if bool(value) else 0
            sets.append("%s=?" % key)
            args.append(value)
        if not sets:
            return self.note(note_id)
        # An empty edited title remains useful and readable by deriving it from the corrected body.
        if "title" in fields and not str(fields.get("title") or "").strip():
            body = str(fields.get("body") or current["body"])
            args[sets.index("title=?")] = _clip(body.strip().splitlines()[0] if body.strip() else "Note", 80)
        sets.append("updated_at=?")
        args.extend((_now(), note_id))
        self._exec("UPDATE notes SET %s WHERE id=?" % ", ".join(sets), tuple(args))
        return self.note(note_id)

    def delete_note(self, note_id: str) -> dict | None:
        """Forget one personal note and every relation pointing to it."""
        note = self.note(note_id)
        if not note:
            return None
        with self._lock:
            self._db.execute(
                "DELETE FROM relations WHERE (src_type='note' AND src_id=?) OR "
                "(dst_type='note' AND dst_id=?)", (note_id, note_id))
            self._db.execute("DELETE FROM notes WHERE id=?", (note_id,))
            self._db.commit()
        return note

    # -------------------------------------------------------------------- people
    def add_person(self, name: str, *, role: str = "", org: str = "", project_id: str = "",
                   notes: str = "", person_id: str = "") -> dict:
        pid = person_id or new_id("person")
        self._exec("INSERT OR REPLACE INTO people(id,name,role,org,project_id,notes,created_at) VALUES(?,?,?,?,?,?,"
                   "COALESCE((SELECT created_at FROM people WHERE id=?),?))",
                   (pid, name, role, org, project_id, notes, pid, _now()))
        return self._row("SELECT * FROM people WHERE id=?", (pid,))

    def people(self, *, project_id: str = "") -> list[dict]:
        if project_id:
            return self._rows("SELECT * FROM people WHERE project_id=? ORDER BY name", (project_id,))
        return self._rows("SELECT * FROM people ORDER BY name")

    # ----------------------------------------------------------------- relations
    def link(self, src_type: str, src_id: str, dst_type: str, dst_id: str, rel: str = "related") -> None:
        self._exec("INSERT OR IGNORE INTO relations(src_type,src_id,dst_type,dst_id,rel,created_at) VALUES(?,?,?,?,?,?)",
                   (src_type, src_id, dst_type, dst_id, rel, _now()))

    def related(self, src_type: str, src_id: str) -> list[dict]:
        out = []
        rows = self._rows("SELECT * FROM relations WHERE (src_type=? AND src_id=?) OR (dst_type=? AND dst_id=?)",
                          (src_type, src_id, src_type, src_id))
        for r in rows:
            if r["src_type"] == src_type and r["src_id"] == src_id:
                t, i = r["dst_type"], r["dst_id"]
            else:
                t, i = r["src_type"], r["src_id"]
            label = self._label(t, i)
            if label:
                out.append({"type": t, "id": i, "label": label, "rel": r["rel"]})
        return out

    def _label(self, kind: str, obj_id: str) -> str:
        table = {"project": "projects", "goal": "goals", "task": "tasks", "event": "events",
                 "note": "notes", "person": "people", "workflow": "workflows"}.get(kind)
        if not table:
            return ""
        col = "name" if table in ("projects", "people", "workflows") else "title"
        row = self._row("SELECT %s AS label FROM %s WHERE id=?" % (col, table), (obj_id,))
        return row["label"] if row else ""

    # ---------------------------------------------------------------- activities
    def record_activity(self, kind: str, summary: str, *, actor: str = "collie", detail: dict | None = None,
                        project_id: str = "", task_id: str = "", goal_id: str = "", run_id: str = "",
                        session: str = "", device_id: str = "", at: int | None = None) -> dict:
        if actor not in ACTIVITY_ACTORS:
            actor = "collie"
        at = int(at if at is not None else _now())
        cur = self._exec("INSERT INTO activities(at,actor,kind,summary,detail_json,project_id,task_id,goal_id,run_id,"
                         "session,device_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (at, actor, kind, _clip(summary, 400), json.dumps(detail or {}, ensure_ascii=False, default=str),
                          project_id or "", task_id or "", goal_id or "", str(run_id or ""), session or "", device_id or ""))
        return {"id": cur.lastrowid, "at": at, "actor": actor, "kind": kind, "summary": _clip(summary, 400),
                "detail": detail or {}, "project_id": project_id, "task_id": task_id, "goal_id": goal_id,
                "run_id": str(run_id or ""), "session": session, "device_id": device_id}

    def recent_activity(self, *, limit: int = 30, since: int | None = None, project_id: str = "",
                        kinds: tuple | None = None) -> list[dict]:
        sql, args = "SELECT * FROM activities WHERE 1=1", []
        if since is not None:
            sql += " AND at >= ?"; args.append(int(since))
        if project_id:
            sql += " AND project_id=?"; args.append(project_id)
        if kinds:
            sql += " AND kind IN (%s)" % ",".join("?" * len(kinds)); args += list(kinds)
        sql += " ORDER BY at DESC, id DESC LIMIT ?"; args.append(int(limit))
        rows = self._rows(sql, tuple(args))
        for r in rows:
            try:
                r["detail"] = json.loads(r.pop("detail_json") or "{}")
            except Exception:
                r["detail"] = {}
        return rows

    def activities_for_day(self, day: str) -> list[dict]:
        """Every activity of one day, oldest first.

        This used to ask for "the 2000 most recent rows since midnight" and filter in Python. Once
        more than 2000 rows existed *after* the requested day, that window contained none of the
        day's rows — and ``build_journal`` writes with INSERT OR REPLACE, so rebuilding an old day
        silently replaced a good entry with an empty one. Bound both ends in SQL instead.
        """
        start = int(_dt.datetime.strptime(day, "%Y-%m-%d").timestamp())
        rows = self._rows("SELECT * FROM activities WHERE at >= ? AND at < ? ORDER BY at, id",
                          (start, start + 86400))
        for r in rows:
            try:
                r["detail"] = json.loads(r.pop("detail_json") or "{}")
            except Exception:
                r["detail"] = {}
        return rows

    # ------------------------------------------------------------------- journal
    def journal_entry(self, day: str) -> dict | None:
        row = self._row("SELECT * FROM journal WHERE day=?", (day,))
        return _journal_row(row) if row else None

    def journal(self, *, limit: int = 14) -> list[dict]:
        rows = self._rows("SELECT * FROM journal ORDER BY day DESC LIMIT ?", (int(limit),))
        return [_journal_row(r) for r in rows if valid_day(r.get("day"))]

    def build_journal(self, day: str | None = None, *, narrator=None, source: str = "auto") -> dict:
        """Compress one day's activity stream into an AI-maintained journal entry.

        Deterministic by default; ``narrator(day_dict) -> str`` may add prose (a model), and a
        failure there degrades to the deterministic entry instead of losing the day.
        """
        day = day or day_key()
        acts = self.activities_for_day(day)
        # A journal is a compression, not a log: rank what the day was ABOUT (finished work, runs,
        # decisions, notes, handoffs) above bookkeeping (tasks merely created), then cap. A day of
        # planning still shows its plan, but only after everything that actually happened.
        primary, secondary, decisions = [], [], []
        touched_goals, touched_projects = set(), set()
        for a in acts:
            line = _clip(a["summary"], 160)
            if a["kind"] in ("task_done", "run", "note", "cloud_task", "handoff", "restore",
                             "suggestion_accepted", "workflow_automated", "verified", "event"):
                primary.append(line)
            elif a["kind"] in ("task_created", "sync", "workflow_learned", "file_changed", "focus"):
                secondary.append(line)
            if a["kind"] == "decision":
                decisions.append(_clip(a["summary"], 200))
            if a.get("goal_id"):
                touched_goals.add(a["goal_id"])
            if a.get("project_id"):
                touched_projects.add(a["project_id"])
        # de-duplicate while keeping order, then fill the remaining room with bookkeeping
        primary = list(dict.fromkeys(primary))
        secondary = [x for x in dict.fromkeys(secondary) if x not in primary]
        happened = (primary + secondary)[:_JOURNAL_HAPPENED_CAP]
        open_loops = []
        for gid in sorted(touched_goals):
            for t in self.tasks(goal_id=gid, include_done=False)[:4]:
                open_loops.append(t["title"])
        if not touched_goals:
            for t in self.tasks(status="doing")[:3] + self.tasks(status="next")[:3]:
                open_loops.append(t["title"])
        open_loops = list(dict.fromkeys(open_loops))[:8]
        nxt = [s["title"] for s in self.suggestions(status="open", limit=5)]
        for gid in sorted(touched_goals):
            t = self.next_task(gid)
            if t and t["title"] not in nxt:
                nxt.append(t["title"])
        nxt = nxt[:6]
        entry = {"day": day, "happened": happened, "decisions": decisions, "open_loops": open_loops, "next": nxt,
                 "narrative": "", "source": source, "generated_at": _now(),
                 "projects": [self._label("project", p) for p in sorted(touched_projects) if self._label("project", p)]}
        if narrator is not None:
            try:
                prose = narrator(entry)
                if prose:
                    entry["narrative"] = str(prose).strip()
                    entry["source"] = "llm"
            except Exception:
                pass
        self._exec("INSERT OR REPLACE INTO journal(day,happened_json,decisions_json,open_loops_json,next_json,narrative,"
                   "source,generated_at) VALUES(?,?,?,?,?,?,?,?)",
                   (day, json.dumps(entry["happened"], ensure_ascii=False), json.dumps(entry["decisions"], ensure_ascii=False),
                    json.dumps(entry["open_loops"], ensure_ascii=False), json.dumps(entry["next"], ensure_ascii=False),
                    entry["narrative"], entry["source"], entry["generated_at"]))
        return entry

    def record_decision(self, text: str, *, project_id: str = "", goal_id: str = "", actor: str = "user",
                        memory=None) -> dict:
        """A decision is an activity *and* a long-term memory claim (kind=decision) when a memory store is given."""
        act = self.record_activity("decision", text, actor=actor, project_id=project_id, goal_id=goal_id)
        if memory is not None:
            try:
                project = ""
                if project_id:
                    p = self.project(project_id)
                    project = p["name"] if p else ""
                memory.remember(text, project=project or "global", keys="decision personal-state",
                                kind="decision", subject="owner", source="host", confidence=0.9)
            except TypeError:
                try:
                    memory.remember(text, project=project or "global", keys="decision personal-state")
                except Exception:
                    pass
            except Exception:
                pass
        return act

    # ----------------------------------------------------------------- summaries
    def weekly_summary(self, week: str | None = None) -> dict:
        """Roll daily journal entries up into one week: the second compression level."""
        week = week or week_key()
        days = [j for j in self.journal(limit=60) if week_key(_dt.datetime.strptime(j["day"], "%Y-%m-%d").timestamp()) == week]
        happened = list(dict.fromkeys(h for j in reversed(days) for h in j["happened"]))
        decisions = list(dict.fromkeys(d for j in reversed(days) for d in j["decisions"]))
        open_loops = list(dict.fromkeys(o for j in days[:1] for o in j["open_loops"])) if days else []
        body = "Week %s — %d day(s) recorded, %d thing(s) happened, %d decision(s)." % (
            week, len(days), len(happened), len(decisions))
        facts = {"days": [j["day"] for j in days], "happened": happened[:40], "decisions": decisions[:20],
                 "open_loops": open_loops[:10]}
        self._exec("INSERT INTO summaries(id,scope,key,body,facts_json,period_start,period_end,generated_at) "
                   "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(scope,key) DO UPDATE SET body=excluded.body, "
                   "facts_json=excluded.facts_json, generated_at=excluded.generated_at",
                   (new_id("summary"), "week", week, body, json.dumps(facts, ensure_ascii=False), None, None, _now()))
        return {"scope": "week", "key": week, "body": body, **facts}

    def project_timeline(self, project_id: str, *, limit: int = 60) -> dict:
        """The third compression level: one project's trajectory (activities + journal mentions)."""
        p = self.project(project_id)
        acts = self.recent_activity(limit=limit, project_id=project_id)
        days = sorted({day_key(a["at"]) for a in acts})
        journal = [j for j in self.journal(limit=90) if j["day"] in days]
        goals = [g for g in self.goals(None) if g["project_id"] == project_id]
        return {"project": p, "activities": acts, "journal": journal, "goals": goals,
                "tasks": self.tasks(project_id=project_id)}

    # ----------------------------------------------------------------- workflows
    def upsert_workflow(self, name: str, *, trigger: str = "", steps: list | None = None, status: str = "observed",
                        source: str = "learned", workflow_id: str = "", confidence: float | None = None) -> dict:
        now = _now()
        existing = self._row("SELECT * FROM workflows WHERE id=?", (workflow_id,)) if workflow_id else \
            self._row("SELECT * FROM workflows WHERE lower(name)=lower(?)", (name,))
        if existing:
            self._exec("UPDATE workflows SET name=?, trigger=COALESCE(NULLIF(?,''),trigger), "
                       "steps_json=CASE WHEN ? THEN ? ELSE steps_json END, status=?, source=?, "
                       "confidence=COALESCE(?,confidence), updated_at=? WHERE id=?",
                       (name, trigger, 1 if steps is not None else 0, json.dumps(steps or [], ensure_ascii=False),
                        status, source, confidence, now, existing["id"]))
            return self.workflow(existing["id"])
        wid = workflow_id or new_id("workflow")
        self._exec("INSERT INTO workflows(id,name,trigger,steps_json,status,observations,confidence,source,created_at,updated_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (wid, name, trigger, json.dumps(steps or [], ensure_ascii=False), status, 0,
                    confidence if confidence is not None else 0.3, source, now, now))
        return self.workflow(wid)

    def workflow(self, workflow_id: str) -> dict | None:
        row = self._row("SELECT * FROM workflows WHERE id=?", (workflow_id,))
        return _workflow_row(row) if row else None

    def workflows(self, *, status: str | None = None) -> list[dict]:
        rows = (self._rows("SELECT * FROM workflows WHERE status=? ORDER BY updated_at DESC", (status,)) if status
                else self._rows("SELECT * FROM workflows ORDER BY updated_at DESC"))
        return [_workflow_row(r) for r in rows]

    def bump_workflow(self, workflow_id: str, *, observed: bool = False, accepted: bool = False,
                      rejected: bool = False, used: bool = False, status: str | None = None,
                      confidence: float | None = None) -> dict | None:
        sets, args = ["updated_at=?"], [_now()]
        if observed:
            sets.append("observations=observations+1")
        if accepted:
            sets.append("accepted=accepted+1")
        if rejected:
            sets.append("rejected=rejected+1")
        if used:
            sets.append("last_used_at=?"); args.append(_now())
        if status:
            sets.append("status=?"); args.append(status)
        if confidence is not None:
            sets.append("confidence=?"); args.append(float(confidence))
        args.append(workflow_id)
        self._exec("UPDATE workflows SET %s WHERE id=?" % ", ".join(sets), tuple(args))
        return self.workflow(workflow_id)

    def add_workflow_observation(self, prev_kind: str, next_kind: str, *, goal_id: str = "", run_id: str = "",
                                 workflow_id: str = "") -> None:
        self._exec("INSERT INTO workflow_observations(workflow_id,prev_kind,next_kind,goal_id,at,run_id) VALUES(?,?,?,?,?,?)",
                   (workflow_id, prev_kind, next_kind, goal_id, _now(), str(run_id or "")))

    def transition_counts(self) -> dict[tuple[str, str], int]:
        rows = self._rows("SELECT prev_kind,next_kind,COUNT(DISTINCT COALESCE(NULLIF(goal_id,''),CAST(id AS TEXT))) AS n "
                          "FROM workflow_observations GROUP BY prev_kind,next_kind")
        return {(r["prev_kind"], r["next_kind"]): int(r["n"]) for r in rows}

    # --------------------------------------------------------------- suggestions
    def add_suggestion(self, title: str, *, kind: str = "next_step", body: str = "", task_id: str = "",
                       workflow_id: str = "", goal_id: str = "", action: dict | None = None,
                       confidence: float = 0.5, source: str = "workflow", dedupe: bool = True) -> dict:
        if dedupe:
            dup = self._row("SELECT * FROM suggestions WHERE status='open' AND title=? AND kind=?", (title, kind))
            if dup:
                return _suggestion_row(dup)
        sid = new_id("suggestion")
        self._exec("INSERT INTO suggestions(id,kind,title,body,task_id,workflow_id,goal_id,action_json,status,confidence,"
                   "source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (sid, kind, title, body, task_id, workflow_id, goal_id, json.dumps(action or {}, ensure_ascii=False),
                    "open", float(confidence), source, _now()))
        return self.suggestion(sid)

    def suggestion(self, suggestion_id: str) -> dict | None:
        row = self._row("SELECT * FROM suggestions WHERE id=?", (suggestion_id,))
        return _suggestion_row(row) if row else None

    def suggestions(self, *, status: str | None = "open", limit: int = 20) -> list[dict]:
        rows = (self._rows("SELECT * FROM suggestions WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, int(limit)))
                if status else self._rows("SELECT * FROM suggestions ORDER BY created_at DESC LIMIT ?", (int(limit),)))
        return [_suggestion_row(r) for r in rows]

    def resolve_suggestion(self, suggestion_id: str, status: str, *, actor: str = "user") -> dict | None:
        if status not in ("accepted", "dismissed", "expired"):
            raise ValueError("unknown suggestion resolution: %s" % status)
        s = self.suggestion(suggestion_id)
        if not s or s["status"] != "open":
            return s
        self._exec("UPDATE suggestions SET status=?, resolved_at=? WHERE id=?", (status, _now(), suggestion_id))
        self.record_activity("suggestion_%s" % ("accepted" if status == "accepted" else "rejected"),
                             "%s suggestion: %s" % ("Accepted" if status == "accepted" else "Dismissed", s["title"]),
                             actor=actor, task_id=s["task_id"], goal_id=s["goal_id"],
                             detail={"suggestion_id": suggestion_id, "workflow_id": s["workflow_id"]})
        if s["workflow_id"]:
            self.bump_workflow(s["workflow_id"], accepted=(status == "accepted"), rejected=(status != "accepted"))
        return self.suggestion(suggestion_id)

    # ------------------------------------------------------------------- devices
    def upsert_device(self, device_id: str, name: str, *, platform: str = "", kind: str = "desktop",
                      this_device: bool = False, runtime: dict | None = None) -> dict:
        self._exec("INSERT INTO devices(device_id,name,platform,kind,this_device,runtime_json,last_seen) VALUES(?,?,?,?,?,?,?) "
                   "ON CONFLICT(device_id) DO UPDATE SET name=excluded.name, platform=excluded.platform, kind=excluded.kind, "
                   "this_device=excluded.this_device, runtime_json=excluded.runtime_json, last_seen=excluded.last_seen",
                   (device_id, name, platform, kind, 1 if this_device else 0, json.dumps(runtime or {}, ensure_ascii=False), _now()))
        return self._row("SELECT * FROM devices WHERE device_id=?", (device_id,))

    def devices(self) -> list[dict]:
        rows = self._rows("SELECT * FROM devices ORDER BY this_device DESC, last_seen DESC")
        for r in rows:
            try:
                r["runtime"] = json.loads(r.pop("runtime_json") or "{}")
            except Exception:
                r["runtime"] = {}
        return rows

    # --------------------------------------------------------------- cloud tasks
    def add_cloud_task(self, title: str, *, runtime: str = "sauna-cloud", scheduled_for: int | None = None,
                       deliver_at: int | None = None, detail: dict | None = None) -> dict:
        cid = new_id("cloud_task")
        now = _now()
        self._exec("INSERT INTO cloud_tasks(id,title,runtime,status,scheduled_for,deliver_at,detail_json,created_at,updated_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   (cid, title, runtime, "scheduled", scheduled_for, deliver_at, json.dumps(detail or {}, ensure_ascii=False),
                    now, now))
        return self.cloud_task(cid)

    def cloud_task(self, cloud_task_id: str) -> dict | None:
        row = self._row("SELECT * FROM cloud_tasks WHERE id=?", (cloud_task_id,))
        return _cloud_row(row) if row else None

    def cloud_tasks(self, *, status: str | None = None, limit: int = 50) -> list[dict]:
        rows = (self._rows("SELECT * FROM cloud_tasks WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, int(limit)))
                if status else self._rows("SELECT * FROM cloud_tasks ORDER BY created_at DESC LIMIT ?", (int(limit),)))
        return [_cloud_row(r) for r in rows]

    def update_cloud_task(self, cloud_task_id: str, *, status: str | None = None, result: str | None = None,
                          detail: dict | None = None) -> dict | None:
        sets, args = ["updated_at=?"], [_now()]
        if status:
            sets.append("status=?"); args.append(status)
        if result is not None:
            sets.append("result=?"); args.append(result)
        if detail is not None:
            sets.append("detail_json=?"); args.append(json.dumps(detail, ensure_ascii=False))
        args.append(cloud_task_id)
        self._exec("UPDATE cloud_tasks SET %s WHERE id=?" % ", ".join(sets), tuple(args))
        return self.cloud_task(cloud_task_id)

    # ------------------------------------------------------------ sync / export
    def delete_prefixed(self, table: str, column: str, prefix: str) -> int:
        """Delete rows whose id starts with an exact literal prefix.

        ``LIKE 'demo_%'`` treats ``_`` as a single-character wildcard, so it also matches ids like
        ``demoX...``. ESCAPE makes the prefix mean what it says.
        """
        if table not in ("tasks", "events", "notes", "people", "goals", "projects", "workflows",
                         "suggestions", "activities", "relations"):
            raise ValueError("unknown table: %s" % table)
        if column not in ("id", "src_id", "dst_id", "goal_id", "project_id", "task_id"):
            raise ValueError("unknown column: %s" % column)
        pattern = prefix.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%") + "%"
        cur = self._exec("DELETE FROM %s WHERE %s LIKE ? ESCAPE '\\'" % (table, column), (pattern,))
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def core_schema_status(self) -> dict:
        """Expose migration/sync health without leaking entity contents."""
        with self._lock:
            version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
            latest = int(self._db.execute("SELECT COALESCE(MAX(seq),0) FROM sync_changes").fetchone()[0])
            conflicts = int(self._db.execute(
                "SELECT COUNT(*) FROM sync_conflicts WHERE status='open'").fetchone()[0])
            migrations = [dict(r) for r in self._db.execute(
                "SELECT version,applied_at,description,checksum FROM schema_migrations ORDER BY version")]
        return {"schema_version": version, "supported_version": PERSONAL_CORE_SCHEMA_VERSION,
                "wire_format": PERSONAL_DELTA_FORMAT, "latest_cursor": latest,
                "open_conflicts": conflicts, "migrations": migrations,
                "device_id": self._origin_device}

    def peer_cursor(self, peer_id: str) -> dict:
        peer_id = str(peer_id or "").strip()[:160]
        if not peer_id:
            raise ValueError("peer_id is required")
        row = self._row("SELECT * FROM sync_peers WHERE peer_id=?", (peer_id,))
        return row or {"peer_id": peer_id, "push_cursor": 0, "pull_cursor": 0, "updated_at": 0}

    def set_peer_push_cursor(self, peer_id: str, cursor: int) -> dict:
        peer_id = str(peer_id or "").strip()[:160]
        cursor = max(0, int(cursor or 0))
        if not peer_id:
            raise ValueError("peer_id is required")
        self._exec(
            "INSERT INTO sync_peers(peer_id,push_cursor,pull_cursor,updated_at) VALUES(?,?,0,?) "
            "ON CONFLICT(peer_id) DO UPDATE SET push_cursor=MAX(sync_peers.push_cursor,excluded.push_cursor),"
            "updated_at=excluded.updated_at", (peer_id, cursor, _now()))
        return self.peer_cursor(peer_id)

    def enqueue_sync_category(self, category: str) -> int:
        """Queue a bootstrap revision when a previously withheld category is enabled.

        Cursors may safely pass withheld rows.  Re-enqueuing the current versions ensures enabling
        a category later sends its present state without resurrecting historical intermediate edits.
        """
        category = str(category or "")
        specs = [spec for spec in ENTITY_SPECS if spec.category == category]
        if not specs:
            return 0
        now, queued = _now(), 0
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for spec in specs:
                    rows = self._db.execute(
                        "SELECT v.*,t.* FROM entity_versions v LEFT JOIN \"%s\" t ON "
                        "CAST(t.\"%s\" AS TEXT)=v.entity_id WHERE v.entity_type=?" %
                        (spec.table, spec.key), (spec.entity_type,)).fetchall()
                    for row in rows:
                        payload = {c: row[c] for c in spec.columns} if row[spec.key] is not None else {}
                        operation = "delete" if row["deleted_at"] is not None else "upsert"
                        self._db.execute(
                            "INSERT INTO sync_changes(change_id,entity_type,entity_id,operation,base_revision,"
                            "revision,origin_device,hlc,changed_at,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            ("chg_" + secrets.token_hex(16), spec.entity_type, row["entity_id"], operation,
                             max(0, int(row["revision"]) - 1), int(row["revision"]),
                             row["origin_device"], row["hlc"], now,
                             json.dumps(payload, ensure_ascii=False, separators=(",", ":"))))
                        queued += 1
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return queued

    def changes_since(self, cursor: int = 0, *, include: dict | None = None,
                      limit: int = 500) -> dict:
        """Return a bounded, privacy-filtered Personal AI delta after ``cursor``.

        Rows are immutable snapshots captured by SQLite triggers at write time.  This avoids the
        classic delta bug where revision 1 is exported later with revision 3's payload.
        """
        cursor = max(0, int(cursor or 0))
        limit = max(1, min(2000, int(limit or 500)))
        inc = dict(_DEFAULT_SYNC)
        inc.update({"projects": True, "relationships": True})
        inc.update(include or {})
        with self._lock:
            rows = [dict(r) for r in self._db.execute(
                "SELECT * FROM sync_changes WHERE seq>? ORDER BY seq LIMIT ?", (cursor, limit)).fetchall()]
            scanned_cursor = int(rows[-1]["seq"]) if rows else cursor
            has_more = bool(self._db.execute(
                "SELECT 1 FROM sync_changes WHERE seq>? LIMIT 1", (scanned_cursor,)).fetchone())
        changes, withheld = [], set()
        for row in rows:
            spec = ENTITY_BY_TYPE.get(row["entity_type"])
            if not spec:
                continue
            if not bool(inc.get(spec.category, True)):
                withheld.add(spec.category)
                continue
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except (TypeError, ValueError):
                payload = {}
            if spec.entity_type == "device" and payload:
                payload["this_device"] = 0
            if spec.entity_type == "project" and not inc.get("local_files", False) and payload:
                payload["path"] = ""
            changes.append({
                "change_id": row["change_id"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "operation": row["operation"],
                "base_revision": int(row["base_revision"]), "revision": int(row["revision"]),
                "origin_device": row["origin_device"], "hlc": row["hlc"],
                "changed_at": int(row["changed_at"]), "payload": payload,
            })
        return {"format": PERSONAL_DELTA_FORMAT, "source_device": self._origin_device,
                "from_cursor": cursor, "cursor": scanned_cursor, "has_more": has_more,
                "categories": {k: bool(v) for k, v in inc.items()},
                "withheld": sorted(withheld), "changes": changes}

    @staticmethod
    def _validate_remote_change(change: dict):
        if not isinstance(change, dict):
            raise SyncDeltaError("delta change must be an object")
        entity_type = str(change.get("entity_type") or "")
        spec = ENTITY_BY_TYPE.get(entity_type)
        if not spec:
            raise SyncDeltaError("unknown delta entity_type: %s" % entity_type)
        change_id = str(change.get("change_id") or "")
        entity_id = str(change.get("entity_id") or "")
        operation = str(change.get("operation") or "")
        if not change_id or len(change_id) > 200 or not entity_id or len(entity_id) > 300:
            raise SyncDeltaError("delta change has an invalid id")
        if operation not in ("upsert", "delete"):
            raise SyncDeltaError("delta operation must be upsert or delete")
        try:
            base_revision = int(change.get("base_revision", 0))
            revision = int(change.get("revision", 0))
            changed_at = int(change.get("changed_at", 0))
        except (TypeError, ValueError):
            raise SyncDeltaError("delta revision fields must be integers")
        if base_revision < 0 or revision < 1 or revision <= base_revision or changed_at < 0:
            raise SyncDeltaError("delta revision is not monotonic")
        origin = str(change.get("origin_device") or "")[:160]
        hlc = str(change.get("hlc") or "")[:300]
        if not origin or not hlc:
            raise SyncDeltaError("delta origin and HLC are required")
        payload = change.get("payload") or {}
        if not isinstance(payload, dict):
            raise SyncDeltaError("delta payload must be an object")
        if len(json.dumps(payload, ensure_ascii=False, default=str)) > 1_000_000:
            raise SyncDeltaError("delta payload exceeds 1 MiB")
        if operation == "upsert":
            missing = [c for c in spec.columns if c not in payload]
            if missing or str(payload.get(spec.key)) != entity_id:
                raise SyncDeltaError("delta payload does not match %s/%s" % (entity_type, entity_id))
        return spec, change_id, entity_id, operation, base_revision, revision, changed_at, origin, hlc, payload

    def _write_entity_change(self, spec, entity_id: str, operation: str, payload: dict) -> None:
        if operation == "delete":
            self._db.execute('DELETE FROM "%s" WHERE CAST("%s" AS TEXT)=?' %
                             (spec.table, spec.key), (entity_id,))
            return
        values = [payload.get(c) for c in spec.columns]
        if spec.entity_type == "device":
            values[spec.columns.index("this_device")] = 0
        updates = [c for c in spec.columns if c != spec.key]
        self._db.execute(
            'INSERT INTO "%s"(%s) VALUES(%s) ON CONFLICT("%s") DO UPDATE SET %s' %
            (spec.table, ",".join('"%s"' % c for c in spec.columns),
             ",".join("?" for _ in spec.columns), spec.key,
             ",".join('"%s"=excluded."%s"' % (c, c) for c in updates)), tuple(values))

    def _record_conflict(self, change: dict, local_revision: int, local_payload: dict) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO sync_conflicts(conflict_id,remote_change_id,entity_type,entity_id,"
            "local_revision,remote_base_revision,local_json,remote_json,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("cnf_" + secrets.token_hex(12), change["change_id"], change["entity_type"],
             change["entity_id"], local_revision, int(change.get("base_revision") or 0),
             json.dumps(local_payload, ensure_ascii=False, default=str),
             json.dumps(change, ensure_ascii=False, default=str), "open", _now()))

    def apply_delta(self, delta: dict, *, peer_id: str = "sauna") -> dict:
        """Apply one v2 delta atomically, preserving divergent edits in the conflict tray."""
        if not isinstance(delta, dict) or delta.get("format") != PERSONAL_DELTA_FORMAT:
            raise SyncDeltaError("not a Personal AI delta v2")
        peer_id = str(peer_id or "").strip()[:160]
        if not peer_id:
            raise SyncDeltaError("peer_id is required")
        changes = delta.get("changes") or []
        if not isinstance(changes, list) or len(changes) > 5000:
            raise SyncDeltaError("delta changes must be a bounded list")
        validated = [self._validate_remote_change(change) for change in changes]
        applied = ignored = conflicts = 0
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for change, parts in zip(changes, validated):
                    (spec, change_id, entity_id, operation, base_revision, revision,
                     changed_at, origin, hlc, payload) = parts
                    if self._db.execute("SELECT 1 FROM sync_applied WHERE change_id=?",
                                        (change_id,)).fetchone():
                        ignored += 1
                        continue
                    version = self._db.execute(
                        "SELECT * FROM entity_versions WHERE entity_type=? AND entity_id=?",
                        (spec.entity_type, entity_id)).fetchone()
                    local_revision = int(version["revision"]) if version else 0
                    local_row = self._db.execute(
                        'SELECT * FROM "%s" WHERE CAST("%s" AS TEXT)=?' % (spec.table, spec.key),
                        (entity_id,)).fetchone()
                    local_payload = {c: local_row[c] for c in spec.columns} if local_row else {}
                    same_value = ((operation == "delete" and not local_row) or
                                  (operation == "upsert" and local_payload == payload))
                    same_origin_stale = bool(version and version["origin_device"] == origin and
                                             revision <= local_revision)
                    clean = not version or base_revision == local_revision
                    if same_value or same_origin_stale:
                        ignored += 1
                    elif not clean:
                        self._record_conflict(change, local_revision, local_payload)
                        conflicts += 1
                    else:
                        with self._remote_apply(origin):
                            self._write_entity_change(spec, entity_id, operation, payload)
                        self._db.execute(
                            "INSERT INTO entity_versions(entity_type,entity_id,revision,origin_device,hlc,deleted_at,"
                            "changed_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(entity_type,entity_id) DO UPDATE SET "
                            "revision=excluded.revision,origin_device=excluded.origin_device,hlc=excluded.hlc,"
                            "deleted_at=excluded.deleted_at,changed_at=excluded.changed_at",
                            (spec.entity_type, entity_id, revision, origin, hlc,
                             changed_at if operation == "delete" else None, changed_at))
                        applied += 1
                    self._db.execute(
                        "INSERT INTO sync_applied(change_id,peer_id,applied_at) VALUES(?,?,?)",
                        (change_id, peer_id, _now()))
                pull_cursor = max(0, int(delta.get("cursor") or 0))
                self._db.execute(
                    "INSERT INTO sync_peers(peer_id,push_cursor,pull_cursor,updated_at) VALUES(?,0,?,?) "
                    "ON CONFLICT(peer_id) DO UPDATE SET pull_cursor=MAX(sync_peers.pull_cursor,"
                    "excluded.pull_cursor),updated_at=excluded.updated_at",
                    (peer_id, pull_cursor, _now()))
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return {"applied": applied, "ignored": ignored, "conflicts": conflicts,
                "cursor": max(0, int(delta.get("cursor") or 0)), "peer_id": peer_id}

    def sync_conflicts(self, *, status: str = "open", limit: int = 100) -> list[dict]:
        if status not in ("open", "resolved_local", "resolved_remote"):
            raise ValueError("unknown conflict status")
        rows = self._rows(
            "SELECT * FROM sync_conflicts WHERE status=? ORDER BY created_at DESC LIMIT ?",
            (status, max(1, min(500, int(limit or 100)))))
        for row in rows:
            for key in ("local_json", "remote_json"):
                try:
                    row[key[:-5]] = json.loads(row.pop(key) or "{}")
                except (TypeError, ValueError):
                    row[key[:-5]] = {}
        return rows

    def resolve_sync_conflict(self, conflict_id: str, resolution: str) -> dict | None:
        """Resolve with the local value or publish the remote value as a new local revision."""
        if resolution not in ("local", "remote"):
            raise ValueError("resolution must be local or remote")
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM sync_conflicts WHERE conflict_id=? AND status='open'",
                (conflict_id,)).fetchone()
            if not row:
                return None
            remote = json.loads(row["remote_json"] or "{}")
            if resolution == "remote":
                parts = self._validate_remote_change(remote)
                spec, _cid, entity_id, operation, *_rest, payload = parts
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    # This is an explicit local decision, not a remote replay.  Let the ordinary
                    # trigger create a fresh revision so the resolution propagates to every peer.
                    self._write_entity_change(spec, entity_id, operation, payload)
                    self._db.execute(
                        "UPDATE sync_conflicts SET status='resolved_remote',resolved_at=? WHERE conflict_id=?",
                        (_now(), conflict_id))
                    self._db.commit()
                except Exception:
                    self._db.rollback()
                    raise
            else:
                self._db.execute(
                    "UPDATE sync_conflicts SET status='resolved_local',resolved_at=? WHERE conflict_id=?",
                    (_now(), conflict_id))
                self._db.commit()
        return self._row("SELECT * FROM sync_conflicts WHERE conflict_id=?", (conflict_id,))

    def export_snapshot(self, *, include: dict | None = None) -> dict:
        """Everything Sauna (or a new device) needs to restore this person's AI — filtered by the
        user's sync choices. ``include`` maps category -> bool; absent categories default to True
        except the sensitive ones, which default to False."""
        inc = dict(_DEFAULT_SYNC)
        inc.update(include or {})
        snap = {"format": "collie-personal-state/1", "exported_at": _now(),
                "device_id": self.get_meta("device_id"), "categories": {k: bool(v) for k, v in inc.items()}}
        if inc.get("projects", True):
            snap["projects"] = self.projects(None)
            if not inc.get("local_files", False):
                for project in snap["projects"]:
                    project["path"] = ""
        if inc.get("relationships", True):
            snap["people"] = self.people()
        if inc.get("goals", True):
            snap["goals"] = self._rows("SELECT * FROM goals")
        if inc.get("tasks", True):
            snap["tasks"] = self._rows("SELECT * FROM tasks")
        if inc.get("calendar", True):
            snap["events"] = self._rows("SELECT * FROM events")
        if inc.get("notes", True):
            snap["notes"] = self._rows("SELECT * FROM notes")
            snap["relations"] = self._rows("SELECT * FROM relations")
        if inc.get("journal", True):
            snap["journal"] = self._rows("SELECT * FROM journal")
            snap["summaries"] = self._rows("SELECT * FROM summaries")
        if inc.get("workflows", True):
            snap["workflows"] = self._rows("SELECT * FROM workflows")
            snap["suggestions"] = self._rows("SELECT * FROM suggestions WHERE status='open'")
        if inc.get("agent_activity", True):
            rows = self._rows("SELECT * FROM activities ORDER BY at DESC LIMIT 500")
            if not inc.get("conversations", False):
                # "Agent activity" means what Collie DID, not what it SAID. The answer excerpt an
                # activity carries for the local Activity view is conversation content, so it only
                # travels when the person also enabled conversation history.
                for row in rows:
                    try:
                        detail = json.loads(row.get("detail_json") or "{}")
                    except Exception:
                        continue
                    if detail.pop("answer", None) is not None:
                        row["detail_json"] = json.dumps(detail, ensure_ascii=False)
            snap["activities"] = rows
        if inc.get("preferences", True):
            snap["meta"] = {r["key"]: r["value"] for r in self._rows("SELECT * FROM meta")
                            if r["key"] in _SYNC_META_KEYS}
        snap["devices"] = self._rows("SELECT * FROM devices")
        snap["cloud_tasks"] = self._rows("SELECT * FROM cloud_tasks")
        return snap

    def import_snapshot(self, snap: dict, *, merge: bool = True) -> dict:
        """Restore personal state from a snapshot (the 'Welcome back' path). Merge keeps local rows
        and upserts by id; counts are returned so the UI can say exactly what came back."""
        if not isinstance(snap, dict) or not str(snap.get("format", "")).startswith("collie-personal-state/"):
            raise ValueError("not a Collie personal-state snapshot")
        counts = {}
        plan = [
            ("projects", "projects", ("id", "name", "kind", "status", "summary", "path", "created_at", "updated_at")),
            ("people", "people", ("id", "name", "role", "org", "project_id", "notes", "created_at")),
            ("goals", "goals", ("id", "title", "status", "project_id", "due_at", "summary", "created_at", "updated_at")),
            ("tasks", "tasks", ("id", "title", "status", "project_id", "goal_id", "kind", "due_at", "order_key", "source",
                                "notes", "done_at", "created_at", "updated_at")),
            ("events", "events", ("id", "title", "start_at", "end_at", "all_day", "kind", "location", "project_id",
                                  "goal_id", "external_ref", "notes", "created_at", "updated_at")),
            ("notes", "notes", ("id", "title", "body", "project_id", "goal_id", "source", "pinned", "created_at", "updated_at")),
            ("relations", "relations", ("src_type", "src_id", "dst_type", "dst_id", "rel", "created_at")),
            ("journal", "journal", ("day", "happened_json", "decisions_json", "open_loops_json", "next_json", "narrative",
                                    "source", "generated_at")),
            ("summaries", "summaries", ("id", "scope", "key", "body", "facts_json", "period_start", "period_end", "generated_at")),
            ("workflows", "workflows", ("id", "name", "trigger", "steps_json", "status", "observations", "confidence", "source",
                                        "accepted", "rejected", "last_used_at", "created_at", "updated_at")),
            ("suggestions", "suggestions", ("id", "kind", "title", "body", "task_id", "workflow_id", "goal_id", "action_json",
                                            "status", "confidence", "source", "created_at", "resolved_at")),
            ("cloud_tasks", "cloud_tasks", ("id", "title", "runtime", "status", "scheduled_for", "deliver_at", "detail_json",
                                            "result", "created_at", "updated_at")),
            ("devices", "devices", ("device_id", "name", "platform", "kind", "this_device", "runtime_json", "last_seen")),
        ]
        with self._lock:
            for key, table, cols in plan:
                rows = snap.get(key) or []
                n = 0
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    vals = [r.get(c) for c in cols]
                    if table == "devices":
                        vals[cols.index("this_device")] = 0   # another machine's "this" is not ours
                    if table == "journal" and not valid_day(r.get("day")):
                        continue          # a malformed key would break every later render
                    verb = "INSERT OR IGNORE" if merge else "INSERT OR REPLACE"
                    cur = self._db.execute(
                        "%s INTO %s(%s) VALUES(%s)" % (verb, table, ",".join(cols), ",".join("?" * len(cols))),
                        tuple(vals))
                    n += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                counts[key] = n
            acts = snap.get("activities") or []
            n = 0
            for a in acts:
                if not isinstance(a, dict):
                    continue
                dup = self._db.execute("SELECT 1 FROM activities WHERE at=? AND kind=? AND summary=?",
                                       (a.get("at"), a.get("kind"), a.get("summary"))).fetchone()
                if dup:
                    continue
                self._db.execute("INSERT INTO activities(at,actor,kind,summary,detail_json,project_id,task_id,goal_id,run_id,"
                                 "session,device_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                 (a.get("at") or _now(), a.get("actor") or "collie", a.get("kind") or "run",
                                  a.get("summary") or "", a.get("detail_json") or "{}", a.get("project_id") or "",
                                  a.get("task_id") or "", a.get("goal_id") or "", str(a.get("run_id") or ""),
                                  a.get("session") or "", a.get("device_id") or ""))
                n += 1
            counts["activities"] = n
            for k, v in (snap.get("meta") or {}).items():
                if merge and self._db.execute("SELECT 1 FROM meta WHERE key=?", (k,)).fetchone():
                    continue
                self._db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (k, str(v)))
            self._db.commit()
        self.record_activity("restore", "Restored personal state from snapshot (%s)" % ", ".join(
            "%d %s" % (v, k) for k, v in counts.items() if v), actor="sauna", detail={"counts": counts})
        return counts

    # --------------------------------------------------------------- projections
    def render_views(self, out_dir: str | None = None, *, profile_lines: list[str] | None = None) -> dict[str, str]:
        """Project the structured state into Markdown files a person can read or edit.

        Returns {name: path}. Markdown is a *view*: nothing reads it back."""
        out_dir = out_dir or os.path.join(state_dir(), "state")
        os.makedirs(out_dir, exist_ok=True)
        now = _now()
        files = {}

        def _write(name: str, text: str) -> None:
            # Two surfaces can render at once (a run finishing while the web UI saves a note); a
            # plain truncating write interleaves. Write beside the target, then replace atomically.
            p = os.path.join(out_dir, name)
            tmp = "%s.%d.%s.tmp" % (p, os.getpid(), secrets.token_hex(4))
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            os.replace(tmp, p)
            files[name] = p

        # today.md
        lines = ["# Today — %s" % _dt.datetime.fromtimestamp(now).strftime("%A, %B %d"), "",
                 "_Projection of the structured personal state; edit the state, not this file._", ""]
        ups = self.upcoming(now=now)
        if ups:
            lines.append("## Upcoming")
            for e in ups[:5]:
                lines.append("- **%s** · %s%s" % (e["title"], _fmt_when(e), (" · goal: %s (%d%%)" % (
                    e["goal"]["title"], round((e["preparation"] or 0) * 100))) if e.get("goal") else ""))
            lines.append("")
        goals = self.goals()
        if goals:
            lines.append("## Goals")
            for g in goals:
                lines.append("- %s — %s %d%%" % (g["title"], _bar(g["progress"]), round(g["progress"] * 100)))
                for t in self.tasks(goal_id=g["id"])[:12]:
                    mark = {"done": "✓", "doing": "→", "next": "→"}.get(t["status"], "○")
                    lines.append("  - %s %s" % (mark, t["title"]))
            lines.append("")
        sugg = self.suggestions()
        if sugg:
            lines.append("## Suggested next")
            for s in sugg[:5]:
                lines.append("- %s" % s["title"])
            lines.append("")
        acts = self.recent_activity(limit=12)
        if acts:
            lines.append("## Recent activity")
            for a in acts:
                lines.append("- %s · %s" % (_dt.datetime.fromtimestamp(a["at"]).strftime("%H:%M"), a["summary"]))
            lines.append("")
        _write("today.md", "\n".join(lines))

        # recent_activity.md
        lines = ["# Recent activity", ""]
        for a in self.recent_activity(limit=100):
            lines.append("- %s · [%s] %s" % (_dt.datetime.fromtimestamp(a["at"]).strftime("%Y-%m-%d %H:%M"), a["actor"], a["summary"]))
        _write("recent_activity.md", "\n".join(lines) + "\n")

        # project_summary.md
        lines = ["# Projects", ""]
        for p in self.projects(None):
            lines.append("## %s (%s)" % (p["name"], p["status"]))
            if p["summary"]:
                lines.append(p["summary"])
            tl = self.project_timeline(p["id"], limit=8)
            for g in tl["goals"]:
                lines.append("- goal: %s — %d%%" % (g["title"], round(g["progress"] * 100)))
            for a in tl["activities"][:8]:
                lines.append("- %s · %s" % (_dt.datetime.fromtimestamp(a["at"]).strftime("%m-%d %H:%M"), a["summary"]))
            lines.append("")
        _write("project_summary.md", "\n".join(lines))

        # profile.md
        lines = ["# Profile", "", "_Who this Collie believes it is working for. Derived; edit the underlying state._", ""]
        for k in ("owner_name", "owner_role", "owner_location"):
            v = self.get_meta(k)
            if v:
                lines.append("- %s: %s" % (k.replace("owner_", "").title(), v))
        if profile_lines:
            lines.append("")
            lines.append("## Confirmed preferences & habits (from Collie memory)")
            lines.extend("- %s" % l for l in profile_lines)
        if goals:
            lines.append("")
            lines.append("## Active goals")
            lines.extend("- %s" % g["title"] for g in goals)
        ppl = self.people()
        if ppl:
            lines.append("")
            lines.append("## People")
            lines.extend("- %s%s" % (p["name"], (" — %s%s" % (p["role"], (", " + p["org"]) if p["org"] else "")) if p["role"] or p["org"] else "")
                         for p in ppl)
        wfs = [w for w in self.workflows() if w["status"] in ("suggested", "confirmed", "automated")]
        if wfs:
            lines.append("")
            lines.append("## Learned workflows")
            for w in wfs:
                lines.append("- %s (%s, seen %d×): %s" % (w["name"], w["status"], w["observations"],
                                                           " → ".join(s.get("title", "") for s in w["steps"])))
        _write("profile.md", "\n".join(lines) + "\n")
        # journal as markdown, one file per day, for the same reason: readable, never parsed back
        for j in self.journal(limit=7):
            _write("journal-%s.md" % j["day"], _journal_markdown(j))
        return files


# ---------------------------------------------------------------------------- helpers
_JOURNAL_HAPPENED_CAP = 14   # a day compresses to a readable page, not a log dump

_DEFAULT_SYNC = {
    "preferences": True, "projects": True, "relationships": True, "goals": True, "tasks": True,
    "calendar": True, "notes": True, "journal": True, "workflows": True, "agent_activity": True,
    "conversations": False, "local_files": False, "browser_history": False, "screen_history": False,
}

# A positive allow-list is safer than trying to recognize every future credential/cache key.  In
# particular, ``sauna_token_ref``, browser-link state, inbox caches and peer cursors must never ride
# inside a personal snapshot merely because they do not start with ``secret:``.
_SYNC_META_KEYS = frozenset(("owner_name", "owner_role", "owner_location", "related_topic"))

_STOP = {"the", "a", "an", "to", "of", "for", "and", "or", "in", "on", "with", "my", "me", "this", "that",
         "is", "it", "up", "do", "finish", "finishing", "please", "now", "then", "let", "lets", "let's", "go",
         "make", "get", "can", "you", "i", "we", "our", "your", "about", "at", "by", "from", "into", "out"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9一-鿿]+", (text or "").lower()) if w not in _STOP and len(w) > 1}


_KIND_HINTS = (
    # order = specificity: "prepare system design examples" is design work, "prepare product thesis"
    # is writing; the generic "prepare / rehearse" only wins when nothing more specific matches.
    ("research", ("research", "investigate", "look into", "survey", "competitor", "调研", "研究")),
    ("design", ("design", "architecture", "system design", "设计", "架构")),
    ("build", ("build", "implement", "prototype", "ship", "code", "fix", "write code", "实现", "开发")),
    ("write", ("write", "draft", "document", "notes", "report", "thesis", "summary", "brief", "撰写", "文档", "报告")),
    ("review", ("review", "check", "verify", "test", "审查", "验证", "测试")),
    ("communicate", ("email", "send", "notify", "message", "call", "reply", "邮件", "通知", "回复")),
    ("prepare", ("prepare", "prep", "rehearse", "practice", "准备", "练习")),
)


def _infer_kind(title: str) -> str:
    t = (title or "").lower()
    for kind, hints in _KIND_HINTS:
        if any(h in t for h in hints):
            return kind
    return "task"


def _bar(progress: float, width: int = 10) -> str:
    n = int(round(max(0.0, min(1.0, progress or 0.0)) * width))
    return "█" * n + "░" * (width - n)


def _fmt_when(e: dict) -> str:
    start = _dt.datetime.fromtimestamp(e["start_at"])
    if e.get("all_day"):
        return start.strftime("%A, %b %d")
    return start.strftime("%A · %I:%M %p").replace(" 0", " ")


def _journal_row(row: dict) -> dict:
    out = dict(row)
    for k in ("happened", "decisions", "open_loops", "next"):
        try:
            out[k] = json.loads(out.pop(k + "_json") or "[]")
        except Exception:
            out[k] = []
    return out


def _journal_markdown(j: dict) -> str:
    lines = ["# %s" % _dt.datetime.strptime(j["day"], "%Y-%m-%d").strftime("%B %d"), ""]
    if j.get("narrative"):
        lines += [j["narrative"], ""]
    for head, key in (("What happened", "happened"), ("Decisions", "decisions"), ("Open loops", "open_loops"), ("Next", "next")):
        if j.get(key):
            lines.append("## %s" % head)
            lines.extend("- %s" % x for x in j[key])
            lines.append("")
    lines.append("_Generated by Collie (%s)._" % j.get("source", "auto"))
    return "\n".join(lines) + "\n"


def _workflow_row(row: dict) -> dict:
    out = dict(row)
    try:
        out["steps"] = json.loads(out.pop("steps_json") or "[]")
    except Exception:
        out["steps"] = []
    return out


def _suggestion_row(row: dict) -> dict:
    out = dict(row)
    try:
        out["action"] = json.loads(out.pop("action_json") or "{}")
    except Exception:
        out["action"] = {}
    return out


def _cloud_row(row: dict) -> dict:
    out = dict(row)
    try:
        out["detail"] = json.loads(out.pop("detail_json") or "{}")
    except Exception:
        out["detail"] = {}
    return out
