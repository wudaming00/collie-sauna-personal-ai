"""Demo data for the Collie × Sauna prototype — explicit, labelled, removable.

`collie state seed-demo` / `POST /api/state/demo {action: seed}` populates the scenario used in
docs/DEMO.md (prepare for the Sauna interview).  Every seeded row carries an id that starts with
``demo_`` so ``reset()`` can remove exactly what was added and nothing the person created.

This is *data*, not behaviour: the executive loop, workflow learning, journal compression, Sauna
connector and device context all run their real code paths over it.
"""
from __future__ import annotations

import datetime as _dt
import os
import time

DEMO_PREFIX = "demo_"


def _ts(year, month, day, hour=0, minute=0) -> int:
    return int(_dt.datetime(year, month, day, hour, minute).timestamp())


def seed(state, executive=None, sauna=None, *, connect_sauna: bool = False, now: int | None = None) -> dict:
    now = int(now if now is not None else time.time())
    today = _dt.datetime.fromtimestamp(now)
    # Anchor the calendar relative to "today" so the demo reads the same whenever it runs:
    # the interview is the next Tuesday 11:00 (or 4 days out if today is Tuesday).
    days_ahead = (1 - today.weekday()) % 7 or 4
    interview = (today + _dt.timedelta(days=days_ahead)).replace(hour=11, minute=0, second=0, microsecond=0)
    architecture_review = (interview + _dt.timedelta(hours=4)).replace(minute=0)
    yesterday = (today - _dt.timedelta(days=1))
    s = state

    # Demo data must never overwrite something the person wrote. Fill only what is empty, and
    # remember what we filled so reset() can put it back exactly.
    filled = []
    for key, value in (("owner_name", "Alex"),
                       ("owner_role", "software engineer · building Collie"),
                       ("owner_location", "Seattle, WA")):
        if not s.get_meta(key):
            s.set_meta(key, value)
            filled.append(key)
    s.set_meta("demo_filled_meta", ",".join(filled))
    s.set_meta("demo_prev_focus", s.get_meta("focus_task"))

    p = s.upsert_project("Collie", summary="Native, local-first AI layer on each device (open source).",
                         path=os.getcwd(), project_id=DEMO_PREFIX + "prj_collie")
    sauna_p = s.upsert_project("Sauna by Wordware", kind="company",
                               summary="Person-level intelligence cloud: memory, context, workflows, cloud agents.",
                               project_id=DEMO_PREFIX + "prj_sauna")
    g = s.add_goal("Prepare for Sauna interview", project_id=p["id"], due_at=int(interview.timestamp()),
                   summary="Get the Sauna offer: show Collie as the native layer and the product thesis.",
                   goal_id=DEMO_PREFIX + "gol_sauna")
    s.add_person("Jordan Lee", role="Product Lead", org="Sauna by Wordware", project_id=sauna_p["id"],
                 notes="Runs the intro; cares about product sense and ownership.", person_id=DEMO_PREFIX + "per_interviewer")
    s.add_person("Casey", role="Recruiting", org="Sauna by Wordware", project_id=sauna_p["id"],
                 person_id=DEMO_PREFIX + "per_recruiter")

    tasks = [
        ("Research Sauna", "research", "done", _ts(today.year, today.month, today.day) - 2 * 86400 + 3600 * 10),
        ("Research interviewer", "research", "done", _ts(today.year, today.month, today.day) - 2 * 86400 + 3600 * 14),
        ("Define product thesis", "write", "done", _ts(today.year, today.month, today.day) - 86400 + 3600 * 20),
        ("Build Collie prototype", "build", "doing", None),
        ("Prepare system design examples", "design", "open", None),
        ("Rehearse the demo", "prepare", "open", None),
        ("Generate interview-day brief", "write", "open", None),
    ]
    ids = []
    for i, (title, kind, status, done_at) in enumerate(tasks, 1):
        tid = DEMO_PREFIX + "tsk_%d" % i
        existing = s.task(tid)
        if existing is None:
            t = s.add_task(title, project_id=p["id"], goal_id=g["id"], kind=kind, status=status, order_key=i,
                           source="user", task_id=tid,
                           notes=("Update the Collie architecture notes (docs/SAUNA_VISION.md) and regenerate the project summary"
                                  if title == "Build Collie prototype" else ""))
            if done_at:
                s._exec("UPDATE tasks SET done_at=?, updated_at=? WHERE id=?", (done_at, done_at, tid))
        ids.append(tid)
    s.set_meta("focus_task", DEMO_PREFIX + "tsk_4")
    before_workflows = {w["id"] for w in s.workflows()}

    s.add_event("Sauna interview", int(interview.timestamp()), end_at=int(interview.timestamp()) + 20 * 60,
                kind="interview", location="Zoom · with Jordan Lee (Product Lead)", project_id=sauna_p["id"],
                goal_id=g["id"], notes="20-minute intro covering both applications.", event_id=DEMO_PREFIX + "evt_interview")
    s.add_event("Architecture review", int(architecture_review.timestamp()),
                end_at=int(architecture_review.timestamp()) + 3600,
                kind="meeting", location="Zoom", event_id=DEMO_PREFIX + "evt_architecture")
    s.link("event", DEMO_PREFIX + "evt_interview", "person", DEMO_PREFIX + "per_interviewer", "with")
    s.link("event", DEMO_PREFIX + "evt_interview", "project", p["id"], "about")

    s.add_note("Collie is the native, local-first AI layer on each device. Sauna is the person-level intelligence "
               "cloud that makes it persistent, personalized, proactive and portable. Collie understands what you are "
               "doing now; Sauna understands you.", title="Sauna product thesis", project_id=p["id"], goal_id=g["id"],
               pinned=True, note_id=DEMO_PREFIX + "nte_thesis",
               related=[("project", sauna_p["id"]), ("person", DEMO_PREFIX + "per_interviewer")])
    s.add_note("Registry is primarily a trust/security layer, not necessarily a marketplace. Skills declare capabilities "
               "and permissions; installed does not mean trusted.", title="Sauna interview notes", project_id=p["id"],
               goal_id=g["id"], note_id=DEMO_PREFIX + "nte_interview",
               related=[("project", sauna_p["id"]), ("goal", g["id"])])
    s.add_note("Jordan Lee — Product Lead at Sauna by Wordware. The intro covers product thinking, systems design "
               "and implementation ownership. Casey coordinates scheduling.", title="Jordan Lee",
               project_id=sauna_p["id"], note_id=DEMO_PREFIX + "nte_interviewer_profile")

    # decisions (also long-term memory when a memory store is attached)
    mem = getattr(executive, "memory", None) if executive is not None else None
    for text in ("Do not reduce Collie's existing functionality; add the personal layers around it.",
                 "Personal state is structured (SQLite); Markdown is a projection, not canonical memory.",
                 "MCP connects external systems; it does not define the person's internal state."):
        if not any(a["summary"] == text for a in s.recent_activity(limit=200, kinds=("decision",))):
            s.record_decision(text, project_id=p["id"], goal_id=g["id"], memory=mem)

    # yesterday's activity so the journal has history to compress
    y0 = int(yesterday.replace(hour=9, minute=0).timestamp())
    rows = [
        (y0 + 3600 * 1, "run", "Researched Sauna: product, pricing, transition history", "collie"),
        (y0 + 3600 * 3, "task_done", "Completed: Research interviewer", "collie"),
        (y0 + 3600 * 9, "note", "Saved note: Sauna product thesis", "user"),
        (y0 + 3600 * 11, "task_done", "Completed: Define product thesis", "user"),
        (y0 + 3600 * 12, "decision", "Collie remains open source; Sauna is the paid person-level layer.", "user"),
    ]
    for at, kind, summary, actor in rows:
        if not any(a["summary"] == summary for a in s.recent_activity(limit=300, since=at - 60)):
            s.record_activity(kind, summary, actor=actor, project_id=p["id"], goal_id=g["id"], at=at)
    # learned workflow evidence: the same sequence seen in an earlier goal (last interview), so the
    # model has something real to recognise — two observations → "suggested"
    g_old = s.add_goal("Prepare for Tavus interview", project_id=p["id"], goal_id=DEMO_PREFIX + "gol_tavus")
    s.set_goal_status(g_old["id"], "done")
    base = now - 12 * 86400
    for i, (title, kind) in enumerate((("Research Tavus", "research"), ("Research interviewer", "research"),
                                       ("Prepare thesis", "write"), ("Build demo", "build"),
                                       ("Prepare system design", "design"), ("Rehearse", "prepare")), 1):
        tid = DEMO_PREFIX + "tsk_old_%d" % i
        if s.task(tid) is None:
            s.add_task(title, project_id=p["id"], goal_id=g_old["id"], kind=kind, status="done", order_key=i,
                       source="user", task_id=tid)
            s._exec("UPDATE tasks SET done_at=?, updated_at=? WHERE id=?", (base + i * 86400, base + i * 86400, tid))
    if executive is not None:
        try:
            executive.workflows.learn_from_history()
        except Exception:
            pass
    # learn_from_history mints ordinary wf_ ids, so reset() cannot spot them by prefix. Record
    # exactly which workflows this seed brought into existence.
    s.set_meta("demo_workflows", ",".join(sorted({w["id"] for w in s.workflows()} - before_workflows)))

    # Seeding is setup, not the person's activity.  Showing thirteen "Added task" rows timestamped
    # one second ago made Today look fabricated and pushed the meaningful preparation history below
    # the fold.  Remove only scaffold receipts tied to demo ids, backdate the three seeded design
    # decisions, and leave a small intentional today trail that tells the story chronologically.
    escaped_prefix = DEMO_PREFIX.replace("_", "\\_") + "%"
    s._exec("DELETE FROM activities WHERE kind='task_created' AND task_id LIKE ? ESCAPE '\\'",
            (escaped_prefix,))
    for summary in ("Saved note: Sauna product thesis", "Saved note: Sauna interview notes",
                    "Saved note: Jordan Lee"):
        s._exec("DELETE FROM activities WHERE kind='note' AND summary=?", (summary,))
    decisions = (
        "Do not reduce Collie's existing functionality; add the personal layers around it.",
        "Personal state is structured (SQLite); Markdown is a projection, not canonical memory.",
        "MCP connects external systems; it does not define the person's internal state.",
    )
    for offset, summary in enumerate(decisions, 13):
        s._exec("UPDATE activities SET at=? WHERE kind='decision' AND summary=?",
                (y0 + 3600 * offset, summary))
    today_morning = int(today.replace(hour=9, minute=15, second=0, microsecond=0).timestamp())
    s._exec("UPDATE activities SET at=? WHERE kind='workflow_learned' AND summary LIKE 'Learned a workflow:%'",
            (today_morning + 1800,))
    today_summary = "Refreshed the Sauna interview context and preparation gaps"
    if not any(a["summary"] == today_summary for a in s.recent_activity(limit=300)):
        s.record_activity("run", today_summary, actor="collie", project_id=p["id"], goal_id=g["id"],
                          at=today_morning)
    # Rebuild projections only after the scaffold cleanup; Markdown must reflect the same canonical
    # timeline the UI exposes.
    s.build_journal(yesterday.strftime("%Y-%m-%d"))
    s.build_journal(today.strftime("%Y-%m-%d"))
    if connect_sauna and sauna is not None and not sauna.connected:
        sauna.connect("demo.user@example.com")
    try:
        s.render_views()
    except Exception:
        pass
    return {"goal": g["id"], "tasks": ids, "interview_at": int(interview.timestamp()), "project": p["id"],
            "sauna_connected": bool(sauna and sauna.connected)}


def reset(state) -> dict:
    """Remove exactly what seed() added — including the profile fields it filled, the workflows it
    caused to be learned, and the focus it moved."""
    s = state
    removed = {}
    for table, col in (("tasks", "id"), ("events", "id"), ("notes", "id"), ("people", "id"), ("goals", "id"),
                       ("projects", "id"), ("suggestions", "id")):
        removed[table] = s.delete_prefixed(table, col, DEMO_PREFIX)
    for col in ("src_id", "dst_id"):
        s.delete_prefixed("relations", col, DEMO_PREFIX)
    for col in ("goal_id", "project_id", "task_id"):
        s.delete_prefixed("activities", col, DEMO_PREFIX)
    for col in ("goal_id", "task_id"):
        s.delete_prefixed("suggestions", col, DEMO_PREFIX)
    s._exec("DELETE FROM workflow_observations WHERE goal_id LIKE ? ESCAPE '\\'", (DEMO_PREFIX.replace("_", "\\_") + "%",))
    workflow_ids = [w for w in (s.get_meta("demo_workflows") or "").split(",") if w]
    for wid in workflow_ids:
        s._exec("DELETE FROM workflows WHERE id=?", (wid,))
    removed["workflows"] = len(workflow_ids) + s.delete_prefixed("workflows", "id", DEMO_PREFIX)
    # the "Learned a workflow: …" rows carry no goal, so the prefix sweep above misses them; they
    # would otherwise sit in Recent activity pointing at a workflow that no longer exists.
    for wid in workflow_ids:
        s._exec("DELETE FROM activities WHERE kind='workflow_learned' AND detail_json LIKE ?", ("%" + wid + "%",))
    for key in [k for k in (s.get_meta("demo_filled_meta") or "").split(",") if k]:
        s.set_meta(key, "")
    prev_focus = s.get_meta("demo_prev_focus")
    if s.get_meta("focus_task").startswith(DEMO_PREFIX):
        s.set_meta("focus_task", prev_focus if prev_focus and s.task(prev_focus) else "")
    for key in ("demo_filled_meta", "demo_prev_focus", "demo_workflows"):
        s.set_meta(key, "")
    try:
        s.render_views()
    except Exception:
        pass
    return removed
