import sys

import pytest

from harness import identityvault


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
        item = self.items.get((service, account))
        if item is None:
            return False
        if item[1] != entropy:
            raise identityvault.SecretNotFound("wrong binding")
        del self.items[(service, account)]
        return True


def test_vault_reference_is_opaque_bound_and_deletable():
    backend = FakeBackend()
    vault = identityvault.IdentityVault(backend=backend)
    ref = vault.put("Never-on-disk!", collie_id="collie-a", account="acct-1", kind="password")

    assert ref.startswith("cv1_")
    assert "Never" not in ref
    assert vault.get(ref, collie_id="collie-a", account="acct-1", kind="password") == b"Never-on-disk!"
    with pytest.raises(identityvault.SecretNotFound):
        vault.get(ref, collie_id="collie-b", account="acct-1", kind="password")
    with pytest.raises(identityvault.SecretNotFound):
        vault.get(ref, collie_id="collie-a", account="acct-2", kind="password")
    with pytest.raises(identityvault.SecretNotFound):
        vault.get(ref, collie_id="collie-a", account="acct-1", kind="totp")

    assert vault.delete(ref, collie_id="collie-a", account="acct-1", kind="password") is True
    assert vault.delete(ref, collie_id="collie-a", account="acct-1", kind="password") is False


def test_use_wipes_temporary_buffer_after_consumer_returns():
    vault = identityvault.IdentityVault(backend=FakeBackend())
    ref = vault.put(b"short-lived", collie_id="c", account="a", kind="password")
    observed = []

    def consume(value):
        observed.append(value)
        assert bytes(value) == b"short-lived"
        return "filled"

    assert vault.use(ref, collie_id="c", account="a", kind="password", consumer=consume) == "filled"
    assert bytes(observed[0]) == b"\0" * len(b"short-lived")


def test_unavailable_native_backend_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(identityvault.sys, "platform", "linux")
    monkeypatch.setattr(identityvault.shutil, "which", lambda _name: None)
    with pytest.raises(identityvault.VaultUnavailable):
        identityvault.platform_backend(tmp_path)


def test_invalid_secret_reference_is_rejected_before_backend_access():
    vault = identityvault.IdentityVault(backend=FakeBackend())
    with pytest.raises(ValueError):
        vault.get("../../plaintext", collie_id="c", account="a", kind="password")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI integration")
def test_windows_dpapi_round_trip_stores_only_ciphertext(tmp_path):
    vault = identityvault.IdentityVault(state_dir=tmp_path)
    raw = b"DPAPI-only-password-123!"
    ref = vault.put(raw, collie_id="rowan", account="acct-1", kind="password")
    files = list((tmp_path / "account-vault").glob("*.dpapi"))
    assert len(files) == 1
    assert raw not in files[0].read_bytes()
    assert vault.get(ref, collie_id="rowan", account="acct-1", kind="password") == raw
    assert vault.delete(ref, collie_id="rowan", account="acct-1", kind="password") is True
