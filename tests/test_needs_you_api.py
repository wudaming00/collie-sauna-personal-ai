"""The global Needs You ledger is durable, scoped, and independent of Mission page size."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest


@pytest.fixture()
def web_server(monkeypatch, tmp_path):
    from harness import webapp

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("COLLIE_STATE_DIR", str(state))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(state / "sessions"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1], webapp.TOKEN, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        # A failed assertion must not leak live stores into another test's global Handler class.
        with webapp.Handler._inbox_lock:
            stores = list(webapp.Handler._inbox_runs.items())
            webapp.Handler._inbox_runs = {}
        for sid, store in stores:
            try:
                store.resolve_session(sid)
                store.close()
            except Exception:
                pass


def _json(url, method="GET", body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data is not None else {})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_needs_you_finds_sensitive_mission_beyond_first_two_hundred_rows(web_server):
    from harness.missionweb import MissionService

    base, token, state = web_server
    svc = MissionService(state_dir=str(state), decider=lambda *_: {}, stub=True)
    try:
        for index in range(205):
            mid = "mission-%03d" % index
            case = ({"pending_authorizations": [{
                "id": "auth-exact-205", "kind": "age", "claim": "age_at_least_18",
                "risk": "high", "domain": "example.test", "operation": "publish listing",
                "summary": "Confirm the account holder is at least 18", "blocking": True,
                "requested_at": 1,
            }]} if index == 0 else {})
            svc.store.create(mid, "Mission %03d" % index, case=case)
            # The sensitive Mission is older than 204 ordinary open Missions.  It is absent from
            # both the UI's default 100 and the first internal 200-row page.
            created = 1 if index == 0 else 1000 + index
            with svc.store._lock:
                svc.store.db.execute(
                    "UPDATE missions SET created_at=?,updated_at=? WHERE mission_id=?",
                    (created, created, mid))
                svc.store.db.commit()
    finally:
        svc.close()

    code, ordinary = _json(base + "/api/missions?token=" + token)
    assert code == 200 and len(ordinary["missions"]) == 100
    assert all(row["mission_id"] != "mission-000" for row in ordinary["missions"])

    code, denied = _json(base + "/api/needs-you")
    assert code == 403 and denied["error"] == "forbidden"
    code, ledger = _json(base + "/api/needs-you?limit=25&token=" + token)
    assert code == 200 and ledger["total"] == 1 and not ledger["has_more"]
    item = ledger["items"][0]
    assert item["mission"] == "mission-000" and item["category"] == "authorization"
    assert item["data"]["nonce"] == "auth-exact-205"
    assert item["data"]["payload"]["operation"] == "publish listing"
    assert all(item["data"].get(field) for field in
               ("reason", "impact_summary", "approve_effect", "reject_effect"))


def test_needs_you_paginates_with_total_and_persisted_exact_approval_details(
        web_server, tmp_path):
    from harness import webapp
    from harness.inbox import InboxStore

    base, token, _ = web_server
    store = InboxStore(str(tmp_path / "live-inbox.db"))
    ids = []
    for index in range(3):
        payload = {"url": "https://example.test/%d" % index, "submit": True}
        item = store.add(
            "session-a", tool="browser_submit", call_id="call-%d" % index,
            title="Approve exact submission %d?" % index, body="submit listing %d" % index,
            reason="Publishing changes an external listing.",
            impact_summary="This will publish listing %d." % index,
            approve_effect="The exact listing payload will be submitted once.",
            reject_effect="The listing stays unchanged and the run seeks an alternative.",
            payload=payload)
        ids.append(item.id)
    webapp.Handler._inbox_open("session-a", store)
    try:
        code, first = _json(base + "/api/needs-you?limit=2&token=" + token)
        assert code == 200 and first["total"] == 3 and first["has_more"]
        assert len(first["items"]) == 2 and first["next_cursor"]
        # A newer sensitive request arriving between pages must not shift an offset and make the
        # remaining original item disappear. The opaque cursor is anchored to the last seen key.
        newer = store.add(
            "session-a", tool="browser_submit", call_id="call-new",
            title="A newer exact request", payload={"submit": True, "new": True})
        with store._lock:
            store.db.execute("UPDATE inbox_items SET created_at=? WHERE id=?",
                             (9_999_999_999, newer.id))
            store.db.commit()
        code, second = _json(
            base + "/api/needs-you?limit=2&cursor=" + first["next_cursor"] +
            "&token=" + token)
        assert code == 200 and second["total"] == 4 and not second["has_more"]
        assert len(second["items"]) == 1
        combined = first["items"] + second["items"]
        assert {item["data"]["id"] for item in combined} == set(ids)
        for item in combined:
            data = item["data"]
            assert data["nonce"].startswith("call-")
            assert data["payload"]["submit"] is True and len(data["payload_sha256"]) == 64
            assert data["actionable"] is True

        code, invalid = _json(base + "/api/needs-you?cursor=not-a-cursor&token=" + token)
        assert code == 400 and "cursor" in invalid["error"]
    finally:
        webapp.Handler._inbox_close("session-a")


def test_approval_resolution_cannot_cross_session_or_rebind_payload(tmp_path):
    from harness import webapp
    from harness.inbox import InboxStore, R_ALLOW

    store = InboxStore(str(tmp_path / "shared.db"))
    own = store.add("session-a", call_id="call-a", payload={"path": "a"})
    other = store.add("session-b", call_id="call-b", payload={"path": "b"})
    webapp.Handler._inbox_open("session-a", store)
    try:
        assert webapp.Handler._inbox_answer("session-a", other.id, R_ALLOW) is False
        assert store.get(other.id).pending
        assert webapp.Handler._inbox_answer("session-a", own.id, R_ALLOW) is True
        assert store.get(own.id).payload_json == '{"path":"a"}'
    finally:
        webapp.Handler._inbox_close("session-a")


def test_needs_you_cursor_does_not_skip_an_unseen_mission_when_it_updates(web_server):
    """Mission updated_at is mutable; the pagination anchor must not be."""
    from harness.jobs import QUEUED
    from harness.missionweb import MissionService

    base, token, state = web_server
    svc = MissionService(state_dir=str(state), decider=lambda *_: {}, stub=True)
    try:
        for index, created in enumerate((100, 200, 300)):
            mid = "mutable-%d" % index
            svc.store.create(mid, "Sensitive %d" % index, case={
                "pending_authorizations": [{
                    "id": "auth-%d" % index, "kind": "person",
                    "operation": "publish %d" % index, "blocking": True,
                }]})
            with svc.store._lock:
                svc.store.db.execute(
                    "UPDATE missions SET created_at=?,updated_at=? WHERE mission_id=?",
                    (created, created, mid))
                svc.store.db.commit()
    finally:
        svc.close()

    code, first = _json(base + "/api/needs-you?limit=2&token=" + token)
    assert code == 200 and first["total"] == 3 and first["has_more"]
    seen = {item["mission"] for item in first["items"]}
    unseen = ({"mutable-0", "mutable-1", "mutable-2"} - seen).pop()

    svc = MissionService(state_dir=str(state), decider=lambda *_: {}, stub=True)
    try:
        assert svc.store.patch_case(unseen, {"note": "changed after page one"},
                                    allowed_states=(QUEUED,))
    finally:
        svc.close()

    code, second = _json(
        base + "/api/needs-you?limit=2&cursor=" + first["next_cursor"] +
        "&token=" + token)
    assert code == 200 and [item["mission"] for item in second["items"]] == [unseen]
