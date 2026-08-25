"""Collie account registry: lifecycle metadata beside, never inside, the vault.

This SQLite database is an inventory and state machine.  It intentionally holds
no passwords, TOTP seeds, recovery codes, OAuth tokens, or verification codes.
Those values are stored by :mod:`harness.identityvault`; this module persists
only random opaque references that have no meaning without the Collie/account
binding supplied to the OS-backed vault.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import re
import secrets
import sqlite3
import string
import threading
import time
import unicodedata
import urllib.parse
from pathlib import Path

from .identityvault import IdentityVault, SecretNotFound, VaultError


STATUSES = (
    "planned", "registering", "challenge_wait", "active", "degraded",
    "rotating", "transfer_pending", "deletion_pending", "retired",
)
OWNERSHIP_CLASSES = (
    "user_owned_assigned_to_collie",
    "collie_owned_work_identity",
    "organization_owned_assigned_to_collie",
)
SECRET_FACTORS = ("password", "totp", "recovery_codes")
FACTOR_CLASSES = (
    "password", "email_otp", "sms_otp", "totp", "passkey", "security_key",
    "recovery_codes", "oauth", "magic_link",
)

_TRANSITIONS = {
    "planned": {"registering", "deletion_pending", "retired"},
    "registering": {"challenge_wait", "degraded", "deletion_pending"},
    "challenge_wait": {"registering", "degraded", "deletion_pending"},
    "active": {"degraded", "rotating", "transfer_pending", "deletion_pending"},
    "degraded": {"registering", "active", "rotating", "transfer_pending",
                 "deletion_pending"},
    "rotating": {"active", "degraded", "deletion_pending"},
    "transfer_pending": {"active", "degraded", "deletion_pending", "retired"},
    "deletion_pending": {"active", "retired"},
    "retired": set(),
}


class AccountRegistryError(RuntimeError):
    pass


class AccountNotFound(AccountRegistryError, KeyError):
    pass


class DuplicateAccount(AccountRegistryError):
    pass


class IdempotencyConflict(AccountRegistryError):
    pass


class InvalidTransition(AccountRegistryError):
    pass


class CredentialExists(AccountRegistryError):
    pass


def _now() -> int:
    return int(time.time())


def _json_list(value) -> str:
    return json.dumps(sorted({str(item) for item in (value or []) if str(item)}),
                      separators=(",", ":"), ensure_ascii=True)


def _load_list(value) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return sorted({str(item) for item in parsed if isinstance(item, str) and item})


def _load_refs(value) -> dict[str, str]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): str(ref) for key, ref in parsed.items()
            if key in SECRET_FACTORS and isinstance(ref, str) and ref.startswith("cv1_")}


def normalize_username(username: str) -> str:
    value = unicodedata.normalize("NFKC", str(username or "")).strip().casefold()
    if not value or len(value) > 320 or "\x00" in value:
        raise ValueError("username must be a non-empty bounded value")
    return value


def normalize_origin(origin: str) -> str:
    value = str(origin or "").strip()
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("origin must be an http(s) origin")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("origin must not contain credentials, query, or fragment")
    host = parsed.hostname.lower().rstrip(".")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("origin hostname is invalid") from exc
    scheme = parsed.scheme.lower()
    loopback = host in {"localhost", "127.0.0.1", "::1"}
    if scheme != "https" and not (
            loopback and os.environ.get("COLLIE_ALLOW_INSECURE_ACCOUNT_LOOPBACK") == "1"):
        raise ValueError(
            "account credentials require HTTPS (or an explicitly enabled loopback dev origin)")
    port = parsed.port
    rendered_host = ("[" + host + "]") if ":" in host else host
    if port and not ((scheme == "https" and port == 443) or
                     (scheme == "http" and port == 80)):
        rendered_host = "%s:%d" % (rendered_host, port)
    return "%s://%s" % (scheme, rendered_host)


def normalize_tenant(tenant: str) -> str:
    value = unicodedata.normalize("NFKC", str(tenant or "")).strip().casefold()
    if len(value) > 320 or "\x00" in value:
        raise ValueError("tenant is too long")
    return value


def generate_password(length: int = 32) -> str:
    """Generate a high-entropy password with all common character classes."""
    length = int(length)
    if length < 24 or length > 256:
        raise ValueError("password length must be between 24 and 256")
    classes = (string.ascii_lowercase, string.ascii_uppercase, string.digits,
               "!#$%&()*+,-./:;<=>?@[]^_{|}~")
    chars = [secrets.choice(group) for group in classes]
    alphabet = "".join(classes)
    chars.extend(secrets.choice(alphabet) for _ in range(length - len(chars)))
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def generate_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def generate_recovery_codes(count: int = 10) -> list[str]:
    if not 1 <= int(count) <= 50:
        raise ValueError("recovery code count must be between 1 and 50")
    return ["%s-%s-%s" % (secrets.token_hex(2), secrets.token_hex(2),
                           secrets.token_hex(2)) for _ in range(int(count))]


class AccountRegistry:
    """A registry instance is permanently scoped to one Collie identity."""

    def __init__(self, path: str | os.PathLike[str], *, collie_id: str,
                 vault: IdentityVault):
        self.path = os.path.abspath(os.path.expanduser(str(path)))
        self.collie_id = str(collie_id or "").strip()
        if not self.collie_id or "\x00" in self.collie_id:
            raise ValueError("collie_id is required")
        if vault is None:
            raise ValueError("an OS-backed vault is required")
        self.vault = vault
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._init_schema()
        try:
            from . import plat
            plat.chmod_private(self.path)
        except Exception:
            pass

    def _init_schema(self) -> None:
        statuses = ",".join("'%s'" % value for value in STATUSES)
        ownership = ",".join("'%s'" % value for value in OWNERSHIP_CLASSES)
        with self.db:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS account_registry (
                    account_id TEXT PRIMARY KEY,
                    collie_id TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    tenant TEXT NOT NULL DEFAULT '',
                    normalized_username TEXT NOT NULL,
                    username_display TEXT NOT NULL,
                    ownership TEXT NOT NULL CHECK (ownership IN (%s)),
                    legal_principal TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK (status IN (%s)),
                    scopes_json TEXT NOT NULL DEFAULT '[]',
                    factor_classes_json TEXT NOT NULL DEFAULT '[]',
                    secret_refs_json TEXT NOT NULL DEFAULT '{}',
                    idempotency_key TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    verified_at INTEGER NOT NULL DEFAULT 0,
                    rotated_at INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL,
                    retired_at INTEGER NOT NULL DEFAULT 0,
                    expected_active_text TEXT NOT NULL DEFAULT '',
                    expected_active_path TEXT NOT NULL DEFAULT '',
                    pre_submit_state_digest TEXT NOT NULL DEFAULT '',
                    submission_started_at INTEGER NOT NULL DEFAULT 0,
                    completion_evidence_digest TEXT NOT NULL DEFAULT '',
                    credential_setup_token TEXT NOT NULL DEFAULT '',
                    credential_setup_at INTEGER NOT NULL DEFAULT 0,
                    credential_mutation_phase TEXT NOT NULL DEFAULT '',
                    credential_pending_refs_json TEXT NOT NULL DEFAULT '{}',
                    credential_cleanup_refs_json TEXT NOT NULL DEFAULT '{}'
                )
            """ % (ownership, statuses))
            columns = {row[1] for row in self.db.execute(
                "PRAGMA table_info(account_registry)")}
            additions = {
                "expected_active_text": "TEXT NOT NULL DEFAULT ''",
                "expected_active_path": "TEXT NOT NULL DEFAULT ''",
                "pre_submit_state_digest": "TEXT NOT NULL DEFAULT ''",
                "submission_started_at": "INTEGER NOT NULL DEFAULT 0",
                "completion_evidence_digest": "TEXT NOT NULL DEFAULT ''",
                "credential_setup_token": "TEXT NOT NULL DEFAULT ''",
                "credential_setup_at": "INTEGER NOT NULL DEFAULT 0",
                "credential_mutation_phase": "TEXT NOT NULL DEFAULT ''",
                "credential_pending_refs_json": "TEXT NOT NULL DEFAULT '{}'",
                "credential_cleanup_refs_json": "TEXT NOT NULL DEFAULT '{}'",
            }
            for name, declaration in additions.items():
                if name not in columns:
                    self.db.execute(
                        "ALTER TABLE account_registry ADD COLUMN %s %s" %
                        (name, declaration))
            self.db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS account_identity_unique
                ON account_registry(collie_id, origin, tenant, normalized_username)
            """)
            self.db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS account_idempotency_unique
                ON account_registry(collie_id, idempotency_key)
                WHERE idempotency_key <> ''
            """)
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS account_submission_steps (
                    account_id TEXT NOT NULL,
                    step TEXT NOT NULL,
                    snapshot_digest TEXT NOT NULL,
                    expected_active_text TEXT NOT NULL DEFAULT '',
                    expected_active_path TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL CHECK (state IN ('firing','fired','confirmed')),
                    previous_account_status TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(account_id, step),
                    UNIQUE(account_id, snapshot_digest),
                    FOREIGN KEY(account_id) REFERENCES account_registry(account_id)
                        ON DELETE CASCADE
                )
            """)
            step_columns = {row[1] for row in self.db.execute(
                "PRAGMA table_info(account_submission_steps)")}
            step_additions = {
                "expected_active_text": "TEXT NOT NULL DEFAULT ''",
                "expected_active_path": "TEXT NOT NULL DEFAULT ''",
            }
            for name, declaration in step_additions.items():
                if name not in step_columns:
                    self.db.execute(
                        "ALTER TABLE account_submission_steps ADD COLUMN %s %s" %
                        (name, declaration))

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    @staticmethod
    def _public(row: sqlite3.Row | dict) -> dict:
        # secret_refs_json and idempotency_key are intentionally absent.
        return {
            "account_id": row["account_id"],
            "collie_id": row["collie_id"],
            "origin": row["origin"],
            "tenant": row["tenant"],
            "username": row["username_display"],
            "normalized_username": row["normalized_username"],
            "ownership": row["ownership"],
            "legal_principal": row["legal_principal"],
            "status": row["status"],
            "scopes": _load_list(row["scopes_json"]),
            "factor_classes": _load_list(row["factor_classes_json"]),
            "created_at": int(row["created_at"]),
            "verified_at": int(row["verified_at"]),
            "rotated_at": int(row["rotated_at"]),
            "updated_at": int(row["updated_at"]),
            "retired_at": int(row["retired_at"]),
        }

    @staticmethod
    def _receipt(event: str, row: sqlite3.Row | dict, *, factors=()) -> dict:
        """Audit-safe metadata: no secret values and no vault references."""
        return {
            "event": event,
            "account_id": row["account_id"],
            "collie_id": row["collie_id"],
            "origin": row["origin"],
            "status": row["status"],
            "factor_classes": sorted({str(item) for item in factors}),
            "at": _now(),
        }

    def _row(self, account_id: str) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT * FROM account_registry WHERE account_id=? AND collie_id=?",
            (str(account_id), self.collie_id)).fetchone()
        if row is None:
            raise AccountNotFound("account does not exist for this Collie")
        return row

    def get(self, account_id: str) -> dict:
        with self._lock:
            return self._public(self._row(account_id))

    def list(self, *, include_retired: bool = False) -> list[dict]:
        with self._lock:
            sql = "SELECT * FROM account_registry WHERE collie_id=?"
            params: tuple = (self.collie_id,)
            if not include_retired:
                sql += " AND status <> 'retired'"
            sql += " ORDER BY updated_at DESC, account_id"
            return [self._public(row) for row in self.db.execute(sql, params)]

    def create(self, *, origin: str, username: str, tenant: str = "",
               ownership: str = "user_owned_assigned_to_collie",
               legal_principal: str = "", scopes=(), factor_classes=(),
               idempotency_key: str = "") -> dict:
        origin = normalize_origin(origin)
        normalized = normalize_username(username)
        display = str(username).strip()
        tenant = normalize_tenant(tenant)
        ownership = str(ownership)
        if ownership not in OWNERSHIP_CLASSES:
            raise ValueError("unsupported ownership class")
        legal_principal = str(legal_principal or "").strip()
        if not legal_principal or len(legal_principal) > 1024 or "\x00" in legal_principal:
            raise ValueError("legal_principal is required and must be bounded")
        idempotency_key = str(idempotency_key or "").strip()
        if len(idempotency_key) > 256 or "\x00" in idempotency_key:
            raise ValueError("idempotency_key is too long")
        factors = sorted({str(item) for item in factor_classes if str(item)})
        if any(item not in FACTOR_CLASSES for item in factors):
            raise ValueError("unsupported factor class")
        requested_scopes = _json_list(scopes)
        requested_factors = _json_list(factors)
        now = _now()
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if idempotency_key:
                    existing = self.db.execute(
                        "SELECT * FROM account_registry WHERE collie_id=? AND idempotency_key=?",
                        (self.collie_id, idempotency_key)).fetchone()
                    if existing is not None:
                        identity = (existing["origin"], existing["tenant"],
                                    existing["normalized_username"], existing["ownership"],
                                    existing["legal_principal"], existing["scopes_json"])
                        requested = (origin, tenant, normalized, ownership, legal_principal,
                                     requested_scopes)
                        # Factor classes are lifecycle state, not an immutable
                        # creation-intent field: create_credentials() legitimately
                        # adds password/TOTP/recovery factors after the planned row
                        # is created. A retry with the original factor subset must
                        # remain idempotent, while reusing the key to request a
                        # different new factor is still a conflict.
                        existing_factors = set(_load_list(existing["factor_classes_json"]))
                        requested_factor_set = set(factors)
                        if identity != requested or not requested_factor_set.issubset(
                                existing_factors):
                            raise IdempotencyConflict(
                                "idempotency key is already bound to another account intent")
                        self.db.commit()
                        return self._public(existing)
                duplicate = self.db.execute("""
                    SELECT account_id FROM account_registry
                    WHERE collie_id=? AND origin=? AND tenant=? AND normalized_username=?
                """, (self.collie_id, origin, tenant, normalized)).fetchone()
                if duplicate is not None:
                    raise DuplicateAccount("this Collie already has that service account")
                account_id = "acct_" + secrets.token_urlsafe(18)
                self.db.execute("""
                    INSERT INTO account_registry(
                        account_id, collie_id, origin, tenant, normalized_username,
                        username_display, ownership, legal_principal, status, scopes_json,
                        factor_classes_json, secret_refs_json, idempotency_key,
                        created_at, updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (account_id, self.collie_id, origin, tenant, normalized, display,
                        ownership, legal_principal, "planned", requested_scopes,
                        requested_factors, "{}", idempotency_key, now, now))
                row = self._row(account_id)
                self.db.commit()
                return self._public(row)
            except Exception:
                self.db.rollback()
                raise

    def transition(self, account_id: str, status: str) -> dict:
        status = str(status)
        if status not in STATUSES:
            raise ValueError("unknown account status")
        with self._lock, self.db:
            row = self._row(account_id)
            current = row["status"]
            if row["credential_setup_token"]:
                raise InvalidTransition(
                    "a credential mutation is in progress or requires recovery")
            if status == current:
                return self._public(row)
            if status not in _TRANSITIONS[current]:
                raise InvalidTransition("illegal account transition: %s -> %s" %
                                        (current, status))
            if status == "active" and not int(row["verified_at"]):
                raise InvalidTransition(
                    "new account activation requires bound completion evidence")
            if (current == "deletion_pending" and status == "active"
                    and not int(row["verified_at"])
                    and not int(row["submission_started_at"])):
                # ``abort_prepared`` releases its SQLite transaction while it
                # erases OS-vault entries.  This shape identifies that
                # local-only abort fence, not a previously verified account
                # being restored from retirement.  Never let another process
                # resurrect the row after its credentials have been erased.
                raise InvalidTransition(
                    "an unsubmitted account preparation is being aborted")
            now = _now()
            verified = int(row["verified_at"])
            retired = int(row["retired_at"])
            if status == "active" and not verified:
                verified = now
            if status == "retired":
                retired = now
            self.db.execute("""
                UPDATE account_registry SET status=?, verified_at=?, retired_at=?, updated_at=?
                WHERE account_id=? AND collie_id=?
            """, (status, verified, retired, now, account_id, self.collie_id))
            return self._public(self._row(account_id))

    def begin_submission(self, account_id: str, *, step: str,
                         expected_active_text: str, expected_active_path: str,
                         pre_state_digest: str) -> dict:
        """Persist the MAC-bound active-state contract before the remote click."""
        text = " ".join(str(expected_active_text or "").split())
        path = str(expected_active_path or "").strip()
        digest = str(pre_state_digest or "").strip().lower()
        step = str(step or "").strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", step):
            raise ValueError("submission step must be a safe bounded label")
        generic = {"account", "account active", "active", "dashboard", "home",
                   "success", "welcome", "done", "complete", "completed"}
        if (len(text) < 12 or text.casefold() in generic
                or len(set(re.findall(r"[A-Za-z0-9\u0080-\uffff]", text))) < 6):
            raise ValueError("expected active text must be a specific visible postcondition")
        if (not path.startswith("/") or path == "/" or len(path) < 4
                or "?" in path or "#" in path or ".." in path):
            raise ValueError("expected active path must be a specific query-free absolute path")
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("pre-submit page-state digest is required")
        now = _now()
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(account_id)
                if row["status"] not in {"registering", "challenge_wait"}:
                    raise InvalidTransition("account is not ready for a registration submit")
                if (row["credential_setup_token"]
                        or _load_refs(row["credential_pending_refs_json"])):
                    raise InvalidTransition(
                        "credential setup is still in progress or requires recovery")
                inflight = self.db.execute("""
                    SELECT step FROM account_submission_steps
                    WHERE account_id=? AND state='firing' LIMIT 1
                """, (account_id,)).fetchone()
                if inflight is not None:
                    raise InvalidTransition(
                        "another registration step is still firing and must be reconciled")
                duplicate = self.db.execute("""
                    SELECT step,state FROM account_submission_steps
                    WHERE account_id=? AND (step=? OR snapshot_digest=?)
                """, (account_id, step, digest)).fetchone()
                if duplicate is not None:
                    raise InvalidTransition(
                        "this registration step or exact browser target already fired; reconcile it")
                self.db.execute("""
                    INSERT INTO account_submission_steps(
                        account_id,step,snapshot_digest,expected_active_text,
                        expected_active_path,state,previous_account_status,
                        created_at,updated_at) VALUES(?,?,?,?,?,'firing',?,?,?)
                """, (account_id, step, digest, text, path,
                        row["status"], now, now))
                self.db.execute("""
                    UPDATE account_registry
                    SET status='challenge_wait', expected_active_text=?,
                        expected_active_path=?, pre_submit_state_digest=?,
                        submission_started_at=?, completion_evidence_digest='', updated_at=?
                    WHERE account_id=? AND collie_id=?
                """, (text, path, digest, now, now, account_id, self.collie_id))
                self.db.commit()
                return self._public(self._row(account_id))
            except Exception:
                self.db.rollback()
                raise

    def settle_submission(self, account_id: str, *, step: str,
                          fired: bool, confirmed: bool = False) -> dict:
        """Close the crash fence; only a proven no-click may release it."""
        step = str(step or "").strip().lower()
        now = _now()
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute("""
                    SELECT * FROM account_submission_steps
                    WHERE account_id=? AND step=?
                """, (account_id, step)).fetchone()
                if row is None:
                    raise InvalidTransition("submission step has no durable firing fence")
                if not fired:
                    if row["state"] != "firing":
                        raise InvalidTransition("a fired registration step cannot be released")
                    self.db.execute(
                        "DELETE FROM account_submission_steps WHERE account_id=? AND step=?",
                        (account_id, step))
                    previous = self.db.execute("""
                        SELECT * FROM account_submission_steps
                        WHERE account_id=?
                        ORDER BY created_at DESC, rowid DESC LIMIT 1
                    """, (account_id,)).fetchone()
                    if previous is not None:
                        self.db.execute("""
                            UPDATE account_registry SET status='challenge_wait',
                                expected_active_text=?, expected_active_path=?,
                                pre_submit_state_digest=?, submission_started_at=?,
                                completion_evidence_digest='', updated_at=?
                            WHERE account_id=? AND collie_id=?
                        """, (previous["expected_active_text"],
                                previous["expected_active_path"],
                                previous["snapshot_digest"],
                                int(previous["created_at"]), now,
                                account_id, self.collie_id))
                    else:
                        self.db.execute("""
                            UPDATE account_registry SET status=?,
                                expected_active_text='', expected_active_path='',
                                pre_submit_state_digest='', submission_started_at=0,
                                completion_evidence_digest='',
                                updated_at=? WHERE account_id=? AND collie_id=?
                        """, (row["previous_account_status"], now,
                                account_id, self.collie_id))
                else:
                    state = "confirmed" if confirmed else "fired"
                    self.db.execute("""
                        UPDATE account_submission_steps SET state=?,updated_at=?
                        WHERE account_id=? AND step=? AND state='firing'
                    """, (state, now, account_id, step))
                self.db.commit()
                return self._public(self._row(account_id))
            except Exception:
                self.db.rollback()
                raise

    def submission_contract(self, account_id: str) -> dict:
        """Host-only completion contract; excluded from every public projection."""
        with self._lock:
            row = self._row(account_id)
            return {
                "status": row["status"],
                "expected_active_text": row["expected_active_text"],
                "expected_active_path": row["expected_active_path"],
                "pre_state_digest": row["pre_submit_state_digest"],
                "submission_started_at": int(row["submission_started_at"]),
            }

    def complete_submission(self, account_id: str, *, evidence_digest: str) -> dict:
        digest = str(evidence_digest or "").strip().lower()
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("completion evidence digest is required")
        now = _now()
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(account_id)
                if row["status"] == "active":
                    self.db.commit()
                    return self._public(row)
                if (row["status"] != "challenge_wait" or not row["submission_started_at"]
                        or not row["expected_active_text"] or not row["expected_active_path"]):
                    raise InvalidTransition(
                        "account has no bound submitted registration to complete")
                if hmac.compare_digest(digest, row["pre_submit_state_digest"]):
                    raise InvalidTransition(
                        "completion evidence is unchanged from the pre-submit page")
                changed = self.db.execute("""
                    UPDATE account_registry
                    SET status='active', verified_at=?, completion_evidence_digest=?, updated_at=?
                    WHERE account_id=? AND collie_id=? AND status='challenge_wait'
                """, (now, digest, now, account_id, self.collie_id))
                if changed.rowcount != 1:
                    raise InvalidTransition(
                        "account lifecycle changed before completion could commit")
                self.db.execute("""
                    UPDATE account_submission_steps SET state='confirmed',updated_at=?
                    WHERE account_id=? AND state IN ('firing','fired')
                """, (now, account_id))
                result = self._public(self._row(account_id))
                if result["status"] != "active":
                    raise InvalidTransition("account activation did not commit")
                self.db.commit()
                return result
            except Exception:
                self.db.rollback()
                raise

    @staticmethod
    def _secret_value(kind: str, supplied=None) -> bytes:
        if kind == "password":
            value = generate_password() if supplied is None else str(supplied)
            if len(value) < 16:
                raise ValueError("supplied password is too short")
            return value.encode("utf-8")
        if kind == "totp":
            value = generate_totp_secret() if supplied is None else str(supplied).strip()
            if len(value) < 16:
                raise ValueError("TOTP secret is too short")
            return value.encode("ascii")
        if kind == "recovery_codes":
            if supplied is not None and isinstance(supplied, (str, bytes, bytearray)):
                raise ValueError("recovery codes must be a sequence, not one string")
            value = generate_recovery_codes() if supplied is None else list(supplied)
            if not value or any(not str(code) for code in value):
                raise ValueError("recovery codes must be a non-empty sequence")
            return json.dumps([str(code) for code in value], separators=(",", ":")).encode("utf-8")
        raise ValueError("unsupported secret factor")

    def _write_new_secrets(self, row: sqlite3.Row, kinds, values=None,
                           reserved_refs=None):
        refs: dict[str, str] = {}
        values = values or {}
        reserved_refs = reserved_refs or {}
        try:
            for kind in kinds:
                secret = bytearray(self._secret_value(kind, values.get(kind)))
                try:
                    refs[kind] = self.vault.put(
                        secret, collie_id=self.collie_id,
                        account=row["account_id"], kind=kind,
                        ref=reserved_refs.get(kind, ""))
                finally:
                    for index in range(len(secret)):
                        secret[index] = 0
            return refs
        except Exception:
            for kind, ref in refs.items():
                try:
                    self.vault.delete(ref, collie_id=self.collie_id,
                                      account=row["account_id"], kind=kind)
                except Exception:
                    pass
            raise

    def create_credentials(self, account_id: str, *, factors=("password",),
                           values: dict | None = None) -> dict:
        kinds = tuple(dict.fromkeys(str(item) for item in factors))
        if not kinds or any(kind not in SECRET_FACTORS for kind in kinds):
            raise ValueError("credentials may contain password, totp, or recovery_codes")
        with self._lock:
            token = "create:" + secrets.token_urlsafe(24)
            pending = {kind: "cv1_" + secrets.token_urlsafe(24) for kind in kinds}
            previous_status = ""
            recovering = None
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(account_id)
                previous_status = str(row["status"])
                if row["status"] in {"deletion_pending", "retired"}:
                    raise InvalidTransition("credentials cannot be added to a retiring account")
                old_refs = _load_refs(row["secret_refs_json"])
                overlap = set(kinds).intersection(old_refs)
                if overlap:
                    raise CredentialExists("credential already exists; rotate it instead")
                existing_token = str(row["credential_setup_token"] or "")
                if existing_token:
                    persisted = _load_refs(row["credential_pending_refs_json"])
                    cutoff = _now() - 300
                    if (existing_token.startswith("create:") and persisted
                            and (row["credential_mutation_phase"] == "recovery_required"
                                 or int(row["credential_setup_at"] or 0) <= cutoff)):
                        claimed = self.db.execute("""
                            UPDATE account_registry
                            SET credential_mutation_phase='create_cleanup',
                                credential_setup_at=?, updated_at=?
                            WHERE account_id=? AND collie_id=?
                              AND credential_setup_token=?
                              AND (credential_mutation_phase='recovery_required'
                                   OR credential_setup_at<=?)
                        """, (_now(), _now(), account_id, self.collie_id,
                                existing_token, cutoff))
                        if claimed.rowcount != 1:
                            raise InvalidTransition(
                                "credential setup recovery is already in progress")
                        self.db.commit()
                        recovering = (existing_token, persisted)
                    else:
                        raise InvalidTransition(
                            "credential setup is already in progress or requires recovery")
                if recovering is not None:
                    pass
                else:
                    now = _now()
                    claimed = self.db.execute("""
                        UPDATE account_registry
                        SET status=CASE WHEN status='planned' THEN 'registering' ELSE status END,
                            credential_setup_token=?, credential_setup_at=?,
                            credential_mutation_phase='create_write',
                            credential_pending_refs_json=?, updated_at=?
                        WHERE account_id=? AND collie_id=? AND credential_setup_token=''
                          AND status NOT IN ('deletion_pending','retired')
                    """, (token, now,
                            json.dumps(pending, sort_keys=True, separators=(",", ":")),
                            now, account_id, self.collie_id))
                    if claimed.rowcount != 1:
                        raise InvalidTransition(
                            "credential setup could not acquire its durable fence")
                    self.db.commit()
            except Exception:
                self.db.rollback()
                raise

            if recovering is not None:
                self._finish_create_cleanup(account_id, *recovering)
                return self.create_credentials(
                    account_id, factors=kinds, values=values)

            new_refs = {}
            try:
                # The reserved opaque refs are already durable. If the process
                # dies here, recovery can erase each possible vault item without
                # knowing or logging its plaintext value.
                row = self._row(account_id)
                new_refs = self._write_new_secrets(
                    row, kinds, values, reserved_refs=pending)
                self.db.execute("BEGIN IMMEDIATE")
                current = self._row(account_id)
                if current["credential_setup_token"] != token:
                    raise InvalidTransition("credential setup ownership changed")
                old_refs = _load_refs(current["secret_refs_json"])
                all_refs = dict(old_refs)
                all_refs.update(new_refs)
                all_factors = set(_load_list(current["factor_classes_json"]))
                all_factors.update(kinds)
                now = _now()
                self.db.execute("""
                    UPDATE account_registry
                    SET secret_refs_json=?, factor_classes_json=?,
                        credential_setup_token='', credential_setup_at=0,
                        credential_mutation_phase='',
                        credential_pending_refs_json='{}', updated_at=?
                    WHERE account_id=? AND collie_id=? AND credential_setup_token=?
                """, (json.dumps(all_refs, sort_keys=True, separators=(",", ":")),
                        _json_list(all_factors), now, account_id, self.collie_id, token))
                self.db.commit()
                changed = self._row(account_id)
                return self._receipt("credentials.created", changed, factors=kinds)
            except Exception:
                try:
                    self.db.rollback()
                except Exception:
                    pass
                try:
                    self._finish_create_cleanup(
                        account_id, token, pending,
                        restore_status=previous_status or "planned")
                except Exception:
                    # The recovery helper keeps the token and pending opaque
                    # refs durable if even one OS-vault erase fails.
                    pass
                raise

    def get_secret(self, account_id: str, kind: str) -> bytes:
        """Host-only secret access. Never put this return value in model/event/receipt data."""
        kind = str(kind)
        with self._lock:
            row = self._row(account_id)
            if (row["status"] in {"deletion_pending", "retired"}
                    or row["credential_setup_token"]):
                raise InvalidTransition(
                    "credentials are unavailable during an account mutation")
            ref = _load_refs(row["secret_refs_json"]).get(kind)
            if not ref:
                raise SecretNotFound("credential factor is not provisioned")
            return self.vault.get(ref, collie_id=self.collie_id,
                                  account=account_id, kind=kind)

    def use_secret(self, account_id: str, kind: str, consumer):
        kind = str(kind)
        with self._lock:
            row = self._row(account_id)
            if (row["status"] in {"deletion_pending", "retired"}
                    or row["credential_setup_token"]):
                raise InvalidTransition(
                    "credentials are unavailable during an account mutation")
            ref = _load_refs(row["secret_refs_json"]).get(kind)
            if not ref:
                raise SecretNotFound("credential factor is not provisioned")
        return self.vault.use(ref, collie_id=self.collie_id,
                              account=account_id, kind=kind, consumer=consumer)

    def _mark_credential_recovery(self, account_id: str, token: str) -> None:
        try:
            with self.db:
                self.db.execute("""
                    UPDATE account_registry
                    SET credential_mutation_phase='recovery_required', updated_at=?
                    WHERE account_id=? AND collie_id=? AND credential_setup_token=?
                """, (_now(), account_id, self.collie_id, token))
        except Exception:
            # The durable token and refs still fail closed even if a faulting
            # database trigger prevents the nicer recovery-state projection.
            pass

    def _finish_create_cleanup(self, account_id: str, token: str,
                               pending: dict[str, str], *,
                               restore_status: str = "") -> dict:
        """Erase an interrupted credential write before releasing its fence."""
        try:
            for kind, ref in pending.items():
                self.vault.delete(ref, collie_id=self.collie_id,
                                  account=account_id, kind=kind)
        except Exception:
            self._mark_credential_recovery(account_id, token)
            raise
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self._row(account_id)
            if row["credential_setup_token"] == token:
                status = restore_status or str(row["status"])
                changed = self.db.execute("""
                    UPDATE account_registry
                    SET status=CASE WHEN submission_started_at=0 THEN ? ELSE status END,
                        credential_setup_token='', credential_setup_at=0,
                        credential_mutation_phase='',
                        credential_pending_refs_json='{}', updated_at=?
                    WHERE account_id=? AND collie_id=?
                      AND credential_setup_token=?
                """, (status, _now(), account_id, self.collie_id, token))
                if changed.rowcount != 1:
                    raise InvalidTransition(
                        "credential setup cleanup could not commit")
                row = self._row(account_id)
            elif row["credential_setup_token"]:
                raise InvalidTransition("credential setup cleanup ownership changed")
            result = self._public(row)
            self.db.commit()
            return result
        except Exception:
            self.db.rollback()
            self._mark_credential_recovery(account_id, token)
            raise

    def _finish_rotate_pending_cleanup(self, account_id: str, token: str,
                                       pending: dict[str, str]) -> dict:
        """Erase uncommitted replacement refs before a safe rotation retry."""
        try:
            for kind, ref in pending.items():
                self.vault.delete(ref, collie_id=self.collie_id,
                                  account=account_id, kind=kind)
        except Exception:
            self._mark_credential_recovery(account_id, token)
            raise
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self._row(account_id)
            if row["credential_setup_token"] == token:
                changed = self.db.execute("""
                    UPDATE account_registry
                    SET status='degraded', credential_setup_token='',
                        credential_setup_at=0, credential_mutation_phase='',
                        credential_pending_refs_json='{}',
                        credential_cleanup_refs_json='{}', updated_at=?
                    WHERE account_id=? AND collie_id=? AND status='rotating'
                      AND credential_setup_token=?
                """, (_now(), account_id, self.collie_id, token))
                if changed.rowcount != 1:
                    raise InvalidTransition(
                        "credential rotation write cleanup could not commit")
                row = self._row(account_id)
            elif row["credential_setup_token"]:
                raise InvalidTransition(
                    "credential rotation write cleanup ownership changed")
            result = self._public(row)
            self.db.commit()
            return result
        except Exception:
            self.db.rollback()
            self._mark_credential_recovery(account_id, token)
            raise

    def _finish_rotate_cleanup(self, account_id: str, token: str,
                               cleanup: dict[str, str]) -> dict:
        """Idempotently erase superseded refs, then publish the rotated account."""
        try:
            for kind, ref in cleanup.items():
                self.vault.delete(ref, collie_id=self.collie_id,
                                  account=account_id, kind=kind)
        except Exception:
            self._mark_credential_recovery(account_id, token)
            raise
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self._row(account_id)
            if row["credential_setup_token"] == token:
                changed = self.db.execute("""
                    UPDATE account_registry
                    SET status='active', credential_setup_token='',
                        credential_setup_at=0, credential_mutation_phase='',
                        credential_pending_refs_json='{}',
                        credential_cleanup_refs_json='{}', updated_at=?
                    WHERE account_id=? AND collie_id=? AND status='rotating'
                      AND credential_setup_token=?
                """, (_now(), account_id, self.collie_id, token))
                if changed.rowcount != 1:
                    raise InvalidTransition("credential rotation cleanup could not commit")
                row = self._row(account_id)
            elif not (row["status"] == "active" and not row["credential_setup_token"]):
                raise InvalidTransition("credential rotation cleanup ownership changed")
            result = self._public(row)
            self.db.commit()
            return self._receipt("credentials.rotated", result, factors=tuple(cleanup))
        except Exception:
            self.db.rollback()
            self._mark_credential_recovery(account_id, token)
            raise

    def _finish_revoke(self, account_id: str, token: str,
                       targets: dict[str, str]) -> dict:
        """Replay a persisted revoke intent safely after a crash or DB failure."""
        try:
            for kind, ref in targets.items():
                self.vault.delete(ref, collie_id=self.collie_id,
                                  account=account_id, kind=kind)
        except Exception:
            self._mark_credential_recovery(account_id, token)
            raise
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self._row(account_id)
            if row["credential_setup_token"] != token:
                if not row["credential_setup_token"]:
                    self.db.commit()
                    return self._receipt(
                        "credentials.revoked", self._public(row), factors=tuple(targets))
                raise InvalidTransition("credential revocation ownership changed")
            refs = _load_refs(row["secret_refs_json"])
            if any(refs.get(kind) != ref for kind, ref in targets.items()):
                raise InvalidTransition("credential references changed during revocation")
            remaining = {kind: ref for kind, ref in refs.items() if kind not in targets}
            classes = set(_load_list(row["factor_classes_json"]))
            classes.difference_update(targets)
            changed = self.db.execute("""
                UPDATE account_registry
                SET secret_refs_json=?, factor_classes_json=?,
                    credential_setup_token='', credential_setup_at=0,
                    credential_mutation_phase='',
                    credential_pending_refs_json='{}',
                    credential_cleanup_refs_json='{}', updated_at=?
                WHERE account_id=? AND collie_id=? AND credential_setup_token=?
            """, (json.dumps(remaining, sort_keys=True, separators=(",", ":")),
                    _json_list(classes), _now(), account_id, self.collie_id, token))
            if changed.rowcount != 1:
                raise InvalidTransition("credential revocation could not commit")
            result = self._public(self._row(account_id))
            self.db.commit()
            return self._receipt("credentials.revoked", result, factors=tuple(targets))
        except Exception:
            self.db.rollback()
            self._mark_credential_recovery(account_id, token)
            raise

    def rotate_credentials(self, account_id: str, *, factors=("password",),
                           values: dict | None = None) -> dict:
        kinds = tuple(dict.fromkeys(str(item) for item in factors))
        if not kinds or any(kind not in SECRET_FACTORS for kind in kinds):
            raise ValueError("unsupported credential factor")
        with self._lock:
            token = "rotate:" + secrets.token_urlsafe(24)
            pending = {kind: "cv1_" + secrets.token_urlsafe(24) for kind in kinds}
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(account_id)
                existing_token = str(row["credential_setup_token"] or "")
                existing_cleanup = _load_refs(row["credential_cleanup_refs_json"])
                existing_pending = _load_refs(row["credential_pending_refs_json"])
                if existing_token:
                    if (existing_token.startswith("rotate:") and existing_cleanup
                            and row["status"] == "rotating"
                            and (row["credential_mutation_phase"] == "recovery_required"
                                 or int(row["credential_setup_at"] or 0) <= _now() - 300)):
                        claimed = self.db.execute("""
                            UPDATE account_registry
                            SET credential_mutation_phase='rotate_cleanup',
                                credential_setup_at=?, updated_at=?
                            WHERE account_id=? AND collie_id=?
                              AND credential_setup_token=?
                              AND (credential_mutation_phase='recovery_required'
                                   OR credential_setup_at<=?)
                        """, (_now(), _now(), account_id, self.collie_id,
                                existing_token, _now() - 300))
                        if claimed.rowcount != 1:
                            raise InvalidTransition(
                                "credential rotation recovery is already in progress")
                        self.db.commit()
                        return self._finish_rotate_cleanup(
                            account_id, existing_token, existing_cleanup)
                    if (existing_token.startswith("rotate:") and existing_pending
                            and not existing_cleanup and row["status"] == "rotating"
                            and (row["credential_mutation_phase"] == "recovery_required"
                                 or int(row["credential_setup_at"] or 0) <= _now() - 300)):
                        claimed = self.db.execute("""
                            UPDATE account_registry
                            SET credential_mutation_phase='rotate_pending_cleanup',
                                credential_setup_at=?, updated_at=?
                            WHERE account_id=? AND collie_id=?
                              AND credential_setup_token=?
                              AND (credential_mutation_phase='recovery_required'
                                   OR credential_setup_at<=?)
                        """, (_now(), _now(), account_id, self.collie_id,
                                existing_token, _now() - 300))
                        if claimed.rowcount != 1:
                            raise InvalidTransition(
                                "credential rotation recovery is already in progress")
                        self.db.commit()
                        self._finish_rotate_pending_cleanup(
                            account_id, existing_token, existing_pending)
                        return self.rotate_credentials(
                            account_id, factors=kinds, values=values)
                    raise InvalidTransition(
                        "a credential mutation is already in progress or requires recovery")
                old_refs = _load_refs(row["secret_refs_json"])
                missing = [kind for kind in kinds if kind not in old_refs]
                if missing:
                    raise SecretNotFound("credential factor is not provisioned")
                if row["status"] not in {"active", "degraded"}:
                    raise InvalidTransition(
                        "credentials can rotate only on active or degraded accounts")
                now = _now()
                claimed = self.db.execute("""
                    UPDATE account_registry
                    SET status='rotating', credential_setup_token=?, credential_setup_at=?,
                        credential_mutation_phase='rotate_write',
                        credential_pending_refs_json=?, credential_cleanup_refs_json='{}',
                        updated_at=?
                    WHERE account_id=? AND collie_id=? AND credential_setup_token=''
                      AND status IN ('active','degraded')
                """, (token, now,
                        json.dumps(pending, sort_keys=True, separators=(",", ":")),
                        now, account_id, self.collie_id))
                if claimed.rowcount != 1:
                    raise InvalidTransition("credential rotation could not acquire its durable fence")
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

            new_refs = {}
            try:
                row = self._row(account_id)
                new_refs = self._write_new_secrets(
                    row, kinds, values, reserved_refs=pending)
                self.db.execute("BEGIN IMMEDIATE")
                current = self._row(account_id)
                if (current["credential_setup_token"] != token
                        or current["status"] != "rotating"):
                    raise InvalidTransition("credential rotation ownership changed")
                current_refs = _load_refs(current["secret_refs_json"])
                if any(current_refs.get(kind) != old_refs.get(kind) for kind in kinds):
                    raise InvalidTransition("credential references changed during rotation")
                merged = dict(current_refs)
                merged.update(new_refs)
                cleanup = {kind: old_refs[kind] for kind in kinds}
                now = _now()
                changed_row = self.db.execute("""
                    UPDATE account_registry
                    SET secret_refs_json=?, rotated_at=?,
                        credential_mutation_phase='rotate_cleanup',
                        credential_pending_refs_json='{}',
                        credential_cleanup_refs_json=?, updated_at=?
                    WHERE account_id=? AND collie_id=? AND status='rotating'
                      AND credential_setup_token=?
                """, (json.dumps(merged, sort_keys=True, separators=(",", ":")), now,
                        json.dumps(cleanup, sort_keys=True, separators=(",", ":")),
                        now, account_id, self.collie_id, token))
                if changed_row.rowcount != 1:
                    raise InvalidTransition("credential rotation could not commit")
                self.db.commit()
            except Exception:
                try:
                    self.db.rollback()
                except Exception:
                    pass
                try:
                    self._finish_rotate_pending_cleanup(
                        account_id, token, pending)
                except Exception:
                    # Keep token+pending refs durable when OS-vault cleanup
                    # fails; a later identical operation resumes safely.
                    pass
                raise
            return self._finish_rotate_cleanup(account_id, token, cleanup)

    def revoke_credentials(self, account_id: str, *, factors=None,
                           _allow_retiring: bool = False) -> dict:
        with self._lock:
            token = "revoke:" + secrets.token_urlsafe(24)
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(account_id)
                refs = _load_refs(row["secret_refs_json"])
                kinds = tuple(refs) if factors is None else tuple(
                    dict.fromkeys(str(item) for item in factors))
                if any(kind not in SECRET_FACTORS for kind in kinds):
                    raise ValueError("unsupported credential factor")
                if (row["status"] == "retired"
                        or (row["status"] == "deletion_pending" and not _allow_retiring)):
                    raise InvalidTransition("credentials cannot be revoked while retiring")
                existing_token = str(row["credential_setup_token"] or "")
                if existing_token:
                    persisted = _load_refs(row["credential_pending_refs_json"])
                    requested = {kind: refs[kind] for kind in kinds if kind in refs}
                    if (existing_token.startswith("revoke:")
                            and persisted == requested
                            and (row["credential_mutation_phase"] == "recovery_required"
                                 or int(row["credential_setup_at"] or 0) <= _now() - 300)):
                        claimed = self.db.execute("""
                            UPDATE account_registry
                            SET credential_mutation_phase='revoke_delete',
                                credential_setup_at=?, updated_at=?
                            WHERE account_id=? AND collie_id=?
                              AND credential_setup_token=?
                              AND (credential_mutation_phase='recovery_required'
                                   OR credential_setup_at<=?)
                        """, (_now(), _now(), account_id, self.collie_id,
                                existing_token, _now() - 300))
                        if claimed.rowcount != 1:
                            raise InvalidTransition(
                                "credential revocation recovery is already in progress")
                        self.db.commit()
                        return self._finish_revoke(
                            account_id, existing_token, persisted)
                    raise InvalidTransition(
                        "a credential mutation is already in progress or requires recovery")
                targets = {kind: refs[kind] for kind in kinds if kind in refs}
                next_status = "degraded" if row["status"] in {"active", "rotating"} \
                    else row["status"]
                now = _now()
                status_clause = ("status <> 'retired'" if _allow_retiring
                                 else "status NOT IN ('deletion_pending','retired')")
                claimed = self.db.execute("""
                    UPDATE account_registry
                    SET status=?, credential_setup_token=?, credential_setup_at=?,
                        credential_mutation_phase='revoke_delete',
                        credential_pending_refs_json=?, updated_at=?
                    WHERE account_id=? AND collie_id=? AND credential_setup_token=''
                      AND %s
                """ % status_clause, (next_status, token, now,
                        json.dumps(targets, sort_keys=True, separators=(",", ":")),
                        now, account_id, self.collie_id))
                if claimed.rowcount != 1:
                    raise InvalidTransition("credential revocation could not acquire its durable fence")
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            return self._finish_revoke(account_id, token, targets)

    def abort_prepared(self, account_id: str) -> dict:
        """Erase a local-only signup preparation before any remote submit began.

        ``account.submit`` moves a registration to ``challenge_wait`` *before*
        firing the browser click.  Therefore ``registering`` is the durable
        proof that Collie has generated/fill-ready credentials but has not begun
        a consequential remote submission.  This method deliberately refuses
        every later state instead of guessing whether a remote account exists.
        """
        with self._lock:
            # Cross-process CAS claim: once deletion_pending is committed,
            # begin_submission cannot race in and expose a half-deleted secret.
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(account_id)
                setup_expired = bool(
                    row["credential_setup_token"]
                    and int(row["credential_setup_at"] or 0) <= _now() - 300)
                setup_clear = not row["credential_setup_token"]
                resumable = (row["status"] == "deletion_pending"
                             and not int(row["submission_started_at"])
                             and not int(row["verified_at"]))
                if not ((row["status"] == "registering" and not row["submission_started_at"]
                         and not int(row["verified_at"])
                         and (setup_clear or setup_expired)) or resumable):
                    raise InvalidTransition(
                        "only a prepared registration with no remote submit may be aborted")
                if row["status"] == "registering":
                    changed = self.db.execute("""
                        UPDATE account_registry SET status='deletion_pending',
                            credential_setup_token='',credential_setup_at=0,
                            credential_mutation_phase='',updated_at=?
                        WHERE account_id=? AND collie_id=? AND status='registering'
                          AND submission_started_at=0 AND verified_at=0
                          AND (credential_setup_token='' OR credential_setup_at<=?)
                    """, (_now(), account_id, self.collie_id, _now() - 300))
                    if changed.rowcount != 1:
                        raise InvalidTransition("account preparation changed while aborting")
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            refs = _load_refs(row["secret_refs_json"])
            refs.update(_load_refs(row["credential_pending_refs_json"]))
            receipt = self._receipt("account.preparation_aborted", row,
                                    factors=tuple(refs))
            for kind, ref in refs.items():
                self.vault.delete(ref, collie_id=self.collie_id,
                                  account=account_id, kind=kind)
            with self.db:
                deleted = self.db.execute(
                    "DELETE FROM account_registry WHERE account_id=? AND collie_id=? "
                    "AND status='deletion_pending' AND submission_started_at=0 "
                    "AND verified_at=0",
                    (account_id, self.collie_id))
                if deleted.rowcount != 1:
                    raise InvalidTransition("account preparation changed while aborting")
            return receipt

    def retire(self, account_id: str) -> dict:
        with self._lock:
            row = self._row(account_id)
            if row["status"] == "retired":
                return self._receipt("account.retired", row)
            if row["status"] != "deletion_pending":
                self.transition(account_id, "deletion_pending")
            self.revoke_credentials(account_id, _allow_retiring=True)
            retired = self.transition(account_id, "retired")
            return self._receipt("account.retired", self._row(account_id),
                                 factors=retired["factor_classes"])


__all__ = [
    "AccountNotFound", "AccountRegistry", "AccountRegistryError", "CredentialExists",
    "DuplicateAccount", "FACTOR_CLASSES", "IdempotencyConflict", "InvalidTransition", "OWNERSHIP_CLASSES",
    "SECRET_FACTORS", "STATUSES", "generate_password", "generate_recovery_codes",
    "generate_totp_secret", "normalize_origin", "normalize_username",
]
