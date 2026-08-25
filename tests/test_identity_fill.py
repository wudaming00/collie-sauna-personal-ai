import json
from types import SimpleNamespace


def _form_actuator(label):
    from harness.webact import FakeActuator

    class IdentityForm(FakeActuator):
        def snapshot(self):
            return {"url": "https://accounts.example.test/signup",
                    "snapshot": '[e9] textbox "%s"' % label}

    return IdentityForm()


def test_email_identity_is_resolved_inside_executor_and_never_returned():
    from harness.primitives import _real_identity_fill

    actuator = _form_actuator("Work email")
    seen = []

    def reader(field):
        seen.append(field)
        return "rowan.team@collie.run", {
            "source": "collie_mail", "account": "r•••@collie.run"}

    result = _real_identity_fill(actuator, reader)(SimpleNamespace(
        args={"field": "email", "label": "Work email"}, job_id="mission-1"))
    assert result["filled"] and result["field"] == "email"
    assert result["case"] == {"identity_field_filled": "email"}
    assert seen == ["email"]
    assert actuator.calls[-1] == ("type_ref", "e9", "[sensitive]", False)
    assert "rowan.team@collie.run" not in json.dumps(result)


def test_phone_identity_never_crosses_the_host_bridge_result():
    from harness.primitives import _real_identity_fill

    actuator = _form_actuator("Phone number")
    result = _real_identity_fill(actuator)(SimpleNamespace(
        args={"field": "phone", "label": "Phone number"}, job_id="mission-2"))
    assert result["filled"] and result["source"] == "google_voice"
    assert actuator.calls[-1] == ("fill_work_identity", "e9", "phone")
    encoded = json.dumps(result)
    assert "1234" in encoded
    assert "5551231234" not in encoded


def test_identity_field_resolution_fails_closed_when_ambiguous():
    from harness.primitives import _real_identity_fill
    from harness.webact import FakeActuator

    class Ambiguous(FakeActuator):
        def snapshot(self):
            return {"snapshot": '[e1] textbox "Email"\n[e2] textbox "Recovery email"'}

    called = []
    result = _real_identity_fill(Ambiguous(), lambda field: called.append(field))(SimpleNamespace(
        args={"field": "email"}, job_id="mission-3"))
    assert result == {"filled": False, "error": "work-identity field is missing or ambiguous"}
    assert called == []


def test_registered_identity_fill_takes_only_an_opaque_field_kind():
    from harness.primitives import register_primitives

    rows = register_primitives(stub=True)
    status = next(row for row in rows if row.name == "identity.status")
    cap = next(row for row in rows if row.name == "identity.fill")
    public = status.execute(SimpleNamespace(args={}, job_id="mission-4"))
    assert public["identity"]["email"] == "collie@example.invalid"
    assert public["identity"]["phone"] == "+15550100000"
    assert cap.reversible and cap.risk == "read"
    assert "email|phone" in cap.args_hint
    assert "value" not in cap.args_hint


def test_model_has_secret_free_account_and_truthful_communications_status():
    from harness.jobs import clear_registry
    from harness.primitives import register_primitives

    clear_registry()
    rows = register_primitives(
        stub=False,
        account_loader=lambda: {
            "collie_id": "host-rowan",
            "accounts": [{"account_id": "acct_public", "origin": "https://example.test",
                          "username": "rowan", "status": "planned"}],
            "vault": {"available": True, "os_backed": True,
                      "backend": "windows_dpapi_current_user",
                      "plaintext_fallback": False},
            "communications": {"should_not": "leak through account.status"},
        },
        communications_loader=lambda: {
            "google_voice": {"connected": True, "assigned": True,
                             "sms": {"collie_dispatch_configured": False},
                             "calls": {"collie_dispatch_configured": False}},
            "programmable_telephony": {"configured": False},
            "voice_synthesis": {"configured": False},
        })
    account = next(row for row in rows if row.name == "account.status")
    communications = next(row for row in rows if row.name == "communications.status")
    account_result = account.execute(SimpleNamespace(args={}, job_id="mission-account"))
    communications_result = communications.execute(
        SimpleNamespace(args={}, job_id="mission-communications"))

    assert account.reversible and account.risk == "read"
    assert communications.reversible and communications.risk == "read"
    assert account_result["accounts"][0]["account_id"] == "acct_public"
    assert "communications" not in account_result
    encoded = json.dumps({"accounts": account_result, "communications": communications_result})
    assert "secret_refs" not in encoded and "cv1_" not in encoded
    assert communications_result["google_voice"]["sms"]["collie_dispatch_configured"] is False
    assert communications_result["voice_synthesis"]["configured"] is False


def test_default_mission_leash_allows_identity_account_and_communications_reads():
    from harness.mission import world_leash

    may = world_leash()["may"]
    assert "identity.*" in may
    assert "account.*" in may
    assert "communications.*" in may


def test_collie_public_work_contacts_are_visible_in_model_context(monkeypatch, tmp_path):
    from harness import workidentity
    from harness.context import ContextComposer
    from harness.tools import ToolRegistry

    class EmptyMemory:
        def core_blocks(self, scopes): return []
        def trusted_profile(self, project, device_id=""): return {}
        def recall(self, *args, **kwargs): return []

    monkeypatch.setattr(workidentity, "model_identity", lambda: {
        "principal": "collie", "name": "Rowan", "email": "rowan.owner@collie.run",
        "phone": "+16505551234", "mailbox_status": "ready", "phone_status": "ready",
        "ownership": "collie_owned_work_identity", "status": "ready"})
    composer = ContextComposer(EmptyMemory(), ToolRegistry(), auto_prefetch=False)
    composer.include_project_rules = False
    composer.include_skills = False
    system, _, _ = composer.build({"messages": []}, "register the account", str(tmp_path), "p")
    assert "YOUR COLLIE WORK IDENTITY" in system
    assert "rowan.owner@collie.run" in system and "+16505551234" in system
    assert "verification code" not in system.lower() and "password" not in system.lower()
