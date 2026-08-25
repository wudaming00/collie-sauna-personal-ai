"""OS-backed storage for Collie's account credentials.

The account registry stores only opaque references.  Secret bytes live in the
operating system's credential boundary:

* Windows: DPAPI (CurrentUser), with the encrypted blobs in Collie's state dir.
* macOS: the login Keychain through the native Security framework.
* Linux: Secret Service through ``secret-tool``.

There is deliberately no plaintext or home-grown encryption fallback.  If the
platform service is unavailable, callers get :class:`VaultUnavailable` and the
account operation stops.
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Protocol


DEFAULT_SERVICE = "run.collie.account-vault"
_REF_RE = re.compile(r"^cv1_[A-Za-z0-9_-]{24,96}$")
_ENVELOPE = b"collie-vault-v1:"


class VaultError(RuntimeError):
    """A credential-vault operation failed without exposing secret material."""


class VaultUnavailable(VaultError):
    """No usable OS credential service is available; never fall back to plaintext."""


class SecretNotFound(VaultError, KeyError):
    """The bound reference does not exist (or belongs to another account)."""


class VaultBackend(Protocol):
    """Small injectable seam used by the real OS stores and unit tests."""

    def put(self, service: str, account: str, secret: bytes, entropy: bytes) -> None: ...
    def get(self, service: str, account: str, entropy: bytes) -> bytes: ...
    def delete(self, service: str, account: str, entropy: bytes) -> bool: ...


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


class _WindowsDPAPIBackend:
    """DPAPI-encrypted blobs scoped to the current Windows user."""

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_ulong),
                    ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    _CRYPTPROTECT_UI_FORBIDDEN = 0x1

    def __init__(self, root: str | os.PathLike[str]):
        if sys.platform != "win32":
            raise VaultUnavailable("Windows DPAPI is not available on this platform")
        self.root = Path(root)
        try:
            self._crypt32 = ctypes.windll.crypt32
            self._kernel32 = ctypes.windll.kernel32
        except (AttributeError, OSError) as exc:
            raise VaultUnavailable("Windows DPAPI is unavailable") from exc
        blob_ptr = ctypes.POINTER(self._DATA_BLOB)
        self._crypt32.CryptProtectData.argtypes = [
            blob_ptr, ctypes.c_wchar_p, blob_ptr, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_ulong, blob_ptr,
        ]
        self._crypt32.CryptProtectData.restype = ctypes.c_int
        self._crypt32.CryptUnprotectData.argtypes = [
            blob_ptr, ctypes.POINTER(ctypes.c_wchar_p), blob_ptr, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_ulong, blob_ptr,
        ]
        self._crypt32.CryptUnprotectData.restype = ctypes.c_int
        self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.LocalFree.restype = ctypes.c_void_p

    @classmethod
    def _blob(cls, data: bytes):
        # Keep the backing array alive for the duration of the native call.
        buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data) if data else None
        blob = cls._DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
        return blob, buf

    def _protect(self, value: bytes, entropy: bytes) -> bytes:
        source, source_buf = self._blob(value)
        extra, extra_buf = self._blob(entropy)
        output = self._DATA_BLOB()
        ok = self._crypt32.CryptProtectData(
            ctypes.byref(source), None, ctypes.byref(extra), None, None,
            self._CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output))
        # Explicit names keep ctypes buffers alive until after CryptProtectData.
        _ = source_buf, extra_buf
        if not ok:
            raise VaultUnavailable("Windows DPAPI could not protect the credential")
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            self._kernel32.LocalFree(output.pbData)

    def _unprotect(self, value: bytes, entropy: bytes) -> bytes:
        source, source_buf = self._blob(value)
        extra, extra_buf = self._blob(entropy)
        output = self._DATA_BLOB()
        ok = self._crypt32.CryptUnprotectData(
            ctypes.byref(source), None, ctypes.byref(extra), None, None,
            self._CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output))
        _ = source_buf, extra_buf
        if not ok:
            # A wrong Collie/account binding intentionally looks like a missing secret.
            raise SecretNotFound("credential reference is absent or belongs to another account")
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            self._kernel32.LocalFree(output.pbData)

    def _path(self, service: str, account: str) -> Path:
        name = hashlib.sha256((service + "\0" + account).encode("utf-8")).hexdigest()
        return self.root / (name + ".dpapi")

    def put(self, service: str, account: str, secret: bytes, entropy: bytes) -> None:
        protected = self._protect(secret, entropy)
        _private_dir(self.root)
        target = self._path(service, account)
        temp = target.with_name(target.name + ".tmp-" + secrets.token_hex(6))
        try:
            with open(temp, "xb") as fh:
                fh.write(protected)
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.chmod(temp, 0o600)
            except OSError:
                pass
            os.replace(temp, target)
        finally:
            try:
                temp.unlink()
            except OSError:
                pass

    def get(self, service: str, account: str, entropy: bytes) -> bytes:
        try:
            protected = self._path(service, account).read_bytes()
        except OSError as exc:
            raise SecretNotFound("credential reference was not found") from exc
        return self._unprotect(protected, entropy)

    def delete(self, service: str, account: str, entropy: bytes) -> bool:
        path = self._path(service, account)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise VaultError("DPAPI credential could not be deleted") from exc


class _MacOSKeychainBackend:
    """Minimal ctypes wrapper around Generic Password Keychain items."""

    _NOT_FOUND = -25300
    _DUPLICATE = -25299

    def __init__(self):
        if sys.platform != "darwin":
            raise VaultUnavailable("macOS Keychain is not available on this platform")
        try:
            self._security = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/Security.framework/Security")
            self._core = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        except OSError as exc:
            raise VaultUnavailable("macOS Keychain is unavailable") from exc
        self._security.SecKeychainFindGenericPassword.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ]
        self._security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        self._security.SecKeychainAddGenericPassword.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        self._security.SecKeychainItemModifyAttributesAndData.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
        ]
        self._security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        self._security.SecKeychainItemDelete.argtypes = [ctypes.c_void_p]
        self._security.SecKeychainItemDelete.restype = ctypes.c_int32
        self._security.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._security.SecKeychainItemFreeContent.restype = ctypes.c_int32
        self._core.CFRelease.argtypes = [ctypes.c_void_p]
        self._core.CFRelease.restype = None

    @staticmethod
    def _raw(value: str):
        data = value.encode("utf-8")
        return data, len(data)

    def _find(self, service: str, account: str):
        service_b, service_n = self._raw(service)
        account_b, account_n = self._raw(account)
        length = ctypes.c_uint32()
        data = ctypes.c_void_p()
        item = ctypes.c_void_p()
        status = self._security.SecKeychainFindGenericPassword(
            None, service_n, ctypes.c_char_p(service_b), account_n,
            ctypes.c_char_p(account_b),
            ctypes.byref(length), ctypes.byref(data), ctypes.byref(item))
        return int(status), length, data, item

    def put(self, service: str, account: str, secret: bytes, entropy: bytes) -> None:
        del entropy  # binding is represented by the hashed Keychain service/account pair
        status, length, data, item = self._find(service, account)
        if status == 0:
            try:
                self._security.SecKeychainItemFreeContent(None, data)
                changed = self._security.SecKeychainItemModifyAttributesAndData(
                    item, None, len(secret), ctypes.c_char_p(secret))
                if changed != 0:
                    raise VaultError("macOS Keychain could not update the credential")
                return
            finally:
                if item:
                    self._core.CFRelease(item)
        if item:
            self._core.CFRelease(item)
        if status != self._NOT_FOUND:
            raise VaultUnavailable("macOS Keychain could not be queried")
        service_b, service_n = self._raw(service)
        account_b, account_n = self._raw(account)
        added = self._security.SecKeychainAddGenericPassword(
            None, service_n, ctypes.c_char_p(service_b), account_n,
            ctypes.c_char_p(account_b), len(secret), ctypes.c_char_p(secret), None)
        if added not in (0, self._DUPLICATE):
            raise VaultError("macOS Keychain could not store the credential")

    def get(self, service: str, account: str, entropy: bytes) -> bytes:
        del entropy
        status, length, data, item = self._find(service, account)
        if status == self._NOT_FOUND:
            raise SecretNotFound("credential reference was not found")
        if status != 0:
            raise VaultUnavailable("macOS Keychain could not read the credential")
        try:
            return ctypes.string_at(data, length.value)
        finally:
            self._security.SecKeychainItemFreeContent(None, data)
            if item:
                self._core.CFRelease(item)

    def delete(self, service: str, account: str, entropy: bytes) -> bool:
        del entropy
        status, length, data, item = self._find(service, account)
        if status == self._NOT_FOUND:
            return False
        if status != 0:
            raise VaultUnavailable("macOS Keychain could not query the credential")
        try:
            self._security.SecKeychainItemFreeContent(None, data)
            deleted = self._security.SecKeychainItemDelete(item)
            if deleted != 0:
                raise VaultError("macOS Keychain could not delete the credential")
            return True
        finally:
            if item:
                self._core.CFRelease(item)


class _LinuxSecretServiceBackend:
    """Secret Service client using secret-tool; secret values travel over stdin/stdout only."""

    def __init__(self, executable: str | None = None):
        if not sys.platform.startswith("linux"):
            raise VaultUnavailable("Secret Service is not available on this platform")
        self.executable = executable or shutil.which("secret-tool")
        if not self.executable:
            raise VaultUnavailable("Secret Service requires the secret-tool command")

    def _run(self, args: list[str], *, value: bytes | None = None):
        try:
            # A vault read must never flash a console window at the person (plat.no_window_kwargs
            # is a no-op off Windows, so this stays correct on the Linux path that uses it).
            from . import plat
            return subprocess.run(
                [self.executable, *args], input=value, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=False, timeout=20, **plat.no_window_kwargs())
        except (OSError, subprocess.SubprocessError) as exc:
            raise VaultUnavailable("Secret Service is unavailable") from exc

    def put(self, service: str, account: str, secret: bytes, entropy: bytes) -> None:
        del entropy
        result = self._run(
            ["store", "--label=Collie account credential", "service", service,
             "account", account], value=secret)
        if result.returncode != 0:
            raise VaultUnavailable("Secret Service refused the credential write")

    def get(self, service: str, account: str, entropy: bytes) -> bytes:
        del entropy
        result = self._run(["lookup", "service", service, "account", account])
        if result.returncode != 0:
            if result.stderr.strip():
                raise VaultUnavailable("Secret Service could not read the credential")
            raise SecretNotFound("credential reference was not found")
        if not result.stdout:
            raise SecretNotFound("credential reference was not found")
        return result.stdout.rstrip(b"\r\n")

    def delete(self, service: str, account: str, entropy: bytes) -> bool:
        del entropy
        result = self._run(["clear", "service", service, "account", account])
        if result.returncode == 0:
            return True
        # secret-tool uses 1 when no matching item exists.
        if result.returncode == 1 and not result.stderr.strip():
            return False
        raise VaultUnavailable("Secret Service refused the credential deletion")


def platform_backend(state_dir: str | os.PathLike[str] | None = None) -> VaultBackend:
    """Select the native store, failing closed on unsupported/unavailable systems."""
    if sys.platform == "win32":
        if state_dir is None:
            from .controlplane import state_dir as current_state_dir
            state_dir = current_state_dir()
        return _WindowsDPAPIBackend(Path(state_dir) / "account-vault")
    if sys.platform == "darwin":
        return _MacOSKeychainBackend()
    if sys.platform.startswith("linux"):
        return _LinuxSecretServiceBackend()
    raise VaultUnavailable("this platform has no supported OS credential service")


class IdentityVault:
    """Bind opaque credential references to exactly one Collie, account, and factor.

    ``get`` intentionally returns bytes only to the immediate caller.  It has no
    serialization, listing, receipt, or model-context API.  Host-side consumers
    should prefer :meth:`use`, whose temporary bytearray is wiped on return.
    """

    def __init__(self, *, state_dir: str | os.PathLike[str] | None = None,
                 backend: VaultBackend | None = None,
                 service_name: str = DEFAULT_SERVICE):
        self.service_name = str(service_name or DEFAULT_SERVICE)
        self.backend = backend if backend is not None else platform_backend(state_dir)

    @staticmethod
    def _validate_part(label: str, value: str) -> str:
        value = str(value or "").strip()
        if not value or len(value) > 1024 or "\x00" in value:
            raise ValueError("%s must be a non-empty bounded string" % label)
        return value

    def _bound(self, ref: str, collie_id: str, account: str, kind: str):
        if not _REF_RE.fullmatch(str(ref or "")):
            raise ValueError("invalid credential reference")
        collie_id = self._validate_part("collie_id", collie_id)
        account = self._validate_part("account", account)
        kind = self._validate_part("kind", kind).lower()
        collie_hash = hashlib.sha256(collie_id.encode("utf-8")).hexdigest()[:32]
        account_hash = hashlib.sha256(account.encode("utf-8")).hexdigest()[:32]
        service = "%s.v1.%s" % (self.service_name, collie_hash)
        key = "%s.%s.%s" % (account_hash, kind, ref)
        entropy = hashlib.sha256(
            (self.service_name + "\0" + collie_id + "\0" + account + "\0" +
             kind + "\0" + ref).encode("utf-8")).digest()
        return service, key, entropy

    @staticmethod
    def _wrap(secret: str | bytes | bytearray) -> bytes:
        if isinstance(secret, str):
            raw = secret.encode("utf-8")
        elif isinstance(secret, (bytes, bytearray)):
            raw = bytes(secret)
        else:
            raise TypeError("secret must be str or bytes")
        if not raw or len(raw) > 1024 * 1024:
            raise ValueError("secret must be non-empty and at most 1 MiB")
        return _ENVELOPE + base64.urlsafe_b64encode(raw)

    @staticmethod
    def _unwrap(value: bytes) -> bytes:
        if not value.startswith(_ENVELOPE):
            raise VaultError("credential envelope is invalid")
        try:
            return base64.b64decode(value[len(_ENVELOPE):], altchars=b"-_", validate=True)
        except (ValueError, TypeError) as exc:
            raise VaultError("credential envelope is invalid") from exc

    def put(self, secret: str | bytes | bytearray, *, collie_id: str,
            account: str, kind: str, ref: str = "") -> str:
        # AccountRegistry may reserve the opaque reference in its transaction
        # before touching the non-transactional OS vault.  That makes a crash
        # recoverable without ever storing plaintext or a predictable key.
        ref = str(ref or ("cv1_" + secrets.token_urlsafe(24)))
        service, key, entropy = self._bound(ref, collie_id, account, kind)
        self.backend.put(service, key, self._wrap(secret), entropy)
        return ref

    def get(self, ref: str, *, collie_id: str, account: str, kind: str) -> bytes:
        service, key, entropy = self._bound(ref, collie_id, account, kind)
        return self._unwrap(self.backend.get(service, key, entropy))

    def use(self, ref: str, *, collie_id: str, account: str, kind: str,
            consumer):
        """Call ``consumer`` with a wipeable buffer and never serialize the value."""
        value = bytearray(self.get(ref, collie_id=collie_id, account=account, kind=kind))
        try:
            return consumer(value)
        finally:
            for index in range(len(value)):
                value[index] = 0

    def delete(self, ref: str, *, collie_id: str, account: str, kind: str) -> bool:
        service, key, entropy = self._bound(ref, collie_id, account, kind)
        return self.backend.delete(service, key, entropy)


__all__ = [
    "DEFAULT_SERVICE", "IdentityVault", "SecretNotFound", "VaultBackend",
    "VaultError", "VaultUnavailable", "platform_backend",
]
