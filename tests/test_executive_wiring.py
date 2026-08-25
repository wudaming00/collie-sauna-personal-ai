"""The executive loop must ride the REAL run, not a parallel bookkeeping path.

These tests drive `Harness.run()` itself (scripted provider, real tools, real gate-less loop) and
assert that finishing a run updated the person's state — task done, goal moved, journal written,
next step suggested — and that the situation block reached the model's system prompt.

They also pin the safety rules: a benchmark/subagent harness must not write a person's journal, and
a sink that explodes must not damage the run.
"""
import os
import time

import pytest

from harness.executive import Executive
from harness.loop import Harness
from harness.personal_state import PersonalState
from harness.providers import Completion, ToolCall, Usage
from harness.recorder import Recorder
from harness.context import ContextComposer, TokenBudgeter
from harness.tools import default_registry
from _util import _ScriptProvider


class _Mem:
    """Minimal memory adapter: the loop only needs these to run."""

    def __init__(self):
        self.stored = []

    def core_blocks(self, scopes):
        return []

    def trusted_profile(self, *a, **k):
        return []

    def recall(self, *a, **k):
        return []

    def remember(self, text, **kw):
        self.stored.append(text)
        return 1

    def propose(self, text, **kw):
        return 1

    def set_block(self, *a, **k):
        pass

    def consolidate(self, *a, **k):
        pass

    def close(self):
        pass


def _harness(tmp_path, script, cwd):
    mem = _Mem()
    registry = default_registry()
    composer = ContextComposer(mem, registry, TokenBudgeter(6000))
    recorder = Recorder(str(tmp_path / "runs.db"))
    h = Harness(_ScriptProvider(script), mem, registry, composer, recorder, cwd=str(cwd), project="collie")
    h.self_verify = False
    return h


def _edit_then_answer(target):
    """A run that really writes a file through the real tool, then answers."""
    return [
        Completion(text="", tool_calls=[ToolCall(id="c1", name="write_file",
                                                 args={"path": target, "content": "# Architecture\nSauna is the person layer.\n"})],
                   usage=Usage(input_tokens=10, output_tokens=5), stop_reason="tool_use"),
        Completion(text="Updated the architecture notes.", tool_calls=[],
                   usage=Usage(input_tokens=12, output_tokens=6), stop_reason="end_turn"),
    ]


@pytest.fixture
def personal(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("COLLIE_PERSONAL_DB", raising=False)
    s = PersonalState()
    ex = Executive(s)
    p = s.upsert_project("Collie", path=str(tmp_path))
    g = s.add_goal("Prepare for Sauna interview", project_id=p["id"])
    t1 = s.add_task("Research Sauna", goal_id=g["id"], project_id=p["id"], kind="research", status="done", order_key=1)
    t2 = s.add_task("Build Collie prototype", goal_id=g["id"], project_id=p["id"], kind="build", order_key=2)
    t3 = s.add_task("Prepare system design examples", goal_id=g["id"], project_id=p["id"], kind="design", order_key=3)
    s.add_event("Sauna interview", int(time.time()) + 4 * 86400, kind="interview", goal_id=g["id"], project_id=p["id"])
    s.set_meta("focus_task", t2["id"])
    try:
        yield ex, s, (p, g, [t1, t2, t3])
    finally:
        s.close()


def test_a_real_run_closes_the_loop(personal, tmp_path):
    ex, s, (p, g, tasks) = personal
    h = _harness(tmp_path, _edit_then_answer("notes.md"), tmp_path)
    h.activity_sink = ex.on_run_complete
    res = h.run("web", "Finish updating the Collie architecture notes")
    assert res.answer.startswith("Updated")
    assert (tmp_path / "notes.md").exists(), "the run really wrote the file"
    out = getattr(res, "executive", None)
    assert out, "the loop handed the finished run to the executive layer"
    assert out["task"]["title"] == "Build Collie prototype" and out["task"]["status"] == "done"
    assert out["goal"]["progress"] == pytest.approx(2 / 3)
    assert out["suggestion"]["title"] == "Next: Prepare system design examples"
    kinds = [a["kind"] for a in s.recent_activity(limit=10)]
    assert "run" in kinds and "file_changed" in kinds and "task_done" in kinds
    changed = [a for a in s.recent_activity(limit=10) if a["kind"] == "file_changed"][0]
    assert "notes.md" in changed["summary"]
    j = s.journal_entry(out["journal_day"])
    assert any("Completed: Build Collie prototype" in x for x in j["happened"])
    views = os.path.join(str(tmp_path / "state"), "state", "today.md")
    assert os.path.exists(views), "the Markdown projection was regenerated"
    assert "Prepare system design examples" in open(views, encoding="utf-8").read()


def test_the_situation_block_reaches_the_model(personal, tmp_path):
    ex, s, (p, g, tasks) = personal
    seen = {}

    def capture(messages):
        return Completion(text="ok", tool_calls=[], usage=Usage(input_tokens=1, output_tokens=1), stop_reason="end_turn")

    h = _harness(tmp_path, [capture], tmp_path)
    device = {"foreground": {"app": "Chrome", "title": "Sauna - Google Chrome"},
              "selection": {"text": "person-level intelligence"}, "project": {"name": "Collie", "source": "window"}}
    h.composer.situation = ex.situation_block(device=device, sauna={"connected": True, "context": "- goal \"Prepare for Sauna interview\" (33%)"},
                                              prompt="where am I", cwd=str(tmp_path))
    original = h.provider.complete

    def spy(system, messages, tool_schemas, on_text=None):
        seen["system"] = system
        return original(system, messages, tool_schemas, on_text=on_text)

    h.provider.complete = spy
    h.activity_sink = ex.on_run_complete
    h.run("web", "where am I with the Sauna interview")
    system = seen["system"]
    assert "DEVICE CONTEXT" in system and "Chrome" in system
    assert "PERSONAL STATE" in system and "Build Collie prototype" in system
    assert "PERSON-LEVEL CONTEXT" in system and "Prepare for Sauna interview" in system
    # the volatile block must stay AFTER the cached stable prefix (tool names / identity)
    assert system.index("DEVICE CONTEXT") > system.index("TOOLS")


def test_a_broken_sink_never_damages_the_run(personal, tmp_path):
    ex, s, _ = personal
    h = _harness(tmp_path, _edit_then_answer("x.md"), tmp_path)

    def explode(summary):
        raise RuntimeError("state store on fire")

    h.activity_sink = explode
    res = h.run("web", "Finish updating the Collie architecture notes")
    assert res.answer.startswith("Updated") and not res.error
    assert getattr(res, "executive", "missing") is None


def test_no_sink_means_no_personal_state(personal, tmp_path):
    """Benchmarks, pack attempts and delegate children run with activity_sink unset."""
    ex, s, (p, g, tasks) = personal
    h = _harness(tmp_path, _edit_then_answer("y.md"), tmp_path)
    res = h.run("bench", "Build Collie prototype")
    assert getattr(res, "executive", None) is None
    assert s.task(tasks[1]["id"])["status"] != "done"
    assert not [a for a in s.recent_activity(limit=10) if a["kind"] == "run"]


def test_make_harness_wires_the_sink_only_for_person_facing_runs(tmp_path, monkeypatch):
    from harness import cli
    from harness.gate import Gate

    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COLLIE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("COLLIE_SUBAGENT", raising=False)
    monkeypatch.setattr(cli, "DATA", str(tmp_path / "data"))
    gated = cli.make_harness(str(tmp_path), provider="mock", project="p", gate=Gate(cwd=str(tmp_path)))
    assert callable(gated.activity_sink), "a person-facing run records personal state"
    ungated = cli.make_harness(str(tmp_path), provider="mock", project="p", gate=None)
    assert ungated.activity_sink is None, "an ungated (benchmark/child) run must not write a journal"
    monkeypatch.setenv("COLLIE_SUBAGENT", "1")
    child = cli.make_harness(str(tmp_path), provider="mock", project="p", gate=Gate(cwd=str(tmp_path)))
    assert child.activity_sink is None, "a delegate child must not write the person's state"


def test_web_run_releases_borrowed_memory_before_the_harness_closes_it(tmp_path, monkeypatch):
    """Process-wide personal services must not retain a run-owned SQLite connection."""
    from harness.executive import default_executive
    from harness.personalweb import release_run_memory
    from harness.sauna import default_client

    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path / "release-state"))
    monkeypatch.delenv("COLLIE_PERSONAL_DB", raising=False)
    borrowed = _Mem()
    ex = default_executive(memory=borrowed)
    sauna = default_client(ex.state, memory=borrowed)
    assert ex.memory is borrowed and sauna.memory is borrowed

    release_run_memory(borrowed)

    assert ex.memory is None and sauna.memory is None
    ex.state.set_meta("after_run", "still-open")
    assert ex.state.get_meta("after_run") == "still-open"
