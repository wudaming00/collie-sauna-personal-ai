"""Personal Workflow Model: observe → suggest → confirm → automate, with evidence before trust."""
import time

import pytest

from harness.personal_state import PersonalState
from harness.workflows import SUGGEST_AFTER, WorkflowModel


@pytest.fixture
def model(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("COLLIE_PERSONAL_DB", raising=False)
    s = PersonalState()
    m = WorkflowModel(s)
    m.ensure_templates()
    try:
        yield m
    finally:
        s.close()


def _interview_goal(s, name="Sauna", done_until=3, base=None):
    g = s.add_goal("Prepare for %s interview" % name)
    titles = [("Research %s" % name, "research"), ("Research interviewer", "research"), ("Define product thesis", "write"),
              ("Build prototype", "build"), ("Prepare system design", "design"), ("Rehearse", "prepare")]
    base = base or int(time.time()) - 10 * 86400
    tasks = []
    for i, (title, kind) in enumerate(titles, 1):
        t = s.add_task(title, goal_id=g["id"], kind=kind, status="done" if i <= done_until else "open", order_key=i)
        if i <= done_until:
            s._exec("UPDATE tasks SET done_at=? WHERE id=?", (base + i * 3600, t["id"]))
        tasks.append(t)
    return g, tasks


def test_templates_exist_and_match_an_interview_goal(model):
    s = model.state
    names = {w["name"] for w in s.workflows(status="template")}
    assert {"Interview preparation", "Bug fix"} <= names
    g, tasks = _interview_goal(s)
    wf = model.workflow_for_goal(g["id"])
    assert wf and wf["name"] == "Interview preparation"


def test_suggest_after_points_at_the_next_unfinished_step(model):
    s = model.state
    g, tasks = _interview_goal(s)
    done = s.complete_task(tasks[3]["id"])   # Build prototype
    sug = model.suggest_after(done)
    assert sug["title"] == "Next: Prepare system design"
    assert "interview preparation workflow" in sug["body"]
    assert sug["action"]["type"] == "run" and sug["action"]["task_id"] == tasks[4]["id"]
    assert sug["action"]["stage"] == "suggest"        # templates never auto-run
    assert "Prepare system design" in sug["action"]["prompt"]
    # accepting feeds the workflow's evidence
    s.resolve_suggestion(sug["id"], "accepted")
    assert s.workflow(sug["workflow_id"])["accepted"] == 1


def test_transitions_become_learned_workflows_after_enough_goals(model):
    s = model.state
    g1, t1 = _interview_goal(s, "Tavus", done_until=6, base=int(time.time()) - 30 * 86400)
    g2, t2 = _interview_goal(s, "Sauna", done_until=6, base=int(time.time()) - 10 * 86400)
    learned = model.learn_from_history()
    assert learned, "two goals with the same sequence must produce learned workflows"
    names = {w["name"] for w in learned}
    assert "After build, work on the design" in names
    w = [x for x in learned if x["name"] == "After build, work on the design"][0]
    assert w["status"] == "suggested" and w["observations"] == 2 and w["source"] == "learned"
    assert any(a["kind"] == "workflow_learned" for a in s.recent_activity(limit=20))
    # a third goal exhibiting it → confirmed
    g3, t3 = _interview_goal(s, "Canopy", done_until=6, base=int(time.time()) - 2 * 86400)
    learned = model.learn_from_history()
    w = [x for x in learned if x["name"] == "After build, work on the design"][0]
    assert w["status"] == "confirmed" and w["observations"] == 3


def test_one_goal_is_not_enough_evidence(model):
    s = model.state
    _interview_goal(s, "Only", done_until=6)
    assert model.learn_from_history() == []
    assert SUGGEST_AFTER == 2


def test_observe_task_completion_records_the_transition(model):
    s = model.state
    g, tasks = _interview_goal(s, done_until=3)
    done = s.complete_task(tasks[3]["id"])
    model.observe_task_completion(done)
    counts = s.transition_counts()
    assert counts.get(("write", "build")) == 1


def test_automated_workflows_only_auto_run_safe_local_kinds(model):
    s = model.state
    w = s.upsert_workflow("After build, work on the design", steps=[{"kind": "build"}, {"kind": "design"}], status="confirmed")
    assert model.stage_for(w, "design") == "suggest"
    model.automate(w["id"], True)
    w = s.workflow(w["id"])
    assert w["status"] == "automated"
    assert model.stage_for(w, "design") == "auto"
    assert model.stage_for(w, "communicate") == "suggest"   # notifying people is never automatic
    model.automate(w["id"], False)
    assert s.workflow(w["id"])["status"] == "confirmed"


def test_goal_without_plan_falls_back_to_learned_transition(model):
    s = model.state
    _interview_goal(s, "A", done_until=6, base=int(time.time()) - 30 * 86400)
    _interview_goal(s, "B", done_until=6, base=int(time.time()) - 20 * 86400)
    model.learn_from_history()
    g = s.add_goal("Ship the patch")
    t = s.add_task("Build the fix", goal_id=g["id"], kind="build")
    done = s.complete_task(t["id"])
    sug = model.suggest_after(done)
    assert sug and sug["action"]["type"] == "new_task" and sug["action"]["kind"] == "design"
    assert "seen 2 times" in sug["body"]
