"""Sauna connector — the person-level intelligence layer, behind a small protocol boundary.

Collie is the device.  Sauna is the person: long-term memory, the personal state model carried
across devices and time, learned workflows, cloud execution.  This module is Collie's *client* to
that layer.  In this prototype the cloud half is mocked **locally** — ``SaunaClient`` keeps the
"cloud" copy under ``~/.collie/sauna/`` — but the boundary is the real one, so swapping the mock
for HTTPS calls to Sauna does not change any caller:

    Personal AI Protocol (see docs/PERSONAL_AI_PROTOCOL.md)
      status()                    connection, plan, last sync, what is shared
      sync(reason)                Collie -> Sauna: the person's state, filtered by sync choices
      person_context(query, …)    Sauna -> Collie: richer, long-term, cross-device context
      signals(events)             Collie -> Sauna: behaviour & outcomes (accepted/rejected, done…)
      handoff(task, …)            Collie -> Sauna Cloud: a task to run while the device is off
      devices()                   the person's runtimes (this desktop, phones, cloud, others)
      export_snapshot / restore   Personal AI Portability: move the person's AI to a new device
      route(text)                 where should this run? (local · cloud · needs approval)

Honesty rules: everything this client returns is labelled with ``mode = "prototype"``; a mocked
cloud task is never reported as executed; the credential (mock token) lives in the OS credential
vault when one is available and is never handed to the model.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import secrets
import sys
import threading
import time

from .personal_state import PersonalState, _DEFAULT_SYNC, _clip, _words, state_dir

__all__ = ["SaunaClient", "SYNC_CATEGORIES", "default_client"]

MODE = "prototype"           # local mock of Sauna Cloud; the protocol boundary is real
SAUNA_APP = "https://app.sauna.ai/"
SAUNA_EMAIL = "hey@sauna.ai"
# Lines that mean "the conversation is over and the app's own furniture has started". Sauna renders
# its right-hand rail after the answer, so without this the rail reads as part of the reply.
_REPLY_END = frozenset(("Ideas", "Dashboard", "Filter", "Dreams", "Show more", "Show all",
                        "Done", "Junk", "Search", "New space", "More ways to Sauna", "Inbox"))
SYNC_CATEGORIES = [
    # key, label, group, default
    ("preferences", "Preferences", "Personal state", True),
    ("memory", "Typed memory claims", "Personal state", True),
    ("projects", "Projects", "Personal state", True),
    ("relationships", "People & relationships", "Personal state", True),
    ("goals", "Goals", "Personal state", True),
    ("tasks", "Tasks", "Personal state", True),
    ("calendar", "Calendar", "Personal state", True),
    ("notes", "Notes", "Personal state", True),
    ("journal", "Journal", "Personal state", True),
    ("workflows", "Learned workflows", "Personal state", True),
    ("agent_activity", "Agent activity", "Activity", True),
    ("conversations", "Full conversation history", "Activity", False),
    ("local_files", "Local files", "Sensitive local context", False),
    ("browser_history", "Browser history", "Sensitive local context", False),
    ("screen_history", "Screen history", "Sensitive local context", False),
]
_CLOUD_HINTS = ("tonight", "overnight", "by tomorrow", "tomorrow morning", "while i'm away", "while i am away",
                "in the background", "every day", "every morning", "weekly", "long report", "all competitors",
                "the remaining competitors", "when i'm offline", "when i am offline", "run this later", "by monday",
                "by friday", "over the weekend", "今晚", "明早", "明天早上", "后台", "每天")
_LOCAL_HINTS = ("this file", "these files", "local file", "my browser", "logged in", "logged-in", "on my screen",
                "this window", "this tab", "selected text", "clipboard", "my desktop", "this repo", "this repository",
                "这个文件", "我的浏览器", "本机")
_APPROVAL_HINTS = ("send", "email", "reply to", "delete", "pay", "purchase", "buy", "post ", "publish", "tweet",
                   "message them", "call ", "发送", "邮件", "删除", "付款", "发布")

_SINGLETON = None
_SINGLETON_PATH = None
_SINGLETON_LOCK = threading.Lock()


def default_client(state: PersonalState | None = None, memory=None) -> "SaunaClient":
    """One client per state file. Locked for the same reason as ``default_executive``: the web
    server is threaded, and an unlocked check-then-create hands different requests different
    clients."""
    global _SINGLETON, _SINGLETON_PATH
    from .personal_state import default_path
    path = default_path()
    if state is None:
        from .executive import default_executive
        state = default_executive(memory=memory).state
    with _SINGLETON_LOCK:
        if _SINGLETON is None or _SINGLETON_PATH != path or _SINGLETON.state is not state:
            _SINGLETON = SaunaClient(state, memory=memory)
            _SINGLETON_PATH = path
        elif memory is not None and _SINGLETON.memory is None:
            _SINGLETON.memory = memory
        return _SINGLETON


class SaunaClient:
    def __init__(self, state: PersonalState, *, memory=None, cloud_dir: str | None = None, device_id: str = "",
                 vault=None):
        self.state = state
        self.memory = memory
        self.cloud_dir = cloud_dir or os.environ.get("COLLIE_SAUNA_DIR") or os.path.join(state_dir(), "sauna")
        self._device_id = device_id
        self._vault = vault
        self._link_cache = None          # (at, dict) — see link()

    # ------------------------------------------------------------------ identity
    @property
    def device_id(self) -> str:
        if self._device_id:
            return self._device_id
        did = self.state.get_meta("device_id")
        if not did:
            try:
                from .brain_router import collie_device_id
                did = collie_device_id()
            except Exception:
                did = "dev_" + secrets.token_hex(6)
            self.state.set_meta("device_id", did)
        self._device_id = did
        return did

    def device_name(self) -> str:
        name = self.state.get_meta("device_name")
        if name:
            return name
        try:
            import platform
            name = platform.node() or "this computer"
        except Exception:
            name = "this computer"
        return name

    def _platform(self) -> str:
        return {"win32": "Windows", "darwin": "macOS"}.get(sys.platform, sys.platform)

    # -------------------------------------------------------------------- status
    @property
    def connected(self) -> bool:
        return self.state.get_meta("sauna_connected") == "1"

    def status(self) -> dict:
        prefs = self.sync_prefs()
        cloud = self.state.cloud_tasks(limit=50)
        last_sync = self.state.get_meta("sauna_last_sync")
        return {
            "mode": MODE,
            "connected": self.connected,
            "account": self.state.get_meta("sauna_account"),
            "plan": self.state.get_meta("sauna_plan") or ("personal" if self.connected else ""),
            "person_id": self.state.get_meta("sauna_person_id"),
            "device_id": self.device_id,
            "device_name": self.device_name(),
            "last_sync": int(last_sync) if last_sync else None,
            "sync": prefs,
            "sync_catalog": [{"key": k, "label": l, "group": g, "default": d, "enabled": prefs.get(k, d)}
                             for (k, l, g, d) in SYNC_CATEGORIES],
            "cloud": {"scheduled": len([c for c in cloud if c["status"] == "scheduled"]),
                      "running": len([c for c in cloud if c["status"] == "running"]),
                      "done": len([c for c in cloud if c["status"] == "done"])},
            "cloud_execution": self.state.get_meta("sauna_cloud_execution", "1") == "1",
            "credential": "vault" if self.state.get_meta("sauna_token_ref") else ("none" if not self.connected else "session"),
            "sync_backend": "local-prototype" if self.connected else "none",
            "personal_core": self.state.core_schema_status(),
            "adds": self.context_catalog(),
            # `connected` above is the local mock sync. `link` is what is really wired.
            "link": self.link(),
        }

    def connect(self, account: str = "", *, plan: str = "personal") -> dict:
        account = (account or self.state.get_meta("sauna_account") or "you@sauna.ai").strip()
        self.state.set_meta("sauna_account", account)
        self.state.set_meta("sauna_plan", plan)
        if not self.state.get_meta("sauna_person_id"):
            self.state.set_meta("sauna_person_id", "per_" + secrets.token_hex(6))
        # the credential never touches the model: OS vault if available, otherwise nothing durable
        token = "sauna_" + secrets.token_urlsafe(24)
        ref = self._vault_put(token, account)
        self.state.set_meta("sauna_token_ref", ref or "")
        self.state.set_meta("sauna_connected", "1")
        self._link_cache = None          # connecting changes the answer link() just cached
        self.state.upsert_device(self.device_id, self.device_name(), platform=self._platform(), kind="desktop",
                                 this_device=True, runtime={"collie": True, "surfaces": ["capsule", "web", "cli"]})
        self.state.record_activity("sync", "Connected Sauna (%s) — local by default, sync what you choose" % account,
                                   actor="sauna", detail={"account": account, "mode": MODE, "credential": "vault" if ref else "session"})
        self.sync(reason="connect")
        return self.status()

    def disconnect(self, *, forget_cloud_copy: bool = False) -> dict:
        ref = self.state.get_meta("sauna_token_ref")
        if ref:
            self._vault_delete(ref, self.state.get_meta("sauna_account"))
        self.state.set_meta("sauna_token_ref", "")
        self.state.set_meta("sauna_connected", "0")
        self._link_cache = None
        if forget_cloud_copy:
            try:
                for name in ("person.json", "person.delta.json", "memory.delta.json",
                             "sessions.delta.json", "signals.jsonl"):
                    p = os.path.join(self.cloud_dir, name)
                    if os.path.exists(p):
                        os.remove(p)
            except Exception:
                pass
        self.state.record_activity("sync", "Disconnected Sauna — Collie keeps working locally", actor="sauna")
        return self.status()

    # ---------------------------------------------------------------------- sync
    def sync_prefs(self) -> dict:
        prefs = {}
        for (k, _l, _g, d) in SYNC_CATEGORIES:
            v = self.state.get_meta("sync:" + k)
            prefs[k] = (v == "1") if v in ("0", "1") else bool(d)
        return prefs

    def set_sync_pref(self, key: str, enabled: bool) -> dict:
        if key not in {k for (k, *_r) in SYNC_CATEGORIES}:
            raise ValueError("unknown sync category: %s" % key)
        was_enabled = self.sync_prefs().get(key, False)
        self.state.set_meta("sync:" + key, "1" if enabled else "0")
        if enabled and not was_enabled:
            # The peer cursor may already have passed edits made while this category was private.
            # Queue the present versions once; historical intermediate values remain withheld.
            self.state.enqueue_sync_category(key)
            if key in ("memory", "preferences"):
                mem, owned = self._memory_handle()
                try:
                    if mem is not None:
                        kinds = ("preference", "habit", "identity") if key == "preferences" else None
                        mem.memory_sync().requeue_current(kinds=kinds)
                finally:
                    if owned:
                        mem.close()
            if key == "conversations":
                from .session_memory import SessionMemory
                archive = SessionMemory()
                try:
                    archive.session_sync().requeue_current()
                finally:
                    archive.close()
        return self.sync_prefs()

    def _memory_handle(self):
        if self.memory is not None:
            return self.memory, False
        try:
            from .cli import _paths
            from .memory import SqliteMemory
            return SqliteMemory(_paths()[0]), True
        except Exception:
            return None, False

    def sync(self, reason: str = "manual") -> dict:
        """Collie -> Sauna prototype adapter.

        ``person.json`` remains the v1 portability snapshot.  The Personal State, typed Memory
        and privacy-gated Session Memory delta batches exercise the production contracts:
        immutable revisions, tombstones, conflict preservation and peer cursors.  A real Sauna
        adapter sends the same pages over authenticated HTTPS instead of writing this mock.
        """
        if not self.connected:
            return {"synced": False, "reason": "not connected"}
        prefs = self.sync_prefs()
        snap = self.state.export_snapshot(include=prefs)
        snap["device_id"] = self.device_id
        snap["device_name"] = self.device_name()
        snap["person_id"] = self.state.get_meta("sauna_person_id")
        os.makedirs(self.cloud_dir, exist_ok=True)
        path = os.path.join(self.cloud_dir, "person.json")
        # A per-writer temp name: os.replace is atomic, but a SHARED temp name is not — two syncs
        # finishing together (auto-sync fires from every run) interleaved into one file and both
        # promoted it, leaving invalid JSON for the next restore.
        tmp = "%s.%d.%s.tmp" % (path, os.getpid(), secrets.token_hex(4))
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False, default=str)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        peer_id = "sauna-prototype"
        push_cursor = int(self.state.peer_cursor(peer_id).get("push_cursor") or 0)
        pages, next_cursor = [], push_cursor
        while True:
            page = self.state.changes_since(next_cursor, include=prefs, limit=500)
            pages.append(page)
            next_cursor = int(page["cursor"])
            if not page["has_more"] or next_cursor <= int(page["from_cursor"]):
                break
        delta_path = os.path.join(self.cloud_dir, "person.delta.json")
        delta_tmp = "%s.%d.%s.tmp" % (delta_path, os.getpid(), secrets.token_hex(4))
        try:
            with open(delta_tmp, "w", encoding="utf-8") as f:
                json.dump({"format": "collie-personal-delta-batch/2", "peer_id": peer_id,
                           "pages": pages}, f, ensure_ascii=False, default=str)
            os.replace(delta_tmp, delta_path)
        finally:
            if os.path.exists(delta_tmp):
                try:
                    os.remove(delta_tmp)
                except OSError:
                    pass
        self.state.set_peer_push_cursor(peer_id, next_cursor)
        memory_path = os.path.join(self.cloud_dir, "memory.delta.json")
        memory_pages, memory_changes, memory_cursor = [], 0, 0
        mem, owned_memory = self._memory_handle()
        try:
            if mem is not None:
                memory_sync = mem.memory_sync()
                memory_peer = "sauna-memory-prototype"
                memory_cursor = int(memory_sync.peer_cursor(memory_peer).get("push_cursor") or 0)
                if prefs.get("memory", True):
                    scopes = ["global"]
                    if prefs.get("projects", True):
                        scopes.extend(row[0] for row in mem.db.execute("""SELECT DISTINCT scope
                            FROM facts WHERE scope=project AND COALESCE(mission_id,'')=''
                            AND scope<>'global'""").fetchall())
                    next_memory_cursor = memory_cursor
                    while True:
                        page = memory_sync.changes_since(
                            next_memory_cursor, allowed_scopes=scopes,
                            include_profile=prefs.get("preferences", True), limit=500)
                        memory_pages.append(page)
                        next_memory_cursor = int(page["cursor"])
                        if not page["has_more"] or next_memory_cursor <= int(page["from_cursor"]):
                            break
                    memory_cursor = next_memory_cursor
                    memory_sync.set_push_cursor(memory_peer, memory_cursor)
                memory_changes = sum(len(page.get("changes") or []) for page in memory_pages)
                memory_tmp = "%s.%d.%s.tmp" % (
                    memory_path, os.getpid(), secrets.token_hex(4))
                try:
                    with open(memory_tmp, "w", encoding="utf-8") as f:
                        json.dump({"format": "collie-memory-delta-batch/2",
                                   "peer_id": memory_peer, "pages": memory_pages},
                                  f, ensure_ascii=False, default=str)
                    os.replace(memory_tmp, memory_path)
                finally:
                    if os.path.exists(memory_tmp):
                        try:
                            os.remove(memory_tmp)
                        except OSError:
                            pass
        finally:
            if owned_memory and mem is not None:
                mem.close()
        session_path = os.path.join(self.cloud_dir, "sessions.delta.json")
        session_pages, session_changes, session_cursor = [], 0, 0
        from .session_memory import SessionMemory
        archive = SessionMemory()
        try:
            session_sync = archive.session_sync()
            session_peer = "sauna-sessions-prototype"
            session_cursor = int(session_sync.peer_cursor(session_peer).get("push_cursor") or 0)
            if prefs.get("conversations", False):
                allowed_projects = None if prefs.get("projects", True) else ["global"]
                next_session_cursor = session_cursor
                while True:
                    page = session_sync.changes_since(
                        next_session_cursor, allowed_projects=allowed_projects, limit=200)
                    session_pages.append(page)
                    next_session_cursor = int(page["cursor"])
                    if not page["has_more"] or next_session_cursor <= int(page["from_cursor"]):
                        break
                session_cursor = next_session_cursor
                session_sync.set_push_cursor(session_peer, session_cursor)
            session_changes = sum(len(page.get("changes") or []) for page in session_pages)
            session_tmp = "%s.%d.%s.tmp" % (
                session_path, os.getpid(), secrets.token_hex(4))
            try:
                with open(session_tmp, "w", encoding="utf-8") as f:
                    json.dump({"format": "collie-session-memory-delta-batch/1",
                               "peer_id": session_peer,
                               "sharing_enabled": bool(prefs.get("conversations", False)),
                               "purge_remote_when_disabled": not bool(
                                   prefs.get("conversations", False)),
                               "pages": session_pages}, f, ensure_ascii=False, default=str)
                os.replace(session_tmp, session_path)
            finally:
                if os.path.exists(session_tmp):
                    try:
                        os.remove(session_tmp)
                    except OSError:
                        pass
        finally:
            archive.close()
        now = int(time.time())
        self.state.set_meta("sauna_last_sync", str(now))
        shared = [k for k, v in prefs.items() if v]
        withheld = [k for k, v in prefs.items() if not v]
        counts = {k: len(v) for k, v in snap.items() if isinstance(v, list)}
        if reason != "auto":
            self.state.record_activity("sync", "Synced personal state to Sauna (%s)" % ", ".join(shared[:6]),
                                       actor="sauna", detail={"reason": reason, "shared": shared, "withheld": withheld,
                                                              "counts": counts, "wire_format": "v2"})
        delta_changes = sum(len(page["changes"]) for page in pages)
        return {"synced": True, "at": now, "shared": shared, "withheld": withheld,
                "counts": counts, "path": path, "delta_path": delta_path,
                "delta_changes": delta_changes, "cursor": next_cursor,
                "memory_delta_path": memory_path if mem is not None else "",
                "memory_delta_changes": memory_changes, "memory_cursor": memory_cursor,
                "session_delta_path": session_path,
                "session_delta_changes": session_changes,
                "session_cursor": session_cursor,
                "wire_format": "collie-personal-delta/2"}

    # ------------------------------------------------------------------- context
    def context_catalog(self) -> list[dict]:
        """What Sauna adds on top of local context (for the Context panel): available only when connected."""
        s = self.state
        conn = self.connected
        prefs = self.sync_prefs()
        goals = s.goals()
        ups = s.upcoming(limit=3)
        decisions = s.recent_activity(limit=5, kinds=("decision",))
        wfs = [w for w in s.workflows() if w["status"] in ("suggested", "confirmed", "automated")]
        people = s.people()
        prof = self._profile_rows()
        items = [
            ("Related project history", prefs.get("agent_activity", True) and bool(s.recent_activity(limit=1))),
            ("Active goal", prefs.get("goals", True) and bool(goals)),
            ("Upcoming deadline", prefs.get("calendar", True) and bool(ups)),
            ("Previous decisions", prefs.get("agent_activity", True) and bool(decisions)),
            ("User preferences", prefs.get("preferences", True) and bool(prof)),
            ("People & relationships", prefs.get("relationships", True) and bool(people)),
            ("Learned workflows", prefs.get("workflows", True) and bool(wfs)),
        ]
        return [{"label": l, "available": conn and a, "local_only": not conn} for (l, a) in items]

    def person_context(self, query: str = "", *, project_id: str = "", cwd: str = "", budget: int = 1400) -> str:
        """Sauna -> Collie: the long-term, cross-device context that makes a short request unambiguous.
        Empty when not connected (Collie still works, from the device alone)."""
        if not self.connected:
            return ""
        s = self.state
        prefs = self.sync_prefs()
        q = _words(query)
        lines = []
        # the person
        if prefs.get("preferences", True):
            for k, label in (("owner_name", "name"), ("owner_role", "role"), ("owner_location", "location")):
                v = s.get_meta(k)
                if v:
                    lines.append("- %s: %s" % (label, v))
        # active goals with trajectory
        if prefs.get("goals", True):
            for g in s.goals()[:3]:
                tasks = s.tasks(goal_id=g["id"]) if prefs.get("tasks", True) else []
                done = [t["title"] for t in tasks if t["status"] == "done"]
                left = [t["title"] for t in tasks if t["status"] not in ("done", "dropped")]
                trajectory = (": done — %s; remaining — %s" %
                              ("; ".join(done[-4:]) or "nothing yet", "; ".join(left[:3]) or "nothing")) \
                    if prefs.get("tasks", True) else ""
                progress = " (%d%%)" % round(g["progress"] * 100) if prefs.get("tasks", True) else ""
                lines.append("- goal \"%s\"%s%s" % (g["title"], progress, trajectory))
        # upcoming with meaning
        if prefs.get("calendar", True):
            for e in s.upcoming(limit=3):
                when = _dt.datetime.fromtimestamp(e["start_at"]).strftime("%a %b %d %I:%M %p").replace(" 0", " ")
                who = [p["name"] + (" (%s)" % p["role"] if p["role"] else "") for p in s.people()
                       if prefs.get("relationships", True) and e.get("project_id") and
                       p["project_id"] == e["project_id"]][:3]
                lines.append("- upcoming: %s — %s%s%s" % (e["title"], when,
                                                          (" · with " + ", ".join(who)) if who else "",
                                                          (" · goal \"%s\"" % e["goal"]["title"])
                                                          if e.get("goal") and prefs.get("goals", True) else ""))
        # what was decided before
        if prefs.get("agent_activity", True):
            for a in s.recent_activity(limit=4, kinds=("decision",)):
                lines.append("- decided: %s" % _clip(a["summary"], 140))
        # confirmed preferences / habits (trusted memory only)
        if prefs.get("preferences", True):
            for r in self._profile_rows()[:5]:
                lines.append("- preference: %s" % _clip(r, 120))
        # how the person works
        if prefs.get("workflows", True):
            for w in [w for w in s.workflows() if w["status"] in ("suggested", "confirmed", "automated")][:2]:
                lines.append("- workflow \"%s\" (%s): %s" % (w["name"], w["status"],
                                                             " → ".join(st.get("title", "") for st in w["steps"][:6])))
        # notes the query points at
        if q and prefs.get("notes", True):
            for n in s.notes(limit=200):
                if _words(n["title"]) & q or (len(q) >= 2 and len(_words(n["body"][:400]) & q) >= 2):
                    lines.append("- note \"%s\": %s" % (n["title"], _clip(n["body"], 160)))
                    break
        # recent cross-device history
        if prefs.get("agent_activity", True):
            for a in s.recent_activity(limit=3, kinds=("run", "task_done", "note")):
                lines.append("- yesterday/recent: %s" % _clip(a["summary"], 100))
        # rank: lines that share words with the query float up; always keep the first few
        if q:
            head, tail = lines[:2], lines[2:]
            tail.sort(key=lambda l: -len(_words(l) & q))
            lines = head + tail
        text = "\n".join(lines)
        if len(text) > budget:
            text = text[: budget - 1] + "…"
        return text

    def _profile_rows(self) -> list[str]:
        if self.memory is None:
            return []
        try:
            rows = self.memory.trusted_profile("global") or []
        except Exception:
            return []
        out = []
        for r in rows[:8]:
            try:
                out.append(str(r.get("text") or r.get("value") or ""))
            except Exception:
                continue
        return [x for x in out if x]

    # ------------------------------------------------------------------- signals
    def signals(self, events: list[dict]) -> dict:
        """Collie -> Sauna learning loop: behaviour and outcomes.  Appended to the (mock) cloud log."""
        if not self.connected:
            return {"sent": 0, "reason": "not connected"}
        os.makedirs(self.cloud_dir, exist_ok=True)
        path = os.path.join(self.cloud_dir, "signals.jsonl")
        n = 0
        with open(path, "a", encoding="utf-8") as f:
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                row = dict(ev)
                row.setdefault("at", int(time.time()))
                row.setdefault("device_id", self.device_id)
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                n += 1
        return {"sent": n, "path": path}

    # -------------------------------------------------------------------- routing
    def route(self, text: str, *, has_local_context: bool = False) -> dict:
        """Where should this run?  Rules only (section 24): private/local → this device; long-running or
        parallel → Sauna Cloud (when connected); anything with external effect → approval."""
        t = (text or "").lower()
        reasons, runtime = [], "local"
        if any(h in t for h in _LOCAL_HINTS) or has_local_context:
            reasons.append("needs this computer (local files, logged-in browser, or on-screen context)")
        cloudish = any(h in t for h in _CLOUD_HINTS)
        if cloudish:
            reasons.append("long-running or scheduled work that should not depend on this device being on")
            if self.connected and self.state.get_meta("sauna_cloud_execution", "1") == "1" and not reasons[:-1]:
                runtime = "cloud"
        needs_approval = any(h in t for h in _APPROVAL_HINTS)
        if needs_approval:
            reasons.append("has external effect — approval required before it runs")
        when = _schedule_from_text(t)
        return {"runtime": runtime, "offer_cloud": bool(cloudish and self.connected), "needs_approval": needs_approval,
                "reasons": reasons, "connected": self.connected, "scheduled_for": when.get("scheduled_for"),
                "deliver_at": when.get("deliver_at")}

    def handoff(self, text: str, *, scheduled_for: int | None = None, deliver_at: int | None = None,
                runtime: str = "sauna-cloud", source: str = "user") -> dict:
        """Schedule a task on Sauna Cloud (mocked: recorded, visible, never claimed as executed)."""
        if not self.connected:
            raise RuntimeError("Sauna is not connected; run it here instead")
        when = _schedule_from_text(text.lower())
        scheduled_for = scheduled_for or when.get("scheduled_for")
        deliver_at = deliver_at or when.get("deliver_at")
        ct = self.state.add_cloud_task(_clip(text, 160), runtime=runtime, scheduled_for=scheduled_for, deliver_at=deliver_at,
                                       detail={"prompt": text, "source": source, "mode": MODE, "device_id": self.device_id})
        self.state.record_activity("handoff", "Handed off to Sauna Cloud: %s" % _clip(text, 100), actor="sauna",
                                   detail={"cloud_task_id": ct["id"], "scheduled_for": scheduled_for, "deliver_at": deliver_at,
                                           "mode": MODE})
        self.signals([{"kind": "cloud_handoff", "text": _clip(text, 200), "scheduled_for": scheduled_for}])
        return ct

    def cloud_mark(self, cloud_task_id: str, status: str, *, result: str = "") -> dict | None:
        ct = self.state.update_cloud_task(cloud_task_id, status=status, result=result or None)
        if ct:
            self.state.record_activity("cloud_task", "Cloud task %s: %s" % (status, ct["title"]), actor="sauna",
                                       detail={"cloud_task_id": cloud_task_id})
        return ct


    # ------------------------------------------------------------------- push
    # The other direction. Sauna exposes no inbound API — no REST, no webhook, no MCP server of its
    # own — so "Collie calls Sauna" cannot be an HTTP call. What Sauna *does* accept is a message
    # from the person, on one of its own surfaces. Collie already holds the one thing that makes
    # that possible without new credentials: the browser the person is signed in to.
    #
    # This is not a scrape. It is Collie typing into the person's own session, on their own machine,
    # in their own name — the same hand it uses for every other site, subject to the same gate.

    PUSH_TRANSPORTS = ("browser", "email")

    def push(self, text: str, *, transport: str = "browser", wait: float = 25.0,
             space: str = "collie-sauna") -> dict:
        """Send something INTO the person's Sauna and return what came back.

        ``browser`` drives the signed-in session and reads the reply. ``email`` reports the
        documented address instead of pretending to send: Sauna only recognises mail from the exact
        address on the account, and Collie holds no credential for the owner's personal mailbox —
        claiming otherwise would be the kind of unearned success this project exists to refuse.
        """
        text = str(text or "").strip()
        if not text:
            raise ValueError("nothing to push")
        if transport not in self.PUSH_TRANSPORTS:
            raise ValueError("transport must be one of %s" % (self.PUSH_TRANSPORTS,))
        if transport == "email":
            account = self.state.get_meta("sauna_account") or "the address on your Sauna account"
            self.state.record_activity(
                "push", "Prepared a Sauna message for delivery by email: %s" % _clip(text, 90),
                actor="sauna", detail={"transport": "email", "to": SAUNA_EMAIL, "from": account})
            return {"ok": False, "transport": "email", "to": SAUNA_EMAIL, "from_required": account,
                    "text": text, "sent": False,
                    "why": ("Sauna accepts email only from the exact address on the account. Collie "
                            "has no credential for that mailbox, so it cannot send as you — forward "
                            "this text to %s from %s and it lands in the same place." % (SAUNA_EMAIL, account))}
        started = time.time()
        try:
            from . import browserbridge
        except Exception as exc:
            raise RuntimeError("the browser bridge is unavailable: %s" % exc)

        def call(cmd, timeout=60):
            out = browserbridge._call(dict(cmd, space=space), timeout=timeout)
            if not out.get("ok"):
                raise RuntimeError(str(out.get("error") or out)[:200])
            return out.get("data")

        call({"action": "open", "url": SAUNA_APP, "active": False}, timeout=60)
        composer = self._sauna_composer(call)
        if not composer:
            raise RuntimeError("could not find Sauna's composer on %s" % SAUNA_APP)
        call({"action": "type", "ref": composer, "text": text, "submit": True}, timeout=60)
        reply, waited, stable = "", 0.0, ""
        while waited < wait:
            time.sleep(3.0)
            waited = time.time() - started
            page = call({"action": "read"}, timeout=45)
            body = page if isinstance(page, str) else str((page or {}).get("text") or "")
            current = self._sauna_reply(body, text)
            # Sauna streams. Accept an answer only once it stops growing, or the first poll
            # catches a half-written sentence and reports it as the reply.
            if current and current == stable:
                reply = current
                break
            stable = current
        self.state.record_activity(
            "push", "Pushed to Sauna: %s" % _clip(text, 90), actor="sauna",
            detail={"transport": "browser", "replied": bool(reply), "seconds": round(waited, 1)})
        self.signals([{"kind": "push", "transport": "browser", "replied": bool(reply)}])
        self._remember_browser(True)
        return {"ok": True, "transport": "browser", "text": text, "reply": reply,
                "seconds": round(waited, 1),
                "note": "" if reply else "Sauna accepted it but had not answered yet; look in the session list."}

    @staticmethod
    def _sauna_composer(call) -> str:
        """The ref of Sauna's message box. Its ref moves between views, so find it each time."""
        snap = call({"action": "snapshot"}, timeout=45)
        text = snap if isinstance(snap, str) else str((snap or {}).get("snapshot") or "")
        boxes = [l for l in text.splitlines() if "textbox" in l.lower() and "[e" in l]
        if not boxes:
            return ""
        return boxes[-1].split("]")[0].strip("[ ")

    @staticmethod
    def _sauna_reply(page_text: str, sent: str) -> str:
        """Whatever Sauna wrote after our message, with the app chrome removed.

        The trick that makes this robust without knowing Sauna's DOM: the sidebar, nav and
        session list all appear BEFORE our message as well as after it. Any line that occurs on
        both sides is furniture; what is left is the answer.
        """
        # Split on the WHOLE message when the page shows it, so the echoed prompt's own tail is not
        # mistaken for the answer — it is present from the first poll and never changes, which the
        # stability check then reads as "Sauna has finished replying".
        marker = sent if sent in page_text else sent[:40]
        if not page_text or marker not in page_text:
            return ""
        head, _, tail = page_text.rpartition(marker)
        before = {l.strip() for l in head.splitlines() if l.strip()}
        keep = []
        for line in tail.splitlines():
            line = line.strip()
            if not line or line in before or line.isdigit():
                continue
            if line.endswith(("AM", "PM")) and len(line) <= 9:
                continue
            if line.startswith("Ran ") and line.endswith(("command", "commands")):
                continue
            if line in _REPLY_END:
                break          # the right-hand rail starts here; the answer has ended
            keep.append(line)
            if len(keep) >= 20:
                break
        return "\n".join(keep).strip()



    # --------------------------------------------------------------------- link
    LINK_TTL = 20.0

    def link(self, *, probe_browser: bool = False) -> dict:
        """What is actually wired right now, checked rather than remembered.

        The MCP half is a localhost probe, so it is cheap and always live. The browser half is
        NOT: proving it means driving the person's signed-in session, which is a visible side
        effect and takes seconds. Status reads therefore report the LAST OBSERVED browser state
        (stamped, and flagged ``browser_cached``), and only an explicit ``probe_browser=True``
        goes and looks. Every real browser call — inbox, push — refreshes that observation for
        free, so the cheap path stays honest without anyone paying for a probe.
        """
        now = time.time()
        if not probe_browser:
            cached = self._link_cache
            if cached and (now - cached[0]) < self.LINK_TTL:
                return dict(cached[1])
        out = {"mock_sync": self.connected, "browser": False, "mcp": False,
               "mcp_tools": 0, "mcp_writes": False, "mcp_last_cloud_call": None}
        # the device's side: is `collie mcp-serve` up, and has the cloud used it?
        try:
            from . import mcpserve
            token = mcpserve.stored_token(create=False)
            if token:
                import urllib.request
                for port in (8789, 8791):
                    try:
                        req = urllib.request.Request(
                            "http://127.0.0.1:%d/%s/mcp" % (port, token),
                            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
                            headers={"Content-Type": "application/json", "Accept": "application/json"},
                            method="POST")
                        # Loopback: it answers at once or it is not there. A 3s budget per port
                        # made every Today read wait four seconds for a server that was simply off,
                        # and Today is polled by the wallpaper.
                        body = json.loads(urllib.request.urlopen(req, timeout=0.6).read().decode())
                        tools = (body.get("result") or {}).get("tools") or []
                        out["mcp"] = True
                        out["mcp_tools"] = len(tools)
                        out["mcp_writes"] = any(t["name"].startswith(("collie_task_", "collie_note_", "collie_request"))
                                                for t in tools)
                        break
                    except Exception:
                        continue
            path = os.path.join(state_dir(), "mcpserve-audit.log")
            if os.path.exists(path):
                with open(path, encoding="utf-8", errors="replace") as fh:
                    rows = fh.readlines()[-400:]
                for raw in reversed(rows):
                    try:
                        row = json.loads(raw)
                    except Exception:
                        continue
                    if row.get("event") == "tool_call":
                        out["mcp_last_cloud_call"] = "%s · %s" % (row.get("at", ""), row.get("tool", ""))
                        break
        except Exception:
            pass
        # the cloud's side: can we see the person's signed-in Sauna?
        if probe_browser:
            try:
                from . import browserbridge
                res = browserbridge._call({"action": "open", "url": SAUNA_APP, "active": False,
                                           "space": "collie-sauna"}, timeout=45)
                page = res.get("data")
                body = page if isinstance(page, str) else str((page or {}).get("text") or "")
                out["browser"] = bool(res.get("ok")) and ("Connections" in body or "New agent" in body)
                out["signed_out"] = bool(res.get("ok")) and not out["browser"]
                self._remember_browser(out["browser"], signed_out=out.get("signed_out"))
            except Exception as exc:
                out["browser_error"] = str(exc)[:140]
            out["browser_cached"] = False
        else:
            seen = self._browser_seen()
            out["browser"] = bool(seen.get("ok"))
            out["browser_at"] = seen.get("at")
            out["browser_cached"] = True
            if seen.get("signed_out"):
                out["signed_out"] = True
        self._link_cache = (now, dict(out))
        return out

    def _browser_seen(self) -> dict:
        raw = self.state.get_meta("sauna_link_browser")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _remember_browser(self, ok: bool, *, signed_out=None) -> None:
        """Record that a real browser call did (or did not) reach Sauna, so cheap reads can say so."""
        row = {"ok": bool(ok), "at": _now_int()}
        if signed_out is not None:
            row["signed_out"] = bool(signed_out)
        try:
            self.state.set_meta("sauna_link_browser", json.dumps(row))
            self._link_cache = None          # the next read should pick this up, not the stale tuple
        except Exception:
            pass

    # -------------------------------------------------------------------- inbox
    # Sauna's own queue, pulled into the desktop. There is no API to ask, so Collie reads the app
    # the way the person would — through the browser they are already signed in to. That is the
    # honest description and also the limitation: this is a projection of a page, so it is labelled
    # ``source: "browser"`` and it can go stale or shift when Sauna's UI changes.

    def inbox(self, *, refresh: bool = True, space: str = "collie-sauna") -> dict:
        """What is waiting in Sauna: the counters and the live session queue."""
        if not refresh:
            cached = self.state.get_meta("sauna_inbox_cache")
            if cached:
                try:
                    return json.loads(cached)
                except Exception:
                    pass
        try:
            from . import browserbridge
        except Exception as exc:
            return {"ok": False, "error": "browser bridge unavailable: %s" % exc, "items": [], "counts": {}}
        try:
            out = browserbridge._call({"action": "open", "url": SAUNA_APP + "dashboard",
                                       "active": False, "space": space}, timeout=60)
            if not out.get("ok"):
                raise RuntimeError(str(out.get("error"))[:160])
            page = out.get("data")
            body = page if isinstance(page, str) else str((page or {}).get("text") or "")
            if not body:
                read = browserbridge._call({"action": "read", "space": space}, timeout=45)
                d = read.get("data")
                body = d if isinstance(d, str) else str((d or {}).get("text") or "")
        except Exception as exc:
            self._remember_browser(False)
            return {"ok": False, "error": str(exc)[:200], "items": [], "counts": {},
                    "hint": "sign in to Sauna in the browser Collie drives, then refresh"}
        parsed = _parse_sauna_dashboard(body)
        parsed.update({"ok": True, "at": _now_int(), "source": "browser", "url": SAUNA_APP + "dashboard"})
        self._remember_browser(True)
        try:
            self.state.set_meta("sauna_inbox_cache", json.dumps(parsed, ensure_ascii=False))
        except Exception:
            pass
        return parsed

    def open_session(self, title: str, *, space: str = "collie-sauna") -> dict:
        """Bring one Sauna session to the front in the person's browser."""
        from . import browserbridge
        browserbridge._call({"action": "open", "url": SAUNA_APP + "dashboard", "active": True,
                             "space": space}, timeout=60)
        out = browserbridge._call({"action": "click", "text": title, "space": space}, timeout=45)
        return {"ok": bool(out.get("ok")), "title": title, "error": out.get("error")}

    # ------------------------------------------------------------------- devices
    def devices(self) -> list[dict]:
        out = []
        this = {"device_id": self.device_id, "name": self.device_name(), "platform": self._platform(), "kind": "desktop",
                "this_device": True, "status": "online", "runtime": {"collie": True, "surfaces": ["capsule", "web", "cli"]},
                "last_seen": int(time.time())}
        out.append(this)
        try:
            from . import remote_identity
            ident = remote_identity.load_or_create()
            for d in ident.devices():
                out.append({"device_id": "phone:" + str(d.get("device_id") or d.get("name") or "")[:16],
                            "name": d.get("name") or "Phone", "platform": "iOS", "kind": "phone", "this_device": False,
                            "status": "paired", "runtime": {"supervises": self.device_name()}, "last_seen": d.get("last_seen")})
        except Exception:
            pass
        if self.connected:
            out.append({"device_id": "sauna-cloud", "name": "Sauna Cloud", "platform": "cloud", "kind": "cloud",
                        "this_device": False, "status": "mock" if MODE == "prototype" else "online",
                        "runtime": {"long_running": True, "parallel": True, "offline_ok": True}, "last_seen": int(time.time())})
        seen = {d["device_id"] for d in out}
        for d in self.state.devices():
            if d["device_id"] in seen:
                continue
            d = dict(d)
            d["status"] = "synced" if self.connected else "known"
            d["this_device"] = False
            out.append(d)
        return out

    # --------------------------------------------------------------- portability
    def export_snapshot(self, path: str | None = None) -> str:
        snap = self.state.export_snapshot(include=self.sync_prefs())
        snap["device_id"] = self.device_id
        snap["device_name"] = self.device_name()
        snap["person_id"] = self.state.get_meta("sauna_person_id")
        path = path or os.path.join(self.cloud_dir, "export-%s.json" % _dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=1, default=str)
        self.state.record_activity("sync", "Exported personal AI snapshot (%s)" % os.path.basename(path), actor="sauna",
                                   detail={"path": path})
        return path

    def restore(self, source: str | dict | None = None) -> dict:
        """'Welcome back': bring a person's AI onto this device from Sauna (mock cloud copy) or a file."""
        if source is None:
            source = os.path.join(self.cloud_dir, "person.json")
        if isinstance(source, str):
            with open(source, "r", encoding="utf-8") as f:
                snap = json.load(f)
        else:
            snap = source
        counts = self.state.import_snapshot(snap, merge=True)
        from_device = snap.get("device_name") or snap.get("device_id") or "another device"
        if snap.get("device_id") and snap.get("device_id") != self.device_id:
            self.state.upsert_device(snap["device_id"], snap.get("device_name") or "Other device",
                                     platform=str(snap.get("platform") or ""), kind="desktop", this_device=False)
        if snap.get("person_id"):
            self.state.set_meta("sauna_person_id", snap["person_id"])
        self.state.record_activity("restore", "Welcome back — restored goals, tasks, notes, journal and workflows from %s" % from_device,
                                   actor="sauna", detail={"counts": counts, "from": from_device})
        return {"counts": counts, "from": from_device, "welcome": _welcome(self.state)}

    # --------------------------------------------------------------------- vault
    def _vault_put(self, token: str, account: str) -> str:
        try:
            from .identityvault import IdentityVault
            vault = self._vault or IdentityVault(service_name="collie.sauna")
            return vault.put(token, collie_id=self.device_id, account=account or "sauna", kind="token")
        except Exception:
            return ""

    def _vault_delete(self, ref: str, account: str) -> None:
        try:
            from .identityvault import IdentityVault
            vault = self._vault or IdentityVault(service_name="collie.sauna")
            vault.delete(ref, collie_id=self.device_id, account=account or "sauna", kind="token")
        except Exception:
            pass


# ---------------------------------------------------------------------------- helpers
# The dashboard is a flat text render: counters, then one (title, subtitle, when) triple per row.
_SAUNA_CHROME = frozenset((
    "Home", "Dashboard", "Knowledge", "Skills", "Connections", "Apps", "Scheduled", "New agent",
    "New space", "More ways to Sauna", "Filter", "Inbox", "Search", "Done", "Junk", "Show all",
    "Show more", "Ideas", "Dreams", "Personal", "Ember", "Working", "Review", "Suggested",
    "Search across sessions and knowledge", "Collapse sidebar", "Profile",
))
_WHEN = re.compile(r"^(?:\d{1,2}:\d{2}\s*(?:AM|PM)|\d+\s+(?:sec|min|hour|day|week)s?\s+ago|Just now|Yesterday|[A-Z][a-z]{2}\s+\d{1,2})$")
_COUNT = re.compile(r"^\((\d+)\)$")


def _now_int() -> int:
    return int(time.time())


def _parse_sauna_dashboard(body: str) -> dict:
    """Counters and session rows out of Sauna's dashboard text."""
    lines = [l.strip() for l in (body or "").splitlines() if l.strip()]
    counts, items = {}, []
    for i, line in enumerate(lines):
        if line in ("Working", "Review", "Suggested") and i + 1 < len(lines):
            hit = _COUNT.match(lines[i + 1])
            if hit:
                counts[line.lower()] = int(hit.group(1))
    # rows: a title, a subtitle, then something that looks like a time
    seen = set()
    for i in range(len(lines) - 2):
        when = lines[i + 2]
        if not _WHEN.match(when):
            continue
        title, subtitle = lines[i], lines[i + 1]
        if title in _SAUNA_CHROME or subtitle in _SAUNA_CHROME:
            continue
        if _COUNT.match(title) or _COUNT.match(subtitle) or _WHEN.match(title):
            continue
        if len(title) > 120 or title in seen:
            continue
        seen.add(title)
        items.append({"title": title, "subtitle": subtitle, "when": when})
    return {"counts": counts, "items": items[:20]}


def _schedule_from_text(t: str) -> dict:
    now = _dt.datetime.now()
    out = {"scheduled_for": None, "deliver_at": None}
    if "tonight" in t or "今晚" in t or "overnight" in t:
        start = now.replace(hour=21, minute=0, second=0, microsecond=0)
        if start < now:
            start = now + _dt.timedelta(minutes=5)
        out["scheduled_for"] = int(start.timestamp())
    if "tomorrow morning" in t or "明早" in t or "明天早上" in t or "by tomorrow" in t:
        deliver = (now + _dt.timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
        out["deliver_at"] = int(deliver.timestamp())
        out["scheduled_for"] = out["scheduled_for"] or int((now + _dt.timedelta(minutes=5)).timestamp())
    m = re.search(r"by (monday|tuesday|wednesday|thursday|friday|saturday|sunday)", t)
    if m:
        names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        target = names.index(m.group(1))
        days = (target - now.weekday()) % 7 or 7
        deliver = (now + _dt.timedelta(days=days)).replace(hour=8, minute=0, second=0, microsecond=0)
        out["deliver_at"] = int(deliver.timestamp())
        out["scheduled_for"] = out["scheduled_for"] or int((now + _dt.timedelta(minutes=5)).timestamp())
    return out


def _welcome(state: PersonalState) -> dict:
    return {
        "goals": [g["title"] for g in state.goals()[:5]],
        "tasks_open": len(state.tasks(include_done=False)),
        "notes": len(state.notes(limit=1000)),
        "events": len(state.upcoming(limit=20)),
        "workflows": [w["name"] for w in state.workflows() if w["status"] in ("suggested", "confirmed", "automated")][:5],
        "journal_days": len(state.journal(limit=60)),
    }
