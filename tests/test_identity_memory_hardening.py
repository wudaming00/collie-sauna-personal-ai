"""P1 regression tests for memory, identity-state, vault, and account planning."""
from __future__ import annotations

import concurrent.futures
import json
import os
import types

import pytest


def _save_identity_connection(args):
    """Top-level worker so Windows' spawn multiprocessing can import it."""
    root, index = args
    from harness import workidentity
    workidentity._save({
        "connection_%03d" % index: {
            "connected": True, "verified_at": index,
        }}, root)
    return index


@pytest.mark.parametrize("field,payload", [
    ("text", "production password is NeverStoreThis-9041!"),
    ("text", "The password for GitHub is NeverStoreQualified9041!"),
    ("text", "Use NeverStoreBeforeLabel9041! as the password for GitHub"),
    ("text", "The API key for staging is NeverStoreApiKey9041!"),
    ("keys", "api_key=sk_live_NeverStoreThis9041"),
    ("evidence", {"otp": "483921"}),
    ("provenance", {"refresh_token": "NeverStoreThisRefreshToken9041"}),
])
def test_model_memory_admission_rejects_secrets_before_sqlite(field, payload, tmp_path):
    from harness.memory import SqliteMemory

    path = tmp_path / "memory.db"
    memory = SqliteMemory(str(path))
    values = {
        "text": "deployment uses the release workflow",
        "keys": "deployment workflow",
        "evidence": {"kind": "agent observation"},
        "provenance": {"run_id": 9},
    }
    values[field] = payload
    marker = (payload if isinstance(payload, str) else next(iter(payload.values())))
    try:
        assert memory.propose(project="repo", **values) == -1
        assert memory.count() == 0
    finally:
        memory.close()

    # A reject-after-write implementation still leaks through SQLite/WAL.  The
    # marker must never have reached any database sidecar in the first place.
    needle = str(marker).encode("utf-8")
    for candidate in tmp_path.glob("memory.db*"):
        assert needle not in candidate.read_bytes()


def test_memory_admission_allows_non_secret_facts_and_discussion(tmp_path):
    from harness.memory import SqliteMemory

    memory = SqliteMemory(str(tmp_path / "memory.db"))
    try:
        first = memory.propose(
            "Password rotation is required by the service policy",
            keys="security policy", project="repo")
        second = memory.propose(
            "The parser treats token = None as an absent value",
            keys="parser behavior", project="repo")
        assert first > 0 and second > 0
        assert memory.count() == 2
    finally:
        memory.close()


def test_host_active_and_preference_writes_route_secrets_to_vault_not_sqlite(tmp_path):
    from harness.memory import MemorySecretRejected, SqliteMemory

    path = tmp_path / "memory.db"
    active_marker = "NeverStoreHostPassword-9041!"
    preference_marker = "NeverStorePreferenceToken9041"
    block_marker = "NeverStoreCoreRecovery9041"
    memory = SqliteMemory(str(path))
    try:
        with pytest.raises(MemorySecretRejected, match="OS credential vault"):
            memory.remember(
                "service password: " + active_marker,
                keys="account credential", project="repo", status="active",
                source="local_user")
        with pytest.raises(MemorySecretRejected, match="OS credential vault"):
            memory.set_preference(
                "service.access_token", preference_marker, project="repo")
        with pytest.raises(MemorySecretRejected, match="OS credential vault"):
            memory.set_block(
                "global", "recovery_codes", block_marker)
        assert memory.count() == 0
        assert memory.db.execute("SELECT COUNT(*) FROM blocks").fetchone()[0] == 0
    finally:
        memory.close()

    for candidate in tmp_path.glob("memory.db*"):
        raw = candidate.read_bytes()
        assert active_marker.encode("utf-8") not in raw
        assert preference_marker.encode("utf-8") not in raw
        assert block_marker.encode("utf-8") not in raw


def test_existing_habit_update_cannot_persist_secret_evidence_or_provenance(tmp_path):
    from harness.memory import MemorySecretRejected, SqliteMemory

    path = tmp_path / "memory.db"
    marker = "NeverStoreHabitRefreshToken9041"
    memory = SqliteMemory(str(path))
    try:
        habit_id = memory.record_habit_observation(
            "routing.provider", "codex", project="repo",
            evidence={"choice": "configured"}, provenance={"run_id": 1})
        before = memory.get_claim(habit_id)
        with pytest.raises(MemorySecretRejected, match="OS credential vault"):
            memory.record_habit_observation(
                "routing.provider", "codex", project="repo",
                evidence={"refresh_token": marker},
                provenance={"run_id": 2})
        after = memory.get_claim(habit_id)
        assert after["observations"] == before["observations"] == 1
        assert marker not in json.dumps(after)
    finally:
        memory.close()
    for candidate in tmp_path.glob("memory.db*"):
        assert marker.encode("utf-8") not in candidate.read_bytes()


def test_legacy_basename_memory_is_read_only_and_ambiguity_fails_closed(tmp_path):
    from harness.memory import SqliteMemory, project_scope

    checkout_a = tmp_path / "owner-a" / "backend"
    checkout_b = tmp_path / "owner-b" / "backend"
    (checkout_a / ".git").mkdir(parents=True)
    (checkout_b / ".git").mkdir(parents=True)
    project_a = project_scope(str(checkout_a))
    project_b = project_scope(str(checkout_b))
    assert project_a.startswith("backend@")
    assert project_b.startswith("backend@") and project_b != project_a

    memory = SqliteMemory(str(tmp_path / "memory.db"))
    try:
        legacy_id = memory.remember(
            "legacy sentinel build uses make", keys="legacy sentinel",
            project="backend", scope="backend", status="verified")
        memory.set_block(
            "project:backend", "instructions", "Legacy build guidance uses make")

        # With one known canonical checkout, the old basename is a read-only
        # continuity alias; the durable fact itself is not rewritten.
        assert [hit["id"] for hit in memory.recall(
            "legacy sentinel", project=project_a)] == [legacy_id]
        assert [row["value"] for row in memory.core_blocks(
            ["project:" + project_a, "global"])] == ["Legacy build guidance uses make"]
        first = memory.legacy_project_alias_status(project_a)
        assert first["status"] == "read_only_available"
        assert memory.get_claim(legacy_id)["project"] == "backend"
        assert memory.get_claim(legacy_id)["scope"] == "backend"

        # Seeing a second same-basename checkout makes the provenance
        # unknowable. Neither side may consume the legacy rows until a local
        # host chooses their owner.
        assert memory.recall("legacy sentinel", project=project_b) == []
        assert memory.recall("legacy sentinel", project=project_a) == []
        assert memory.core_blocks(["project:" + project_a, "global"]) == []
        assert memory.core_blocks(["project:" + project_b, "global"]) == []
        ambiguous = memory.legacy_project_alias_status(project_a)
        assert ambiguous["status"] == "ambiguous_selection_required"
        assert set(ambiguous["candidates"]) == {project_a, project_b}

        selected = memory.select_legacy_project_alias(project_a)
        assert selected["status"] == "selected" and selected["selected"] == project_a
        assert [hit["id"] for hit in memory.recall(
            "legacy sentinel", project=project_a)] == [legacy_id]
        assert [row["value"] for row in memory.core_blocks(
            ["project:" + project_a, "global"])] == ["Legacy build guidance uses make"]
        assert memory.recall("legacy sentinel", project=project_b) == []
        assert memory.legacy_project_alias_status(project_b)["status"] == "selected_elsewhere"
        assert {row["id"] for row in memory.list_claims(project=project_a)} == {legacy_id}
    finally:
        memory.close()


def test_remember_tool_does_not_hand_secret_to_custom_adapter():
    from harness.tools import RememberTool

    class CapturingMemory:
        called = False

        def propose(self, *_args, **_kwargs):
            self.called = True
            raise AssertionError("secret crossed the tool boundary")

    memory = CapturingMemory()
    ctx = types.SimpleNamespace(
        memory=memory, project="repo", checkpoint_scope="session-1")
    result = RememberTool().run({
        "text": "MFA code is 483921", "keys": "account verification"}, ctx)
    assert "never stored" in result
    assert "483921" not in result
    assert memory.called is False

    ctx.checkpoint_scope = "access_token=NeverStoreCheckpoint9041"
    result = RememberTool().run({
        "text": "deployment uses make", "keys": "build workflow"}, ctx)
    assert "never stored" in result
    assert "NeverStoreCheckpoint9041" not in result
    assert memory.called is False


def test_settlement_refuses_secret_review_metadata_without_wal_leak(tmp_path):
    from harness.loop import Harness
    from harness.memory import SqliteMemory
    from harness.recorder import RunResult

    path = tmp_path / "memory.db"
    memory = SqliteMemory(str(path))
    harness = object.__new__(Harness)
    harness.memory = memory
    harness.project = "repo"
    producer = {"run_id": 41, "task_id": "learn", "provider": "stub", "model": "stub-1"}
    claim_id = memory.propose(
        "release builds use make", project="repo", scope="repo",
        source="run_consolidation", provenance=producer)
    result = RunResult(
        run_id=41, task_id="learn", provider="stub", model="stub-1",
        memory_claim_ids=[claim_id])
    marker = "NeverStoreThisAccessToken9041"
    try:
        settled = harness.settle_run_memory(
            result, True,
            {"passed": True, "source": "access_token=" + marker},
            source="test_host")
        assert settled == {"promoted": 0, "rejected": 0}
        assert memory.get_claim(claim_id)["status"] == "proposed"
    finally:
        memory.close()
    for candidate in tmp_path.glob("memory.db*"):
        assert marker.encode("utf-8") not in candidate.read_bytes()


def test_workidentity_concurrent_writers_do_not_lose_connections(tmp_path):
    from harness import workidentity

    root = str(tmp_path)
    # Threads cover the in-process mutex; processes cover the OS file lock.
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        assert sorted(pool.map(
            lambda index: _save_identity_connection((root, index)), range(50))) == list(range(50))
    with concurrent.futures.ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 2)) as pool:
        assert sorted(pool.map(
            _save_identity_connection, ((root, index) for index in range(50, 100)))) == list(range(50, 100))

    persisted = json.loads((tmp_path / "work-identities.json").read_text(encoding="utf-8"))
    assert set(persisted) == {"connection_%03d" % index for index in range(100)}
    assert not list(tmp_path.glob("work-identities.json.*.tmp"))
    assert workidentity._load(root) == persisted


def test_collie_mailbox_and_otp_are_isolated_by_state_root(tmp_path, monkeypatch):
    from harness import dogmail, workidentity

    root_a = tmp_path / "collie-a"
    root_b = tmp_path / "collie-b"
    root_a.mkdir()
    root_b.mkdir()

    def seed(root, dog, address):
        dogmail.save({
            "handle": {"name": "owner", "verified": True, "priv": "handle-private"},
            "dogs": {dog: {"address": address, "priv": "dog-private",
                            "pub": "dog-public", "cursor": 0}},
        }, root)
        workidentity._save({
            "collie_mail": {"connected": True, "dog": dog, "verified_at": 1},
        }, root)

    seed(root_a, "rowan", "rowan.a@collie.run")
    seed(root_b, "juno", "juno.b@collie.run")
    assert workidentity.public_identity(root_a)["email"] == "rowan.a@collie.run"
    assert workidentity.public_identity(root_b)["email"] == "juno.b@collie.run"

    def fake_fetch(*_args, **kwargs):
        root = os.path.abspath(str(kwargs.get("state_dir")))
        if root == os.path.abspath(str(root_a)):
            code = "111111"
        elif root == os.path.abspath(str(root_b)):
            code = "222222"
        else:
            raise AssertionError("mail fetch escaped its Collie state root")
        return [{
            "at": 2_000_000_000, "from": "login@example.test",
            "subject": "Example verification code", "text": "Your code is " + code,
        }]

    monkeypatch.setattr(dogmail, "fetch", fake_fetch)
    monkeypatch.setattr(dogmail.time, "time", lambda: 2_000_000_000)
    code_a, _ = workidentity.take_verification_code(
        "Example", state_dir=root_a, channel="email")
    code_b, _ = workidentity.take_verification_code(
        "Example", state_dir=root_b, channel="email")
    assert code_a == "111111" and code_b == "222222"


def test_corrupt_mail_and_work_identity_state_are_preserved_and_fail_closed(tmp_path):
    from harness import dogmail, workidentity

    mail_root = tmp_path / "mail-state"
    identity_root = tmp_path / "identity-state"
    mail_root.mkdir()
    identity_root.mkdir()
    mail_path = mail_root / "mail.json"
    identity_path = identity_root / "work-identities.json"
    mail_raw = b'{"handle":{"priv":"irreplaceable-mail-key"}'
    identity_raw = b'{"google_voice":{"number":"+16505550101"}'
    mail_path.write_bytes(mail_raw)
    identity_path.write_bytes(identity_raw)

    with pytest.raises(dogmail.MailStateCorrupt, match="original was preserved"):
        dogmail.load(mail_root)
    with pytest.raises(dogmail.MailStateCorrupt):
        dogmail.save({"handle": {}, "dogs": {}}, mail_root)
    with pytest.raises(workidentity.WorkIdentityStateCorrupt, match="original was preserved"):
        workidentity._load(identity_root)
    with pytest.raises(workidentity.WorkIdentityStateCorrupt):
        workidentity._save({"collie_mail": {"connected": False}}, identity_root)

    assert mail_path.read_bytes() == mail_raw
    assert identity_path.read_bytes() == identity_raw
    mail_backups = list(mail_root.glob("mail.json.corrupt-*.bak"))
    identity_backups = list(identity_root.glob("work-identities.json.corrupt-*.bak"))
    assert len(mail_backups) == 1 and mail_backups[0].read_bytes() == mail_raw
    assert len(identity_backups) == 1 and identity_backups[0].read_bytes() == identity_raw


class _ReadableMissingBackend:
    def get(self, *_args, **_kwargs):
        from harness.identityvault import SecretNotFound
        raise SecretNotFound("expected missing probe")


class _UnconfirmedBackend:
    pass


class _UnavailableBackend:
    def get(self, *_args, **_kwargs):
        from harness.identityvault import VaultUnavailable
        raise VaultUnavailable("credential service unavailable")


@pytest.mark.parametrize("backend,available,operational,locked,status", [
    (_ReadableMissingBackend(), False, True, None, "operational_lock_unconfirmed"),
    (_UnconfirmedBackend(), False, None, None, "detected_unconfirmed"),
    (_UnavailableBackend(), False, False, None, "unavailable_or_locked"),
])
def test_vault_status_distinguishes_detection_from_operation(
        monkeypatch, tmp_path, backend, available, operational, locked, status):
    from harness import accountcontrol

    monkeypatch.setattr(
        accountcontrol, "IdentityVault",
        lambda **_kwargs: types.SimpleNamespace(backend=backend))
    result = accountcontrol.vault_status(tmp_path)
    assert result["backend_detected"] is True
    assert result["available"] is available
    assert result["operational"] is operational
    assert result["status"] == status
    assert result["locked"] is locked
    assert result["plaintext_fallback"] is False


def test_public_account_plan_rejects_secret_factors_and_legacy_plan_can_cancel(tmp_path):
    from harness import accountcontrol

    body = {
        "origin": "https://example.test", "username": "collie@example.test",
        "legal_principal": "owner-authorized-collie",
        "factor_classes": ["password"],
    }
    with pytest.raises(ValueError, match="host-side account.prepare"):
        accountcontrol.plan_account(body, tmp_path, "collie-a")

    # Older callers could create this metadata-only shape directly.  It names
    # a desired factor but contains no vault reference or credential bytes.
    with accountcontrol.open_registry(tmp_path, "collie-a") as registry:
        legacy = registry.create(**body)
        assert legacy["status"] == "planned"
    cancelled = accountcontrol.cancel_plan(
        legacy["account_id"], tmp_path, "collie-a")
    assert cancelled["account"]["status"] == "retired"
    assert cancelled["credentials_deleted"] is False
