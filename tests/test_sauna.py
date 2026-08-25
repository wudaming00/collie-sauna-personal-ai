"""Sauna connector: local by default, sync what you choose, person-level context only when connected,
cloud handoff is recorded (never claimed executed), portability restores the person's AI."""
import json
import os
import time

import pytest

from harness.personal_state import PersonalState
from harness.sauna import SaunaClient
from harness import sessions


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("COLLIE_PERSONAL_DB", raising=False)
    s = PersonalState()
    c = SaunaClient(s, cloud_dir=str(tmp_path / "cloud"), device_id="dev_test")
    try:
        yield c
    finally:
        s.close()


def _fill(s):
    p = s.upsert_project("Collie")
    g = s.add_goal("Prepare for Sauna interview", project_id=p["id"])
    s.add_task("Research Sauna", goal_id=g["id"], project_id=p["id"], status="done")
    s.add_task("Prepare system design", goal_id=g["id"], project_id=p["id"])
    s.add_event("Sauna interview", int(time.time()) + 4 * 86400, kind="interview", goal_id=g["id"], project_id=p["id"])
    s.add_person("Jordan Lee", role="Product Lead", org="Sauna", project_id=p["id"])
    s.record_decision("Collie stays open source", project_id=p["id"])
    s.add_note("private diary", title="Diary")
    return p, g


def test_local_by_default(client):
    st = client.status()
    assert st["connected"] is False and st["mode"] == "prototype"
    assert client.person_context("anything") == ""
    assert all(not a["available"] for a in client.context_catalog())
    with pytest.raises(RuntimeError):
        client.handoff("research competitors tonight")
    assert client.sync() == {"synced": False, "reason": "not connected"}


def test_connect_syncs_filtered_snapshot_and_keeps_credential_out_of_state(client):
    s = client.state
    _fill(s)
    client.set_sync_pref("notes", False)
    st = client.connect("demo.user@example.com")
    assert st["connected"] and st["account"] == "demo.user@example.com"
    assert st["credential"] in ("vault", "session")
    assert not any(k.startswith("secret:") for k in [r["key"] for r in s._rows("SELECT key FROM meta")])
    cloud = json.load(open(os.path.join(client.cloud_dir, "person.json"), encoding="utf-8"))
    assert "notes" not in cloud and cloud["categories"]["notes"] is False
    assert cloud["categories"]["screen_history"] is False
    assert len(cloud["tasks"]) == 2 and cloud["device_id"] == "dev_test"
    acts = [a for a in s.recent_activity(limit=10) if a["kind"] == "sync"]
    assert acts and "withheld" in acts[0]["detail"]
    assert "notes" in acts[0]["detail"]["withheld"]
    assert s.devices()[0]["this_device"] == 1
    delta = json.load(open(os.path.join(client.cloud_dir, "person.delta.json"), encoding="utf-8"))
    assert delta["format"] == "collie-personal-delta-batch/2"
    assert all(change["entity_type"] != "note"
               for page in delta["pages"] for change in page["changes"])
    assert "sauna_token_ref" not in json.dumps(cloud), "opaque credential references are local too"


def test_person_context_is_richer_than_the_device(client):
    s = client.state
    _fill(s)
    client.connect()
    ctx = client.person_context("Where am I with the Sauna interview?")
    assert "goal \"Prepare for Sauna interview\"" in ctx and "50%" in ctx
    assert "Jordan Lee (Product Lead)" in ctx
    assert "decided: Collie stays open source" in ctx
    assert len(ctx) <= 1400
    adds = {a["label"]: a["available"] for a in client.context_catalog()}
    assert adds["Active goal"] and adds["Upcoming deadline"] and adds["Previous decisions"] and adds["People & relationships"]


def test_person_context_obeys_the_same_privacy_policy_as_sync(client):
    s = client.state
    _fill(s)
    s.add_note("DO NOT SEND THIS NOTE", title="Private strategy")
    client.set_sync_pref("notes", False)
    client.set_sync_pref("tasks", False)
    client.connect()
    context = client.person_context("What is in my private strategy note and what tasks remain?")
    assert "DO NOT SEND THIS NOTE" not in context
    assert "Prepare system design" not in context
    assert "50%" not in context, "task completion must not leak through aggregate goal progress"


def test_enabling_a_withheld_category_requeues_its_current_state(client):
    s = client.state
    client.set_sync_pref("notes", False)
    client.connect()
    s.add_note("created while private", title="Later shared")
    client.sync(reason="private edit")
    before = s.peer_cursor("sauna-prototype")["push_cursor"]

    client.set_sync_pref("notes", True)
    result = client.sync(reason="notes enabled")
    assert result["cursor"] > before and result["delta_changes"] >= 1
    batch = json.load(open(result["delta_path"], encoding="utf-8"))
    notes = [c for p in batch["pages"] for c in p["changes"] if c["entity_type"] == "note"]
    assert notes and notes[-1]["payload"]["body"] == "created while private"


def test_session_memory_sync_is_opt_in_safe_and_requeued(client):
    sessions.save("sauna-thread", [
        {"role": "user", "content": "The Atlas rollout starts Tuesday"},
        {"role": "tool", "content": "local tool output"},
        {"role": "assistant", "content": "I recorded the rollout date"},
        {"role": "user", "content": "password = do-not-upload"},
    ], project="global")
    client.connect()
    path = os.path.join(client.cloud_dir, "sessions.delta.json")
    disabled = json.load(open(path, encoding="utf-8"))
    assert disabled["sharing_enabled"] is False and disabled["pages"] == []

    client.set_sync_pref("conversations", True)
    result = client.sync(reason="conversations enabled")
    enabled = json.load(open(result["session_delta_path"], encoding="utf-8"))
    wire = json.dumps(enabled, ensure_ascii=False)
    assert enabled["sharing_enabled"] is True and result["session_delta_changes"] >= 1
    assert "Atlas rollout" in wire
    assert "local tool output" not in wire and "do-not-upload" not in wire


def test_routing_rules(client):
    client.connect()
    cloud = client.route("Research the remaining competitors tonight and have a report ready tomorrow morning")
    assert cloud["runtime"] == "cloud" and cloud["offer_cloud"] and cloud["scheduled_for"] and cloud["deliver_at"]
    local = client.route("Fix this file and check it in my browser tonight")
    assert local["runtime"] == "local" and local["offer_cloud"]   # offered, but local wins when local context is needed
    appr = client.route("Send the email to the recruiter")
    assert appr["needs_approval"]
    client.disconnect()
    assert client.route("research everything tonight")["runtime"] == "local"


def test_handoff_is_recorded_and_honest(client):
    s = client.state
    client.connect()
    ct = client.handoff("Research the remaining competitors tonight and have a report ready tomorrow morning")
    assert ct["status"] == "scheduled" and ct["runtime"] == "sauna-cloud" and ct["detail"]["mode"] == "prototype"
    assert ct["scheduled_for"] and ct["deliver_at"]
    assert s.recent_activity(limit=1)[0]["kind"] == "handoff"
    assert os.path.exists(os.path.join(client.cloud_dir, "signals.jsonl"))
    assert client.status()["cloud"]["scheduled"] == 1
    client.cloud_mark(ct["id"], "running")
    assert client.status()["cloud"]["running"] == 1


def test_devices_and_portability(client, tmp_path):
    s = client.state
    _fill(s)
    client.connect()
    names = [(d["name"], d["kind"]) for d in client.devices()]
    assert names[0][1] == "desktop" and ("Sauna Cloud", "cloud") in names
    path = client.export_snapshot(str(tmp_path / "export.json"))
    assert os.path.exists(path)
    other = PersonalState(str(tmp_path / "mac.db"))
    try:
        c2 = SaunaClient(other, cloud_dir=client.cloud_dir, device_id="dev_mac")
        out = c2.restore()                           # from the cloud copy
        assert out["welcome"]["goals"] == ["Prepare for Sauna interview"]
        assert out["welcome"]["tasks_open"] == 1 and out["welcome"]["events"] == 1
        assert other.recent_activity(limit=1)[0]["summary"].startswith("Welcome back")
        assert any(d["device_id"] == "dev_test" for d in other.devices())
        out2 = c2.restore(path)                      # from a file, idempotent
        assert len(other.tasks()) == 2
    finally:
        other.close()


def test_disconnect_keeps_local_state(client):
    s = client.state
    _fill(s)
    client.connect()
    client.disconnect()
    assert client.status()["connected"] is False
    assert len(s.tasks()) == 2 and client.person_context("x") == ""


def test_agent_activity_syncs_what_collie_did_not_what_it_said(client):
    """"Agent activity" is on by default; "Full conversation history" is not. An activity row's
    answer excerpt is conversation content, so it must not ride along on the default settings."""
    s = client.state
    _fill(s)
    s.record_activity("run", "Finish the architecture notes", actor="collie",
                      detail={"files": ["docs/x.md"], "answer": "SECRET DRAFT TEXT the model wrote"})
    client.connect()
    cloud = json.load(open(os.path.join(client.cloud_dir, "person.json"), encoding="utf-8"))
    blob = json.dumps(cloud, ensure_ascii=False)
    assert "Finish the architecture notes" in blob, "what Collie did still syncs"
    assert "docs/x.md" in blob, "and the evidence of what it touched"
    assert "SECRET DRAFT TEXT" not in blob, "but not what it said"
    # turning conversation history on is what lets the text travel
    client.set_sync_pref("conversations", True)
    client.sync(reason="manual")
    cloud = json.load(open(os.path.join(client.cloud_dir, "person.json"), encoding="utf-8"))
    assert "SECRET DRAFT TEXT" in json.dumps(cloud, ensure_ascii=False)
