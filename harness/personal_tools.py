"""Agent tools over the Personal State Model.

Three small tools so a spoken sentence in the capsule becomes structured personal state without
the person deciding which folder or database it belongs in:

* ``state_today``  (read)  — the executive state: upcoming, goals, tasks, suggestions, recent.
  "Where am I with the Sauna interview?" is answered from here, not guessed.
* ``note_save``    (write) — "Remember this" / "Add this to my Sauna interview notes".  Appends to
  an existing note when the person names one, otherwise creates one; relations are inferred from
  the project/goal named or from the current focus.
* ``task_update``  (write) — add / complete / re-status / focus a task; progress flows into goals,
  events (preparation %) and the journal through the state model.

Risk classes are declared in ``risk._BASE`` (READ / WRITE_LOCAL).  All writes are local; nothing
here talks to Sauna — sync is the person's choice, made in Settings.
"""
from __future__ import annotations

from .tools import Tool

__all__ = ["StateTodayTool", "NoteSaveTool", "TaskUpdateTool", "register_personal"]


class _StateUnavailable(RuntimeError):
    """The personal store could not be opened (read-only home, missing drive, locked file).

    Collie's rule is that a tool reports a problem; it does not raise one. Raising here would
    surface as a stack trace in the middle of a run for something the person can simply ignore.
    """


class _NotPersonFacing(RuntimeError):
    """This run is a benchmark, a Pack attempt, or another machine-driven execution."""


def _exec(ctx):
    from .executive import default_executive
    # `cli.make_harness` withholds the activity sink from gate-less runs — benchmarks, Pack
    # attempts, delegate children — because they must not write a person's journal. The tools have
    # to honour the same boundary: N Pack attempts otherwise all write the one real personal.db,
    # and `state_today` would dump the owner's goals and people into a benchmark transcript.
    if getattr(ctx, "gate", None) is None:
        raise _NotPersonFacing("no gate on this run")
    try:
        return default_executive(memory=getattr(ctx, "memory", None))
    except Exception as exc:                     # sqlite3.OperationalError, OSError, …
        raise _StateUnavailable(str(exc)) from exc


_NOT_PERSONAL = ("ERROR: personal state is not available in this run. Benchmarks, Pack attempts and "
                 "delegate children deliberately cannot read or change the owner's tasks, notes or journal.")


def _unavailable(exc) -> str:
    return ("ERROR: personal state is unavailable on this machine (%s). Collie can still run; "
            "notes and tasks are not being recorded." % str(exc)[:160])


def _project_id(state, name: str, ctx) -> str:
    if name:
        p = state.find_project(name) or state.upsert_project(name.strip())
        return p["id"]
    # the run's project (cwd scope) if it is a known project
    try:
        cwd = getattr(ctx, "cwd", "") or ""
        import os
        p = state.find_project(os.path.basename(cwd.rstrip("\\/"))) if cwd else None
        return p["id"] if p else ""
    except Exception:
        return ""


class StateTodayTool(Tool):
    name = "state_today"
    description = ("Read the owner's executive state: upcoming events with their meaning, active goals with "
                   "progress, tasks (done / next / open), pending suggestions, recent activity and cloud tasks. "
                   "Optional `query` focuses the answer (e.g. 'Sauna interview'). Use it before answering "
                   "'where am I with…', 'what's next', 'what did I do today'.")
    schema = {"type": "object", "properties": {"query": {"type": "string"}}}

    def run(self, args, ctx):
        try:
            ex = _exec(ctx)
            return ex.answer(str(args.get("query") or ""))
        except _NotPersonFacing:
            return _NOT_PERSONAL
        except _StateUnavailable as exc:
            return _unavailable(exc)
        except Exception as exc:
            return "ERROR: could not read personal state: %s" % exc


class NoteSaveTool(Tool):
    name = "note_save"
    description = ("Save a note into the owner's personal state (local). Use when the owner says 'remember this', "
                   "'note that…', 'add this to my X notes'. Args: text (required); title (optional); "
                   "append_to (optional: the title of an existing note to append to); project (optional name); "
                   "goal (optional title); decision (optional bool: also record it as a decision).")
    schema = {"type": "object", "properties": {
        "text": {"type": "string"}, "title": {"type": "string"}, "append_to": {"type": "string"},
        "project": {"type": "string"}, "goal": {"type": "string"}, "decision": {"type": "boolean"}},
        "required": ["text"]}

    def run(self, args, ctx):
        text = str(args.get("text") or "").strip()
        if not text:
            return "ERROR: note_save needs text"
        try:
            ex = _exec(ctx)
        except _NotPersonFacing:
            return _NOT_PERSONAL
        except _StateUnavailable as exc:
            return _unavailable(exc)
        s = ex.state
        project_id = _project_id(s, str(args.get("project") or ""), ctx)
        goal_id = ""
        goal_name = str(args.get("goal") or "").strip()
        if goal_name:
            for g in s.goals(None):
                if goal_name.lower() in g["title"].lower():
                    goal_id = g["id"]
                    if not project_id:
                        project_id = g["project_id"]
                    break
        if not goal_id:
            focus = s.task(s.get_meta("focus_task")) if s.get_meta("focus_task") else None
            if focus:
                goal_id = focus.get("goal_id") or ""
                project_id = project_id or focus.get("project_id") or ""
        append_to = str(args.get("append_to") or "").strip()
        target = s.find_note(append_to) if append_to else None
        if target is None and append_to:
            for n in s.notes(limit=300):
                if append_to.lower() in n["title"].lower():
                    target = n
                    break
        if target is not None:
            n = s.append_note(target["id"], text, source="collie")
            verb = "Appended to note"
        else:
            n = s.add_note(text, title=str(args.get("title") or ""), project_id=project_id, goal_id=goal_id,
                           source="collie")
            verb = "Saved note"
        if args.get("decision"):
            s.record_decision(text, project_id=project_id, goal_id=goal_id, actor="user", memory=getattr(ctx, "memory", None))
        rel = []
        if project_id:
            p = s.project(project_id)
            rel.append("project → %s" % (p["name"] if p else project_id))
        if goal_id:
            g = s.goal(goal_id)
            rel.append("goal → %s" % (g["title"] if g else goal_id))
        try:
            ex.state.render_views()
        except Exception:
            pass
        return "%s \"%s\" (id %s)%s" % (verb, n["title"], n["id"], (" · related: " + ", ".join(rel)) if rel else "")


class TaskUpdateTool(Tool):
    name = "task_update"
    description = ("Create or update one of the owner's tasks. Args: action = add | done | status | focus | list; "
                   "title (for add, or to find a task by title); task_id (optional exact id); status "
                   "(open|next|doing|done|dropped for action=status); project (optional name); goal (optional title). "
                   "Completing a task updates the goal's progress, the related event's preparation and the journal, "
                   "and may produce a next-step suggestion.")
    schema = {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["add", "done", "status", "focus", "list"]},
        "title": {"type": "string"}, "task_id": {"type": "string"}, "status": {"type": "string"},
        "project": {"type": "string"}, "goal": {"type": "string"}},
        "required": ["action"]}

    def run(self, args, ctx):
        try:
            ex = _exec(ctx)
        except _NotPersonFacing:
            return _NOT_PERSONAL
        except _StateUnavailable as exc:
            return _unavailable(exc)
        s = ex.state
        action = str(args.get("action") or "").strip().lower()
        title = str(args.get("title") or "").strip()
        task_id = str(args.get("task_id") or "").strip()
        if action == "list":
            rows = s.tasks(include_done=False)[:20]
            if not rows:
                return "No open tasks."
            return "\n".join("%s · %s [%s]%s" % (t["id"], t["title"], t["status"],
                                                 (" · goal " + (s.goal(t["goal_id"]) or {}).get("title", "")) if t.get("goal_id") else "")
                             for t in rows)
        if action == "add":
            if not title:
                return "ERROR: task_update add needs a title"
            project_id = _project_id(s, str(args.get("project") or ""), ctx)
            goal_id = ""
            goal_name = str(args.get("goal") or "").strip()
            if goal_name:
                for g in s.goals(None):
                    if goal_name.lower() in g["title"].lower():
                        goal_id = g["id"]; project_id = project_id or g["project_id"]
                        break
            t = s.add_task(title, project_id=project_id, goal_id=goal_id, source="collie")
            return "Added task \"%s\" (id %s)%s" % (t["title"], t["id"], (" to goal " + (s.goal(goal_id) or {}).get("title", "")) if goal_id else "")
        # find the task
        t = s.task(task_id) if task_id else None
        if t is None and title:
            t, _score = s.match_task(title, min_score=0.34)
            if t is None:
                for row in s.tasks(include_done=True, limit=500):
                    if title.lower() in row["title"].lower():
                        t = row
                        break
        if t is None:
            return "ERROR: no task matches %r" % (title or task_id)
        if action == "done":
            done = s.complete_task(t["id"], actor="collie")
            try:
                ex.workflows.observe_task_completion(done)
                sug = ex.workflows.suggest_after(done)
            except Exception:
                sug = None
            try:
                s.build_journal(); s.render_views()
            except Exception:
                pass
            g = s.goal(done["goal_id"]) if done and done.get("goal_id") else None
            out = "Completed \"%s\"" % done["title"]
            if g:
                out += " · goal \"%s\" now %d%%" % (g["title"], round(g["progress"] * 100))
            if sug:
                out += " · suggested next: %s" % sug["title"]
            return out
        if action == "status":
            status = str(args.get("status") or "").strip().lower()
            try:
                t2 = s.update_task(t["id"], status=status)
            except ValueError as exc:
                return "ERROR: %s" % exc
            return "Task \"%s\" is now %s" % (t2["title"], t2["status"])
        if action == "focus":
            s.set_meta("focus_task", t["id"])
            if t["status"] in ("open", "next"):
                s.update_task(t["id"], status="doing")
            return "Focused on \"%s\" — runs about it will count toward this task" % t["title"]
        return "ERROR: unknown action %r" % action


def register_personal(registry) -> None:
    for tool in (StateTodayTool(), NoteSaveTool(), TaskUpdateTool()):
        registry.register(tool)
