import json
import logging
import sqlite3
import threading

import pytest

from harness import accounts, identityvault


class FakeBackend:
    def __init__(self):
        self.items = {}

    def put(self, service, account, secret, entropy):
        self.items[(service, account)] = (bytes(secret), bytes(entropy))

    def get(self, service, account, entropy):
        try:
            value, expected = self.items[(service, account)]
        except KeyError as exc:
            raise identityvault.SecretNotFound("missing") from exc
        if expected != entropy:
            raise identityvault.SecretNotFound("wrong binding")
        return value

    def delete(self, service, account, entropy):
        existing = self.items.get((service, account))
        if existing is None:
            return False
        if existing[1] != entropy:
            raise identityvault.SecretNotFound("wrong binding")
        del self.items[(service, account)]
        return True


class UnavailableBackend(FakeBackend):
    def put(self, service, account, secret, entropy):
        raise identityvault.VaultUnavailable("locked")


def registry(tmp_path, collie_id="rowan", backend=None):
    backend = backend or FakeBackend()
    vault = identityvault.IdentityVault(backend=backend)
    return accounts.AccountRegistry(tmp_path / "accounts.db", collie_id=collie_id, vault=vault), backend


def new_account(reg, **overrides):
    values = {
        "origin": "https://Example.COM:443/signup?ignored",
        "username": "Rowan@Collie.Run",
        "tenant": "Personal",
        "legal_principal": "Workspace owner",
        "idempotency_key": "signup-1",
    }
    values.update(overrides)
    # Origin query strings are intentionally refused; use a clean default.
    values["origin"] = overrides.get("origin", "https://Example.COM:443")
    return reg.create(**values)


def activate_with_bound_evidence(reg, account_id):
    if reg.get(account_id)["status"] == "planned":
        reg.transition(account_id, "registering")
    reg.begin_submission(
        account_id, step="registration",
        expected_active_text="Your Collie workspace is ready",
        expected_active_path="/dashboard", pre_state_digest="a" * 64)
    reg.settle_submission(
        account_id, step="registration", fired=True, confirmed=True)
    return reg.complete_submission(account_id, evidence_digest="b" * 64)


def test_create_is_idempotent_and_unique(tmp_path):
    reg, _ = registry(tmp_path)
    first = new_account(reg)
    second = new_account(reg, username="rowan@collie.run")
    assert second["account_id"] == first["account_id"]
    assert first["origin"] == "https://example.com"
    assert first["normalized_username"] == "rowan@collie.run"
    assert "idempotency_key" not in first

    with pytest.raises(accounts.IdempotencyConflict):
        new_account(reg, username="another@collie.run")
    with pytest.raises(accounts.DuplicateAccount):
        new_account(reg, idempotency_key="signup-2")


def test_account_origins_require_https_by_default(tmp_path, monkeypatch):
    reg, _ = registry(tmp_path)
    with pytest.raises(ValueError, match="HTTPS"):
        new_account(reg, origin="http://accounts.example.test")
    with pytest.raises(ValueError, match="HTTPS"):
        new_account(reg, origin="http://127.0.0.1:3000")
    monkeypatch.setenv("COLLIE_ALLOW_INSECURE_ACCOUNT_LOOPBACK", "1")
    local = new_account(reg, origin="http://127.0.0.1:3000", idempotency_key="local-1")
    assert local["origin"] == "http://127.0.0.1:3000"


def test_create_retry_survives_credentials_adding_factor_classes(tmp_path):
    reg, _ = registry(tmp_path)
    first = new_account(reg, factor_classes=())
    reg.create_credentials(first["account_id"], factors=("password",))

    retried = new_account(reg, username="rowan@collie.run", factor_classes=())
    assert retried["account_id"] == first["account_id"]
    assert retried["factor_classes"] == ["password"]

    with pytest.raises(accounts.IdempotencyConflict):
        new_account(reg, factor_classes=("sms_otp",))


def test_lifecycle_rejects_illegal_transitions_and_records_verification(tmp_path):
    reg, _ = registry(tmp_path)
    account = new_account(reg)
    with pytest.raises(accounts.InvalidTransition):
        reg.transition(account["account_id"], "active")
    assert reg.transition(account["account_id"], "registering")["status"] == "registering"
    with pytest.raises(accounts.InvalidTransition):
        reg.transition(account["account_id"], "active")
    active = activate_with_bound_evidence(reg, account["account_id"])
    assert active["verified_at"] > 0
    with pytest.raises(accounts.InvalidTransition):
        reg.transition(account["account_id"], "planned")


def test_secrets_never_enter_db_public_projection_receipt_or_logs(tmp_path, caplog):
    reg, _ = registry(tmp_path)
    account = new_account(reg)
    password = "S3cret-Only-In-The-OS-Vault!"
    totp = "JBSWY3DPEHPK3PXP"
    recovery = ["alpha-111", "beta-222"]
    caplog.set_level(logging.DEBUG)
    receipt = reg.create_credentials(
        account["account_id"], factors=("password", "totp", "recovery_codes"),
        values={"password": password, "totp": totp, "recovery_codes": recovery})

    public = reg.get(account["account_id"])
    serial = json.dumps({"receipt": receipt, "public": public, "logs": caplog.text})
    for secret in (password, totp, *recovery):
        assert secret not in serial
        for path in tmp_path.rglob("*"):
            if path.is_file():
                assert secret.encode() not in path.read_bytes()
    assert "secret_ref" not in serial
    assert set(public["factor_classes"]) == {"password", "totp", "recovery_codes"}
    assert reg.get_secret(account["account_id"], "password") == password.encode()
    assert reg.get_secret(account["account_id"], "totp") == totp.encode()
    assert json.loads(reg.get_secret(account["account_id"], "recovery_codes")) == recovery

    raw_refs = sqlite3.connect(tmp_path / "accounts.db").execute(
        "SELECT secret_refs_json FROM account_registry").fetchone()[0]
    assert all(ref.startswith("cv1_") for ref in json.loads(raw_refs).values())
    assert all(ref not in serial for ref in json.loads(raw_refs).values())


def test_registry_and_vault_are_cross_collie_isolated(tmp_path):
    backend = FakeBackend()
    first, _ = registry(tmp_path, "rowan", backend)
    account = new_account(first)
    first.create_credentials(account["account_id"], values={"password": "Rowan-password-123456!"})

    second, _ = registry(tmp_path, "juno", backend)
    with pytest.raises(accounts.AccountNotFound):
        second.get(account["account_id"])
    # Even possession of an internal opaque ref and account id is insufficient.
    ref = sqlite3.connect(tmp_path / "accounts.db").execute(
        "SELECT secret_refs_json FROM account_registry WHERE account_id=?",
        (account["account_id"],)).fetchone()[0]
    with pytest.raises(identityvault.SecretNotFound):
        second.vault.get(json.loads(ref)["password"], collie_id="juno",
                         account=account["account_id"], kind="password")


def test_vault_unavailable_fails_closed_without_registry_secret_ref(tmp_path):
    reg, _ = registry(tmp_path, backend=UnavailableBackend())
    account = new_account(reg)
    with pytest.raises(identityvault.VaultUnavailable):
        reg.create_credentials(account["account_id"])
    current = reg.get(account["account_id"])
    assert current["status"] == "planned"
    assert current["factor_classes"] == []
    stored = sqlite3.connect(tmp_path / "accounts.db").execute(
        "SELECT secret_refs_json FROM account_registry").fetchone()[0]
    assert stored == "{}"


def test_concurrent_credential_setup_has_one_durable_winner_and_no_orphan(tmp_path):
    class BlockingBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def put(self, service, account, secret, entropy):
            self.started.set()
            assert self.release.wait(5)
            return super().put(service, account, secret, entropy)

    backend = BlockingBackend()
    first, _ = registry(tmp_path, backend=backend)
    account_id = new_account(first)["account_id"]
    second, _ = registry(tmp_path, backend=backend)
    outcomes = []

    def create_first():
        try:
            outcomes.append(first.create_credentials(account_id))
        except Exception as exc:  # pragma: no cover - asserted below
            outcomes.append(exc)

    worker = threading.Thread(target=create_first)
    worker.start()
    assert backend.started.wait(5)
    with pytest.raises(accounts.InvalidTransition, match="credential setup"):
        second.begin_submission(
            account_id, step="registration",
            expected_active_text="Your Collie workspace is ready",
            expected_active_path="/dashboard",
            pre_state_digest="a" * 64)
    with pytest.raises(accounts.InvalidTransition, match="in progress"):
        second.create_credentials(account_id)
    backend.release.set()
    worker.join(5)
    assert len(outcomes) == 1 and isinstance(outcomes[0], dict)
    stored = sqlite3.connect(tmp_path / "accounts.db").execute(
        "SELECT secret_refs_json,credential_pending_refs_json,credential_setup_token "
        "FROM account_registry WHERE account_id=?", (account_id,)).fetchone()
    refs = json.loads(stored[0])
    assert len(refs) == 1 and len(backend.items) == 1
    assert stored[1] == "{}" and stored[2] == ""


def test_rotate_fence_excludes_other_rotate_and_revoke_without_orphans(tmp_path):
    class BlockingRotationBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.block = False
            self.started = threading.Event()
            self.release = threading.Event()

        def put(self, service, account, secret, entropy):
            if self.block:
                self.started.set()
                assert self.release.wait(5)
            return super().put(service, account, secret, entropy)

    backend = BlockingRotationBackend()
    first, _ = registry(tmp_path, backend=backend)
    account_id = new_account(first)["account_id"]
    first.create_credentials(
        account_id, values={"password": "Original-password-123456!"})
    activate_with_bound_evidence(first, account_id)
    second, _ = registry(tmp_path, backend=backend)
    outcomes = []
    backend.block = True

    def rotate_first():
        try:
            outcomes.append(first.rotate_credentials(
                account_id, values={"password": "Replacement-password-654321!"}))
        except Exception as exc:  # pragma: no cover - asserted below
            outcomes.append(exc)

    worker = threading.Thread(target=rotate_first)
    worker.start()
    assert backend.started.wait(5)
    with pytest.raises(accounts.InvalidTransition):
        second.rotate_credentials(
            account_id, values={"password": "Competing-password-111111!"})
    with pytest.raises(accounts.InvalidTransition, match="mutation"):
        second.revoke_credentials(account_id, factors=("password",))
    backend.release.set()
    worker.join(5)
    assert len(outcomes) == 1 and isinstance(outcomes[0], dict)
    assert first.get_secret(account_id, "password") == b"Replacement-password-654321!"
    assert len(backend.items) == 1


def test_revoke_fence_excludes_rotate_until_vault_deletion_commits(tmp_path):
    class BlockingDeleteBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.block = False
            self.started = threading.Event()
            self.release = threading.Event()

        def delete(self, service, account, entropy):
            if self.block:
                self.started.set()
                assert self.release.wait(5)
            return super().delete(service, account, entropy)

    backend = BlockingDeleteBackend()
    first, _ = registry(tmp_path, backend=backend)
    account_id = new_account(first)["account_id"]
    first.create_credentials(
        account_id, values={"password": "Original-password-123456!"})
    activate_with_bound_evidence(first, account_id)
    second, _ = registry(tmp_path, backend=backend)
    outcomes = []
    backend.block = True

    def revoke_first():
        try:
            outcomes.append(first.revoke_credentials(
                account_id, factors=("password",)))
        except Exception as exc:  # pragma: no cover - asserted below
            outcomes.append(exc)

    worker = threading.Thread(target=revoke_first)
    worker.start()
    assert backend.started.wait(5)
    with pytest.raises(accounts.InvalidTransition, match="mutation"):
        second.rotate_credentials(
            account_id, values={"password": "Competing-password-111111!"})
    with pytest.raises(accounts.InvalidTransition, match="mutation"):
        second.revoke_credentials(account_id, factors=("password",))
    backend.release.set()
    worker.join(5)
    assert len(outcomes) == 1 and isinstance(outcomes[0], dict)
    assert first.get(account_id)["factor_classes"] == []
    assert backend.items == {}


def test_rotate_cleanup_failure_remains_tracked_and_retry_finishes(tmp_path):
    class FailDeleteOnceBackend(FakeBackend):
        fail_next_delete = False

        def delete(self, service, account, entropy):
            if self.fail_next_delete:
                self.fail_next_delete = False
                raise identityvault.VaultUnavailable("simulated cleanup failure")
            return super().delete(service, account, entropy)

    backend = FailDeleteOnceBackend()
    reg, _ = registry(tmp_path, backend=backend)
    account_id = new_account(reg)["account_id"]
    reg.create_credentials(
        account_id, values={"password": "Original-password-123456!"})
    activate_with_bound_evidence(reg, account_id)
    backend.fail_next_delete = True
    with pytest.raises(identityvault.VaultUnavailable, match="cleanup"):
        reg.rotate_credentials(
            account_id, values={"password": "Replacement-password-654321!"})

    fenced = sqlite3.connect(tmp_path / "accounts.db").execute(
        "SELECT status,credential_setup_token,credential_mutation_phase,"
        "credential_cleanup_refs_json FROM account_registry WHERE account_id=?",
        (account_id,)).fetchone()
    assert fenced[0] == "rotating" and fenced[1].startswith("rotate:")
    assert fenced[2] == "recovery_required"
    assert len(json.loads(fenced[3])) == 1 and len(backend.items) == 2

    recovered = reg.rotate_credentials(
        account_id, values={"password": "Must-not-create-a-third-secret!"})
    assert recovered["status"] == "active"
    assert reg.get_secret(account_id, "password") == b"Replacement-password-654321!"
    assert len(backend.items) == 1


def test_revoke_finalize_failure_is_idempotently_resumable(tmp_path):
    reg, backend = registry(tmp_path)
    account_id = new_account(reg)["account_id"]
    reg.create_credentials(
        account_id, values={"password": "Original-password-123456!"})
    activate_with_bound_evidence(reg, account_id)
    reg.db.execute("""
        CREATE TRIGGER fail_revoke_finalize
        BEFORE UPDATE OF secret_refs_json ON account_registry
        WHEN OLD.credential_setup_token LIKE 'revoke:%'
        BEGIN SELECT RAISE(FAIL, 'simulated revoke finalize failure'); END
    """)
    with pytest.raises(sqlite3.IntegrityError, match="simulated revoke"):
        reg.revoke_credentials(account_id, factors=("password",))
    fenced = reg.db.execute(
        "SELECT credential_setup_token,credential_mutation_phase,"
        "credential_pending_refs_json FROM account_registry WHERE account_id=?",
        (account_id,)).fetchone()
    assert fenced[0].startswith("revoke:")
    assert fenced[1] == "recovery_required"
    assert len(json.loads(fenced[2])) == 1 and backend.items == {}

    reg.db.execute("DROP TRIGGER fail_revoke_finalize")
    recovered = reg.revoke_credentials(account_id, factors=("password",))
    assert recovered["status"] == "degraded"
    assert reg.get(account_id)["factor_classes"] == []
    stored = reg.db.execute(
        "SELECT secret_refs_json,credential_setup_token,credential_pending_refs_json "
        "FROM account_registry WHERE account_id=?", (account_id,)).fetchone()
    assert tuple(stored) == ("{}", "", "{}")


def test_create_finalize_and_cleanup_fault_keeps_recoverable_refs(tmp_path):
    class FailDeleteOnceBackend(FakeBackend):
        fail_next_delete = False

        def delete(self, service, account, entropy):
            if self.fail_next_delete:
                self.fail_next_delete = False
                raise identityvault.VaultUnavailable("simulated pending cleanup failure")
            return super().delete(service, account, entropy)

    backend = FailDeleteOnceBackend()
    reg, _ = registry(tmp_path, backend=backend)
    account_id = new_account(reg)["account_id"]
    reg.db.execute("""
        CREATE TRIGGER fail_create_finalize
        BEFORE UPDATE OF secret_refs_json ON account_registry
        WHEN OLD.credential_setup_token LIKE 'create:%'
        BEGIN SELECT RAISE(FAIL, 'simulated create finalize failure'); END
    """)
    backend.fail_next_delete = True
    with pytest.raises(sqlite3.IntegrityError, match="simulated create"):
        reg.create_credentials(
            account_id, values={"password": "Interrupted-password-123456!"})

    fenced = reg.db.execute(
        "SELECT status,secret_refs_json,credential_setup_token,"
        "credential_mutation_phase,credential_pending_refs_json "
        "FROM account_registry WHERE account_id=?", (account_id,)).fetchone()
    assert fenced[0] == "registering" and fenced[1] == "{}"
    assert fenced[2].startswith("create:") and fenced[3] == "recovery_required"
    assert len(json.loads(fenced[4])) == 1 and len(backend.items) == 1

    reg.db.execute("DROP TRIGGER fail_create_finalize")
    recovered = reg.create_credentials(
        account_id, values={"password": "Recovered-password-654321!"})
    assert recovered["event"] == "credentials.created"
    assert reg.get_secret(account_id, "password") == b"Recovered-password-654321!"
    assert len(backend.items) == 1
    stored = reg.db.execute(
        "SELECT credential_setup_token,credential_mutation_phase,"
        "credential_pending_refs_json FROM account_registry WHERE account_id=?",
        (account_id,)).fetchone()
    assert tuple(stored) == ("", "", "{}")


def test_expired_create_process_fence_is_cleaned_before_retry(tmp_path):
    reg, backend = registry(tmp_path)
    account_id = new_account(reg)["account_id"]
    stale_ref = "cv1_stale-process-reserved-reference"
    reg.vault.put(
        "Stale-password-123456!", collie_id=reg.collie_id,
        account=account_id, kind="password", ref=stale_ref)
    with reg.db:
        reg.db.execute("""
            UPDATE account_registry
            SET status='registering', credential_setup_token='create:stale-owner',
                credential_setup_at=0, credential_mutation_phase='create_write',
                credential_pending_refs_json=?
            WHERE account_id=?
        """, (json.dumps({"password": stale_ref}), account_id))

    recovered = reg.create_credentials(
        account_id, values={"password": "Fresh-password-654321!"})
    assert recovered["event"] == "credentials.created"
    assert reg.get_secret(account_id, "password") == b"Fresh-password-654321!"
    assert len(backend.items) == 1


def test_rotate_finalize_and_cleanup_fault_is_resumable_without_orphan(tmp_path):
    class FailDeleteOnceBackend(FakeBackend):
        fail_next_delete = False

        def delete(self, service, account, entropy):
            if self.fail_next_delete:
                self.fail_next_delete = False
                raise identityvault.VaultUnavailable("simulated pending cleanup failure")
            return super().delete(service, account, entropy)

    backend = FailDeleteOnceBackend()
    reg, _ = registry(tmp_path, backend=backend)
    account_id = new_account(reg)["account_id"]
    reg.create_credentials(
        account_id, values={"password": "Original-password-123456!"})
    activate_with_bound_evidence(reg, account_id)
    reg.db.execute("""
        CREATE TRIGGER fail_rotate_finalize
        BEFORE UPDATE OF secret_refs_json ON account_registry
        WHEN OLD.credential_setup_token LIKE 'rotate:%'
        BEGIN SELECT RAISE(FAIL, 'simulated rotate finalize failure'); END
    """)
    backend.fail_next_delete = True
    with pytest.raises(sqlite3.IntegrityError, match="simulated rotate"):
        reg.rotate_credentials(
            account_id, values={"password": "Interrupted-password-111111!"})

    fenced = reg.db.execute(
        "SELECT status,credential_setup_token,credential_mutation_phase,"
        "credential_pending_refs_json,credential_cleanup_refs_json "
        "FROM account_registry WHERE account_id=?", (account_id,)).fetchone()
    assert fenced[0] == "rotating" and fenced[1].startswith("rotate:")
    assert fenced[2] == "recovery_required"
    assert len(json.loads(fenced[3])) == 1 and fenced[4] == "{}"
    assert len(backend.items) == 2

    reg.db.execute("DROP TRIGGER fail_rotate_finalize")
    recovered = reg.rotate_credentials(
        account_id, values={"password": "Recovered-password-222222!"})
    assert recovered["status"] == "active"
    assert reg.get_secret(account_id, "password") == b"Recovered-password-222222!"
    assert len(backend.items) == 1


def test_preparation_abort_cannot_be_resurrected_during_vault_erasure(tmp_path):
    class RacingDeleteBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.on_delete = None

        def delete(self, service, account, entropy):
            callback, self.on_delete = self.on_delete, None
            if callback is not None:
                callback()
            return super().delete(service, account, entropy)

    backend = RacingDeleteBackend()
    first, _ = registry(tmp_path, backend=backend)
    account_id = new_account(first)["account_id"]
    first.create_credentials(account_id)
    second, _ = registry(tmp_path, backend=backend)
    race = []
    secret_race = []

    def try_resurrect():
        try:
            second.get_secret(account_id, "password")
        except Exception as exc:
            secret_race.append(exc)
        try:
            second.transition(account_id, "active")
        except Exception as exc:
            race.append(exc)

    backend.on_delete = try_resurrect
    receipt = first.abort_prepared(account_id)
    assert receipt["event"] == "account.preparation_aborted"
    assert len(race) == 1 and isinstance(race[0], accounts.InvalidTransition)
    assert len(secret_race) == 1 and isinstance(
        secret_race[0], accounts.InvalidTransition)
    assert backend.items == {}
    with pytest.raises(accounts.AccountNotFound):
        second.get(account_id)


def test_no_click_on_later_step_restores_prior_fired_completion_contract(tmp_path):
    reg, _ = registry(tmp_path)
    account_id = new_account(reg)["account_id"]
    reg.transition(account_id, "registering")
    reg.begin_submission(
        account_id, step="registration",
        expected_active_text="Your Collie workspace is ready",
        expected_active_path="/dashboard", pre_state_digest="a" * 64)
    with pytest.raises(accounts.InvalidTransition, match="still firing"):
        reg.begin_submission(
            account_id, step="verification",
            expected_active_text="Email verification is complete",
            expected_active_path="/verified", pre_state_digest="b" * 64)
    reg.settle_submission(
        account_id, step="registration", fired=True, confirmed=False)

    reg.begin_submission(
        account_id, step="verification",
        expected_active_text="Email verification is complete",
        expected_active_path="/verified", pre_state_digest="b" * 64)
    reg.settle_submission(
        account_id, step="verification", fired=False, confirmed=False)

    contract = reg.submission_contract(account_id)
    assert contract["status"] == "challenge_wait"
    assert contract["expected_active_text"] == "Your Collie workspace is ready"
    assert contract["expected_active_path"] == "/dashboard"
    assert contract["pre_state_digest"] == "a" * 64


def test_completion_holds_a_write_lock_across_read_and_activation(tmp_path):
    backend = FakeBackend()
    first, _ = registry(tmp_path, backend=backend)
    second, _ = registry(tmp_path, backend=backend)
    account_id = new_account(first)["account_id"]
    first.transition(account_id, "registering")
    first.begin_submission(
        account_id, step="registration",
        expected_active_text="Your Collie workspace is ready",
        expected_active_path="/dashboard", pre_state_digest="a" * 64)
    first.settle_submission(
        account_id, step="registration", fired=True, confirmed=False)

    second.db.execute("PRAGMA busy_timeout=1")
    original_row = first._row
    attempted = []

    def row_with_competing_writer(requested):
        row = original_row(requested)
        if not attempted:
            try:
                second.transition(account_id, "degraded")
            except sqlite3.OperationalError as exc:
                attempted.append(exc)
        return row

    first._row = row_with_competing_writer
    completed = first.complete_submission(account_id, evidence_digest="b" * 64)
    assert completed["status"] == "active"
    assert len(attempted) == 1 and "locked" in str(attempted[0]).lower()
    assert second.get(account_id)["status"] == "active"


def test_rotate_replaces_and_deletes_old_reference_then_retire_erases_all(tmp_path):
    reg, backend = registry(tmp_path)
    account = new_account(reg)
    account_id = account["account_id"]
    reg.create_credentials(account_id, factors=("password", "totp"), values={
        "password": "Old-password-123456789!", "totp": "JBSWY3DPEHPK3PXP"})
    activate_with_bound_evidence(reg, account_id)
    before = set(backend.items)

    receipt = reg.rotate_credentials(account_id, factors=("password",),
                                     values={"password": "New-password-987654321!"})
    assert receipt["status"] == "active"
    assert reg.get_secret(account_id, "password") == b"New-password-987654321!"
    assert len(set(backend.items) - before) == 1
    assert len(before - set(backend.items)) == 1
    assert reg.get(account_id)["rotated_at"] > 0

    retired = reg.retire(account_id)
    assert retired["status"] == "retired"
    assert backend.items == {}
    assert reg.list() == []
    assert reg.list(include_retired=True)[0]["status"] == "retired"


def test_revoke_one_factor_keeps_the_other_and_degrades_active_account(tmp_path):
    reg, backend = registry(tmp_path)
    account_id = new_account(reg)["account_id"]
    reg.create_credentials(account_id, factors=("password", "totp"), values={
        "password": "Password-to-revoke-123!", "totp": "JBSWY3DPEHPK3PXP"})
    activate_with_bound_evidence(reg, account_id)

    receipt = reg.revoke_credentials(account_id, factors=("totp",))
    assert receipt["status"] == "degraded"
    assert reg.get(account_id)["factor_classes"] == ["password"]
    assert reg.get_secret(account_id, "password") == b"Password-to-revoke-123!"
    with pytest.raises(identityvault.SecretNotFound):
        reg.get_secret(account_id, "totp")
    assert len(backend.items) == 1


def test_password_generators_have_expected_strength_and_shape():
    value = accounts.generate_password()
    assert len(value) == 32
    assert any(c.islower() for c in value)
    assert any(c.isupper() for c in value)
    assert any(c.isdigit() for c in value)
    assert any(not c.isalnum() for c in value)
    assert len(accounts.generate_totp_secret()) >= 32
    assert len(set(accounts.generate_recovery_codes())) == 10
