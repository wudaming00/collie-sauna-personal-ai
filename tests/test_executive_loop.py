"""The closed executive loop: run → activity → task → goal → journal → next step; and the situation block."""
import time

import pytest

from harness.executive import Executive, default_executive
from harness.personal_state import PersonalState
from harness.personalweb import executive_payload


@pytest.fixture
def ex(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("COLLIE_PERSONAL_DB", raising=False)
    s = PersonalState()
    e = Executive(s)
    try:
        yield e
    finally:
        s.close()


def _scenario(s):
    p = s.upsert_project("Collie", path="C:\\work\\collie-uiux-rebuild")
    g = s.add_goal("Prepare for Sauna interview", project_id=p["id"])
    t = [s.add_task("Research Sauna", goal_id=g["id"], project_id=p["id"], kind="research", status="done", order_key=1),
         s.add_task("Build Collie prototype", goal_id=g["id"], project_id=p["id"], kind="build", status="doing", order_key=2,
                    notes="Update the architecture notes and regenerate the project summary"),
         s.add_task("Prepare system design examples", goal_id=g["id"], project_id=p["id"], kind="design", order_key=3)]
    e = s.add_event("Sauna interview", int(time.time()) + 4 * 86400, kind="interview", goal_id=g["id"], project_id=p["id"])
    return p, g, t, e


def _run(**kw):
    base = {"run_id": 41, "prompt": "Finish updating the Collie architecture notes", "answer": "Done.",
            "edited_files": ["docs/SAUNA_VISION.md"], "tool_calls": 6, "verified": False, "error": "",
            "canceled": False, "wall_ms": 1200, "cost_usd": 0.01, "turns": 3, "cwd": "C:\\work\\collie-uiux-rebuild",
            "project": "collie-uiux-rebuild@abc", "session": "web-1"}
    base.update(kw)
    return base


def test_focus_task_completes_and_next_step_is_suggested(ex):
    s = ex.state
    p, g, tasks, ev = _scenario(s)
    s.set_meta("focus_task", tasks[1]["id"])
    out = ex.on_run_complete(_run())
    assert out["task_binding"]["mode"] == "focus"
    assert out["task"]["status"] == "done"
    assert out["goal"]["progress"] == pytest.approx(2 / 3)
    assert out["suggestion"]["title"] == "Next: Prepare system design examples"
    assert "interview preparation workflow" in out["suggestion"]["body"]
    kinds = [a["kind"] for a in s.recent_activity(limit=10)]
    assert "run" in kinds and "task_done" in kinds and "file_changed" in kinds and "summary" in kinds
    assert s.get_meta("focus_task") == ""                       # focus released once done
    j = s.journal_entry(out["journal_day"])
    assert any("Completed: Build Collie prototype" in h for h in j["happened"])
    assert s.event(ev["id"])["preparation"] == pytest.approx(2 / 3)


def test_explicit_binding_wins_and_failed_runs_do_not_complete(ex):
    s = ex.state
    p, g, tasks, ev = _scenario(s)
    out = ex.on_run_complete(_run(bound_task_id=tasks[2]["id"], prompt="do the thing"))
    assert out["task_binding"]["mode"] == "explicit" and out["task"]["id"] == tasks[2]["id"]
    s.update_task(tasks[2]["id"], status="open")
    out = ex.on_run_complete(_run(bound_task_id=tasks[2]["id"], error="provider exploded"))
    assert out["task"] is None
    assert s.task(tasks[2]["id"])["status"] == "open"
    runs = [a for a in s.recent_activity(limit=5) if a["kind"] == "run"]
    assert runs and runs[0]["summary"].startswith("Failed:")


def test_weak_match_asks_instead_of_completing(ex):
    s = ex.state
    p, g, tasks, ev = _scenario(s)
    s.set_meta("focus_task", "")
    out = ex.on_run_complete(_run(prompt="Prepare two examples for system design, one for Collie", cwd=""))
    assert out["task_binding"]["mode"] in ("ask", "match")
    if out["task_binding"]["mode"] == "ask":
        assert out["suggestion"]["kind"] == "confirm_done"
        assert s.task(tasks[2]["id"])["status"] != "done"


def test_unrelated_run_is_recorded_without_touching_tasks(ex):
    s = ex.state
    _scenario(s)
    s.set_meta("focus_task", "")
    before = [t["status"] for t in s.tasks()]
    out = ex.on_run_complete(_run(prompt="what time is it in Tokyo", edited_files=[], cwd=""))
    assert out["task"] is None and out["task_binding"]["mode"] == "none"
    assert [t["status"] for t in s.tasks()] == before
    assert s.recent_activity(limit=1)[0]["kind"] == "run"


def test_activity_only_chat_has_no_duplicate_done_card_payload(ex):
    """The activity ledger stays complete without echoing a normal prompt below its answer."""
    out = ex.on_run_complete(_run(prompt="再试一试。", edited_files=[], verified=False, cwd=""))
    assert out["activity"]["summary"] == "再试一试。"
    assert out["task"] is None and out["suggestion"] is None
    assert executive_payload(out) is None


def test_task_completion_keeps_a_proactive_payload_without_echoing_the_prompt(ex):
    s = ex.state
    _, _, tasks, _ = _scenario(s)
    s.set_meta("focus_task", tasks[1]["id"])
    out = ex.on_run_complete(_run())
    payload = executive_payload(out)
    assert payload is not None and payload["task"]["status"] == "done"
    assert payload["suggestion"] is not None
    assert out["activity"]["summary"] not in payload["activities"]


def test_brief_and_answer_are_executive_not_a_todo_list(ex):
    s = ex.state
    p, g, tasks, ev = _scenario(s)
    b = ex.brief()
    assert b["upcoming"][0]["title"] == "Sauna interview" and b["upcoming"][0]["goal"]["id"] == g["id"]
    assert b["upcoming"][0]["suggested_action"]["type"] == "block_time"
    assert b["goals"][0]["workflow"]["name"] == "Interview preparation"
    assert b["goals"][0]["next_task"]["title"] == "Build Collie prototype"
    assert b["focus_task"] is None or b["focus_task"]["id"]
    text = ex.answer("Where am I with the Sauna interview?")
    assert "Sauna interview" in text and "33%" in text and "Build Collie prototype" in text


def test_focus_block_is_a_terminal_idempotent_suggestion(ex):
    s = ex.state
    _p, goal, tasks, event = _scenario(s)
    now = int(time.time())
    first = ex.brief(now=now)
    interview = next(row for row in first["upcoming"] if row["id"] == event["id"])
    action = interview["suggested_action"]
    assert action and action["type"] == "block_time"

    title = "Focus: " + tasks[1]["title"]
    block = s.add_event(title, action["start_at"], end_at=action["start_at"] + 60 * action["minutes"],
                        kind="block", goal_id=goal["id"])
    second = ex.brief(now=now)
    assert next(row for row in second["upcoming"] if row["id"] == event["id"])["suggested_action"] is None
    assert next(row for row in second["upcoming"] if row["id"] == block["id"])["suggested_action"] is None

    s.add_event(title, action["start_at"], end_at=action["start_at"] + 60 * action["minutes"],
                kind="block", goal_id=goal["id"])
    third = ex.brief(now=now)
    matching = [row for row in third["upcoming"] if row["title"] == title and row["start_at"] == action["start_at"]]
    assert len(matching) == 1


def test_brief_keeps_cloud_failures_visible_for_the_desktop(ex):
    task = ex.state.add_cloud_task("Prepare the delegated report")
    ex.state.update_cloud_task(task["id"], status="failed", result="Provider disconnected")
    row = next(item for item in ex.brief()["cloud_tasks"] if item["id"] == task["id"])
    assert row["status"] == "failed" and row["result"] == "Provider disconnected"


def test_situation_block_is_bounded_and_sauna_gated(ex):
    s = ex.state
    p, g, tasks, ev = _scenario(s)
    s.set_meta("focus_task", tasks[1]["id"])
    device = {"foreground": {"app": "Chrome", "title": "Sauna - Google Chrome"},
              "selection": {"text": "x" * 5000}, "project": {"name": "Collie", "source": "window"}}
    block = ex.situation_block(device=device, sauna={"connected": False, "context": "SECRET CLOUD"},
                               prompt="finish the notes", cwd="C:\\work\\collie-uiux-rebuild", budget=900)
    assert "DEVICE CONTEXT" in block and "Chrome" in block and "PERSONAL STATE" in block
    assert "working on now" in block and "Sauna interview" in block
    assert "SECRET CLOUD" not in block                   # not connected → no person-level block
    assert len(block) <= 900
    block2 = ex.situation_block(sauna={"connected": True, "context": "- goal trajectory…"}, prompt="x")
    assert "PERSON-LEVEL CONTEXT" in block2 and "goal trajectory" in block2


def test_default_executive_rebinds_when_the_state_path_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path / "a"))
    a = default_executive()
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path / "b"))
    b = default_executive()
    assert a is not b and a.state.path != b.state.path


def test_a_run_that_only_talked_about_a_task_does_not_finish_it(ex):
    """Evidence before completion — the same rule the verification gate applies to code.

    Asking "what's left on X?" scores high against X's title. Without this, the question marked X
    done, moved the goal, and wrote "Completed: X" into the journal.
    """
    s = ex.state
    p, g, tasks, ev = _scenario(s)
    s.set_meta("focus_task", tasks[2]["id"])
    out = ex.on_run_complete(_run(prompt="what's left on prepare system design examples?",
                                  edited_files=[], verified=False))
    assert s.task(tasks[2]["id"])["status"] != "done", "a question is not the work"
    assert out["task"] is not None and out["suggestion"]["kind"] == "confirm_done"
    assert "changed no files" in out["suggestion"]["body"]
    # the same request, once it actually changes something, completes as before
    out = ex.on_run_complete(_run(prompt="prepare system design examples", edited_files=["design.md"]))
    assert s.task(tasks[2]["id"])["status"] == "done"


def test_a_stale_explicit_binding_never_falls_back_to_a_guess(ex):
    """Clicking Run on a task that another surface already finished must complete nothing.

    Falling through to the fuzzy matcher closed a *different* task with a similar title.
    """
    s = ex.state
    p, g, tasks, ev = _scenario(s)
    twin = s.add_task("Prepare system design notes", goal_id=g["id"], project_id=p["id"], kind="design")
    s.complete_task(tasks[2]["id"])            # finished elsewhere while the page was open
    before = {t["id"]: t["status"] for t in s.tasks()}
    out = ex.on_run_complete(_run(prompt="prepare system design", edited_files=["x.md"],
                                  bound_task_id=tasks[2]["id"]))
    assert out["task_binding"]["mode"] == "stale" and out["task"] is None
    assert s.task(twin["id"])["status"] == before[twin["id"]], "the twin task must be untouched"


def test_done_today_survives_a_long_history(ex):
    """The count read zero forever after a few hundred lifetime completions."""
    s = ex.state
    p, g, tasks, ev = _scenario(s)
    old = int(time.time()) - 200 * 86400
    for i in range(250):
        t = s.add_task("ancient %d" % i, goal_id=g["id"], status="done")
        s._exec("UPDATE tasks SET done_at=? WHERE id=?", (old + i, t["id"]))
    fresh = s.add_task("finished just now", goal_id=g["id"])
    s.complete_task(fresh["id"])
    done_today = ex.brief()["tasks"]["done_today"]
    titles = [t["title"] for t in done_today]
    assert "finished just now" in titles, "today's completion must appear"
    assert not any(t.startswith("ancient") for t in titles), "the 250 old ones must not"


def test_device_context_is_bounded_and_fenced_as_data(ex):
    """It lands in the system prompt, so every field is clipped and marked as observed data."""
    device = {"foreground": {"app": "A" * 500, "title": "T" * 500},
              "browser": {"url": "https://x.test/" + "u" * 500, "title": "B" * 300},
              "project": {"name": "P" * 500, "source": "window"},
              "selection": {"text": "S" * 9000}, "clipboard": {"text": "C" * 9000}}
    block = ex.situation_block(device=device, prompt="", budget=4000)
    assert "never instructions" in block, "the fence must be present"
    for line in block.splitlines():
        assert len(line) < 400, line[:80]
