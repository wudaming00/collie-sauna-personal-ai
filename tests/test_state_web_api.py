"""The personal layer over HTTP: token-gated, local, and the demo scenario end to end."""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest


@pytest.fixture
def web(monkeypatch, tmp_path):
    from harness import webapp

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("COLLIE_STATE_DIR", str(state))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(state / "sessions"))
    monkeypatch.setenv("COLLIE_SETTINGS_PATH", str(state / "settings.json"))
    monkeypatch.delenv("COLLIE_PERSONAL_DB", raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1], webapp.TOKEN
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)


def _call(url, method="GET", body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_personal_routes_need_the_token(web):
    base, token = web
    for path in ("/api/state/today", "/api/state/core", "/api/state/conflicts",
                 "/api/state/memory-cards",
                 "/api/state/memory-core", "/api/state/session-memory",
                 "/api/context/local", "/api/sauna/status"):
        code, body = _call(base + path)
        assert code == 403, path
    code, body = _call(base + "/api/state/task", "POST", {"action": "add", "title": "x"})
    assert code == 403
    code, body = _call(base + "/api/state/memory-query", "POST", {"query": "what changed"})
    assert code == 403
    code, body = _call(base + "/api/state/session-conflict", "POST",
                       {"id": "sconf_missing", "resolution": "local"})
    assert code == 403
    code, body = _call(base + "/api/state/today?token=" + token)
    assert code == 200 and "goals" in body and body["sauna"]["connected"] is False
    code, core = _call(base + "/api/state/core?token=" + token)
    assert code == 200 and core["core"]["wire_format"] == "collie-personal-delta/2"
    code, conflicts = _call(base + "/api/state/conflicts?token=" + token)
    assert code == 200 and conflicts["conflicts"] == []
    code, cards = _call(base + "/api/state/memory-cards?token=" + token)
    assert code == 200 and len(cards["cards"]) == 7
    assert cards["project"] == "global"
    code, memory_core = _call(base + "/api/state/memory-core?token=" + token)
    assert code == 200 and memory_core["core"]["format"] == "collie-memory-delta/2"
    code, session_memory = _call(base + "/api/state/session-memory?token=" + token)
    assert code == 200 and "recent_threads" in session_memory
    assert session_memory["core"]["format"] == "collie-session-memory-delta/1"
    code, recalled = _call(base + "/api/state/memory-query?token=" + token, "POST",
                           {"query": "what changed", "project": "global"})
    assert code == 200 and recalled["envelope"]["schema"] == "collie-memory-context/2"
    receipt_id = recalled["receipt"]["receipt_id"]
    code, receipt = _call(base + "/api/state/memory-receipt?token=" + token + "&id=" + receipt_id)
    assert code == 200 and receipt["receipt"]["receipt_id"] == receipt_id
    code, preference = _call(base + "/api/state/preference-resolve?token=" + token, "POST",
                             {"attribute": "missing.preference", "default": "fallback"})
    assert code == 200 and preference["value"] == "fallback"


def test_demo_scenario_round_trip(web):
    base, token = web
    q = "?token=" + token
    code, seeded = _call(base + "/api/state/demo" + q, "POST", {"action": "seed"})
    assert code == 200 and seeded["ok"]
    code, today = _call(base + "/api/state/today" + q)
    assert today["goals"][0]["title"] == "Prepare for Sauna interview"
    assert today["upcoming"][0]["title"] == "Sauna interview"
    assert today["focus_task"]["title"] == "Build Collie prototype"
    assert today["goals"][0]["workflow"]["name"] == "Interview preparation"
    # The ambient Make Time action is safe to press twice: the first creates one local block and
    # the second returns the same receipt instead of silently stacking another calendar row.
    block_action = today["upcoming"][0]["suggested_action"]
    block_payload = {"title": "Focus: Build Collie prototype", "start_at": block_action["start_at"],
                     "end_at": block_action["start_at"] + 60 * block_action["minutes"],
                     "kind": "block", "goal": today["goals"][0]["id"], "dedupe": True}
    code, block1 = _call(base + "/api/state/event" + q, "POST", block_payload)
    code2, block2 = _call(base + "/api/state/event" + q, "POST", block_payload)
    assert code == code2 == 200 and block1["created"] is True and block2["created"] is False
    assert block1["event"]["id"] == block2["event"]["id"]
    code, events = _call(base + "/api/state/events" + q)
    matching_blocks = [row for row in events["events"] if row["title"] == block_payload["title"]]
    assert len(matching_blocks) == 1 and matching_blocks[0]["suggested_action"] is None
    # a note from anywhere
    code, n = _call(base + "/api/state/note" + q, "POST", {"text": "Registry = trust layer", "append_to": "Sauna interview notes"})
    assert code == 200 and n["note"]["title"] == "Sauna interview notes" and "trust layer" in n["note"]["body"]
    # complete the focus task → goal moves, suggestion appears
    tid = today["focus_task"]["id"]
    code, done = _call(base + "/api/state/task" + q, "POST", {"action": "done", "task_id": tid})
    assert code == 200 and done["task"]["status"] == "done"
    assert done["goal"]["progress"] == pytest.approx(4 / 7)
    assert done["suggestion"]["title"] == "Next: Prepare system design examples"
    # accept the suggestion → it becomes the focus run
    code, acc = _call(base + "/api/state/suggestion" + q, "POST", {"id": done["suggestion"]["id"], "action": "accept"})
    assert code == 200 and acc["run"]["task_id"] and "system design" in acc["run"]["prompt"].lower()
    code, today2 = _call(base + "/api/state/today" + q)
    assert today2["focus_task"]["title"] == "Prepare system design examples"
    assert today2["counts"]["done_today"] >= 1
    # journal & activity & workflows pages have data
    code, j = _call(base + "/api/state/journal" + q)
    assert j["entries"] and j["week"]["key"]
    code, act = _call(base + "/api/state/activity" + q)
    assert any(a["kind"] == "task_done" for a in act["activity"])
    code, wf = _call(base + "/api/state/workflows" + q)
    assert any(w["source"] == "learned" for w in wf["workflows"])
    # reset removes exactly the demo rows
    code, reset = _call(base + "/api/state/demo" + q, "POST", {"action": "reset"})
    assert code == 200 and reset["removed"]["tasks"] >= 7
    code, today3 = _call(base + "/api/state/today" + q)
    assert not today3["goals"]


def test_sauna_connect_context_handoff_and_devices(web):
    base, token = web
    q = "?token=" + token
    _call(base + "/api/state/demo" + q, "POST", {"action": "seed"})
    code, ctx0 = _call(base + "/api/sauna/context" + q + "&q=interview")
    assert ctx0["connected"] is False and ctx0["context"] == ""
    code, st = _call(base + "/api/sauna/connect" + q, "POST", {"account": "demo.user@example.com"})
    assert code == 200 and st["status"]["connected"]
    code, ctx = _call(base + "/api/sauna/context" + q + "&q=" + urllib.parse.quote("Where am I with the Sauna interview"))
    assert ctx["connected"] and "Prepare for Sauna interview" in ctx["context"] and "Jordan" in ctx["context"]
    code, route = _call(base + "/api/sauna/route" + q, "POST",
                        {"text": "Research the remaining competitors tonight and give me a report tomorrow morning"})
    assert route["runtime"] == "cloud" and route["offer_cloud"]
    code, ho = _call(base + "/api/sauna/handoff" + q, "POST",
                     {"text": "Research the remaining competitors tonight and give me a report tomorrow morning"})
    assert code == 200 and ho["cloud_task"]["status"] == "scheduled" and ho["mode"] == "prototype"
    code, dev = _call(base + "/api/sauna/devices" + q)
    kinds = {d["kind"] for d in dev["devices"]}
    assert "desktop" in kinds and "cloud" in kinds
    code, pref = _call(base + "/api/sauna/sync-pref" + q, "POST", {"key": "journal", "enabled": False})
    assert pref["sync"]["journal"] is False
    code, st2 = _call(base + "/api/sauna/status" + q)
    assert st2["cloud"]["scheduled"] == 1 and st2["sync"]["journal"] is False
    code, exp = _call(base + "/api/sauna/export" + q, "POST", {})
    assert code == 200 and exp["path"].endswith(".json")
    code, off = _call(base + "/api/sauna/disconnect" + q, "POST", {})
    assert off["status"]["connected"] is False


def test_local_context_endpoint_returns_chips(web):
    base, token = web
    code, ctx = _call(base + "/api/context/local?token=" + token + "&wait=0")
    assert code == 200 and "chips" in ctx and "foreground" in ctx and "at" in ctx


def test_personal_state_corrections_and_auto_sync(web):
    base, token = web
    q = "?token=" + token
    _call(base + "/api/state/demo" + q, "POST", {"action": "seed"})
    _call(base + "/api/sauna/connect" + q, "POST", {"account": "person@example.com"})

    code, created = _call(base + "/api/state/note" + q, "POST",
                          {"text": "first draft", "title": "Correct me"})
    assert code == 200 and created["auto_sync"]["synced"] is True
    note_id = created["note"]["id"]
    cloud_path = created["auto_sync"]["path"]
    with open(cloud_path, encoding="utf-8") as fh:
        cloud = json.load(fh)
    assert any(n["id"] == note_id for n in cloud["notes"])

    code, updated = _call(base + "/api/state/note" + q, "POST",
                          {"action": "update", "id": note_id,
                           "text": "corrected text", "title": "Corrected"})
    assert code == 200 and updated["note"]["body"] == "corrected text"
    code, deleted = _call(base + "/api/state/note" + q, "POST",
                          {"action": "delete", "id": note_id})
    assert code == 200 and deleted["deleted"] == note_id and deleted["auto_sync"]["synced"]
    with open(cloud_path, encoding="utf-8") as fh:
        cloud = json.load(fh)
    assert not any(n["id"] == note_id for n in cloud["notes"])

    start = int(time.time()) + 86400
    code, event = _call(base + "/api/state/event" + q, "POST",
                        {"title": "First time", "start_at": start, "end_at": start + 1800})
    event_id = event["event"]["id"]
    code, moved = _call(base + "/api/state/event" + q, "POST",
                        {"action": "update", "id": event_id, "title": "New time",
                         "start_at": start + 3600, "end_at": start + 5400, "kind": "meeting"})
    assert code == 200 and moved["event"]["title"] == "New time"
    code, gone = _call(base + "/api/state/event" + q, "POST", {"action": "delete", "id": event_id})
    assert code == 200 and gone["deleted"] == event_id

    code, today = _call(base + "/api/state/today" + q)
    done_id = today["goals"][0]["tasks"][0]["id"]
    code, reopened = _call(base + "/api/state/task" + q, "POST",
                           {"action": "reopen", "task_id": done_id})
    assert code == 200 and reopened["task"]["status"] == "open" and reopened["task"]["done_at"] is None

    # A privacy toggle applies to the cloud copy immediately, not after some unrelated later run.
    code, hidden = _call(base + "/api/sauna/sync-pref" + q, "POST",
                         {"key": "notes", "enabled": False})
    assert code == 200 and hidden["result"]["synced"] is True
    with open(hidden["result"]["path"], encoding="utf-8") as fh:
        cloud = json.load(fh)
    assert cloud["categories"]["notes"] is False and "notes" not in cloud
