"""HTTP regression for the explicit Mission control surface."""

import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

from harness import sessions, settings, webapp
from harness.mission import MissionStore


def _allow_claude_subscription(provider, *, account_evidence=None, environ=None,
                               model="", require_direct_probe=True):
    if provider != "claude-agent-sdk" or account_evidence is not None:
        raise RuntimeError("unreviewed subscription route")
    assert isinstance(environ, dict)
    return {
        "format": "collie-subscription-guard-v1",
        "schema_version": 1,
        "provider": provider,
        "verdict": "allow",
    }


def _request(url, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_mission_api_is_authed_persistent_and_manageable(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    # Creation and management are model-free. The provider is intentionally absent
    # to prove these endpoints do not initialize one just to read/update state.
    monkeypatch.delenv("COLLIE_PROVIDER", raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = "http://127.0.0.1:%d" % server.server_address[1]
    token = "?token=" + webapp.TOKEN
    try:
        code, _ = _request(root + "/api/missions")
        assert code == 403

        code, created = _request(
            root + "/api/mission" + token, "POST", {"goal": "watch replies"})
        assert code == 201 and created["state"] == "queued"
        mid = created["mission_id"]

        code, bad_mode = _request(
            root + "/api/mission" + token, "POST",
            {"goal": "do not coerce strings into authority", "autonomous": "false"})
        assert code == 400 and "boolean" in bad_mode["error"]

        code, status = _request(root + "/api/mission?id=" + mid + "&token=" + webapp.TOKEN)
        assert code == 200 and status["mission_id"] == mid
        assert set(status["controls"]) == {"run", "pause", "cancel"}
        assert status["report"]["format_version"] == 1

        for endpoint, bad_body, field in (
                ("pause", {"id": 1}, "id"),
                ("confirm", {"id": mid, "nonce": ["bad"]}, "nonce"),
                ("continue", {"id": mid, "note": {"bad": True}}, "note"),
                ("reconcile", {"id": mid, "note": []}, "note")):
            code, invalid = _request(
                root + "/api/mission/" + endpoint + token, "POST", bad_body)
            assert code == 400 and field in invalid["error"]

        code, _ = _request(root + "/api/mission/report?id=" + mid)
        assert code == 403
        code, report = _request(
            root + "/api/mission/report?id=" + mid + "&token=" + webapp.TOKEN)
        assert code == 200 and report["mission_id"] == mid
        assert "case" not in report and "markdown" in report

        code, paused = _request(
            root + "/api/mission/pause" + token, "POST", {"id": mid})
        assert code == 200 and paused["state"] == "paused"
        code, resumed = _request(
            root + "/api/mission/resume" + token, "POST", {"id": mid})
        assert code == 200 and resumed["state"] == "queued"
        code, cancelled = _request(
            root + "/api/mission/cancel" + token, "POST", {"id": mid})
        assert code == 200 and cancelled["state"] == "cancelled"

        code, listed = _request(root + "/api/missions" + token)
        assert code == 200 and listed["missions"][0]["mission_id"] == mid

        code, failed = _request(
            root + "/api/mission" + token, "POST", {"goal": "retry a failed mission safely"})
        failed_id = failed["mission_id"]
        store = MissionStore(str(tmp_path / "jobs.db"))
        store.set_state(failed_id, "failed", "synthetic failure")
        store.close()
        code, retried = _request(
            root + "/api/mission/retry" + token, "POST",
            {"id": failed_id, "note": "retry only unfinished work"})
        assert code == 200 and retried["mission_id"] != failed_id
        assert retried["state"] == "queued"
        code, retried_again = _request(
            root + "/api/mission/retry" + token, "POST",
            {"id": failed_id, "note": "duplicate delivery"})
        assert code == 200 and retried_again["mission_id"] == retried["mission_id"]

        code, handed_off = _request(
            root + "/api/mission" + token, "POST",
            {"goal": "return accepted work exactly once"})
        handed_off_id = handed_off["mission_id"]
        store = MissionStore(str(tmp_path / "jobs.db"))
        store.set_state(handed_off_id, "done_accepted", "handed off")
        store.close()
        code, continued = _request(
            root + "/api/mission/continue" + token, "POST",
            {"id": handed_off_id, "note": "continue remaining work"})
        assert code == 200 and continued["mission_id"] != handed_off_id
        code, continued_again = _request(
            root + "/api/mission/continue" + token, "POST",
            {"id": handed_off_id, "note": "duplicate delivery"})
        assert code == 200 and continued_again["mission_id"] == continued["mission_id"]

        code, uncertain = _request(
            root + "/api/mission" + token, "POST", {"goal": "uncertain external action"})
        umid = uncertain["mission_id"]
        store = MissionStore(str(tmp_path / "jobs.db"))
        assert store.claim_run(umid, lease_s=-1)
        store.close()
        code, ticked = _request(root + "/api/mission/tick" + token, "POST", {})
        assert code == 200 and ticked["recovered"] == 1
        code, recovery = _request(
            root + "/api/mission?id=" + umid + "&token=" + webapp.TOKEN)
        assert recovery["state"] == "recovery_required"
        assert set(recovery["controls"]) == {"reconcile", "cancel"}
        code, refused = _request(
            root + "/api/mission/continue" + token, "POST", {"id": umid})
        assert code == 409 and refused["state"] == "recovery_required"
        code, reconciled = _request(
            root + "/api/mission/reconcile" + token, "POST",
            {"id": umid, "note": "inspected target and receipts"})
        assert code == 200 and reconciled["state"] == "queued"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_web_ui_keeps_mission_out_of_the_model_router():
    html = open(webapp.INDEX_HTML, encoding="utf-8").read()
    # Match the composer entry point without pinning its signature: the capsule redesign gave
    # send() a dispatch-id parameter, and this contract is about ORDER inside it, not its arity.
    send_pos = re.search(r"function send\s*\([^)]*\)\s*\{", html).start()
    command_pos = html.index("handleMissionCommand(q)", send_pos)
    steer_pos = html.index("if (running)", send_pos)
    assert command_pos < steer_pos, "mission control must be intercepted before steering"
    assert 'd.kind === "mission"' not in html
    assert 'missionPost("/api/mission/cancel"' in html
    assert 'missionPost("/api/mission/pause"' in html
    assert 'missionPost("/api/mission/reconcile"' in html
    assert 'missionPost("/api/mission/retry"' in html
    assert 't("Progress report")' in html
    assert 'missionCopyReport(report, copyReport)' in html
    assert 'missionDownloadReport(report)' in html
    assert 'if (action === "report") { showMission(mid, true); return true; }' in html
    handler_pos = html.index("function handleMissionCommand")
    malformed_guard = html.index('if (/^start$/i.test(raw))', handler_pos)
    start_call = html.index("_startMissionCard(goal, autonomous, bounds)", malformed_guard)
    assert malformed_guard < start_call
    assert 'if (/^(?:list|ls|help)\\s+/i.test(raw))' in html


def test_thread_mutations_require_authed_post_json(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    sid = "thread-api-contract"
    sessions.save(sid, [{"role": "user", "content": "keep me safe"}])
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = "http://127.0.0.1:%d" % server.server_address[1]
    token = "?token=" + webapp.TOKEN
    try:
        code, _ = _request(root + "/api/rename/" + sid + token)
        assert code == 405
        assert sessions.load(sid)["messages"][0]["content"] == "keep me safe"

        code, _ = _request(
            root + "/api/thread/rename", "POST",
            {"session": sid, "title": "must not apply"})
        assert code == 403
        code, renamed = _request(
            root + "/api/thread/rename" + token, "POST",
            {"session": sid, "title": "Reviewed title"})
        assert code == 200 and renamed["ok"] is True
        assert sessions.recent()[0]["title"] == "Reviewed title"

        code, bad = _request(
            root + "/api/thread/delete" + token, "POST",
            {"session": "../../outside"})
        assert code == 200 and bad["ok"] is False
        code, deleted = _request(
            root + "/api/thread/delete" + token, "POST", {"session": sid})
        assert code == 200 and deleted["ok"] is True and sessions.load(sid) is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_mission_index_http_pagination_is_bounded(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    store = MissionStore(str(tmp_path / "jobs.db"))
    for i in range(215):
        store.create(
            "msn_page_%03d" % i, "bounded history %03d" % i,
            leash={"may": ["research"]}, case={})
    store.close()
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = "http://127.0.0.1:%d" % server.server_address[1]
    auth = "&token=" + webapp.TOKEN
    try:
        code, first = _request(root + "/api/missions?limit=40" + auth)
        assert code == 200 and len(first["missions"]) == 40
        assert first["has_more"] is True and first["next_cursor"]
        cursor = urllib.parse.quote(first["next_cursor"], safe="")
        code, second = _request(
            root + "/api/missions?limit=40&before=" + cursor + auth)
        assert code == 200 and len(second["missions"]) == 40
        assert not ({row["mission_id"] for row in first["missions"]} &
                    {row["mission_id"] for row in second["missions"]})
        code, invalid = _request(
            root + "/api/missions?limit=40&before=broken" + auth)
        assert code == 400 and "cursor" in invalid["error"]
        code, invalid_limit = _request(
            root + "/api/missions?limit=all" + auth)
        assert code == 400 and "limit" in invalid_limit["error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_mission_api_validates_and_atomically_binds_overnight_code_profile(
        monkeypatch, tmp_path):
    state = tmp_path / "state"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("COLLIE_STATE_DIR", str(state))
    monkeypatch.setenv("COLLIE_PROVIDER", "claude-agent-sdk")
    monkeypatch.setenv("COLLIE_MODEL", "claude-opus-4-8")
    monkeypatch.setattr(
        settings, "_HARD_ENV", settings._HARD_ENV | {"COLLIE_PROVIDER", "COLLIE_MODEL"})
    monkeypatch.setattr(
        "harness.subscription_guard.check_subscription_guard",
        _allow_claude_subscription)
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = "http://127.0.0.1:%d" % server.server_address[1]
    token = "?token=" + webapp.TOKEN
    try:
        code, invalid = _request(
            root + "/api/mission" + token, "POST",
            {"goal": "must not persist", "code": True, "overnight": True,
             "workspace": str(tmp_path / "missing"), "no_paid_overage": True})
        assert code == 400 and "does not exist" in invalid["error"]
        code, listed = _request(root + "/api/missions" + token)
        assert code == 200 and listed["missions"] == []

        code, created = _request(
            root + "/api/mission" + token, "POST",
            {"goal": "make the suite green", "code": True, "overnight": True,
             "workspace": str(repo), "verify_command": "python -m pytest -q",
             "provider": "claude-agent-sdk", "model": "claude-opus-4-8",
             "no_paid_overage": True})
        assert code == 201 and created["state"] == "queued"
        assert created["case"]["_isolated_workspace"] == str(repo.resolve())
        assert created["case"]["execution_profile"]["provider"] == "claude-agent-sdk"
        assert created["case"]["execution_profile"]["model"] == "claude-opus-4-8"
        assert created["case"]["execution_profile"]["subscription_only"] is True

        code, invalid_type = _request(
            root + "/api/mission" + token, "POST",
            {"goal": "reject coercion", "code": "true"})
        assert code == 400 and "boolean" in invalid_type["error"]
        code, invalid_path_type = _request(
            root + "/api/mission" + token, "POST",
            {"goal": "reject empty collection coercion", "code": True,
             "workspace": []})
        assert code == 400 and "workspace must be a string" in invalid_path_type["error"]
        code, invalid_provider_type = _request(
            root + "/api/mission" + token, "POST",
            {"goal": "reject provider coercion", "provider": []})
        assert code == 400 and "provider must be a string" in invalid_provider_type["error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_mission_api_refuses_metered_overnight_fallback(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COLLIE_PROVIDER", "anthropic")
    monkeypatch.setenv("COLLIE_MODEL", "claude-opus-4-8")
    monkeypatch.setattr(
        settings, "_HARD_ENV", settings._HARD_ENV | {"COLLIE_PROVIDER", "COLLIE_MODEL"})
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = "http://127.0.0.1:%d" % server.server_address[1]
    token = "?token=" + webapp.TOKEN
    try:
        code, refused = _request(
            root + "/api/mission" + token, "POST",
            {"goal": "never charge API usage", "code": True, "overnight": True,
             "workspace": str(repo), "no_paid_overage": True})
        assert code == 400 and "official Claude Agent SDK" in refused["error"]
        code, listed = _request(root + "/api/missions" + token)
        assert code == 200 and listed["missions"] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
