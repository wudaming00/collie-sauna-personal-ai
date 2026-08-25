import json
import time

import pytest


def _seed_mailbox(tmp_path, monkeypatch, dog="rowan"):
    from harness import dogmail

    monkeypatch.setattr(dogmail, "STORE", str(tmp_path / "mail.json"))
    address = "%s.owner@collie.run" % dog
    dogmail.save({
        "handle": {"name": "owner", "verified": True, "priv": "handle-private"},
        "dogs": {dog: {"address": address, "priv": "dog-private", "pub": "dog-public",
                       "cursor": 0}},
    })
    return address


def test_public_connections_include_collie_public_mail_without_key_material(tmp_path, monkeypatch):
    from harness import workidentity

    address = _seed_mailbox(tmp_path, monkeypatch)
    workidentity._save({"collie_mail": {"connected": True, "dog": "rowan",
                                        "verified_at": 10}}, str(tmp_path))

    rows = workidentity.public_connections(str(tmp_path))
    encoded = json.dumps(rows)
    mail = next(row for row in rows if row["id"] == "collie_mail")
    assert {row["id"] for row in rows} == {"collie_mail", "google_voice"}
    assert mail["connected"] and mail["status"] == "ready"
    assert mail["account"] == address
    assert mail["account_masked"] == "r•••n@collie.run"
    assert address in encoded
    assert "dog-private" not in encoded and "handle-private" not in encoded


def test_verified_namespace_can_idempotently_provision_this_collie(tmp_path, monkeypatch):
    from harness import dogmail, settings, workidentity

    monkeypatch.setattr(dogmail, "STORE", str(tmp_path / "mail.json"))
    monkeypatch.setattr(settings, "get", lambda key, default=None: "Rowan" if key == "COMPANION_NAME" else default)
    dogmail.save({"handle": {"name": "owner", "verified": True, "priv": "handle-private"},
                  "dogs": {}})
    calls = []

    def fake_claim(name, relay="", state_dir=None):
        calls.append(name)
        state = dogmail.load(state_dir)
        dogs = state.setdefault("dogs", {})
        dogs.setdefault(name, {"address": "%s.owner@collie.run" % name,
                               "priv": "dog-private", "pub": "dog-public", "cursor": 0})
        dogmail.save(state, state_dir)
        return {"ok": True, "address": dogs[name]["address"]}

    monkeypatch.setattr(dogmail, "claim_dog", fake_claim)
    first = workidentity.provision_collie_mail(state_dir=str(tmp_path))
    second = workidentity.provision_collie_mail(state_dir=str(tmp_path))
    persisted = (tmp_path / "work-identities.json").read_text(encoding="utf-8")

    assert first["connected"] and second["connected"]
    assert calls == ["rowan", "rowan"]
    assert "rowan.owner@collie.run" not in persisted
    assert "dog-private" not in persisted and json.loads(persisted)["collie_mail"]["dog"] == "rowan"


def test_collie_mail_code_is_fresh_service_bound_unique_and_transient(tmp_path, monkeypatch):
    from harness import dogmail, workidentity

    address = _seed_mailbox(tmp_path, monkeypatch)
    workidentity._save({"collie_mail": {"connected": True, "dog": "rowan"}}, str(tmp_path))
    now = int(time.time())
    messages = [
        {"at": now - 1000, "from": "security@example.test",
         "subject": "Example security code", "text": "Your verification code is 111111"},
        {"at": now, "from": "security@example.test",
         "subject": "Example security code", "text": "Your verification code is 847291"},
        {"at": now, "from": "security@notexample.test",
         "subject": "Example security code", "text": "Your verification code is 222222"},
    ]
    monkeypatch.setattr(dogmail, "fetch", lambda *args, **kwargs: list(messages))

    code, meta = workidentity.take_verification_code(
        "Example", state_dir=str(tmp_path), channel="email",
        sender="example.test", subject="security", max_age_seconds=300)
    assert code == "847291"
    assert meta["source"] == "collie_mail" and meta["account_masked"] == "r•••n@collie.run"
    assert code not in json.dumps(meta) and address not in json.dumps(meta)

    messages.append({"at": now, "from": "security@example.test",
                     "subject": "Example security code",
                     "text": "Your verification code is 339944"})
    with pytest.raises(RuntimeError, match="uniquely matching"):
        workidentity.take_verification_code(
            "Example", state_dir=str(tmp_path), channel="email", sender="example.test")


def test_collie_mail_extracts_code_from_sealed_rfc822_body(tmp_path, monkeypatch):
    import base64
    from email.message import EmailMessage
    from harness import dogmail

    _seed_mailbox(tmp_path, monkeypatch)
    message = EmailMessage()
    message["From"] = "login@notion.so"
    message["To"] = "rowan.owner@collie.run"
    message["Subject"] = "Notion account sign-in"
    message.set_content("Use A7B92C to verify your account.")
    delivered = {"at": int(time.time()), "from": "login@notion.so",
                 "subject": "Notion account sign-in",
                 "raw": base64.b64encode(message.as_bytes()).decode("ascii")}
    monkeypatch.setattr(dogmail, "fetch", lambda *args, **kwargs: [delivered])

    code, meta = dogmail.take_verification_code(
        "rowan", "Notion", sender="notion.so", max_age_seconds=300)
    assert code == "A7B92C" and code not in json.dumps(meta)


def test_auto_code_selection_fails_closed_when_two_channels_match(tmp_path, monkeypatch):
    from harness import dogmail, workidentity

    _seed_mailbox(tmp_path, monkeypatch)
    workidentity._save({
        "collie_mail": {"connected": True, "dog": "rowan"},
        "google_voice": {"connected": True, "last4": "1234"},
    }, str(tmp_path))
    monkeypatch.setattr(dogmail, "take_verification_code",
                        lambda *args, **kwargs: ("111111", {"source": "collie_mail"}))
    monkeypatch.setattr(workidentity, "take_google_voice_code",
                        lambda *args, **kwargs: ("222222", {"source": "google_voice"}))

    with pytest.raises(RuntimeError, match="multiple Collie-owned channels") as exc:
        workidentity.take_verification_code("Example", state_dir=str(tmp_path))
    assert "111111" not in str(exc.value) and "222222" not in str(exc.value)


def test_host_only_identity_resolution_and_phone_fail_honestly(tmp_path, monkeypatch):
    from harness import settings, workidentity

    address = _seed_mailbox(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "get", lambda key, default=None: "Rowan" if key == "COMPANION_NAME" else default)
    workidentity._save({
        "collie_mail": {"connected": True, "dog": "rowan"},
        "google_voice": {"connected": True, "last4": "1234"},
    }, str(tmp_path))

    raw, safe = workidentity.resolve_identity_field("email", str(tmp_path))
    assert raw == address and set(safe) == {"source", "account_masked"}
    assert address not in json.dumps(safe)
    assert workidentity.resolve_identity_field("display_name", str(tmp_path))[0] == "Rowan"
    assert workidentity.resolve_identity_field("username", str(tmp_path))[0] == "rowan"
    with pytest.raises(RuntimeError, match="secure provider seam"):
        workidentity.resolve_identity_field("phone", str(tmp_path))


def test_full_assigned_voice_number_is_collie_public_identity(tmp_path, monkeypatch):
    from harness import browserbridge, dogmail, settings, workidentity

    _seed_mailbox(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "get", lambda key, default=None: "Rowan" if key == "COMPANION_NAME" else default)

    def fake_call(command, timeout=60):
        if command["action"] == "attach":
            return {"ok": True, "data": {"attached": True}}
        if command["action"] == "voice_identity":
            return {"ok": True, "data": {"connected": True, "last4": "1234",
                                           "number": "(650) 555-1234"}}
        return {"ok": True, "data": {"released": True}}

    monkeypatch.setattr(browserbridge, "_call", fake_call)
    row = workidentity.connect_google_voice("1234", str(tmp_path))
    raw, safe = workidentity.resolve_identity_field("phone", str(tmp_path))
    model_identity = workidentity.public_identity(str(tmp_path))
    status_identity = workidentity.model_identity(str(tmp_path))
    persisted = json.loads((tmp_path / "work-identities.json").read_text(encoding="utf-8"))

    assert row["account"] == "+16505551234"
    assert raw == "+16505551234" and safe["account_masked"] == "•••-•••-1234"
    assert model_identity["email"] == "rowan.owner@collie.run"
    assert model_identity["phone"] == "+16505551234"
    assert status_identity["principal"] == "collie" and status_identity["status"] == "ready"
    assert status_identity["email"] == model_identity["email"]
    assert persisted["google_voice"]["ownership"] == "user_owned_assigned_to_collie"
