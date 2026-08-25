"""Model/UI-safe account and communications control-plane projections.

This module deliberately exposes inventory and capability truth, not credentials.
Account metadata lives in SQLite, credential bytes remain in the native OS vault,
and the web control plane offers no local-only rotation that could diverge from a
remote service account.
"""
from __future__ import annotations

import os
from pathlib import Path

from .accounts import AccountRegistry, SECRET_FACTORS
from .identityvault import (
    IdentityVault, SecretNotFound, VaultError, VaultUnavailable,
)


_ACCOUNT_PUBLIC_FIELDS = (
    "account_id", "collie_id", "origin", "tenant", "username",
    "normalized_username", "ownership", "legal_principal", "status", "scopes",
    "factor_classes", "created_at", "verified_at", "rotated_at", "updated_at",
    "retired_at",
)
_ACCOUNT_MODEL_FIELDS = (
    "account_id", "collie_id", "origin", "tenant", "username", "ownership",
    "status", "scopes", "factor_classes", "created_at", "verified_at",
    "rotated_at", "updated_at", "retired_at",
)


def _root(state_dir=None) -> str:
    if state_dir:
        return os.path.abspath(os.path.expanduser(str(state_dir)))
    from .controlplane import state_dir as current_state_dir
    return current_state_dir()


def _backend_label(backend) -> str:
    name = type(backend).__name__.lower()
    if "windowsdpapi" in name:
        return "windows_dpapi_current_user"
    if "macoskeychain" in name:
        return "macos_login_keychain"
    if "linuxsecretservice" in name:
        return "linux_secret_service"
    return "native_os_credential_store"


def vault_status(state_dir=None) -> dict:
    """Report detected and proven vault state without a persistent test write.

    Constructing a backend proves only that its library/command exists.  It does
    not prove that DPAPI works for this user, that Keychain is unlocked, or that
    a Secret Service is reachable.  A read of a randomized nonexistent entry is
    a side-effect-free operational probe on macOS/Linux; DPAPI is exercised by
    an in-memory protect/unprotect round trip.  Anything we cannot prove stays
    explicitly unconfirmed and the compatibility ``available`` bit stays false.
    """
    try:
        vault = IdentityVault(state_dir=_root(state_dir))
    except VaultUnavailable:
        return {
            "available": False,
            "os_backed": False,
            "status": "unavailable",
            "backend": "",
            "backend_detected": False,
            "operational": False,
            "locked": None,
            "plaintext_fallback": False,
        }
    backend = vault.backend
    backend_name = _backend_label(backend)
    operational = None
    locked = None
    try:
        if "windowsdpapi" in type(backend).__name__.lower():
            # DPAPI's backend.get first reads a persistent blob and therefore
            # cannot prove the cryptographic service.  Exercise only its native
            # in-memory operations with disposable random bytes.
            raw = os.urandom(32)
            entropy = os.urandom(32)
            protected = backend._protect(raw, entropy)
            operational = backend._unprotect(protected, entropy) == raw
            locked = False if operational else None
        else:
            # SecretNotFound is the desired result: the service completed a
            # query and no persistent probe entry was created.
            backend.get(
                "run.collie.vault.read-probe",
                "missing-" + os.urandom(16).hex(), os.urandom(32))
            # An existing randomized item is astronomically unlikely, but a
            # successful read still proves the backend is operational.
            operational = True
            locked = False
    except SecretNotFound:
        operational = True
        # A missing-item lookup proves the service answered, not that an
        # existing protected item could be unlocked without interaction.
        locked = None
    except (VaultUnavailable, VaultError, OSError):
        operational = False
        # Backends intentionally normalize platform errors, so claiming
        # "locked" here would be another false green/red.  Keep it unknown.
        locked = None
    except (AttributeError, TypeError):
        # Injectable or future backends without a safe read-only probe remain
        # detected but unconfirmed.
        operational = None
        locked = None
    except Exception:
        # An unexpected provider/runtime failure is still evidence that this
        # instance is not currently usable.  Never turn it into availability.
        operational = False
        locked = None
    available = operational is True and locked is False
    status = ("operational" if available else
              "operational_lock_unconfirmed" if operational is True else
              "unavailable_or_locked" if operational is False else
              "detected_unconfirmed")
    return {
        "available": available,
        "os_backed": True,
        "status": status,
        "backend": backend_name,
        "backend_detected": True,
        "operational": operational,
        "locked": locked,
        "plaintext_fallback": False,
    }


class _UnavailableVault:
    """Read-only registry seam when the native credential service is unavailable."""

    @staticmethod
    def _deny(*_args, **_kwargs):
        raise VaultUnavailable("the OS credential store is unavailable")

    put = get = use = delete = _deny


def open_registry(state_dir=None, collie_id="") -> AccountRegistry:
    root = _root(state_dir)
    try:
        vault = IdentityVault(state_dir=root)
    except VaultUnavailable:
        # Listing and planning non-secret metadata remain useful. Every secret
        # operation still fails closed through this seam.
        vault = _UnavailableVault()
    return AccountRegistry(
        Path(root) / "accounts.db", collie_id=str(collie_id), vault=vault)


def _project_account(row, *, model=False) -> dict:
    allowed = _ACCOUNT_MODEL_FIELDS if model else _ACCOUNT_PUBLIC_FIELDS
    return {key: row.get(key) for key in allowed if key in row}


def communications_status(state_dir=None, collie_id="") -> dict:
    """Truthful provider/runtime status; this function performs no network work."""
    from .workidentity import model_identity, public_connections

    root = _root(state_dir)
    rows = {row.get("id"): row for row in public_connections(root)}
    voice = rows.get("google_voice") or {}
    identity = model_identity(root)
    runtime_collie_id = str(collie_id or identity.get("collie_id") or identity.get("principal") or "")
    try:
        from .telephony_twilio import environment_configuration_status
        programmable = environment_configuration_status(collie_id=runtime_collie_id)
    except Exception:
        # Status is an allowlisted truth surface, never a reason for the identity
        # panel to fail open or expose a host configuration exception.
        programmable = {
            "configured": False, "status": "not_configured",
            "provider": "twilio", "voice_provider": "elevenlabs",
            "runtime": "elevenlabs_native_twilio",
            "provider_probe": "not_performed",
        }
    connected = bool(voice.get("connected"))
    assigned = connected and voice.get("ownership") == "user_owned_assigned_to_collie"
    number = str(identity.get("phone") or "")
    line_hint = ("••••••" + number[-4:]) if number else str(voice.get("account_masked") or "")

    # The browser bridge currently implements identity discovery and OTP receipt,
    # not general outbound messaging or realtime media. Keep those facts separate
    # from the fact that the line itself can make calls/send SMS in Google Voice.
    return {
        "google_voice": {
            "provider": "google_voice",
            "connected": connected,
            "assigned": assigned,
            "ownership": str(voice.get("ownership") or ""),
            "line_hint": line_hint,
            "verification_codes": {
                "configured": connected,
                "runtime": "browser_receive_and_fill" if connected else "not_configured",
                "persistent": False,
            },
            "sms": {
                "line_capable": connected,
                "automation_permitted": False,
                "collie_dispatch_configured": False,
                "runtime": "draft_then_user_send" if connected else "not_configured",
            },
            "calls": {
                "line_capable": connected,
                "automation_permitted": False,
                "collie_dispatch_configured": False,
                "runtime": "manual_handoff_only" if connected else "not_configured",
            },
        },
        "programmable_telephony": {
            "configured": bool(programmable.get("configured")),
            "providers": (["twilio"] if programmable.get("configured") else []),
            "status": str(programmable.get("status") or "not_configured"),
            "runtime": str(programmable.get("runtime") or "elevenlabs_native_twilio"),
            "provider_probe": str(programmable.get("provider_probe") or "not_performed"),
            "adapters": [
                {
                    "kind": "verified_assigned_caller_id",
                    "configured": bool(programmable.get("configured")),
                    "identity_source": "google_voice_assigned_line",
                    "runtime": str(programmable.get("runtime") or "elevenlabs_native_twilio"),
                    "line_hint": str(programmable.get("line_hint") or ""),
                    "capabilities": {
                        "outbound_calls": True,
                        "inbound_calls": False,
                        "sms": False,
                    },
                },
                {
                    "kind": "provider_owned_registered_number",
                    "configured": False,
                    "identity_source": "provider_owned_number",
                    "capabilities": {
                        "outbound_calls": True,
                        "inbound_calls": True,
                        "sms": "requires_sender_registration",
                    },
                },
            ],
        },
        "voice_synthesis": {
            "configured": bool(programmable.get("configured")),
            "provider": "elevenlabs" if programmable.get("configured") else "",
            "voice": "agent_configured" if programmable.get("configured") else "",
            "status": str(programmable.get("status") or "not_configured"),
        },
    }


def snapshot(state_dir=None, collie_id="", *, model=False,
             include_retired=False, include_communications=True) -> dict:
    """Return an allowlisted snapshot with no secret values or opaque vault refs."""
    root = _root(state_dir)
    with open_registry(root, collie_id) as registry:
        accounts = [
            _project_account(row, model=model)
            for row in registry.list(include_retired=bool(include_retired))
        ]
    result = {
        "collie_id": str(collie_id),
        "accounts": accounts,
        "vault": vault_status(root),
    }
    if include_communications:
        result["communications"] = communications_status(root, collie_id=collie_id)
    return result


def plan_account(body: dict, state_dir=None, collie_id="") -> dict:
    """Create only a planned metadata record; never create a local credential."""
    body = body if isinstance(body, dict) else {}
    allowed = {
        "action", "origin", "username", "tenant", "ownership",
        "legal_principal", "scopes", "factor_classes", "idempotency_key",
    }
    unexpected = sorted(set(body) - allowed)
    if unexpected:
        raise ValueError("unsupported account-plan field(s): " + ", ".join(unexpected))
    scopes = body.get("scopes") or []
    factors = body.get("factor_classes") or []
    if not isinstance(scopes, list) or not all(
            isinstance(item, str) and 0 < len(item) <= 160 for item in scopes):
        raise ValueError("scopes must be a list of bounded strings")
    if not isinstance(factors, list) or not all(isinstance(item, str) for item in factors):
        raise ValueError("factor_classes must be a string list")
    secret_factors = sorted(set(factors).intersection(SECRET_FACTORS))
    if secret_factors:
        raise ValueError(
            "secret factor classes require host-side account.prepare; "
            "the public metadata planner cannot create credentials")
    with open_registry(state_dir, collie_id) as registry:
        account = registry.create(
            origin=body.get("origin", ""),
            username=body.get("username", ""),
            tenant=body.get("tenant", ""),
            ownership=body.get("ownership") or "collie_owned_work_identity",
            legal_principal=body.get("legal_principal", ""),
            scopes=scopes,
            factor_classes=factors,
            idempotency_key=body.get("idempotency_key", ""),
        )
    return {
        "event": "account.metadata_planned",
        "account": _project_account(account),
        "credentials_created": False,
    }


def cancel_plan(account_id: str, state_dir=None, collie_id="") -> dict:
    """Retire only an untouched plan; remote/local active accounts are refused."""
    with open_registry(state_dir, collie_id) as registry:
        account = registry.get(account_id)
        if account.get("status") != "planned":
            raise ValueError(
                "only an untouched planned record can be cancelled locally; "
                "active account lifecycle must be coordinated with the remote service")
        # create_credentials always moves a planned record to registering before
        # storing any secret.  A legacy planned row may still name a desired
        # secret factor even though no credential exists; it must remain
        # cancellable instead of becoming an orphaned, un-actionable plan.
        registry.retire(account_id)
        retired = registry.get(account_id)
    return {
        "event": "account.metadata_plan_cancelled",
        "account": _project_account(retired),
        "credentials_deleted": False,
    }


__all__ = [
    "cancel_plan", "communications_status", "open_registry", "plan_account",
    "snapshot", "vault_status",
]
