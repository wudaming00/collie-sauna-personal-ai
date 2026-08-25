"""Authenticated Web operations/control-plane contracts."""
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest


@pytest.fixture
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
        server.shutdown(); server.server_close(); thread.join(timeout=3)


def _json(url, method="GET", body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_collie_work_identity_is_authenticated_and_can_provision_mailbox(
        web_server, monkeypatch):
    from harness import workidentity

    base, token, _ = web_server
    rows = [{"id": "collie_mail", "connected": True,
             "account": "rowan.owner@collie.run"},
            {"id": "google_voice", "connected": True,
             "account": "+16505551234"}]
    identity = {"principal": "collie", "name": "Rowan",
                "email": "rowan.owner@collie.run", "phone": "+16505551234",
                "status": "ready"}
    calls = []
    monkeypatch.setattr(workidentity, "public_connections", lambda state_dir=None: rows)
    monkeypatch.setattr(workidentity, "model_identity", lambda state_dir=None: identity)
    monkeypatch.setattr(
        workidentity, "provision_collie_mail",
        lambda name="", state_dir=None, relay="": calls.append((name, state_dir)) or rows[0])

    code, denied = _json(base + "/api/work-identities")
    assert code == 403 and denied["error"] == "forbidden"
    code, snapshot = _json(base + "/api/work-identities?token=" + token)
    assert code == 200 and snapshot == {"connections": rows, "identity": identity}
    code, response = _json(
        base + "/api/work-identities?token=" + token, "POST",
        {"connection": "collie_mail", "action": "provision", "name": "Rowan"})
    assert code == 200 and response["connection"] == rows[0]
    assert calls and calls[0][0] == "Rowan"


def test_account_registry_api_is_authenticated_secret_free_and_metadata_only(
        web_server, monkeypatch):
    from harness import workidentity

    base, token, _ = web_server
    monkeypatch.setattr(workidentity, "public_connections", lambda state_dir=None: [
        {"id": "collie_mail", "connected": True,
         "account": "rowan.owner@collie.run"},
        {"id": "google_voice", "connected": True,
         "account": "+16505551234", "account_masked": "•••-•••-1234",
         "ownership": "user_owned_assigned_to_collie"},
    ])
    monkeypatch.setattr(workidentity, "model_identity", lambda state_dir=None: {
        "principal": "collie", "name": "Rowan", "phone": "+16505551234",
        "status": "ready"})

    code, denied = _json(base + "/api/accounts")
    assert code == 403 and denied["error"] == "forbidden"
    code, initial = _json(base + "/api/accounts?token=" + token)
    assert code == 200 and initial["accounts"] == []
    assert initial["vault"]["plaintext_fallback"] is False
    assert initial["communications"]["google_voice"]["assigned"] is True
    assert initial["communications"]["google_voice"]["sms"]["collie_dispatch_configured"] is False
    assert initial["communications"]["google_voice"]["sms"]["automation_permitted"] is False
    assert initial["communications"]["google_voice"]["sms"]["runtime"] == "draft_then_user_send"
    assert initial["communications"]["google_voice"]["calls"]["collie_dispatch_configured"] is False
    assert initial["communications"]["google_voice"]["calls"]["automation_permitted"] is False
    assert initial["communications"]["google_voice"]["calls"]["runtime"] == "manual_handoff_only"
    assert initial["communications"]["voice_synthesis"]["configured"] is False
    adapters = initial["communications"]["programmable_telephony"]["adapters"]
    assert adapters[0]["capabilities"] == {
        "outbound_calls": True, "inbound_calls": False, "sms": False}
    assert adapters[1]["capabilities"] == {
        "outbound_calls": True, "inbound_calls": True,
        "sms": "requires_sender_registration"}

    planned_body = {
        "action": "plan", "origin": "https://service.example.test/path",
        "username": "rowan.owner@collie.run", "ownership": "collie_owned_work_identity",
        "legal_principal": "owner-authorized-collie", "scopes": ["profile.read"],
        "factor_classes": ["email_otp"], "idempotency_key": "service-example-rowan",
    }
    code, planned = _json(base + "/api/accounts?token=" + token, "POST", planned_body)
    assert code == 200 and planned["account"]["status"] == "planned"
    assert planned["credentials_created"] is False
    account_id = planned["account"]["account_id"]

    code, listing = _json(base + "/api/accounts?token=" + token)
    assert code == 200 and listing["accounts"][0]["account_id"] == account_id
    wire = json.dumps(listing)
    assert "secret_refs" not in wire and "cv1_" not in wire
    assert "password_value" not in wire and "totp_secret" not in wire

    code, refused = _json(base + "/api/accounts?token=" + token, "POST", {
        "action": "rotate", "account_id": account_id})
    assert code == 400 and "remote-coordinated" in refused["error"]
    code, refused_secret = _json(base + "/api/accounts?token=" + token, "POST", {
        **planned_body, "idempotency_key": "different-plan", "password": "never-store-this"})
    assert code == 400 and "unsupported account-plan field" in refused_secret["error"]

    code, cancelled = _json(base + "/api/accounts?token=" + token, "POST", {
        "action": "cancel_plan", "account_id": account_id})
    assert code == 200 and cancelled["account"]["status"] == "retired"
    assert cancelled["credentials_deleted"] is False


def test_activity_health_and_hooks_are_authenticated_and_content_safe(
        web_server, monkeypatch):
    from harness import controlplane

    base, token, state = web_server
    secret = "PRIVATE-PROMPT-CONTENT"
    monkeypatch.setattr(controlplane, "activity", lambda *_args, **_kwargs: {
        "at": 1, "sessions": [{"session_id": "s1", "state": "external_action",
                                "recovery_required": True, "detail": {"args": secret}}],
        "missions": [{"mission_id": "m1", "state": "running", "goal": secret,
                      "result": secret, "lane": "mission"}],
        "task_runs": [{"run_id": "r1", "role": "reader", "status": "running",
                       "task": secret, "result": secret, "leash": {"secret": secret}}],
        "automations": [{"execution_id": "e1", "automation_id": "a1", "state": "pending",
                         "request_json": secret, "result_json": secret}],
        "notifications": [{"notification_id": 1, "run_id": "r1", "kind": "progress",
                           "state": "queued", "payload": {"text": secret}}],
        "errors": {"missions": secret}})
    monkeypatch.setattr(controlplane, "health", lambda *_args, **_kwargs: {
        "ok": True, "status": "ok", "at": 1,
        "workers": {"web": {"state": "running", "fresh": True, "detail": {"task": secret}}},
        "heartbeats": {"worker:web": {"state": "running", "fresh": True,
                                        "detail": {"prompt": secret}}},
        "services": {"web": {"ok": True, "detail": secret}},
        "credentials": [{"name": "codex-oauth", "state": "ok", "token": secret}],
        "queues": {"notifications": {"pending": 1, "payload": secret}},
        "supervisor": {"installed": False},
        "work": {"interactive_active": 1, "missions_active": 1, "task_runs_active": 1,
        "automations_active": 1, "recovery_required": []},
        "activity_errors": {"task_runs": secret}})

    for path in ("/api/activity", "/api/healthz", "/api/hooks"):
        code, _ = _json(base + path)
        assert code == 403
    code, activity = _json(base + "/api/activity?token=" + token)
    assert code == 200 and activity["task_runs"][0]["role"] == "reader"
    assert secret not in json.dumps(activity)
    code, health = _json(base + "/api/healthz?token=" + token)
    assert code == 200 and health["workers"]["web"]["fresh"] is True
    assert secret not in json.dumps(health)

    # Hook status is inspect-only. Unreviewed exact bytes stay pending.
    hooks = state / "hooks.json"
    hooks.write_text(json.dumps({"hooks": {"Stop": []}}), encoding="utf-8")
    code, status = _json(base + "/api/hooks?token=" + token)
    assert code == 200 and status["trust_changes_allowed"] is False
    assert status["pending"] and status["pending"][0]["sha256"]


def test_pending_approvals_snapshot_is_authenticated_and_only_lists_live_items(
        web_server, tmp_path):
    from harness import webapp
    from harness.inbox import InboxStore, R_DENY

    base, token, _ = web_server
    store = InboxStore(str(tmp_path / "live-inbox.db"))
    first = store.add("session-a", tool="browser_click", title="Publish release?",
                      body="button: Publish v1.4.0", target="https://example.test/release",
                      risk="external write", rule_offer="")
    resolved = store.add("session-a", tool="browser_read", title="Read status?")
    store.resolve(resolved.id, R_DENY)
    webapp.Handler._inbox_open("session-a", store)
    try:
        code, denied = _json(base + "/api/approvals")
        assert code == 403 and denied["error"] == "forbidden"

        code, snapshot = _json(base + "/api/approvals?token=" + token)
        assert code == 200
        approval = snapshot["approvals"][0]
        assert {key: approval[key] for key in (
            "id", "session", "tool", "body", "title", "target", "risk",
            "rule_offer", "state",
        )} == {
            "id": first.id, "session": "session-a", "tool": "browser_click",
            "body": "button: Publish v1.4.0", "title": "Publish release?",
            "target": "https://example.test/release", "risk": "external write",
            "rule_offer": "", "state": "pending",
        }
        assert approval["payload"] == {} and len(approval["payload_sha256"]) == 64
    finally:
        webapp.Handler._inbox_close("session-a")


def test_library_snapshot_and_lifecycle_actions_are_authenticated_and_explicit(
        web_server, monkeypatch):
    from harness.extensions import ExtensionStore

    base, token, _ = web_server
    row = {
        "id": "example.release", "name": "Release helper", "publisher": "Example",
        "description": "Reviewable release assets", "enabled": False,
        "active_version": "", "versions": [{
            "version": "1.2.0", "digest": "a" * 64, "scope_hash": "b" * 64,
            "trust_state": "unreviewed", "approved": False, "revoked": False,
            "integrity_ok": True,
        }],
        "permissions": {"network": ["api.example.test"], "host_hooks": False},
        "components": {"skills": 1, "hooks": 0, "connections": 1,
                       "templates": 0, "assets": 0},
    }
    calls = []
    monkeypatch.setattr(ExtensionStore, "list", lambda self: [row])
    monkeypatch.setattr(ExtensionStore, "enable", lambda self, ext_id, version="", approve=False:
                        calls.append(("enable", ext_id, version, approve)) or
                        dict(row, enabled=True, active_version=version or "1.2.0"))
    monkeypatch.setattr(ExtensionStore, "disable", lambda self, ext_id:
                        calls.append(("disable", ext_id)) or row)
    monkeypatch.setattr(ExtensionStore, "rollback", lambda self, ext_id, approve=False:
                        calls.append(("rollback", ext_id, approve)) or row)
    monkeypatch.setattr(ExtensionStore, "uninstall",
                        lambda self, ext_id, version="", force=False:
                        calls.append(("uninstall", ext_id, version, force)) or
                        {"id": ext_id, "removed_versions": [version or "1.2.0"]})

    code, denied = _json(base + "/api/library")
    assert code == 403 and denied["error"] == "forbidden"
    code, listing = _json(base + "/api/library?token=" + token)
    assert code == 200 and listing == {"extensions": [row]}

    code, denied = _json(base + "/api/library/action", "POST", {
        "action": "enable", "id": row["id"], "version": "1.2.0", "approve": True})
    assert code == 403 and denied["error"] == "forbidden"
    code, invalid = _json(base + "/api/library/action?token=" + token, "POST", {
        "action": "enable", "id": row["id"], "approve": "yes"})
    assert code == 400 and "approve" in invalid["error"]

    code, enabled = _json(base + "/api/library/action?token=" + token, "POST", {
        "action": "enable", "id": row["id"], "version": "1.2.0", "approve": True})
    assert code == 200 and enabled["extension"]["enabled"] is True
    code, removed = _json(base + "/api/library/action?token=" + token, "POST", {
        "action": "uninstall", "id": row["id"], "version": "1.2.0"})
    assert code == 200 and removed["extension"]["removed_versions"] == ["1.2.0"]
    assert calls == [
        ("enable", "example.release", "1.2.0", True),
        ("uninstall", "example.release", "1.2.0", False),
    ]


def test_vscode_embed_headers_require_the_exact_high_entropy_process_token(
        web_server, monkeypatch):
    base, _, _ = web_server
    secret = "vscode-test-" + "a" * 52
    monkeypatch.setenv("COLLIE_VSCODE_EMBED_TOKEN", secret)

    def headers(path):
        with urllib.request.urlopen(base + path, timeout=8) as response:
            response.read()
            return response.headers

    normal = headers("/")
    assert normal.get("X-Frame-Options") == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in normal.get("Content-Security-Policy", "")

    wrong = headers("/?vscode_embed=wrong")
    assert wrong.get("X-Frame-Options") == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in wrong.get("Content-Security-Policy", "")

    embedded = headers("/?vscode_embed=" + secret)
    assert embedded.get("X-Frame-Options") is None
    csp = embedded.get("Content-Security-Policy", "")
    assert "frame-ancestors vscode-webview: https://*.vscode-cdn.net" in csp
    assert "frame-ancestors 'self'" not in csp

    # The token grants no general header bypass: every non-index document remains same-origin.
    remote = headers("/remote?vscode_embed=" + secret)
    assert remote.get("X-Frame-Options") == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in remote.get("Content-Security-Policy", "")

    monkeypatch.setenv("COLLIE_VSCODE_EMBED_TOKEN", "short")
    short = headers("/?vscode_embed=short")
    assert short.get("X-Frame-Options") == "SAMEORIGIN"


def test_recovery_list_detail_and_explicit_reconcile(web_server):
    from harness import sessions

    base, token, state = web_server
    sessions.checkpoint("uncertain", [{"role": "user", "content": "private action"}],
                        run_id="run-1", state="external_action",
                        detail={"tool_name": "publish", "tool_call_id": "call-1",
                                "args": {"secret": "not-on-wire"}})
    code, _ = _json(base + "/api/recovery")
    assert code == 403
    code, listing = _json(base + "/api/recovery?token=" + token)
    assert code == 200 and listing["sessions"][0]["session_id"] == "uncertain"
    assert "not-on-wire" not in json.dumps(listing)
    code, detail = _json(base + "/api/recovery/uncertain?token=" + token)
    assert code == 200 and detail["recovery_required"] is True

    code, refused = _json(base + "/api/recovery/reconcile?token=" + token, "POST", {
        "session": "uncertain", "resolution": "not_fired"})
    assert code == 400 and "confirmed" in refused["error"]
    code, _ = _json(base + "/api/recovery/reconcile", "POST", {
        "session": "uncertain", "resolution": "not_fired", "confirmed": True})
    assert code == 403
    code, reconciled = _json(base + "/api/recovery/reconcile?token=" + token, "POST", {
        "session": "uncertain", "resolution": "not_fired", "confirmed": True})
    assert code == 200 and reconciled["state"]["auto_resumable"] is True
    assert sessions.recovery_state("uncertain")["state"] == "turn_boundary"


def test_authenticated_automation_webhook_only_persists_allowlisted_fields(web_server):
    from harness.automations import AutomationStore

    base, token, state = web_server
    with AutomationStore(str(state / "automations.db")) as store:
        store.upsert({
            "automation_id": "deploy-hook", "task": "check deployment",
            "trigger": {"provider": "webhook", "persist_fields": ["event", "project"]},
            "workspace": {"mode": "isolated"},
            "permissions": {"webhook_ingest": True},
        })
    payload = {"automation_id": "deploy-hook", "delivery_id": "delivery-1",
               "payload": {"event": "deploy", "project": "collie", "secret": "DROP-ME"}}
    code, _ = _json(base + "/api/automation/webhook", "POST", payload)
    assert code == 403
    code, accepted = _json(base + "/api/automation/webhook?token=" + token, "POST", payload)
    assert code == 200 and accepted["accepted"] is True
    with AutomationStore(str(state / "automations.db")) as store:
        persisted = store.executions()[0]["request_json"]
    assert "collie" in persisted and "DROP-ME" not in persisted


def test_specialist_tree_inspect_steer_cancel_and_no_task_leak(web_server, tmp_path):
    from harness.missionweb import MissionService

    base, token, state = web_server
    repo = tmp_path / "repo"; repo.mkdir()
    svc = MissionService(state_dir=str(state), decider=lambda *_: {}, stub=True)
    mission = svc.start("PRIVATE-MISSION-GOAL", may=["research"])
    root = svc.create_run_tree(mission["mission_id"], [
        {"kind": "file", "id": str(repo), "mode": "write"}], workspace=str(repo))["root"]
    child = svc.spawn_specialist(mission["mission_id"], "reader", "PRIVATE-SPECIALIST-TASK",
        resources=[{"kind": "file", "id": str(repo / "a.py"), "mode": "read"}],
        workspace=str(repo))
    svc.close()

    code, _ = _json(base + "/api/mission/run-tree?id=" + mission["mission_id"])
    assert code == 403
    code, tree = _json(base + "/api/mission/run-tree?id=" + mission["mission_id"] + "&token=" + token)
    assert code == 200 and tree["tree"]["root"]["run_id"] == root["run_id"]
    code, specialist = _json(base + "/api/mission/specialist?run_id=" + child["run_id"] + "&token=" + token)
    assert code == 200 and specialist["run"]["role"] == "reader"
    assert "PRIVATE-" not in json.dumps(tree) + json.dumps(specialist)

    code, denied = _json(base + "/api/mission/specialist/steer", "POST", {
        "run_id": child["run_id"], "text": "focus"})
    assert code == 403
    code, steered = _json(base + "/api/mission/specialist/steer?token=" + token, "POST", {
        "run_id": child["run_id"], "text": "focus"})
    assert code == 200 and steered["queued"] is True
    code, cancelled = _json(base + "/api/mission/specialist/cancel?token=" + token, "POST", {
        "run_id": child["run_id"]})
    assert code == 200 and cancelled["run"]["status"] in ("cancel_requested", "cancelled")


def test_model_picker_auto_unpins_provider_and_model_globally(web_server, monkeypatch, tmp_path):
    from harness import settings

    base, token, _ = web_server
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "_PATH", str(settings_path))
    monkeypatch.setattr(settings, "_cache", {"mtime": None, "data": {}})
    monkeypatch.setenv("COLLIE_PROVIDER", "codex-oauth")
    monkeypatch.delenv("COLLIE_MODEL", raising=False)
    settings.update({"PROVIDER": "codex-oauth", "MODEL": "gpt-5.6-sol"})

    code, result = _json(base + "/api/model?token=" + token, "POST", {"auto": True})
    assert code == 200 and result == {
        "ok": True, "provider": "auto", "model": "", "auto": True}
    saved = json.loads(settings_path.read_text("utf-8"))
    assert saved["PROVIDER"] == "auto" and saved.get("MODEL", "") == ""


def test_auto_route_resolves_a_quick_concrete_brain_without_logging_prompt(
        web_server, monkeypatch, tmp_path):
    from harness import brain_router, catalog, providers, settings

    base, token, state = web_server
    settings_path = tmp_path / "route-settings.json"
    brain_path = state / "route-brain.db"
    monkeypatch.setattr(settings, "_PATH", str(settings_path))
    monkeypatch.setattr(settings, "_cache", {"mtime": None, "data": {}})
    monkeypatch.setattr(settings, "_HARD_ENV", set())
    monkeypatch.setenv("COLLIE_BRAIN_DB", str(brain_path))
    monkeypatch.delenv("COLLIE_PROVIDER", raising=False)
    monkeypatch.delenv("COLLIE_MODEL", raising=False)
    settings.update({"PROVIDER": "auto", "MODEL": ""})

    entries = [
        {"provider": "codex-oauth", "model": "gpt-5.6-sol",
         "tags": ["coding", "frontier"], "kind": "subscription", "auth": "ok"},
        {"provider": "codex-oauth", "model": "gpt-5.6-luna",
         "tags": ["coding", "fast"], "kind": "subscription", "auth": "ok"},
        {"provider": "claude-agent-sdk", "model": "claude-opus-4-8",
         "tags": ["coding", "frontier"], "kind": "subscription", "auth": "ok"},
    ]
    monkeypatch.setattr(catalog, "list_entries", lambda discover_live=False: entries)
    calls = []

    class ClassifierProvider:
        name = "codex-oauth"
        model = "gpt-5.6-luna"

        def complete(self, _system, messages, _schemas):
            assert messages == [{"role": "user", "content": secret_prompt}]
            return providers.Completion(
                text='{"kind":"chat","goal":"reply","confidence":0.99}',
                stop_reason="end_turn")

    def make_provider(name, model=None, effort=None, **_kwargs):
        calls.append((name, model, effort))
        return ClassifierProvider()

    monkeypatch.setattr(providers, "make_provider", make_provider)
    # Force this test's process-local store to follow the isolated path above.
    monkeypatch.setattr(brain_router, "_default_store", None)
    monkeypatch.setattr(brain_router, "_default_store_path", "")
    secret_prompt = "voice question customer-secret-123"

    code, routed = _json(
        base + "/api/route?token=" + token, "POST", {"text": secret_prompt})

    assert code == 200 and routed["kind"] == "chat"
    assert calls == [("codex-oauth", "gpt-5.6-luna", "low")]
    with sqlite3.connect(brain_path) as db:
        assert db.execute("SELECT COUNT(*) FROM brain_decisions").fetchone()[0] == 0
    assert secret_prompt.encode() not in brain_path.read_bytes()


def test_auto_route_without_an_authenticated_brain_is_honest_503(
        web_server, monkeypatch, tmp_path):
    from harness import brain_router, catalog, providers, settings

    base, token, state = web_server
    settings_path = tmp_path / "unavailable-settings.json"
    brain_path = state / "unavailable-brain.db"
    monkeypatch.setattr(settings, "_PATH", str(settings_path))
    monkeypatch.setattr(settings, "_cache", {"mtime": None, "data": {}})
    monkeypatch.setattr(settings, "_HARD_ENV", set())
    monkeypatch.setenv("COLLIE_BRAIN_DB", str(brain_path))
    monkeypatch.delenv("COLLIE_PROVIDER", raising=False)
    monkeypatch.delenv("COLLIE_MODEL", raising=False)
    settings.update({"PROVIDER": "auto", "MODEL": ""})
    monkeypatch.setattr(catalog, "list_entries", lambda discover_live=False: [
        {"provider": "codex-oauth", "model": "gpt-5.6-luna",
         "tags": ["fast"], "kind": "subscription", "auth": "not-logged-in"},
    ])
    monkeypatch.setattr(
        providers, "make_provider",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("an unavailable route must not construct a provider")))
    monkeypatch.setattr(brain_router, "_default_store", None)
    monkeypatch.setattr(brain_router, "_default_store_path", "")
    secret_prompt = "unavailable-secret-456"

    code, routed = _json(
        base + "/api/route?token=" + token, "POST", {"text": secret_prompt})

    assert code == 503 and routed["error"] == "model_unavailable"
    assert "no currently authenticated model" in routed["detail"]
    assert secret_prompt.encode() not in brain_path.read_bytes()


def test_route_keeps_a_concrete_provider_pin_outside_brain_auto(
        web_server, monkeypatch, tmp_path):
    from harness import catalog, providers, settings

    base, token, _ = web_server
    settings_path = tmp_path / "pinned-route-settings.json"
    monkeypatch.setattr(settings, "_PATH", str(settings_path))
    monkeypatch.setattr(settings, "_cache", {"mtime": None, "data": {}})
    monkeypatch.setattr(settings, "_HARD_ENV", set())
    monkeypatch.delenv("COLLIE_PROVIDER", raising=False)
    monkeypatch.delenv("COLLIE_MODEL", raising=False)
    monkeypatch.delenv("COLLIE_ROUTER_MODEL", raising=False)
    settings.update({"PROVIDER": "codex-oauth", "MODEL": ""})
    monkeypatch.setattr(
        catalog, "list_entries",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("a concrete provider pin must not inspect Auto candidates")))
    calls = []

    class PinnedClassifierProvider:
        name = "codex-oauth"
        model = "gpt-5.6-terra"

        def complete(self, _system, _messages, _schemas):
            return providers.Completion(
                text='{"kind":"chat","goal":"reply","confidence":0.99}',
                stop_reason="end_turn")

    def make_provider(name, model=None, effort=None, **_kwargs):
        calls.append((name, model, effort))
        return PinnedClassifierProvider()

    monkeypatch.setattr(providers, "make_provider", make_provider)

    code, routed = _json(
        base + "/api/route?token=" + token, "POST", {"text": "hello"})

    assert code == 200 and routed["kind"] == "chat"
    assert calls == [
        ("codex-oauth", None, None),
        ("codex-oauth", None, "low"),
    ]


def test_activity_ui_and_auto_model_contracts():
    page = (Path(__file__).parents[1] / "harness" / "webui" / "index.html").read_text("utf-8")
    for value in ("activityPanel", "/api/activity", "/api/healthz", "/api/hooks",
                  "/api/recovery/reconcile", "/api/mission/specialist/steer",
                  "/api/mission/specialist/cancel"):
        assert value in page
    assert "confirmed: true" in page and "PRIVATE" not in page
    assert "Auto — Collie chooses per task" in page
    # Provider-first routing no longer posts from the old picker entry directly; Auto is still an
    # explicit payload selected by the resolved provider route.
    assert 'provider === "auto" ? { auto:true }' in page
    assert "body: JSON.stringify({ auto: true })" in page
