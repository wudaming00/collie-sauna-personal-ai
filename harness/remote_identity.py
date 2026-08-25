"""Persistent identity + paired-device store for Collie Remote (the desktop side).

The desktop — not the ephemeral relay/DO — is the source of truth for "who is paired". That's what
makes pairing durable: the phone pairs once, and it keeps working even after the desktop was off for
a day, because on every reconnect the desktop re-registers its paired-device set to the relay.

Stored at ~/.collie/remote.json (honouring $COLLIE_STATE_DIR), 0600:
  {
    "device_id": "<stable random>",       # this desktop's identity
    "room":      "<stable slug>",          # stable relay room → phone URL is bookmarkable
    "agent_key": "<stable secret>",        # proves this desktop owns the room (AGENTKEY)
    "devices": { "<sha256(token)>": {"name":..,"paired_at":..,"last_seen":..} }
  }
Session tokens themselves are NEVER stored — only their SHA-256, so the file leaking can't grant
access. The plaintext token lives only in the phone's cookie.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import shutil
import socket
import tempfile
import threading
import time
import uuid


_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


class IdentityCorrupt(RuntimeError):
    """The canonical Collie identity exists but cannot be trusted or replaced."""


def _thread_lock(path: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(os.path.realpath(path), threading.RLock())


@contextlib.contextmanager
def _file_lock(path: str):
    """Cross-process lock for the complete remote.json read/modify/replace transaction."""
    lock_path = path + ".lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fh = open(lock_path, "a+b")
    acquired = False
    try:
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            pass
        if os.name == "nt":
            import msvcrt
            if os.path.getsize(lock_path) == 0:
                fh.write(b"\0"); fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        acquired = True
        yield
    finally:
        if acquired:
            try:
                fh.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


def _state_dir() -> str:
    return os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie")


def _path() -> str:
    return os.path.join(_state_dir(), "remote.json")


def fallback_device_id() -> str:
    """One non-authority machine scope shared by every surface when persistence is unavailable."""
    material = "%s\0%s\0%s" % (socket.gethostname(), uuid.getnode(), os.name)
    return "host-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# Crockford-ish base32 (no I/L/O/U) — ~40 bits at n=8, human-typable. Only used to add a NEW device.
_PAIR_ALPHA = "23456789ABCDEFGHJKMNPQRSTVWXYZ"


def gen_paircode(n: int = 8) -> str:
    return "".join(secrets.choice(_PAIR_ALPHA) for _ in range(n))


class Identity:
    def __init__(self, data: dict, path: str):
        self._d = data
        self._path = path
        self._lock = _thread_lock(path)

    @property
    def device_id(self) -> str:
        with self._lock: return self._d["device_id"]
    @property
    def room(self) -> str:
        with self._lock: return self._d["room"]
    @property
    def agent_key(self) -> str:
        with self._lock: return self._d["agent_key"]

    # devices are keyed by a client-supplied stable device_id (localStorage / Keychain), NOT by the
    # session-token hash — so the SAME client re-pairing UPDATES its entry (new token) instead of
    # spawning a duplicate. entry = {name, token_sha, paired_at, last_seen}.

    def device_hashes(self) -> list[str]:
        """Current valid session-token hashes (one per device). The desktop re-registers this set to
        the relay on connect; a re-paired device's OLD hash is gone because it was replaced in place."""
        with self._lock:
            return [v.get("token_sha") for v in self._d.get("devices", {}).values()
                    if v.get("token_sha")]

    def approved_ids(self) -> set:
        """Device ids a human has already let in. Re-pairing one of these — after a reinstall, or
        after the desktop rotated its code — is not a new decision, so it does not ask again. A
        device that was kicked is gone from `devices` entirely and has to be approved afresh."""
        with self._lock:
            return {k for k in (self._d.get("devices") or {})}

    def add_or_update(self, device_id: str, token_sha: str, name: str = ""):
        """Pair or re-pair `device_id`. Re-pairing keeps the entry (and its custom name) and just
        swaps in the fresh token hash — so no duplicate row appears."""
        device_id = device_id or token_sha    # legacy client with no device_id → key by hash
        def change(data):
            devs = data.setdefault("devices", {})
            now = int(time.time())
            e = devs.get(device_id)
            if e:
                e["token_sha"] = token_sha
                e["last_seen"] = now
                if name and not e.get("name"):
                    e["name"] = name
            else:
                devs[device_id] = {"name": name or "device", "token_sha": token_sha,
                                   "paired_at": now, "last_seen": now}
        self._mutate(change)

    def store_paired_device(self, device_id: str, token_sha: str, name: str,
                            k_dev_b64: str) -> None:
        """Atomically persist a bearer hash and its matching E2E key in one transaction."""
        if not device_id:
            raise ValueError("device id is required")
        def change(data):
            devs = data.setdefault("devices", {})
            now = int(time.time())
            existing = dict(devs.get(device_id) or {})
            existing.update({"token_sha": token_sha, "last_seen": now, "k_dev": k_dev_b64})
            existing.setdefault("paired_at", now)
            if name and not existing.get("name"):
                existing["name"] = name
            existing.setdefault("name", "device")
            devs[device_id] = existing
        self._mutate(change)

    def device_id_for_hash(self, token_sha: str) -> str:
        with self._lock:
            for device_id, entry in (self._d.get("devices") or {}).items():
                if secrets.compare_digest(str(entry.get("token_sha") or ""), str(token_sha or "")):
                    return device_id
        return ""

    def set_device_key(self, device_id: str, k_dev_b64: str) -> None:
        """Remember a paired device's K_dev, so restarting `collie web` does not strand it.

        E2E_DESIGN.md §7: "Desktop restart: K_dev persists (in the device store) → returning device
        keeps working, no re-pair." Without this the desktop generates a fresh keypair on every start
        and every sealed frame from an already-paired phone fails to open — surfacing as an opaque
        5xx rather than as anything a person could act on.

        It lives beside the token hash in the same 0600 file. That is a real step up in what the file
        is worth: this key decrypts that device's traffic, where the hash only recognises it. The
        alternative is re-pairing every phone on every restart, which people would answer by not
        using encryption at all.
        """
        if not device_id:
            return
        def change(data):
            e = data.setdefault("devices", {}).get(device_id)
            if e is not None:
                e["k_dev"] = k_dev_b64
        self._mutate(change)

    def device_keys(self) -> dict:
        """device_id -> K_dev (base64), for every device that paired with encryption."""
        with self._lock:
            return {k: v["k_dev"] for k, v in self._d.get("devices", {}).items()
                    if v.get("k_dev")}

    def rename(self, device_id: str, name: str) -> bool:
        def change(data):
            e = data.get("devices", {}).get(device_id)
            if e is None:
                return False
            e["name"] = name
            return True
        return bool(self._mutate(change))

    def forget_device(self, device_id: str) -> bool:
        def change(data):
            devs = data.setdefault("devices", {})
            if device_id not in devs:
                return False
            del devs[device_id]
            _remove_replay_rows(data, device_id)
            return True
        return bool(self._mutate(change))

    def forget_all(self):
        def change(data):
            data["devices"] = {}
            data.pop("remote_v2_seen", None)
        self._mutate(change)

    def devices(self) -> list:
        """List of {device_id, name, paired_at, last_seen} (token_sha omitted — never leaves here)."""
        with self._lock:
            return [{"device_id": k, "name": v.get("name", "device"),
                     "paired_at": v.get("paired_at"), "last_seen": v.get("last_seen")}
                    for k, v in self._d.get("devices", {}).items()]

    def _mutate(self, fn):
        """Apply one atomic mutation to the newest on-disk snapshot, then publish it in memory."""
        with self._lock, _file_lock(self._path):
            current = _read(self._path) or self._d
            draft = json.loads(json.dumps(current))
            result = fn(draft)
            _atomic_write(self._path, draft)
            self._d = draft
            return result

    def _save(self):
        # Compatibility seam used by remote.py while its replay transaction owns its own lock.
        with self._lock, _file_lock(self._path):
            snapshot = json.loads(json.dumps(self._d))
            _atomic_write(self._path, snapshot)
            self._d = snapshot


def load_or_create() -> Identity:
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        os.chmod(os.path.dirname(path), 0o700)
    except OSError:
        pass
    lock = _thread_lock(path)
    with lock, _file_lock(path):
        try:
            data = _read(path)
        except IdentityCorrupt:
            backup = path + ".bak"
            try:
                data = _read(backup)
            except IdentityCorrupt:
                data = None
            if not data:
                raise IdentityCorrupt(
                    "Collie's canonical identity is damaged and no valid backup is available; "
                    "restore remote.json instead of creating a new device identity")
            _validate_identity(data)
            _atomic_write(path, data, backup_existing=False)
        if data is None:
            data = {
                "device_id": secrets.token_urlsafe(12),
                # a stable, unguessable room slug (~72 bits) → bookmarkable but not enumerable
                "room": secrets.token_urlsafe(12),
                "agent_key": secrets.token_urlsafe(32),
                "devices": {},
            }
            _atomic_write(path, data)
        else:
            _validate_identity(data)
            # migrate v0 entries while holding the same lock used for every subsequent mutation.
            changed = False
            for k, v in (data.get("devices") or {}).items():
                if isinstance(v, dict) and "token_sha" not in v:
                    v["token_sha"] = k
                    changed = True
            if changed:
                _atomic_write(path, data)
    return Identity(data, path)


def _validate_identity(value: dict) -> None:
    required = ("device_id", "room", "agent_key")
    if (not isinstance(value, dict)
            or any(not isinstance(value.get(key), str) or not value.get(key)
                   for key in required)
            or not isinstance(value.get("devices", {}), dict)):
        raise IdentityCorrupt("Collie's canonical identity file has an invalid shape")


def _read(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = json.load(f)
        if not isinstance(value, dict):
            raise IdentityCorrupt("Collie's canonical identity file is not an object")
        return value
    except FileNotFoundError:
        return None
    except (ValueError, UnicodeError) as exc:
        raise IdentityCorrupt("Collie's canonical identity file is malformed") from exc
    except OSError:
        raise


def _remove_replay_rows(data: dict, device_id: str) -> None:
    seen = data.get("remote_v2_seen")
    if isinstance(seen, dict):
        seen.pop(device_id, None)
        if not seen:
            data.pop("remote_v2_seen", None)
    elif isinstance(seen, list):                 # pre-partition migration format
        data["remote_v2_seen"] = [row for row in seen
                                  if not isinstance(row, dict) or row.get("d") != device_id]


def _atomic_write(path: str, data: dict, *, backup_existing: bool = True):
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".remote-", suffix=".tmp", dir=directory, text=True)
    try:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if backup_existing and os.path.exists(path):
            # Preserve the last known-good generation before replacement.  Read
            # and validate first so a corrupted primary is never promoted over a
            # valid recovery copy.
            existing = _read(path)
            _validate_identity(existing)
            backup_tmp = path + ".bak.tmp-" + secrets.token_hex(6)
            try:
                shutil.copyfile(path, backup_tmp)
                with open(backup_tmp, "r+b") as backup_file:
                    os.fsync(backup_file.fileno())
                try:
                    os.chmod(backup_tmp, 0o600)
                except OSError:
                    pass
                os.replace(backup_tmp, path + ".bak")
            finally:
                try:
                    os.unlink(backup_tmp)
                except OSError:
                    pass
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        if os.name != "nt":
            try:
                dfd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
