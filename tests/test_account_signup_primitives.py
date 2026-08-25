import json
from types import SimpleNamespace

from harness.accounts import AccountRegistry
from harness.identityvault import IdentityVault
from harness.primitives import (_real_account_fill, _real_account_prepare,
                                _real_account_abort, _real_account_complete,
                                _real_account_submit, _account_submit_snapshot,
                                register_primitives)
from harness.webact import FakeActuator


class MemoryVaultBackend:
    def __init__(self):
        self.items = {}

    def put(self, service, account, secret, entropy):
        self.items[(service, account)] = (bytes(secret), bytes(entropy))

    def get(self, service, account, entropy):
        value, expected = self.items[(service, account)]
        if expected != bytes(entropy):
            raise KeyError(account)
        return value

    def delete(self, service, account, entropy):
        return self.items.pop((service, account), None) is not None


def registry_factory(tmp_path):
    backend = MemoryVaultBackend()
    vault = IdentityVault(backend=backend)

    def factory():
        return AccountRegistry(tmp_path / "accounts.db", collie_id="collie-rowan", vault=vault)
    return factory


class SecretForm(FakeActuator):
    def __init__(self, url="https://accounts.example.test/signup",
                 label="Create password"):
        super().__init__()
        self._url = url
        self.label = label
        self.raw_values = []

    def snapshot(self):
        return {"url": self._url,
                "snapshot": '[pw1] textbox "%s"' % self.label}

    def type_ref(self, ref, value, submit=False):
        self.raw_values.append(value)
        return super().type_ref(ref, value, submit=submit)


def test_prepare_is_idempotent_and_never_returns_the_generated_password(tmp_path):
    factory = registry_factory(tmp_path)
    prepare = _real_account_prepare(
        factory, lambda: {"email": "rowan@collie.run"})
    rec = SimpleNamespace(args={"origin": "https://accounts.example.test",
                                "scopes": ["profile.read"]}, job_id="mission-one")
    first = prepare(rec)
    second = prepare(rec)
    assert first["prepared"] and second["prepared"]
    assert first["account"]["account_id"] == second["account"]["account_id"]
    assert first["account"]["status"] == "registering"
    assert first["account"]["factor_classes"] == ["password"]
    encoded = json.dumps(first) + json.dumps(second)
    assert "cv1_" not in encoded and "secret_refs" not in encoded
    with factory() as registry:
        password = registry.get_secret(first["account"]["account_id"], "password")
    assert len(password) >= 24 and password.decode() not in encoded


def test_prepare_binds_canonical_identity_and_https(tmp_path):
    factory = registry_factory(tmp_path)
    prepare = _real_account_prepare(
        factory, lambda: {"email": "rowan@collie.run",
                          "legal_principal": "owner:rowan"})
    override = prepare(SimpleNamespace(
        args={"origin": "https://accounts.example.test",
              "username": "owner.personal@example.test"}, job_id="identity"))
    assert not override["prepared"] and "canonical work mailbox" in override["error"]
    ownership = prepare(SimpleNamespace(
        args={"origin": "https://accounts.example.test",
              "legal_principal": "another person"}, job_id="identity"))
    assert not ownership["prepared"] and "host-bound" in ownership["error"]
    insecure = prepare(SimpleNamespace(
        args={"origin": "http://accounts.example.test"}, job_id="identity"))
    assert not insecure["prepared"] and "HTTPS" in insecure["error"]


def test_prepare_refuses_a_legacy_account_with_conflicting_ownership(tmp_path):
    factory = registry_factory(tmp_path)
    with factory() as registry:
        legacy = registry.create(
            origin="https://accounts.example.test",
            username="rowan@collie.run",
            ownership="user_owned_assigned_to_collie",
            legal_principal="Alice",
            idempotency_key="legacy-user-account")
    result = _real_account_prepare(
        factory, lambda: {"email": "rowan@collie.run",
                          "legal_principal": "owner:collie-rowan"})(SimpleNamespace(
            args={"origin": "https://accounts.example.test"}, job_id="identity-collision"))
    assert not result["prepared"]
    assert "canonical ownership" in result["error"]
    with factory() as registry:
        unchanged = registry.get(legacy["account_id"])
        assert unchanged["status"] == "planned"
        assert unchanged["factor_classes"] == []


def test_account_fill_uses_bound_origin_and_returns_no_secret(tmp_path):
    factory = registry_factory(tmp_path)
    prepared = _real_account_prepare(
        factory, lambda: {"email": "rowan@collie.run"})(SimpleNamespace(
            args={"origin": "https://accounts.example.test"}, job_id="mission-two"))
    account_id = prepared["account"]["account_id"]
    with factory() as registry:
        password = registry.get_secret(account_id, "password").decode()
    actuator = SecretForm()
    result = _real_account_fill(actuator, factory)(SimpleNamespace(
        args={"account_id": account_id, "factor": "password",
              "label": "Create password"}, job_id="mission-two"))
    assert result["filled"] and result["factor"] == "password"
    assert actuator.raw_values == [password]
    assert actuator.calls[-1] == ("type_ref", "pw1", "[sensitive]", False)
    encoded = json.dumps(result)
    assert password not in encoded and "cv1_" not in encoded


def test_account_fill_refuses_cross_origin_and_ambiguous_fields(tmp_path):
    factory = registry_factory(tmp_path)
    prepared = _real_account_prepare(
        factory, lambda: {"email": "rowan@collie.run"})(SimpleNamespace(
            args={"origin": "https://accounts.example.test"}, job_id="mission-three"))
    account_id = prepared["account"]["account_id"]
    wrong = SecretForm(url="https://evil.example.test/signup")
    refused = _real_account_fill(wrong, factory)(SimpleNamespace(
        args={"account_id": account_id, "factor": "password",
              "label": "Create password"}, job_id="mission-three"))
    assert not refused["filled"] and "origin" in refused["error"]
    assert wrong.raw_values == []

    class Ambiguous(SecretForm):
        def snapshot(self):
            return {"url": self._url,
                    "snapshot": '[p1] textbox "Password"\n[p2] textbox "Confirm password"'}
    ambiguous = Ambiguous()
    result = _real_account_fill(ambiguous, factory)(SimpleNamespace(
        args={"account_id": account_id, "factor": "password"},
        job_id="mission-three"))
    assert not result["filled"] and "ambiguous" in result["error"]


def test_account_fill_refuses_a_user_browser_profile(tmp_path):
    factory = registry_factory(tmp_path)
    prepared = _real_account_prepare(
        factory, lambda: {"email": "rowan@collie.run"})(SimpleNamespace(
            args={"origin": "https://accounts.example.test"}, job_id="mission-profile"))

    class UserProfile(SecretForm):
        def is_collie_profile(self):
            return False

    actuator = UserProfile()
    result = _real_account_fill(actuator, factory)(SimpleNamespace(
        args={"account_id": prepared["account"]["account_id"],
              "factor": "password", "label": "Create password"},
        job_id="mission-profile"))
    assert not result["filled"]
    assert "isolated managed browser profile" in result["error"]
    assert actuator.raw_values == []


def test_account_fill_revalidates_origin_after_vault_read(tmp_path):
    backend = MemoryVaultBackend()
    vault = IdentityVault(backend=backend)

    def factory():
        return AccountRegistry(
            tmp_path / "redirect-accounts.db",
            collie_id="collie-rowan", vault=vault)

    prepared = _real_account_prepare(
        factory, lambda: {"email": "rowan@collie.run"})(SimpleNamespace(
            args={"origin": "https://accounts.example.test"}, job_id="redirect"))
    actuator = SecretForm()
    original_get = backend.get

    def redirect_during_vault_read(service, account, entropy):
        actuator._url = "https://other.example.test/signup"
        return original_get(service, account, entropy)

    backend.get = redirect_during_vault_read
    result = _real_account_fill(actuator, factory)(SimpleNamespace(
        args={"account_id": prepared["account"]["account_id"],
              "factor": "password", "label": "Create password"},
        job_id="redirect"))
    assert not result["filled"]
    assert "origin changed" in result["error"]
    assert actuator.raw_values == []


class RegistrationBrowser(FakeActuator):
    def __init__(self):
        super().__init__(result_url="https://accounts.example.test/dashboard",
                         page_text="Your Collie workspace is ready")
        self._url = "https://accounts.example.test/signup"

    def snapshot(self):
        clicked = any(call[0] == "click_ref" for call in self.calls)
        if clicked:
            return {"url": self.result_url,
                    "snapshot": "Your Collie workspace is ready"}
        return {"url": self._url,
                "snapshot": '[create] button "Create account"'}

    def page_identity(self):
        clicked = any(call[0] == "click_ref" for call in self.calls)
        return {"tab_id": 7, "url": self.result_url if clicked else self._url,
                "title": "Workspace ready" if clicked else "Create account"}

    def form_snapshot(self):
        return {"fields": [], "actions": []}


def test_account_submit_is_durable_once_and_completion_uses_bound_evidence(tmp_path):
    factory = registry_factory(tmp_path)
    prepared = _real_account_prepare(
        factory, lambda: {"email": "rowan@collie.run"})(SimpleNamespace(
            args={"origin": "https://accounts.example.test"}, job_id="signup"))
    account_id = prepared["account"]["account_id"]
    browser = RegistrationBrowser()
    args = {"account_id": account_id, "step": "registration",
            "button": "Create account",
            "success_text": "Your Collie workspace is ready",
            "active_text": "Your Collie workspace is ready",
            "active_path": "/dashboard"}
    snapshot = _account_submit_snapshot(browser, factory)(args, "signup")
    rec = SimpleNamespace(args=args, job_id="signup", snapshot=snapshot)
    first = _real_account_submit(browser, factory)(rec)
    assert first["submitted"] and first["status"] == "challenge_wait"
    assert len([call for call in browser.calls if call[0] == "click_ref"]) == 1

    retry = _real_account_submit(browser, factory)(rec)
    assert not retry["submitted"] and "already fired" in retry["error"]
    assert len([call for call in browser.calls if call[0] == "click_ref"]) == 1

    active = _real_account_complete(browser, factory)(SimpleNamespace(
        args={"account_id": account_id}, job_id="signup"))
    assert active["completed"] and active["status"] == "active"
    assert len(active["evidence_hash"]) == 64
    with factory() as registry:
        assert registry.get(account_id)["status"] == "active"


def test_account_submit_rejects_a_success_marker_already_on_the_form(tmp_path):
    factory = registry_factory(tmp_path)
    prepared = _real_account_prepare(
        factory, lambda: {"email": "rowan@collie.run"})(SimpleNamespace(
            args={"origin": "https://accounts.example.test"}, job_id="preexisting"))
    class PreexistingMarker(RegistrationBrowser):
        def snapshot(self):
            return {"url": self._url,
                    "snapshot": "Create your Collie\n  workspace is ready\n"
                                "[create] button \"Create account\""}

        def page_identity(self):
            return {"tab_id": 7, "url": self._url,
                    "title": "Create account"}

    browser = PreexistingMarker()
    args = {"account_id": prepared["account"]["account_id"],
            "step": "registration", "button": "Create account",
            "active_text": "Create your Collie workspace is ready",
            "active_path": "/signup"}
    try:
        _account_submit_snapshot(browser, factory)(args, "preexisting")
        assert False, "a pre-existing marker cannot prove a post-submit transition"
    except RuntimeError as exc:
        assert "absent before submit" in str(exc)


def test_account_complete_requires_the_exact_bound_success_path(tmp_path):
    factory = registry_factory(tmp_path)
    prepared = _real_account_prepare(
        factory, lambda: {"email": "rowan@collie.run"})(SimpleNamespace(
            args={"origin": "https://accounts.example.test"}, job_id="exact-path"))
    account_id = prepared["account"]["account_id"]
    with factory() as registry:
        registry.begin_submission(
            account_id, step="registration",
            expected_active_text="Your Collie workspace is ready",
            expected_active_path="/dashboard", pre_state_digest="a" * 64)
        registry.settle_submission(
            account_id, step="registration", fired=True, confirmed=True)

    browser = RegistrationBrowser()
    browser.calls.append(("click_ref", "create"))
    browser.result_url = "https://accounts.example.test/evil/dashboard-preview"
    result = _real_account_complete(browser, factory)(SimpleNamespace(
        args={"account_id": account_id}, job_id="exact-path"))
    assert not result["completed"]
    assert "exactly match" in result["error"]
    with factory() as registry:
        assert registry.get(account_id)["status"] == "challenge_wait"


def test_account_abort_removes_only_a_never_submitted_preparation(tmp_path):
    factory = registry_factory(tmp_path)
    prepared = _real_account_prepare(
        factory, lambda: {"email": "rowan@collie.run"})(SimpleNamespace(
            args={"origin": "https://abort.example.test"}, job_id="abort"))
    account_id = prepared["account"]["account_id"]
    result = _real_account_abort(factory)(SimpleNamespace(
        args={"account_id": account_id}, job_id="abort"))
    assert result["aborted"]
    with factory() as registry:
        try:
            registry.get(account_id)
            assert False, "aborted preparation must be removed"
        except KeyError:
            pass


def test_registered_account_actions_never_accept_secret_values():
    rows = register_primitives(stub=True)
    prepare = next(row for row in rows if row.name == "account.prepare")
    fill = next(row for row in rows if row.name == "account.fill")
    assert prepare.reversible and prepare.risk == "write_local"
    assert fill.reversible and fill.risk == "read"
    hints = prepare.args_hint + fill.args_hint
    assert "password\"" not in hints and "value" not in hints and "secret" not in hints
    assert "account.prepare" in {row.name for row in rows}
    assert "account.fill" in {row.name for row in rows}
    assert {"account.submit", "account.complete", "account.abort"}.issubset(
        {row.name for row in rows})
