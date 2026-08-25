"""Web routes for the personal layer: Today / Tasks / Notes / Calendar / Journal / Memory / Devices,
device context, and the Sauna connector.

Kept out of ``webapp.py`` on purpose (that file is already the largest in the repo); the web app
delegates with two calls — ``handle_get`` / ``handle_post`` — which return True when they served the
request.  Every route is token-gated through the handler's own ``_authed`` (same CSRF contract as the
rest of the API) and reads only the loopback-served Collie.

Routes
------
GET  /api/state/today                 executive brief (+ sauna status + device context summary)
GET  /api/state/tasks|notes|events|journal|activity|workflows|suggestions|projects|people
GET  /api/state/core|conflicts       schema/sync health and conflict review tray
GET  /api/state/views                 the Markdown projections (paths + text)
GET  /api/state/memory-cards          Sauna-compatible views over trusted typed Memory
GET  /api/state/memory-core           claim sync/tombstone/conflict health
GET  /api/state/session-memory        recent threads or indexed session fragments
GET  /api/state/memory-receipt        one retrieval/preference decision receipt
GET  /api/context/local               device context snapshot (settings-gated channels)
GET  /api/sauna/status|devices|context
POST /api/state/task                  {action: add|done|status|focus|unfocus|drop, ...}
POST /api/state/note                  {text, title?, project?, goal?, append_to?, decision?}
POST /api/state/event                 {title, start_at, end_at?, kind?, goal?, project?}
POST /api/state/suggestion            {id, action: accept|dismiss}
POST /api/state/journal/build         {day?}
POST /api/state/conflict              {id, resolution: local|remote}
POST /api/state/memory-query           {query, project?, session?, as_of?, known_at?}
POST /api/state/memory-conflict        {id, resolution: local|remote}
POST /api/state/session-conflict       {id, resolution: local|remote}
POST /api/state/preference-resolve     {attribute, context?, project?, default?, current_request?}
POST /api/state/demo                  {action: seed|reset}      (prototype demo data, explicit)
POST /api/sauna/connect|disconnect|sync|sync-pref|handoff|export|restore|cloud-task|route
"""
from __future__ import annotations

import json
import os
import time

__all__ = ["handle_get", "handle_post", "personal_state_for_run", "release_run_memory",
           "device_context_for_run"]


def _ex(memory=None):
    from .executive import default_executive
    return default_executive(memory=memory)


def _sauna(memory=None):
    from .sauna import default_client
    ex = _ex(memory=memory)
    return default_client(ex.state, memory=memory)


def _flag(key: str, default: str = "on") -> bool:
    try:
        from . import settings
        return str(settings.get(key, default) or default).strip().lower() in ("on", "1", "true", "yes")
    except Exception:
        return default == "on"


def device_context(*, cwd: str = "", wait: float = 0.35, state=None) -> dict:
    """Settings-gated device context snapshot (what the capsule shows and what the model may see)."""
    from . import localcontext
    return localcontext.snapshot(
        active_window=_flag("CONTEXT_ACTIVE_WINDOW"), selection_text=_flag("CONTEXT_SELECTION"),
        clipboard_text=_flag("CONTEXT_CLIPBOARD", "off"), browser_tab=_flag("CONTEXT_BROWSER_TAB"),
        cwd=cwd, state=state, wait=wait)


def device_context_for_run(qs: dict, *, cwd: str = "") -> dict | None:
    """The context the capsule/composer captured at open time travels with the run as ?ctx=<json>
    (so the model sees what the *person* saw, not what is in front after Collie's window opened)."""
    raw = (qs.get("ctx", [""])[0] or "").strip()
    if raw:
        try:
            d = json.loads(raw)
            if isinstance(d, dict):
                # The page captured this at summon time, but the privacy switches are the server's
                # to enforce: a stale tab, an older build, or a hand-made URL must not smuggle in a
                # channel the person turned off.
                if not _flag("CONTEXT_CLIPBOARD", "off"):
                    d.pop("clipboard", None)
                if not _flag("CONTEXT_SELECTION"):
                    d.pop("selection", None)
                if not _flag("CONTEXT_BROWSER_TAB"):
                    d.pop("browser", None)
                if not _flag("CONTEXT_ACTIVE_WINDOW"):
                    d.pop("foreground", None)
                return d
        except Exception:
            pass
    if not _flag("CONTEXT_IN_PROMPT"):
        return None
    try:
        return device_context(cwd=cwd, wait=0.0, state=_ex().state)
    except Exception:
        return None


def personal_state_for_run(h, *, qs: dict, cwd: str, prompt: str, memory=None) -> dict:
    """Wire one harness for the executive loop: situation block (device + local state + Sauna) and
    the activity sink.  Returns what was attached, for the `start` frame / receipts."""
    ex = _ex(memory=memory)
    sauna = _sauna(memory=memory)
    device = device_context_for_run(qs, cwd=cwd) if _flag("CONTEXT_IN_PROMPT") else None
    person = ""
    if sauna.connected:
        try:
            person = sauna.person_context(prompt, cwd=cwd)
        except Exception:
            person = ""
    bound = (qs.get("task", [""])[0] or "").strip()
    if bound:
        h.bound_task_id = bound
    h.entrypoint = (qs.get("entrypoint", [""])[0] or "").strip()
    if _flag("CONTEXT_IN_PROMPT"):
        try:
            block = ex.situation_block(device=device, sauna={"connected": sauna.connected, "context": person},
                                       prompt=prompt, cwd=cwd)
            if block:
                h.composer.situation = block
        except Exception:
            pass

    def _sink(summary: dict, _ex=ex, _sauna=sauna):
        out = _ex.on_run_complete(summary)
        try:
            if _sauna.connected and _flag("SAUNA_AUTO_SYNC"):
                _sauna.sync(reason="auto")
                _sauna.signals([{"kind": "run_complete", "task_done": bool(out.get("task")),
                                 "suggestion": (out.get("suggestion") or {}).get("title", ""),
                                 "verified": bool(summary.get("verified")), "error": bool(summary.get("error"))}])
        except Exception:
            pass
        return out

    h.activity_sink = _sink
    return {"device": _public_device(device) if device else None, "sauna_connected": sauna.connected,
            "person_context": bool(person), "bound_task": bound}


def release_run_memory(memory) -> None:
    """Detach a run-owned memory connection before the web harness closes it.

    ``make_harness`` creates one SqliteMemory per run.  The Executive and Sauna client borrow that
    connection while the run is alive, but they are process-wide singletons.  Leaving the borrowed
    object attached made the next personal/Sauna request reuse a database the web runner had already
    closed (most visibly: run a task on the ambient desktop, then connect Sauna -> HTTP 500).
    """
    if memory is None:
        return
    try:
        ex = _ex()
        if ex.memory is memory:
            ex.memory = None
    except Exception:
        pass
    try:
        sauna = _sauna()
        if sauna.memory is memory:
            sauna.memory = None
    except Exception:
        pass


def _public_device(d: dict) -> dict:
    """What a run frame may carry about device context (bounded; no raw clipboard)."""
    from .localcontext import chips
    fg = d.get("foreground") or {}
    return {"app": fg.get("app"), "title": (fg.get("title") or "")[:120],
            "selection_chars": len((d.get("selection") or {}).get("text") or ""),
            "project": (d.get("project") or {}).get("name"), "chips": chips(d)}


def executive_payload(out: dict | None) -> dict | None:
    """The ``done.personal_state`` payload for a real proactive UI moment.

    Every run is recorded in Activity, including ordinary questions.  That ledger entry is not a
    second user-facing result: returning it by itself made the workbench append ``Done`` followed
    by the user's prompt after every normal chat.  Surface a card only when the executive actually
    changed an attached task or has a concrete suggestion the person can act on.
    """
    if not out:
        return None
    task = out.get("task")
    goal = out.get("goal")
    sug = out.get("suggestion")
    if not task and not sug:
        return None
    return {
        "task": {k: task.get(k) for k in ("id", "title", "status", "goal_id", "project_id")} if task else None,
        "task_binding": out.get("task_binding"),
        "goal": {"id": goal["id"], "title": goal["title"], "progress": goal.get("progress")} if goal else None,
        "suggestion": sug,
        # The base run activity is normally the user's prompt and is already visible immediately
        # above the card.  Keep only material follow-on evidence here so the card never echoes it.
        "activities": [a.get("summary") for a in list(out.get("extra_activities") or []) if a],
        "journal_day": out.get("journal_day"),
        "project": (out.get("project") or {}).get("name") if out.get("project") else None,
    }


# ------------------------------------------------------------------------------ GET
def handle_get(handler, path: str, parsed, qs: dict) -> bool:
    if not (path.startswith("/api/state/") or path.startswith("/api/context/") or path.startswith("/api/sauna/")):
        return False
    if not handler._authed(parsed):
        handler._send_json({"error": "forbidden"}, 403)
        return True
    try:
        ex = _ex()
        s = ex.state
        sauna = _sauna()
    except Exception as exc:
        handler._send_json({"error": "personal state unavailable: %s" % exc,
                            "unavailable": True}, 503)
        return True
    try:
        if path == "/api/state/today":
            b = ex.brief()
            b["sauna"] = sauna.status()
            b["mode"] = "prototype"
            handler._send_json(b)
        elif path == "/api/state/tasks":
            gid = (qs.get("goal", [""])[0] or "").strip()
            include_done = (qs.get("done", ["1"])[0] or "1") != "0"
            handler._send_json({"tasks": s.tasks(goal_id=gid, include_done=include_done),
                                "goals": s.goals(None), "projects": s.projects(None),
                                "focus_task": s.get_meta("focus_task")})
        elif path == "/api/state/notes":
            q = (qs.get("q", [""])[0] or "").strip()
            handler._send_json({"notes": s.notes(query=q, limit=200), "projects": s.projects(None), "goals": s.goals(None)})
        elif path == "/api/state/events":
            now = int(time.time())
            since = int(qs.get("since", [now - 7 * 86400])[0] or now - 7 * 86400)
            until = int(qs.get("until", [now + 60 * 86400])[0] or now + 60 * 86400)
            evs = s.events(since=since, until=until, limit=200)
            from .executive import _when, _suggest_for_event
            for e in evs:
                e["when"] = _when(e, now)
                e["suggested_action"] = _suggest_for_event(e, now, evs)
            handler._send_json({"events": evs, "now": now})
        elif path == "/api/state/journal":
            day = (qs.get("day", [""])[0] or "").strip()
            if day:
                handler._send_json({"entry": s.journal_entry(day), "day": day})
            else:
                from .personal_state import week_key
                handler._send_json({"entries": s.journal(limit=int(qs.get("limit", ["14"])[0] or 14)),
                                    "week": s.weekly_summary(week_key()),
                                    "projects": [{"id": p["id"], "name": p["name"]} for p in s.projects(None)]})
        elif path == "/api/state/activity":
            pid = (qs.get("project", [""])[0] or "").strip()
            limit = max(1, min(500, int(qs.get("limit", ["80"])[0] or 80)))
            handler._send_json({"activity": s.recent_activity(limit=limit, project_id=pid)})
        elif path == "/api/state/workflows":
            handler._send_json({"workflows": s.workflows(), "transitions": [
                {"prev": k[0], "next": k[1], "goals": n} for k, n in s.transition_counts().items()]})
        elif path == "/api/state/suggestions":
            handler._send_json({"suggestions": s.suggestions(status=None, limit=50)})
        elif path == "/api/state/projects":
            pid = (qs.get("id", [""])[0] or "").strip()
            if pid:
                handler._send_json({"timeline": s.project_timeline(pid)})
            else:
                handler._send_json({"projects": s.projects(None)})
        elif path == "/api/state/people":
            handler._send_json({"people": s.people()})
        elif path == "/api/state/core":
            handler._send_json({"core": s.core_schema_status(),
                                "peer": s.peer_cursor("sauna-prototype")})
        elif path == "/api/state/conflicts":
            status = (qs.get("status", ["open"])[0] or "open").strip()
            handler._send_json({"conflicts": s.sync_conflicts(status=status),
                                "core": s.core_schema_status()})
        elif path == "/api/state/views":
            files = s.render_views(profile_lines=ex._profile_lines())
            out = {}
            for name, p in files.items():
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        out[name] = {"path": p, "text": f.read()[:20000]}
                except Exception:
                    out[name] = {"path": p, "text": ""}
            handler._send_json({"views": out})
        elif path == "/api/state/memory-cards":
            project = (qs.get("project", ["global"])[0] or "global").strip()
            from .cli import _paths
            from .memory import SqliteMemory
            from .memory_cards import MemoryCardProjector
            mem = SqliteMemory(_paths()[0])
            try:
                handler._send_json(MemoryCardProjector(s, mem).render(project=project))
            finally:
                mem.close()
        elif path == "/api/state/memory-core":
            from .cli import _paths
            from .memory import SqliteMemory
            mem = SqliteMemory(_paths()[0])
            try:
                sync = mem.memory_sync()
                handler._send_json({"core": sync.status(), "conflicts": sync.conflicts(limit=50)})
            finally:
                mem.close()
        elif path == "/api/state/session-memory":
            from .cli import _paths
            from .session_memory import SessionMemory
            memory_path = _paths()[0]
            archive = SessionMemory(os.path.join(os.path.dirname(memory_path), "session_memory.db"))
            try:
                query = (qs.get("q", [""])[0] or "").strip()
                project = (qs.get("project", ["global"])[0] or "global").strip()
                session_id = (qs.get("session", [""])[0] or "").strip()
                if session_id:
                    thread = archive.open_thread(session_id)
                    handler._send_json({"thread": thread}, 200 if thread else 404)
                elif query:
                    handler._send_json({"fragments": archive.search(query, project=project),
                                        "recent_threads": archive.recent_threads(project=project),
                                        "core": archive.session_sync().status(),
                                        "conflicts": archive.session_sync().conflicts(limit=50)})
                else:
                    handler._send_json({"fragments": [],
                                        "recent_threads": archive.recent_threads(project=project, limit=20),
                                        "core": archive.session_sync().status(),
                                        "conflicts": archive.session_sync().conflicts(limit=50)})
            finally:
                archive.close()
        elif path == "/api/state/memory-receipt":
            receipt_id = (qs.get("id", [""])[0] or "").strip()
            from .cli import _paths
            from .memory import SqliteMemory
            from .memory_retrieval import MemoryRetriever
            mem = SqliteMemory(_paths()[0])
            try:
                if receipt_id.startswith("prefret_"):
                    receipt = mem.preference_resolver().receipt(receipt_id)
                else:
                    receipt = MemoryRetriever(mem).receipt(receipt_id)
                handler._send_json({"receipt": receipt}, 200 if receipt else 404)
            finally:
                mem.close()
        elif path == "/api/state/memory":
            # the person-readable memory page: personal-state facts + Collie's trusted profile
            prof = []
            try:
                from .cli import _paths
                from .memory import SqliteMemory
                mem = SqliteMemory(_paths()[0])
                try:
                    prof = [{k: r.get(k) for k in ("id", "kind", "text", "value", "attribute", "confidence", "status", "scope")}
                            for r in (mem.trusted_profile("global") or [])[:40]]
                finally:
                    mem.close()
            except Exception:
                prof = []
            handler._send_json({"profile": prof, "decisions": s.recent_activity(limit=30, kinds=("decision",)),
                                "people": s.people(), "projects": s.projects(None),
                                "workflows": [w for w in s.workflows() if w["status"] != "template"],
                                "meta": {k: s.get_meta(k) for k in ("owner_name", "owner_role", "owner_location")},
                                "sauna": sauna.status()})
        elif path == "/api/context/local":
            wait = float(qs.get("wait", ["0.35"])[0] or 0.35)
            d = device_context(cwd=os.getcwd(), wait=min(2.5, max(0.0, wait)), state=s)
            from .localcontext import chips
            d["chips"] = chips(d)
            focus_id = s.get_meta("focus_task")
            focus = s.task(focus_id) if focus_id else None
            if focus and focus["status"] not in ("done", "dropped"):
                d["focus_task"] = {"id": focus["id"], "title": focus["title"]}
                d["chips"].append({"kind": "task", "label": "Task · " + focus["title"][:40]})
            else:
                d["focus_task"] = None
            d["sauna"] = {"connected": sauna.connected, "account": s.get_meta("sauna_account")}
            d["chips"].append({"kind": "sauna", "label": "Sauna · connected" if sauna.connected else "Local only"})
            # the raw clipboard never rides a GET unless the person enabled it, and then it is theirs
            handler._send_json(d)
        elif path == "/api/sauna/status":
            handler._send_json(sauna.status())
        elif path == "/api/sauna/devices":
            handler._send_json({"devices": sauna.devices(), "this_device": sauna.device_id})
        elif path == "/api/state/related":
            # Collie nominates, the person picks. Nothing is fetched until a topic has been chosen,
            # because guessing one and presenting its results as "your news" would be asserting a
            # taste nobody stated — and the candidates here (Collie, Sauna) are exactly the words
            # that go embarrassingly wrong when searched blind.
            from . import related as _rel
            topic = s.get_meta("related_topic") or ""
            handler._send_json({"topic": topic, "candidates": _rel.candidates(s),
                                "items": _rel.stories(topic) if topic else [],
                                "source": "Hacker News"})
        elif path == "/api/sauna/link":
            # Cheap by default. ?probe=1 is the human-pressed "check now": it drives the signed-in
            # browser, which is a visible side effect, so it never happens on a poll.
            probe = (qs.get("probe", ["0"])[0] or "0") == "1"
            handler._send_json(sauna.link(probe_browser=probe))
        elif path == "/api/sauna/inbox":
            refresh = (qs.get("refresh", ["1"])[0] or "1") != "0"
            handler._send_json(sauna.inbox(refresh=refresh))
        elif path == "/api/sauna/context":
            q = (qs.get("q", [""])[0] or "").strip()
            handler._send_json({"connected": sauna.connected, "context": sauna.person_context(q, cwd=os.getcwd()),
                                "adds": sauna.context_catalog()})
        else:
            handler._send_json({"error": "unknown personal route"}, 404)
    except Exception as exc:
        handler._send_json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)
    return True


# ------------------------------------------------------------------------------ POST
def handle_post(handler, path: str, parsed) -> bool:
    if not (path.startswith("/api/state/") or path.startswith("/api/sauna/")):
        return False
    if not handler._authed(parsed):
        handler._send_json({"error": "forbidden"}, 403)
        return True
    body = handler._read_json(65536)
    if body is None:
        handler._send_json({"error": "expected JSON object"}, 400)
        return True
    try:
        ex = _ex()
        s = ex.state
        sauna = _sauna()
    except Exception as exc:
        handler._send_json({"error": "personal state unavailable: %s" % exc,
                            "unavailable": True}, 503)
        return True
    try:
        if path == "/api/state/task":
            out = _task_action(ex, body)
            action = str(body.get("action") or "")
            if action in ("add", "done", "status", "drop", "update", "reopen"):
                sync = _auto_sync(sauna, "task", action,
                                  str((out.get("task") or {}).get("id") or body.get("task_id") or ""))
                if sync is not None:
                    out["auto_sync"] = sync
            handler._send_json(out)
        elif path == "/api/state/memory-query":
            query = str(body.get("query") or "").strip()
            if not query:
                return _bad(handler, "query required")
            from .cli import _paths
            from .memory import SqliteMemory
            from .memory_retrieval import MemoryRetriever
            from .session_memory import SessionMemory
            memory_path = _paths()[0]
            mem = SqliteMemory(memory_path)
            archive = SessionMemory(os.path.join(os.path.dirname(memory_path), "session_memory.db"),
                                    embedder=mem.embedder)
            try:
                bundle = MemoryRetriever(mem, session_memory=archive).retrieve(
                    query, project=str(body.get("project") or "global"),
                    current_session=str(body.get("session") or ""),
                    as_of=body.get("as_of"), known_at=body.get("known_at"))
                handler._send_json(bundle)
            finally:
                archive.close(); mem.close()
        elif path == "/api/state/memory-conflict":
            conflict_id = str(body.get("id") or body.get("conflict_id") or "").strip()
            resolution = str(body.get("resolution") or "").strip()
            if not conflict_id:
                return _bad(handler, "conflict id required")
            from .cli import _paths
            from .memory import SqliteMemory
            mem = SqliteMemory(_paths()[0])
            try:
                resolved = mem.memory_sync().resolve_conflict(conflict_id, resolution)
                if not resolved:
                    return _bad(handler, "unknown open Memory conflict", 404)
                handler._send_json({"ok": True, "conflict": resolved,
                                    "core": mem.memory_sync().status()})
            finally:
                mem.close()
        elif path == "/api/state/session-conflict":
            conflict_id = str(body.get("id") or body.get("conflict_id") or "").strip()
            resolution = str(body.get("resolution") or "").strip()
            if not conflict_id:
                return _bad(handler, "conflict id required")
            from .cli import _paths
            from .session_memory import SessionMemory
            memory_path = _paths()[0]
            archive = SessionMemory(os.path.join(os.path.dirname(memory_path), "session_memory.db"))
            try:
                resolved = archive.session_sync().resolve_conflict(conflict_id, resolution)
                handler._send_json({"ok": True, "conflict": resolved,
                                    "core": archive.session_sync().status()})
            finally:
                archive.close()
        elif path == "/api/state/preference-resolve":
            attribute = str(body.get("attribute") or "").strip()
            if not attribute:
                return _bad(handler, "attribute required")
            from .cli import _paths
            from .memory import SqliteMemory
            mem = SqliteMemory(_paths()[0])
            try:
                out = mem.resolve_preference(
                    attribute, context=body.get("context"),
                    project=str(body.get("project") or "global"),
                    device_id=str(body.get("device_id") or ""), default=body.get("default"),
                    current_request_value=body.get("current_request") if
                    "current_request" in body else None,
                    policy_override=body.get("policy_override") if "policy_override" in body else None)
                handler._send_json(out)
            finally:
                mem.close()
        elif path == "/api/state/conflict":
            conflict_id = str(body.get("id") or body.get("conflict_id") or "").strip()
            resolution = str(body.get("resolution") or "").strip()
            if not conflict_id:
                return _bad(handler, "conflict id required")
            resolved = s.resolve_sync_conflict(conflict_id, resolution)
            if not resolved:
                return _bad(handler, "unknown open conflict", 404)
            handler._send_json({"ok": True, "conflict": resolved,
                                "core": s.core_schema_status()})
        elif path == "/api/state/note":
            action = str(body.get("action") or "add")
            note_id = str(body.get("note_id") or body.get("id") or "").strip()
            if action == "delete":
                old = s.delete_note(note_id)
                if not old:
                    return _bad(handler, "unknown note", 404)
                # Do not retain the deleted title/body in the activity ledger: deletion is a
                # privacy operation, not a second hidden copy of the content.
                s.record_activity("note_deleted", "Deleted a note", actor="user",
                                  project_id=old.get("project_id") or "", goal_id=old.get("goal_id") or "",
                                  detail={"note_id": note_id})
                out = {"ok": True, "deleted": note_id}
            elif action == "update":
                if not s.note(note_id):
                    return _bad(handler, "unknown note", 404)
                fields = {}
                if "text" in body or "body" in body:
                    fields["body"] = str(body.get("text") if "text" in body else body.get("body") or "")
                if "title" in body:
                    fields["title"] = str(body.get("title") or "")
                if "project" in body:
                    fields["project_id"] = _project_from(s, body.get("project"))
                if "goal" in body:
                    fields["goal_id"] = _goal_from(s, body.get("goal"))
                if "pinned" in body:
                    fields["pinned"] = bool(body.get("pinned"))
                n = s.update_note(note_id, **fields)
                s.record_activity("note_updated", "Updated note: %s" % n["title"], actor="user",
                                  project_id=n.get("project_id") or "", goal_id=n.get("goal_id") or "",
                                  detail={"note_id": note_id})
                if body.get("decision"):
                    s.record_decision(n["body"], project_id=n.get("project_id") or "",
                                      goal_id=n.get("goal_id") or "", actor="user")
                out = {"ok": True, "note": n}
            elif action in ("add", "append"):
                text = str(body.get("text") or "").strip()
                if not text:
                    return _bad(handler, "text required")
                project_id = _project_from(s, body.get("project"))
                goal_id = _goal_from(s, body.get("goal"))
                append_to = str(body.get("append_to") or "").strip()
                target = s.note(note_id) if action == "append" and note_id else (
                    s.find_note(append_to) if append_to else None)
                if target is None and append_to:
                    # find_note matches title-inside-text; the tool and CLI also try the reverse,
                    # so a partial title appends instead of silently creating a second note.
                    for candidate in s.notes(limit=300):
                        if append_to.lower() in candidate["title"].lower():
                            target = candidate
                            break
                if action == "append" and not target:
                    return _bad(handler, "unknown note", 404)
                if target:
                    n = s.append_note(target["id"], text)
                    action = "append"
                else:
                    n = s.add_note(text, title=str(body.get("title") or ""), project_id=project_id,
                                   goal_id=goal_id, source="user")
                    action = "add"
                if body.get("decision"):
                    s.record_decision(text, project_id=project_id, goal_id=goal_id, actor="user")
                out = {"ok": True, "note": n}
                note_id = n["id"]
            else:
                return _bad(handler, "unknown note action")
            s.build_journal(); _render(s, ex)
            sync = _auto_sync(sauna, "note", action, note_id)
            if sync is not None:
                out["auto_sync"] = sync
            handler._send_json(out)
        elif path == "/api/state/event":
            action = str(body.get("action") or "add")
            event_id = str(body.get("event_id") or body.get("id") or "").strip()
            changed = True
            if action == "delete":
                old = s.delete_event(event_id)
                if not old:
                    return _bad(handler, "unknown event", 404)
                s.record_activity("event_deleted", "Removed from calendar: %s" % old["title"], actor="user",
                                  goal_id=old.get("goal_id") or "", project_id=old.get("project_id") or "",
                                  detail={"event_id": event_id})
                out = {"ok": True, "deleted": event_id}
            elif action == "update":
                if not s.event(event_id):
                    return _bad(handler, "unknown event", 404)
                fields = {k: body[k] for k in ("title", "start_at", "end_at", "all_day", "kind",
                                                "location", "notes") if k in body}
                if "goal" in body:
                    fields["goal_id"] = _goal_from(s, body.get("goal"))
                if "project" in body:
                    fields["project_id"] = _project_from(s, body.get("project"))
                e = s.update_event(event_id, **fields)
                s.record_activity("event_updated", "Updated calendar: %s" % e["title"], actor="user",
                                  goal_id=e.get("goal_id") or "", project_id=e.get("project_id") or "",
                                  detail={"event_id": event_id})
                out = {"ok": True, "event": e}
            elif action == "add":
                title = str(body.get("title") or "").strip()
                start_at = int(body.get("start_at") or 0)
                if not title or not start_at:
                    return _bad(handler, "title and start_at required")
                end_at = int(body.get("end_at")) if body.get("end_at") not in (None, "") else None
                kind = str(body.get("kind") or "meeting")
                goal_id, project_id = _goal_from(s, body.get("goal")), _project_from(s, body.get("project"))
                existing = None
                if body.get("dedupe"):
                    candidates = s.events(since=start_at, until=start_at, limit=50)
                    existing = next((row for row in candidates
                                     if str(row.get("title") or "").casefold() == title.casefold()
                                     and int(row.get("start_at") or 0) == start_at
                                     and (int(row["end_at"]) if row.get("end_at") is not None else None) == end_at
                                     and str(row.get("kind") or "") == kind
                                     and str(row.get("goal_id") or "") == goal_id), None)
                if existing:
                    e, changed = existing, False
                    out = {"ok": True, "event": e, "created": False}
                else:
                    e = s.add_event(title, start_at, end_at=end_at, all_day=bool(body.get("all_day")),
                                    kind=kind, goal_id=goal_id, project_id=project_id,
                                    notes=str(body.get("notes") or ""))
                    s.record_activity("event", "Added to calendar: %s" % title, actor="user",
                                      goal_id=e.get("goal_id") or "", project_id=e.get("project_id") or "",
                                      detail={"event_id": e["id"]})
                    out = {"ok": True, "event": e, "created": True}
                event_id = e["id"]
            else:
                return _bad(handler, "unknown event action")
            if changed:
                s.build_journal(); _render(s, ex)
                sync = _auto_sync(sauna, "event", action, event_id)
                if sync is not None:
                    out["auto_sync"] = sync
            handler._send_json(out)
        elif path == "/api/state/suggestion":
            sid = str(body.get("id") or "")
            action = str(body.get("action") or "")
            sug = s.suggestion(sid)
            if not sug:
                return _bad(handler, "unknown suggestion", 404)
            if action == "accept":
                out = {"ok": True}
                act = sug.get("action") or {}
                if act.get("type") == "complete_task" and act.get("task_id"):
                    done = s.complete_task(act["task_id"], actor="user")
                    out["task"] = done
                    try:
                        ex.workflows.observe_task_completion(done)
                        out["next"] = ex.workflows.suggest_after(done)
                    except Exception:
                        pass
                elif act.get("type") == "new_task":
                    out["task"] = s.add_task(str(act.get("title") or "Next step"), goal_id=act.get("goal_id") or "",
                                             kind=act.get("kind") or "", source="workflow")
                elif act.get("type") == "block_time" and act.get("start_at"):
                    out["event"] = s.add_event("Focus: %s" % (s.task(act.get("task_id", "")) or {}).get("title", "prep"),
                                               int(act["start_at"]), end_at=int(act["start_at"]) + 60 * int(act.get("minutes", 45)),
                                               kind="block", goal_id=sug.get("goal_id") or "")
                elif act.get("type") == "run" and act.get("task_id"):
                    s.set_meta("focus_task", act["task_id"])
                    t = s.task(act["task_id"])
                    if t and t["status"] in ("open", "next"):
                        s.update_task(t["id"], status="doing")
                    out["run"] = {"prompt": act.get("prompt") or (t or {}).get("title", ""), "task_id": act["task_id"]}
                out["suggestion"] = s.resolve_suggestion(sid, "accepted")
                _render(s, ex)
                handler._send_json(out)
            elif action in ("dismiss", "not_now"):
                handler._send_json({"ok": True, "suggestion": s.resolve_suggestion(sid, "dismissed")})
            else:
                return _bad(handler, "action must be accept or dismiss")
        elif path == "/api/state/workflow":
            wid = str(body.get("id") or "")
            action = str(body.get("action") or "")
            if action == "automate":
                handler._send_json({"ok": True, "workflow": ex.workflows.automate(wid, True)})
            elif action == "manual":
                handler._send_json({"ok": True, "workflow": ex.workflows.automate(wid, False)})
            elif action == "confirm":
                handler._send_json({"ok": True, "workflow": ex.workflows.confirm(wid)})
            elif action == "learn":
                handler._send_json({"ok": True, "learned": ex.workflows.learn_from_history()})
            else:
                return _bad(handler, "unknown workflow action")
        elif path == "/api/state/journal/build":
            day = str(body.get("day") or "") or None
            handler._send_json({"ok": True, "entry": s.build_journal(day, narrator=ex.narrator), "rollup": ex.rollup(day)["week"]})
        elif path == "/api/state/meta":
            if "related_topic" in body:
                s.set_meta("related_topic", str(body.get("related_topic") or "")[:80])
            for k in ("owner_name", "owner_role", "owner_location", "device_name"):
                if k in body:
                    s.set_meta(k, str(body.get(k) or "")[:200])
            handler._send_json({"ok": True})
        elif path == "/api/state/demo":
            from . import demo_seed
            action = str(body.get("action") or "seed")
            if action == "seed":
                handler._send_json({"ok": True, "seeded": demo_seed.seed(s, ex, sauna, connect_sauna=bool(body.get("connect_sauna")))})
            elif action == "reset":
                handler._send_json({"ok": True, "removed": demo_seed.reset(s)})
            else:
                return _bad(handler, "action must be seed or reset")
        # ---- sauna
        elif path == "/api/sauna/connect":
            handler._send_json({"ok": True, "status": sauna.connect(str(body.get("account") or ""))})
        elif path == "/api/sauna/disconnect":
            handler._send_json({"ok": True, "status": sauna.disconnect(forget_cloud_copy=bool(body.get("forget")))})
        elif path == "/api/sauna/sync":
            handler._send_json({"ok": True, "result": sauna.sync(reason=str(body.get("reason") or "manual")),
                                "status": sauna.status()})
        elif path == "/api/sauna/sync-pref":
            key = str(body.get("key") or "")
            enabled = bool(body.get("enabled"))
            out = {"ok": True, "sync": sauna.set_sync_pref(key, enabled)}
            # Turning a category off is a privacy action. Apply it to the cloud copy immediately;
            # waiting for a later run would leave data the person just withheld sitting there.
            if sauna.connected:
                out["result"] = sauna.sync(reason="sync preference changed")
            handler._send_json(out)
        elif path == "/api/sauna/push":
            text = str(body.get("text") or "").strip()
            if not text:
                return _bad(handler, "text required")
            try:
                handler._send_json({"ok": True, "result": sauna.push(
                    text, transport=str(body.get("transport") or "browser"),
                    wait=float(body.get("wait") or 60))})
            except RuntimeError as exc:
                return _bad(handler, str(exc), 409)
        elif path == "/api/sauna/open-session":
            title = str(body.get("title") or "").strip()
            if not title:
                return _bad(handler, "title required")
            handler._send_json(sauna.open_session(title))
        elif path == "/api/sauna/route":
            handler._send_json(sauna.route(str(body.get("text") or "")))
        elif path == "/api/sauna/handoff":
            text = str(body.get("text") or "").strip()
            if not text:
                return _bad(handler, "text required")
            ct = sauna.handoff(text, scheduled_for=body.get("scheduled_for"), deliver_at=body.get("deliver_at"))
            handler._send_json({"ok": True, "cloud_task": ct, "mode": "prototype"})
        elif path == "/api/sauna/cloud-task":
            cid = str(body.get("id") or "")
            status = str(body.get("status") or "")
            if status not in ("running", "done", "failed", "cancelled"):
                return _bad(handler, "status must be running|done|failed|cancelled")
            handler._send_json({"ok": True, "cloud_task": sauna.cloud_mark(cid, status, result=str(body.get("result") or ""))})
        elif path == "/api/sauna/export":
            # Deliberately ignores a client-supplied path: this is the one personal route that could
            # otherwise write anywhere on the filesystem, and it does not pass the tool gate.
            # `collie sauna export --path` remains available for choosing a destination by hand.
            handler._send_json({"ok": True, "path": sauna.export_snapshot()})
        elif path == "/api/sauna/restore":
            src = body.get("snapshot") if isinstance(body.get("snapshot"), dict) else (str(body.get("path") or "") or None)
            handler._send_json({"ok": True, "restore": sauna.restore(src)})
        else:
            handler._send_json({"error": "unknown personal route"}, 404)
    except ValueError as exc:
        handler._send_json({"error": str(exc)}, 400)
    except FileNotFoundError as exc:
        handler._send_json({"error": "not found: %s" % exc}, 404)
    except RuntimeError as exc:
        handler._send_json({"error": str(exc)}, 409)
    except Exception as exc:
        handler._send_json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)
    return True


# ------------------------------------------------------------------------------ helpers
def _bad(handler, msg: str, code: int = 400) -> bool:
    handler._send_json({"error": msg}, code)
    return True


def _render(s, ex) -> None:
    try:
        s.render_views(profile_lines=ex._profile_lines())
    except Exception:
        pass


def _auto_sync(sauna, entity: str, action: str, entity_id: str = "") -> dict | None:
    """Apply the advertised "sync after each change" setting without making local edits depend on
    the cloud.  Failures are returned to the caller for an honest UI/debug trail; the local change
    remains committed and usable either way."""
    if not sauna.connected or not _flag("SAUNA_AUTO_SYNC"):
        return None
    try:
        result = sauna.sync(reason="auto")
        sauna.signals([{"kind": "state_change", "entity": entity, "action": action,
                        "entity_id": entity_id}])
        return result
    except Exception as exc:
        return {"synced": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def _project_from(s, name) -> str:
    name = str(name or "").strip()
    if not name:
        return ""
    p = s.project(name) or s.find_project(name) or s.upsert_project(name)
    return p["id"]


def _goal_from(s, name) -> str:
    name = str(name or "").strip()
    if not name:
        return ""
    g = s.goal(name)
    if g:
        return g["id"]
    for g in s.goals(None):
        if name.lower() in g["title"].lower():
            return g["id"]
    return ""


def _task_action(ex, body: dict) -> dict:
    s = ex.state
    action = str(body.get("action") or "").strip()
    tid = str(body.get("task_id") or body.get("id") or "").strip()
    if action == "add":
        title = str(body.get("title") or "").strip()
        if not title:
            raise ValueError("title required")
        t = s.add_task(title, project_id=_project_from(s, body.get("project")), goal_id=_goal_from(s, body.get("goal")),
                       status=str(body.get("status") or "open"), due_at=body.get("due_at"), source="user",
                       notes=str(body.get("notes") or ""))
        _render(s, ex)
        return {"ok": True, "task": t}
    t = s.task(tid)
    if not t:
        raise ValueError("unknown task")
    if action == "done":
        done = s.complete_task(tid, actor="user")
        out = {"ok": True, "task": done, "goal": s.goal(done["goal_id"]) if done.get("goal_id") else None}
        try:
            ex.workflows.observe_task_completion(done)
            out["suggestion"] = ex.workflows.suggest_after(done)
        except Exception:
            out["suggestion"] = None
        if s.get_meta("focus_task") == tid:
            s.set_meta("focus_task", "")
        s.build_journal(); _render(s, ex)
        return out
    if action == "status":
        t2 = s.update_task(tid, status=str(body.get("status") or "open"))
        s.record_activity("task_status", "Task is now %s: %s" % (t2["status"], t2["title"]), actor="user",
                          task_id=tid, goal_id=t2.get("goal_id") or "", project_id=t2.get("project_id") or "")
        s.build_journal()
        _render(s, ex)
        return {"ok": True, "task": t2}
    if action == "reopen":
        t2 = s.update_task(tid, status="open")
        if s.get_meta("focus_task") == tid:
            s.set_meta("focus_task", "")
        s.record_activity("task_reopened", "Reopened: %s" % t2["title"], actor="user", task_id=tid,
                          goal_id=t2.get("goal_id") or "", project_id=t2.get("project_id") or "")
        s.build_journal(); _render(s, ex)
        return {"ok": True, "task": t2,
                "goal": s.goal(t2["goal_id"]) if t2.get("goal_id") else None}
    if action == "focus":
        s.set_meta("focus_task", tid)
        if t["status"] in ("open", "next"):
            s.update_task(tid, status="doing")
        s.record_activity("focus", "Now working on: %s" % t["title"], actor="user", task_id=tid, goal_id=t.get("goal_id") or "",
                          project_id=t.get("project_id") or "")
        return {"ok": True, "task": s.task(tid), "focus_task": tid}
    if action == "unfocus":
        if s.get_meta("focus_task") == tid:
            s.set_meta("focus_task", "")
        return {"ok": True, "focus_task": ""}
    if action == "drop":
        t2 = s.update_task(tid, status="dropped")
        if s.get_meta("focus_task") == tid:
            s.set_meta("focus_task", "")
        s.record_activity("task_dropped", "Dropped: %s" % t2["title"], actor="user", task_id=tid,
                          goal_id=t2.get("goal_id") or "", project_id=t2.get("project_id") or "")
        s.build_journal(); _render(s, ex)
        return {"ok": True, "task": t2}
    if action == "update":
        fields = {k: body[k] for k in ("title", "notes", "due_at", "kind") if k in body}
        t2 = s.update_task(tid, **fields)
        s.record_activity("task_updated", "Updated task: %s" % t2["title"], actor="user", task_id=tid,
                          goal_id=t2.get("goal_id") or "", project_id=t2.get("project_id") or "")
        _render(s, ex)
        return {"ok": True, "task": t2}
    raise ValueError("unknown task action")
