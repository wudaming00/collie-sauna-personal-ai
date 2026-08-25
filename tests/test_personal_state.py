"""Personal State Model: structured, local, canonical; Markdown is a projection."""
import json
import os
import time

import pytest

from harness.personal_state import PersonalState, SyncDeltaError, day_key, week_key


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("COLLIE_PERSONAL_DB", raising=False)
    s = PersonalState()
    try:
        yield s
    finally:
        s.close()


def _scenario(s):
    p = s.upsert_project("Collie", path="C:\\work\\collie")
    g = s.add_goal("Prepare for Sauna interview", project_id=p["id"])
    t1 = s.add_task("Research Sauna", goal_id=g["id"], project_id=p["id"], status="done")
    t2 = s.add_task("Build Collie prototype", goal_id=g["id"], project_id=p["id"], status="doing")
    t3 = s.add_task("Prepare system design examples", goal_id=g["id"], project_id=p["id"])
    e = s.add_event("Sauna interview", int(time.time()) + 3 * 86400, kind="interview", goal_id=g["id"], project_id=p["id"])
    return p, g, (t1, t2, t3), e


def test_goal_progress_and_event_meaning(state):
    p, g, (t1, t2, t3), e = _scenario(state)
    assert state.goal(g["id"])["progress"] == pytest.approx(1 / 3)
    ev = state.event(e["id"])
    assert ev["goal"]["title"] == "Prepare for Sauna interview"
    assert ev["preparation"] == pytest.approx(1 / 3)
    assert [t["title"] for t in ev["remaining"]] == ["Build Collie prototype", "Prepare system design examples"]
    assert t1["kind"] == "research" and t2["kind"] == "build" and t3["kind"] == "design"


def test_complete_task_records_activity_and_moves_goal(state):
    p, g, (t1, t2, t3), e = _scenario(state)
    done = state.complete_task(t2["id"], actor="collie", run_id="7", evidence={"files": ["docs/x.md"]})
    assert done["status"] == "done" and done["done_at"]
    acts = state.recent_activity(limit=5)
    assert acts[0]["kind"] == "task_done" and acts[0]["run_id"] == "7" and acts[0]["detail"]["files"] == ["docs/x.md"]
    assert state.goal(g["id"])["progress"] == pytest.approx(2 / 3)
    # completing twice is idempotent (no second activity row)
    state.complete_task(t2["id"])
    assert len([a for a in state.recent_activity(limit=20) if a["kind"] == "task_done"]) == 1
    assert state.next_task(g["id"])["title"] == "Prepare system design examples"


def test_match_task_is_fuzzy_but_bounded(state):
    _scenario(state)
    t, score = state.match_task("finish building the Collie prototype")
    assert t["title"] == "Build Collie prototype" and score >= 0.6
    none, low = state.match_task("buy groceries for the weekend")
    assert none is None


def test_journal_compresses_the_day(state):
    p, g, tasks, e = _scenario(state)
    state.complete_task(tasks[1]["id"], actor="collie")
    state.record_decision("Markdown is a view, not canonical memory", project_id=p["id"], goal_id=g["id"])
    state.add_suggestion("Next: Prepare system design examples", goal_id=g["id"], task_id=tasks[2]["id"])
    j = state.build_journal()
    assert j["day"] == day_key()
    assert any("Completed: Build Collie prototype" in h for h in j["happened"])
    assert j["decisions"] == ["Markdown is a view, not canonical memory"]
    assert "Prepare system design examples" in j["open_loops"]
    assert j["next"][0].startswith("Next:")
    assert state.journal_entry(day_key())["happened"] == j["happened"]
    # a narrator may add prose; a failing narrator never loses the entry
    j2 = state.build_journal(narrator=lambda entry: (_ for _ in ()).throw(RuntimeError("no model")))
    assert j2["narrative"] == "" and j2["source"] == "auto"
    j3 = state.build_journal(narrator=lambda entry: "A productive day.")
    assert j3["narrative"] == "A productive day." and j3["source"] == "llm"
    w = state.weekly_summary(week_key())
    assert w["key"] == week_key() and w["days"] == [day_key()]


def test_decision_becomes_long_term_memory_when_memory_is_attached(state):
    calls = []

    class Mem:
        def remember(self, text, **kw):
            calls.append((text, kw))
            return 1

    p = state.upsert_project("Collie")
    state.record_decision("Collie stays open source", project_id=p["id"], memory=Mem())
    assert calls and calls[0][0] == "Collie stays open source" and calls[0][1]["kind"] == "decision"
    assert calls[0][1]["project"] == "Collie"


def test_notes_find_and_append(state):
    p = state.upsert_project("Collie")
    n = state.add_note("Registry is a trust layer.", title="Sauna interview notes", project_id=p["id"])
    hit = state.find_note("add this to my Sauna interview notes please")
    assert hit and hit["id"] == n["id"]
    n2 = state.append_note(n["id"], "Skills declare permissions.")
    assert n2["body"].endswith("Skills declare permissions.")
    assert state.notes(query="trust")[0]["id"] == n["id"]


def test_person_can_correct_and_delete_notes_and_events(state):
    p, g, tasks, event = _scenario(state)
    person = state.add_person("Sebastian", project_id=p["id"])
    note = state.add_note("Draft thesis", title="Interview notes", project_id=p["id"],
                          related=[("person", person["id"])])

    corrected = state.update_note(note["id"], title="Sauna interview notes",
                                  body="Corrected thesis", pinned=True)
    assert corrected["id"] == note["id"] and corrected["body"] == "Corrected thesis"
    assert corrected["pinned"] == 1 and corrected["related"][0]["id"] == person["id"]
    deleted = state.delete_note(note["id"])
    assert deleted["title"] == "Sauna interview notes" and state.note(note["id"]) is None
    assert not state.related("person", person["id"]), "deleting a note also removes its graph edge"

    moved = state.update_event(event["id"], title="Sauna final interview",
                               start_at=event["start_at"] + 3600,
                               end_at=event["start_at"] + 7200, kind="meeting")
    assert moved["id"] == event["id"] and moved["title"] == "Sauna final interview"
    assert moved["start_at"] == event["start_at"] + 3600 and moved["kind"] == "meeting"
    assert state.delete_event(event["id"])["id"] == event["id"]
    assert state.event(event["id"]) is None


def test_reopening_a_task_clears_completion_state(state):
    _p, _g, (_t1, task, _t3), _event = _scenario(state)
    assert state.complete_task(task["id"])["done_at"]
    reopened = state.update_task(task["id"], status="open")
    assert reopened["status"] == "open" and reopened["done_at"] is None


def test_suggestions_dedupe_and_resolve_feed_workflows(state):
    w = state.upsert_workflow("Interview prep", steps=[{"kind": "research", "title": "Research"}], status="suggested")
    s1 = state.add_suggestion("Next: Rehearse", workflow_id=w["id"])
    s2 = state.add_suggestion("Next: Rehearse", workflow_id=w["id"])
    assert s1["id"] == s2["id"]
    acc = state.resolve_suggestion(s1["id"], "accepted")
    assert acc["status"] == "accepted"
    assert state.workflow(w["id"])["accepted"] == 1
    assert state.recent_activity(limit=1)[0]["kind"] == "suggestion_accepted"
    with pytest.raises(ValueError):
        state.resolve_suggestion(s1["id"], "maybe")


def test_export_import_roundtrip_respects_sync_choices(state, tmp_path):
    p, g, tasks, e = _scenario(state)
    state.add_note("private thought", title="Diary")
    snap = state.export_snapshot(include={"notes": False})
    assert "notes" not in snap and snap["categories"]["notes"] is False
    assert snap["categories"]["conversations"] is False   # sensitive defaults stay off
    full = state.export_snapshot()
    assert len(full["notes"]) == 1 and len(full["tasks"]) == 3
    other = PersonalState(str(tmp_path / "other.db"))
    try:
        counts = other.import_snapshot(full)
        assert counts["tasks"] == 3 and counts["goals"] == 1 and counts["events"] == 1
        assert other.goal(g["id"])["title"] == "Prepare for Sauna interview"
        # merge is idempotent: a second import adds nothing new
        again = other.import_snapshot(full)
        assert len(other.tasks()) == 3
        assert other.recent_activity(limit=1)[0]["kind"] == "restore"
        with pytest.raises(ValueError):
            other.import_snapshot({"format": "something-else"})
    finally:
        other.close()


def test_v2_delta_applies_updates_and_tombstones(state, tmp_path):
    note = state.add_note("first", title="Shared note")
    first = state.changes_since()
    assert first["format"] == "collie-personal-delta/2"
    assert first["changes"][0]["base_revision"] == 0

    other = PersonalState(str(tmp_path / "replica.db"))
    try:
        out = other.apply_delta(first, peer_id="workstation")
        assert out["applied"] == 1 and other.note(note["id"])["body"] == "first"

        state.update_note(note["id"], body="corrected")
        second = state.changes_since(first["cursor"])
        assert second["changes"][0]["revision"] == 2
        assert other.apply_delta(second, peer_id="workstation")["applied"] == 1
        assert other.note(note["id"])["body"] == "corrected"

        state.delete_note(note["id"])
        third = state.changes_since(second["cursor"])
        assert third["changes"][0]["operation"] == "delete"
        assert other.apply_delta(third, peer_id="workstation")["applied"] == 1
        assert other.note(note["id"]) is None
        version = other._row("SELECT * FROM entity_versions WHERE entity_type='note' AND entity_id=?",
                             (note["id"],))
        assert version["deleted_at"], "the deletion remains a syncable tombstone"
        # Replaying the same page is idempotent.
        assert other.apply_delta(third, peer_id="workstation")["ignored"] == 1
    finally:
        other.close()


def test_v2_delta_preserves_divergent_edits_for_review(state, tmp_path):
    note = state.add_note("base", title="Architecture")
    initial = state.changes_since()
    other = PersonalState(str(tmp_path / "conflict.db"))
    try:
        other.apply_delta(initial, peer_id="laptop")
        state.update_note(note["id"], body="desktop edit")
        other.update_note(note["id"], body="cloud edit")
        result = other.apply_delta(state.changes_since(initial["cursor"]), peer_id="laptop")
        assert result["conflicts"] == 1
        assert other.note(note["id"])["body"] == "cloud edit", "no last-writer data loss"
        conflict = other.sync_conflicts()[0]
        assert conflict["entity_type"] == "note" and conflict["remote"]["payload"]["body"] == "desktop edit"
        other.resolve_sync_conflict(conflict["conflict_id"], "remote")
        assert other.note(note["id"])["body"] == "desktop edit"
        assert other.sync_conflicts() == []
    finally:
        other.close()


def test_v2_delta_is_allowlisted_and_snapshot_meta_cannot_export_connection_state(state):
    state.upsert_project("Private checkout", path="C:\\secret\\repo")
    state.set_meta("owner_name", "Ada")
    state.set_meta("sauna_token_ref", "vault-ref-must-not-travel")
    state.set_meta("sauna_link_browser", "private browser state")
    snap = state.export_snapshot()
    assert snap["meta"] == {"owner_name": "Ada"}
    assert all(not p.get("path") for p in snap.get("projects", []))
    with pytest.raises(SyncDeltaError):
        state.apply_delta({"format": "collie-personal-delta/2", "changes": [{
            "change_id": "evil", "entity_type": "sqlite_master", "entity_id": "x",
            "operation": "delete", "base_revision": 0, "revision": 1,
            "origin_device": "remote", "hlc": "1:0:remote", "changed_at": 1,
        }]})


def test_personal_core_exposes_distinct_commitment_states(state):
    blocked = state.add_task("Waiting on contract", status="blocked")
    waiting = state.add_task("Waiting for Tuesday", status="waiting")
    assert blocked["status"] == "blocked" and waiting["status"] == "waiting"
    health = state.core_schema_status()
    assert health["schema_version"] == health["supported_version"] == 1
    assert health["wire_format"] == "collie-personal-delta/2"


def test_markdown_views_are_projections(state, tmp_path):
    p, g, tasks, e = _scenario(state)
    files = state.render_views(str(tmp_path / "views"), profile_lines=["[preference] concise answers"])
    assert set(files) >= {"today.md", "recent_activity.md", "project_summary.md", "profile.md"}
    today = open(files["today.md"], encoding="utf-8").read()
    assert "Sauna interview" in today and "Prepare for Sauna interview" in today and "Build Collie prototype" in today
    profile = open(files["profile.md"], encoding="utf-8").read()
    assert "concise answers" in profile
    # the journal day file appears once a journal entry exists
    state.build_journal()
    files = state.render_views(str(tmp_path / "views"))
    assert "journal-%s.md" % day_key() in files


def test_cloud_tasks_and_devices(state):
    ct = state.add_cloud_task("Research competitors", scheduled_for=int(time.time()) + 3600, detail={"prompt": "x"})
    assert ct["status"] == "scheduled" and ct["detail"]["prompt"] == "x"
    state.update_cloud_task(ct["id"], status="running")
    assert state.cloud_tasks(status="running")[0]["id"] == ct["id"]
    state.upsert_device("dev1", "MacBook", platform="macOS", kind="desktop", runtime={"collie": True})
    assert state.devices()[0]["runtime"] == {"collie": True}


def test_the_personal_layer_degrades_instead_of_crashing(monkeypatch, tmp_path):
    """A read-only home must not look like a crash.

    Collie's rule is that a tool *reports* a problem rather than raising one — a stack trace in the
    middle of a run for something the person can ignore is worse than the missing feature.
    """
    import types

    from harness import personal_tools

    # a path sqlite can never open on any OS: the parent is a regular file, not a directory
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("I am a file", encoding="utf-8")
    monkeypatch.setenv("COLLIE_PERSONAL_DB", str(blocker / "personal.db"))
    # a gate is what marks a run as person-facing; supply one so we reach the store, not the
    # benchmark refusal (which is covered below)
    ctx = types.SimpleNamespace(cwd=str(tmp_path), project="p", memory=None, checkpoint_scope="",
                                gate=object())
    cases = ((personal_tools.StateTodayTool(), {"query": "anything"}),
             (personal_tools.NoteSaveTool(), {"text": "remember this"}),
             (personal_tools.TaskUpdateTool(), {"action": "list"}))
    for tool, args in cases:
        out = tool.run(args, ctx)          # must not raise
        assert isinstance(out, str) and out.startswith("ERROR:"), (tool.name, out)
        assert "unavailable" in out, tool.name

    # and a gate-less run — a benchmark or a Pack attempt — must not reach the person's state at all
    benchmark_ctx = types.SimpleNamespace(cwd=str(tmp_path), project="p", memory=None,
                                          checkpoint_scope="", gate=None)
    for tool, args in cases:
        out = tool.run(args, benchmark_ctx)
        assert out.startswith("ERROR:") and "not available in this run" in out, (tool.name, out)


def test_demo_data_is_additive_and_fully_reversible(state, tmp_path):
    """Loading the demo must never overwrite something the person wrote.

    It used to set owner_role/owner_location unconditionally and — worse — `upsert_project` matched
    an existing project by NAME, so a real project called "Collie" was adopted and overwritten with
    demo values. No demo_ row existed afterwards, so reset() could never undo it.
    """
    from harness import demo_seed
    from harness.executive import Executive

    s = state
    s.set_meta("owner_name", "Someone Else")
    s.set_meta("owner_role", "Staff PM at Acme")
    s.set_meta("owner_location", "Berlin")
    mine = s.upsert_project("Collie", summary="MY REAL SUMMARY", path="/home/me/real/path")
    my_task = s.add_task("my own task")
    s.set_meta("focus_task", my_task["id"])

    ex = Executive(s)
    demo_seed.seed(s, ex, None)
    assert s.get_meta("owner_role") == "Staff PM at Acme", "a filled field is never overwritten"
    assert s.get_meta("owner_location") == "Berlin"
    assert s.project(mine["id"])["summary"] == "MY REAL SUMMARY", "the person's project is untouched"
    assert s.project(mine["id"])["path"] == "/home/me/real/path"

    demo_seed.reset(s)
    assert s.get_meta("owner_role") == "Staff PM at Acme"
    assert s.project(mine["id"])["summary"] == "MY REAL SUMMARY"
    assert s.task(my_task["id"]) is not None, "the person's task survives"
    assert s.get_meta("focus_task") == my_task["id"], "focus is put back where it was"
    assert not [t for t in s.tasks(include_done=True) if t["id"].startswith("demo_")]
    assert not [w for w in s.workflows() if w["source"] == "learned"], "seeded workflows are removed too"


def test_demo_seed_fills_an_empty_profile_and_reset_clears_only_that(state):
    from harness import demo_seed
    from harness.executive import Executive

    s = state
    demo_seed.seed(s, Executive(s), None)
    assert s.get_meta("owner_role").startswith("software engineer")
    demo_seed.reset(s)
    assert s.get_meta("owner_role") == "", "what the demo filled, the demo removes"
    assert s.get_meta("owner_name") == ""


def test_interview_demo_readiness_is_clean_and_repeatable(state):
    from harness import demo_ready, demo_seed
    from harness.executive import Executive
    from harness.sauna import SaunaClient

    s = state
    ex = Executive(s)
    sauna = SaunaClient(s)
    demo_seed.seed(s, ex, sauna)
    first = demo_ready.scenario_checks(s, sauna)
    assert all(row["level"] == "pass" for row in first), first
    assert not any("[Test]" in str(row.get("detail") or "") for row in first)

    # Re-preparing the same isolated profile never duplicates rows or changes the opening focus.
    demo_seed.seed(s, ex, sauna)
    second = demo_ready.scenario_checks(s, sauna)
    assert all(row["level"] == "pass" for row in second), second
    assert len([t for t in s.tasks(include_done=True) if t["id"].startswith("demo_tsk_")]) == 13
