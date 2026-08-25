"""Personal Workflow Model — what usually happens *next*.

Memory answers "what happened before?".  This module answers "what does this person usually do
after X?" and turns the answer into suggestions that get more capable as evidence accumulates:

    1st time   the person does X, then Y, then Z by hand          -> observed
    2nd time   Collie recognises X→Y and suggests Y               -> suggested
    3rd time+  Collie may perform Y for confirmed workflows        -> confirmed
    later      the person says "automate this"                     -> automated (Collie runs safe
                                                                       steps; asks for the rest)

It is a lightweight rule/pattern prototype, not ML.  Two sources of workflow knowledge:

* **Templates** — well-known shapes (interview preparation, bug fix) shipped so a brand-new
  install can already explain "based on your interview preparation workflow, the next unfinished
  step is…".  Templates never auto-run anything.
* **Learned** — repeated transitions between task *kinds* (research → write → build → …)
  observed across distinct goals.  A transition seen in ≥2 goals becomes a suggested workflow; the
  threshold mirrors ``memory.record_habit_observation`` (evidence before trust).

Safety rules are deliberate and small: a suggestion always carries a stage (``suggest`` or
``auto``); ``auto`` is only possible for workflows the user explicitly automated *and* for step
kinds that are local and reversible (writing, summarising, researching locally).  Anything that
reaches outside the machine stays a suggestion and, when run, still passes the normal gate.
"""
from __future__ import annotations

import json
import os
import time

from .personal_state import PersonalState, _infer_kind, _words

__all__ = ["WorkflowModel", "TEMPLATES", "SAFE_AUTO_KINDS", "SUGGEST_AFTER", "CONFIRM_AFTER"]

SUGGEST_AFTER = 2      # distinct goals in which a transition was observed before Collie suggests it
CONFIRM_AFTER = 3      # observations (or 1 accepted suggestion) before the workflow is "confirmed"
SAFE_AUTO_KINDS = frozenset(("write", "research", "review", "prepare", "design"))

TEMPLATES = [
    {
        "id": "wf_tpl_interview",
        "name": "Interview preparation",
        "trigger": "Interview scheduled",
        "steps": [
            {"kind": "research", "title": "Research company", "hint": "What they build, pricing, positioning, recent news"},
            {"kind": "research", "title": "Research interviewer", "hint": "Role, background, what they will care about"},
            {"kind": "write", "title": "Prepare product thesis", "hint": "The one-minute story and the contrast"},
            {"kind": "build", "title": "Build prototype / technical examples", "hint": "Something real to show"},
            {"kind": "design", "title": "Prepare system design examples", "hint": "Architecture, boundaries, trade-offs"},
            {"kind": "prepare", "title": "Rehearse", "hint": "Answers, demo run-through, timing"},
            {"kind": "write", "title": "Generate interview-day brief", "hint": "Who, when, where, what matters, open questions"},
        ],
    },
    {
        "id": "wf_tpl_bugfix",
        "name": "Bug fix",
        "trigger": "Bug confirmed",
        "steps": [
            {"kind": "review", "title": "Reproduce", "hint": "A failing check that demonstrates the bug"},
            {"kind": "build", "title": "Fix", "hint": "The smallest change that makes the check pass"},
            {"kind": "review", "title": "Test", "hint": "Run the check after the last edit"},
            {"kind": "build", "title": "Update PR", "hint": "Commit, push, describe"},
            {"kind": "communicate", "title": "Notify teammate", "hint": "Who is waiting on this"},
            {"kind": "review", "title": "Verify deployment", "hint": "Observe the fix where users see it"},
        ],
    },
]


class WorkflowModel:
    def __init__(self, state: PersonalState):
        self.state = state

    # ---------------------------------------------------------------- templates
    def ensure_templates(self) -> None:
        for tpl in TEMPLATES:
            if self.state.workflow(tpl["id"]) is None:
                self.state.upsert_workflow(tpl["name"], trigger=tpl["trigger"], steps=tpl["steps"],
                                           status="template", source="template", workflow_id=tpl["id"],
                                           confidence=0.6)

    # ------------------------------------------------------------- observation
    def observe_task_completion(self, task: dict, *, run_id: str = "") -> dict | None:
        """Record the (previous step → this step) transition inside the task's goal and update
        learned workflows.  Returns the learned workflow touched, if any."""
        if not task:
            return None
        goal_id = task.get("goal_id") or ""
        prev = self._previous_completed(task)
        if prev is None:
            return None
        prev_kind, next_kind = prev.get("kind") or _infer_kind(prev["title"]), task.get("kind") or _infer_kind(task["title"])
        if not _useful_transition(prev_kind, next_kind):
            return None
        self.state.add_workflow_observation(prev_kind, next_kind, goal_id=goal_id, run_id=run_id)
        return self._refresh_learned(prev_kind, next_kind)

    def _previous_completed(self, task: dict) -> dict | None:
        goal_id = task.get("goal_id") or ""
        if not goal_id:
            return None
        done = [t for t in self.state.tasks(goal_id=goal_id, status="done") if t["id"] != task["id"] and t.get("done_at")]
        if not done:
            return None
        done.sort(key=lambda t: (t["done_at"] or 0, t["order_key"]))
        return done[-1]

    def _refresh_learned(self, prev_kind: str, next_kind: str) -> dict | None:
        counts = self.state.transition_counts()
        n = counts.get((prev_kind, next_kind), 0)
        if n < SUGGEST_AFTER:
            return None
        name = "After %s, %s" % (_kind_phrase(prev_kind), _kind_phrase(next_kind))
        existing = None
        for w in self.state.workflows():
            if w["source"] == "learned" and w["name"].lower() == name.lower():
                existing = w
                break
        status = "suggested"
        if existing and existing["status"] in ("confirmed", "automated"):
            status = existing["status"]
        elif n >= CONFIRM_AFTER or (existing and existing["accepted"] >= 1):
            status = "confirmed"
        steps = [{"kind": prev_kind, "title": _kind_phrase(prev_kind).capitalize()},
                 {"kind": next_kind, "title": _kind_phrase(next_kind).capitalize()}]
        w = self.state.upsert_workflow(name, trigger="%s completed" % _kind_phrase(prev_kind), steps=steps, status=status,
                                       source="learned", workflow_id=existing["id"] if existing else "",
                                       confidence=min(0.95, 0.35 + 0.15 * n))
        # observations = distinct goals that exhibited the transition
        self.state._exec("UPDATE workflows SET observations=?, updated_at=? WHERE id=?", (n, int(time.time()), w["id"]))
        if not existing:
            self.state.record_activity("workflow_learned", "Learned a workflow: %s (seen in %d goals)" % (name, n),
                                       actor="workflow", detail={"workflow_id": w["id"]})
        return self.state.workflow(w["id"])

    def learn_from_history(self) -> list[dict]:
        """Rebuild transition observations from completed tasks (idempotent enough for a prototype)."""
        # Rebuild only the goals we can still see. The old blanket DELETE threw away every
        # observation whose task had since been dropped or whose goal was deleted — one click on
        # "relearn" permanently lost history it could not regenerate.
        rebuildable = [g["id"] for g in self.state.goals(None)]
        for gid in rebuildable:
            self.state._exec("DELETE FROM workflow_observations WHERE goal_id=?", (gid,))
        learned = []
        for g in self.state.goals(None):
            done = [t for t in self.state.tasks(goal_id=g["id"], status="done") if t.get("done_at")]
            done.sort(key=lambda t: (t["done_at"] or 0, t["order_key"]))
            for a, b in zip(done, done[1:]):
                ka, kb = a.get("kind") or _infer_kind(a["title"]), b.get("kind") or _infer_kind(b["title"])
                if not _useful_transition(ka, kb):
                    continue
                self.state.add_workflow_observation(ka, kb, goal_id=g["id"])
        for (ka, kb), n in self.state.transition_counts().items():
            w = self._refresh_learned(ka, kb)
            if w:
                learned.append(w)
        return learned

    # ----------------------------------------------------------------- matching
    def workflow_for_goal(self, goal_id: str) -> dict | None:
        """Which known workflow best explains this goal's task list (by step-kind overlap)."""
        tasks = self.state.tasks(goal_id=goal_id)
        if not tasks:
            return None
        goal_kinds = [t.get("kind") or _infer_kind(t["title"]) for t in tasks]
        goal_words = set()
        for t in tasks:
            goal_words |= _words(t["title"])
        g = self.state.goal(goal_id)
        if g:
            goal_words |= _words(g["title"])
        best, best_score = None, 0.0
        for w in self.state.workflows():
            if w["status"] not in ("template", "suggested", "confirmed", "automated"):
                continue
            kinds = [s.get("kind") for s in w["steps"] if s.get("kind")]
            if not kinds:
                continue
            overlap = len(set(kinds) & set(goal_kinds)) / float(len(set(kinds) | set(goal_kinds)))
            name_hit = 0.3 if (_words(w["name"]) & goal_words) or (_words(w["trigger"]) & goal_words) else 0.0
            score = overlap + name_hit
            if score > best_score:
                best, best_score = w, score
        return best if best_score >= 0.45 else None

    def stage_for(self, workflow: dict | None, next_kind: str) -> str:
        """'suggest' or 'auto'.  Auto only for explicitly automated workflows and safe local kinds."""
        if workflow and workflow.get("status") == "automated" and next_kind in SAFE_AUTO_KINDS:
            return "auto"
        return "suggest"

    # -------------------------------------------------------------- suggestions
    def suggest_after(self, task: dict, *, run_id: str = "") -> dict | None:
        """After a task completes: what usually comes next?  Creates (or returns) an open suggestion."""
        if not task:
            return None
        goal_id = task.get("goal_id") or ""
        workflow = self.workflow_for_goal(goal_id) if goal_id else None
        nxt = self.state.next_task(goal_id) if goal_id else None
        if nxt is None:
            # no planned step: fall back to learned transitions from this task's kind
            kind = task.get("kind") or _infer_kind(task["title"])
            counts = self.state.transition_counts()
            cands = sorted(((n, k2) for (k1, k2), n in counts.items() if k1 == kind and n >= SUGGEST_AFTER), reverse=True)
            if not cands:
                if goal_id and self.state.goal(goal_id) and self.state.goal(goal_id)["progress"] >= 1.0:
                    return self.state.add_suggestion(
                        "Goal reached: %s" % self.state.goal(goal_id)["title"], kind="goal_done",
                        body="Every step is done. Want a short wrap-up note in the journal?",
                        goal_id=goal_id, action={"type": "journal", "goal_id": goal_id}, confidence=0.9,
                        source="executive")
                return None
            n, k2 = cands[0]
            title = "Next likely step: %s" % _kind_phrase(k2)
            body = "After %s you usually %s (seen %d times)." % (_kind_phrase(kind), _kind_phrase(k2), n)
            learned = next((w for w in self.state.workflows() if w["source"] == "learned" and
                            w["name"].lower() == ("After %s, %s" % (_kind_phrase(kind), _kind_phrase(k2))).lower()), None)
            return self.state.add_suggestion(title, body=body, goal_id=goal_id, workflow_id=learned["id"] if learned else "",
                                             action={"type": "new_task", "kind": k2, "goal_id": goal_id,
                                                     "title": _kind_phrase(k2).capitalize()},
                                             confidence=min(0.9, 0.4 + 0.15 * n), source="workflow")
        next_kind = nxt.get("kind") or _infer_kind(nxt["title"])
        stage = self.stage_for(workflow, next_kind)
        because = ("Based on your %s workflow" % workflow["name"].lower()) if workflow else "Based on this goal's plan"
        title = "Next: %s" % nxt["title"]
        body = "%s, the next unfinished step is \"%s\"." % (because, nxt["title"])
        if workflow and workflow["source"] == "learned":
            body += " You have done this sequence %d times." % workflow["observations"]
        prompt = _run_prompt(nxt, self.state)
        return self.state.add_suggestion(
            title, body=body, task_id=nxt["id"], workflow_id=workflow["id"] if workflow else "", goal_id=goal_id,
            action={"type": "run", "task_id": nxt["id"], "prompt": prompt, "stage": stage,
                    "workflow": workflow["name"] if workflow else ""},
            confidence=0.8 if workflow else 0.6, source="workflow" if workflow else "executive")

    def automate(self, workflow_id: str, on: bool = True) -> dict | None:
        w = self.state.workflow(workflow_id)
        if not w:
            return None
        status = "automated" if on else "confirmed"
        out = self.state.bump_workflow(workflow_id, status=status)
        self.state.record_activity("workflow_%s" % ("automated" if on else "manual"),
                                   "%s workflow: %s" % ("Automated" if on else "Stopped automating", w["name"]),
                                   actor="user", detail={"workflow_id": workflow_id})
        return out

    def confirm(self, workflow_id: str) -> dict | None:
        return self.state.bump_workflow(workflow_id, status="confirmed", accepted=True)


def _useful_transition(prev_kind: str, next_kind: str) -> bool:
    """A transition worth learning names two DIFFERENT kinds of work.

    "after research you research" is true of every research project and predicts nothing; surfacing
    it as a learned workflow spends the user's trust on a tautology. Unclassified steps ("task") are
    skipped for the same reason: they carry no shape to recognise."""
    return bool(prev_kind and next_kind and prev_kind != next_kind
                and prev_kind != "task" and next_kind != "task")


def _kind_phrase(kind: str) -> str:
    return {
        "research": "research", "prepare": "prepare / rehearse", "build": "build", "write": "write it up",
        "review": "review and verify", "communicate": "notify people", "design": "work on the design",
    }.get(kind, kind or "the next step")


def _run_prompt(task: dict, state: PersonalState) -> str:
    """A concrete, self-contained prompt Collie can run for a task, with the task's context attached."""
    parts = [task["title"].rstrip(".") + "."]
    g = state.goal(task["goal_id"]) if task.get("goal_id") else None
    p = state.project(task["project_id"]) if task.get("project_id") else None
    if g:
        parts.append("This is a step toward the goal \"%s\"." % g["title"])
    if p:
        parts.append("Project: %s." % p["name"])
    if task.get("notes"):
        parts.append(task["notes"])
    parts.append("Use what is already in the project and in my notes; record the result as a note when done.")
    return " ".join(parts)
