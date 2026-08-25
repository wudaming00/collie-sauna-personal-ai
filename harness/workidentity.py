"""First-party Collie-owned work identities with explicit operational authority.

Connection records contain Collie's public work address/number plus safe metadata.
Mail private keys stay in the Dogmail store, Google credentials stay in the user's
Chrome profile, and one-time codes live only in the stack frame that moves one
fresh code into the already-open verification form.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import unicodedata


VOICE_SPACE = "connection.google_voice"
VOICE_ORIGIN = "https://voice.google.com"
VOICE_SCOPES = (
    "voice.messages.read",
    "voice.messages.draft_for_user_send",
    "voice.calls.manual_or_forwarded",
    "voice.voicemail.read",
    "verification_code.read_and_fill",
)


_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}


class WorkIdentityStateCorrupt(RuntimeError):
    """The durable identity file is malformed and must not be replaced."""


class WorkIdentityStateUnavailable(RuntimeError):
    """The durable identity file exists but cannot be read safely."""


def _root(state_dir=None):
    if state_dir:
        return os.path.abspath(os.path.expanduser(state_dir))
    from .controlplane import state_dir as current_state_dir
    return current_state_dir()


def _path(state_dir=None):
    return os.path.join(_root(state_dir), "work-identities.json")


def _preserve_corrupt(path, raw):
    digest = hashlib.sha256(raw).hexdigest()[:16]
    backup = "%s.corrupt-%s.bak" % (path, digest)
    try:
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return backup
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            from . import plat
            plat.chmod_private(backup)
        except Exception:
            pass
    except Exception:
        try:
            os.unlink(backup)
        except OSError:
            pass
    return backup


def _load_unlocked(state_dir=None):
    path = _path(state_dir)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise WorkIdentityStateUnavailable(
            "Collie's work identity state cannot be read safely") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        _preserve_corrupt(path, raw)
        raise WorkIdentityStateCorrupt(
            "Collie's work identity state is corrupt; the original was preserved") from exc
    if not isinstance(data, dict):
        _preserve_corrupt(path, raw)
        raise WorkIdentityStateCorrupt(
            "Collie's work identity state is corrupt; the original was preserved")
    return data


def _load(state_dir=None):
    # Atomic replace means readers see either complete generation.  Mutating
    # callers use _mutate(), which additionally serializes read/modify/write.
    return _load_unlocked(state_dir)


@contextlib.contextmanager
def _state_lock(state_dir=None):
    """Serialize writers across both threads and Collie processes."""
    path = _path(state_dir)
    lock_path = path + ".lock"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(lock_path, threading.RLock())
    with thread_lock:
        with open(lock_path, "a+b") as lock_file:
            try:
                from . import plat
                plat.chmod_private(lock_path)
            except Exception:
                pass
            if os.name == "nt":
                import msvcrt
                lock_file.seek(0, os.SEEK_END)
                if lock_file.tell() == 0:
                    lock_file.write(b"\0")
                    lock_file.flush()
                    os.fsync(lock_file.fileno())
                acquired = False
                while not acquired:
                    try:
                        lock_file.seek(0)
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                        acquired = True
                    except OSError:
                        time.sleep(0.01)
                try:
                    yield
                finally:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _write_unlocked(data, state_dir=None):
    path = _path(state_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=os.path.basename(path) + ".", suffix=".tmp",
        dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        try:
            from . import plat
            plat.chmod_private(tmp)
        except Exception:
            pass
        os.replace(tmp, path)
        # Persist the directory entry where the platform supports directory
        # fsync.  Windows' ReplaceFile semantics are already atomic and opening
        # a directory this way is not supported.
        try:
            directory_fd = os.open(os.path.dirname(path), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _merge_dict(base, incoming):
    merged = dict(base) if isinstance(base, dict) else {}
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _save(data, state_dir=None):
    """Merge a possibly stale snapshot without losing another writer's keys."""
    if not isinstance(data, dict):
        raise TypeError("work identity state must be an object")
    with _state_lock(state_dir):
        latest = _load_unlocked(state_dir)
        _write_unlocked(_merge_dict(latest, data), state_dir)


def _mutate(mutator, state_dir=None):
    """Reload and mutate the latest generation while holding the process lock."""
    with _state_lock(state_dir):
        latest = _load_unlocked(state_dir)
        result = mutator(latest)
        _write_unlocked(latest, state_dir)
        return result


def _companion_name():
    from . import settings
    return str(settings.get("COMPANION_NAME", "") or "Collie").strip() or "Collie"


def _mailbox_slug(name):
    """Stable ASCII local-part for a display name, including non-Latin names."""
    original = str(name or "").strip() or "Collie"
    folded = unicodedata.normalize("NFKD", original).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    if not slug:
        slug = "collie-" + hashlib.sha256(original.encode("utf-8")).hexdigest()[:8]
    return slug[:31].rstrip("-") or "collie"


def _mailbox_key(state_dir=None):
    configured = _load(state_dir).get("collie_mail") or {}
    return str(configured.get("dog") or _mailbox_slug(_companion_name()))


def _mail_status(state_dir=None):
    from . import dogmail
    key = _mailbox_key(state_dir)
    status = dogmail.public_mailbox(key, state_dir=state_dir)
    if not status.get("connected") and not (_load(state_dir).get("collie_mail") or {}).get("dog"):
        # Adopt a legacy single-dog store. Multiple boxes are never guessed between.
        only = dogmail.public_mailbox("", state_dir=state_dir)
        if only.get("connected"):
            status = only
    return status


def _voice_connection(state_dir=None):
    row = (_load(state_dir).get("google_voice") or {})
    connected = bool(row.get("connected"))
    number = str(row.get("number") or "")
    masked = ("•••-•••-%s" % row.get("last4")) if connected else ""
    return {
        "id": "google_voice",
        "label": "Google Voice",
        "connected": connected,
        "account": number or masked,
        "account_masked": masked,
        "ownership": str(row.get("ownership") or ""),
        # Project the current product authority even if an older state file still
        # contains a now-retired automatic send/call scope.
        "scopes": list(VOICE_SCOPES) if connected else [],
        "verified_at": int(row.get("verified_at") or 0),
        "description": ("Collie's assigned public work line for inbox, verification codes, "
                        "voicemail, drafted replies, and manual or forwarded calls."),
    }


def _mail_connection(state_dir=None):
    status = _mail_status(state_dir)
    connected = bool(status.get("connected"))
    if connected:
        state = "ready"
    elif status.get("handle_verified"):
        state = "ready_to_provision"
    else:
        state = "setup_required"
    stored = _load(state_dir).get("collie_mail") or {}
    return {
        "id": "collie_mail",
        "label": "Collie Mail",
        "connected": connected,
        "provisionable": bool(status.get("provisionable")),
        "status": state,
        "account": str(status.get("account") or ""),
        "account_masked": str(status.get("account_masked") or ""),
        "ownership": "collie_owned",
        "scopes": (["mail.receive", "verification_code.read_and_fill",
                    "account_registration.identity"] if connected else []),
        "verified_at": int(stored.get("verified_at") or 0),
        "description": "Collie's own encrypted work mailbox for account registration, mail, and verification codes.",
    }


def public_connections(state_dir=None):
    """Return Collie's public work identities, never keys, credentials, or OTPs."""
    return [_mail_connection(state_dir), _voice_connection(state_dir)]


def public_identity(state_dir=None):
    """Model/UI-safe public identity owned by Collie (never the user's identity)."""
    fields = {"display_name": _companion_name()}
    mail = _mail_connection(state_dir)
    voice = _voice_connection(state_dir)
    if mail.get("connected"):
        fields["email"] = mail.get("account", "")
        status = _mail_status(state_dir)
        fields["username"] = str(status.get("dog") or _mailbox_slug(fields["display_name"]))
    else:
        fields["username"] = _mailbox_slug(fields["display_name"])
    # A legacy last-four-only Voice connection is status, not a usable identity.
    if voice.get("connected") and str(voice.get("account") or "").startswith("+"):
        fields["phone"] = voice["account"]
    return fields


def model_identity(state_dir=None):
    """Structured identity.status payload; alias-safe for the Mission capability."""
    public = public_identity(state_dir)
    mail = _mail_connection(state_dir)
    voice = _voice_connection(state_dir)
    result = {
        "principal": "collie",
        "name": public["display_name"],
        "username": public["username"],
        "mailbox_status": mail.get("status") or "setup_required",
        "phone_status": ("ready" if public.get("phone") else
                         ("legacy_masked_only" if voice.get("connected") else "setup_required")),
        "ownership": "collie_owned_work_identity",
    }
    for field in ("email", "phone"):
        if public.get(field):
            result[field] = public[field]
    try:
        from .brain_router import collie_device_id
        result["collie_id"] = collie_device_id()
    except Exception:
        pass
    result["status"] = "ready" if result.get("email") and result.get("phone") else "needs_setup"
    return result


def _public_connection(connection_id, state_dir=None):
    return next(row for row in public_connections(state_dir) if row["id"] == connection_id)


def provision_collie_mail(name="", state_dir=None, relay=""):
    """Idempotently give this Collie a mailbox after namespace ownership exists."""
    from . import dogmail
    data = _load(state_dir)
    existing = data.get("collie_mail") or {}
    legacy = dogmail.public_mailbox("", state_dir=state_dir) if not existing.get("dog") else {}
    dog = str(existing.get("dog") or (legacy.get("dog") if legacy.get("connected") else "")
              or _mailbox_slug(name or _companion_name()))
    result = dogmail.claim_dog(dog, relay=relay, state_dir=state_dir)
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "Collie Mail provisioning failed")
    def update(latest):
        current = latest.get("collie_mail") or {}
        latest["collie_mail"] = {
            "connected": True,
            "dog": dog,
            "scopes": ["mail.receive", "verification_code.read_and_fill",
                       "account_registration.identity"],
            "verified_at": int(current.get("verified_at") or
                               existing.get("verified_at") or time.time()),
        }
    _mutate(update, state_dir)
    return _public_connection("collie_mail", state_dir)


def _bridge_data(result):
    from .browserbridge import _data
    data = _data(result)
    if data is None:
        error = result.get("error") if isinstance(result, dict) else "browser bridge unavailable"
        raise RuntimeError(error or "browser bridge command failed")
    return data


def _normalize_phone(value):
    """Normalize a full assigned Voice number; a last-four hint is not a number."""
    raw = str(value or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if raw.startswith("+") and 8 <= len(digits) <= 15:
        return "+" + digits
    return ""


def connect_google_voice(expected_last4="", state_dir=None):
    """Attach Voice and persist the number explicitly assigned as Collie's identity."""
    expected_last4 = "".join(ch for ch in str(expected_last4 or "") if ch.isdigit())[-4:]
    from . import browserbridge as bb
    attached = _bridge_data(bb._call({
        "action": "attach", "space": VOICE_SPACE, "origin": VOICE_ORIGIN}, timeout=10))
    try:
        identity = _bridge_data(bb._call({
            "action": "voice_identity", "space": VOICE_SPACE}, timeout=10))
        number = _normalize_phone(identity.get("number") or identity.get("phone") or "")
        actual = str(identity.get("last4") or (number[-4:] if number else ""))
        if len(actual) != 4:
            raise RuntimeError("Google Voice number was not visible on the connected page")
        if expected_last4 and actual != expected_last4:
            raise RuntimeError("the open Google Voice number does not match the requested ending")
        connection = {
            "connected": True, "last4": actual, "origin": VOICE_ORIGIN,
            "space": VOICE_SPACE, "scopes": list(VOICE_SCOPES),
            "ownership": "user_owned_assigned_to_collie",
            "verified_at": int(time.time()),
        }
        if number:
            connection["number"] = number
        _mutate(lambda latest: latest.__setitem__("google_voice", connection), state_dir)
        return _public_connection("google_voice", state_dir)
    except Exception:
        if attached.get("attached"):
            bb._call({"action": "release", "space": VOICE_SPACE}, timeout=5)
        raise


def disconnect_google_voice(state_dir=None):
    from . import browserbridge as bb
    try:
        bb._call({"action": "release", "space": VOICE_SPACE}, timeout=5)
    except Exception:
        pass
    _mutate(lambda latest: latest.pop("google_voice", None), state_dir)
    return _public_connection("google_voice", state_dir)


def take_google_voice_code(service, max_age_seconds=600, state_dir=None):
    """Return one fresh matching code transiently; callers must never persist it."""
    row = _load(state_dir).get("google_voice") or {}
    if not row.get("connected"):
        raise RuntimeError("Google Voice verification-code connection is not connected")
    service = str(service or "").strip()
    if not service or len(service) > 100:
        raise RuntimeError("the expected service name is required")
    from . import browserbridge as bb
    data = _bridge_data(bb._call({
        "action": "google_voice_otp", "space": VOICE_SPACE,
        "service": service, "max_age_seconds": max(60, min(900, int(max_age_seconds))),
    }, timeout=10))
    code = str(data.pop("code", "") or "")
    if not code:
        raise RuntimeError(data.get("error") or "no fresh matching verification code found")
    masked = "•••-•••-%s" % row.get("last4")
    return code, {"source": "google_voice", "account_masked": masked, "account": masked,
                  "received_at": int(data.get("received_at") or 0)}


def take_verification_code(service, max_age_seconds=600, state_dir=None, channel="auto",
                           sender="", subject="", mailbox_name="", relay=""):
    """Receive one OTP through Collie's channel and keep it runtime-transient.

    ``auto`` inspects every connected channel and fails closed if more than one
    produces a plausible code. Callers that know the challenge channel should pass
    ``collie_mail``/``email`` or ``google_voice``/``sms`` explicitly. The caller may
    use the returned code immediately but must never persist it in Memory or receipts.
    """
    service = str(service or "").strip()
    if not service or len(service) > 100:
        raise RuntimeError("the expected service name is required")
    channel = str(channel or "auto").strip().lower()
    aliases = {"email": "collie_mail", "mail": "collie_mail",
               "phone": "google_voice", "sms": "google_voice", "voice": "google_voice"}
    channel = aliases.get(channel, channel)
    if channel not in ("auto", "collie_mail", "google_voice"):
        raise RuntimeError("verification channel must be auto, collie_mail/email, or google_voice/sms")

    configured = {row["id"]: row for row in public_connections(state_dir)}
    wanted = ([channel] if channel != "auto" else
              [source for source in ("collie_mail", "google_voice")
               if configured[source].get("connected")])
    if not wanted:
        raise RuntimeError("no Collie-owned verification channel is connected")

    successes = []
    failures = []
    for source in wanted:
        if not configured[source].get("connected"):
            failures.append(source)
            continue
        try:
            if source == "collie_mail":
                from . import dogmail
                status = _mail_status(state_dir)
                successes.append(dogmail.take_verification_code(
                    mailbox_name or status.get("dog") or _mailbox_key(state_dir), service, sender=sender,
                    subject=subject, max_age_seconds=max_age_seconds, relay=relay,
                    state_dir=state_dir))
            else:
                successes.append(take_google_voice_code(
                    service, max_age_seconds=max_age_seconds, state_dir=state_dir))
        except RuntimeError:
            # Error details may contain page/message material. The boundary reports
            # only which safe source failed, never code or identity contents.
            failures.append(source)
    if len(successes) > 1:
        raise RuntimeError("multiple Collie-owned channels produced a matching verification code; specify the channel")
    if len(successes) == 1:
        return successes[0]
    if channel != "auto" and failures:
        raise RuntimeError("no fresh, uniquely matching verification code found in %s" % channel)
    raise RuntimeError("no fresh, uniquely matching verification code found in connected Collie-owned channels")


def _mask_text(value):
    value = str(value or "")
    if not value:
        return ""
    if len(value) == 1:
        return value + "•••"
    return value[0] + "•••" + value[-1]


def resolve_identity_field(field, state_dir=None):
    """Resolve a public work-identity field owned by Collie.

    Unlike the human user's identity, the first tuple item may be used by Collie's
    model and typed into a target form. The second item remains the minimal masked
    representation suitable for durable audit receipts.
    """
    field = str(field or "").strip().lower().replace("-", "_")
    aliases = {"name": "display_name", "display": "display_name",
               "user": "username", "user_name": "username",
               "mail": "email", "email_address": "email",
               "telephone": "phone", "phone_number": "phone"}
    field = aliases.get(field, field)
    if field == "email":
        from . import dogmail
        status = _mail_status(state_dir)
        dog = str(status.get("dog") or _mailbox_key(state_dir))
        value = dogmail.mailbox_address(dog, state_dir=state_dir)
        masked = dogmail.mask_address(value)
        return value, {"source": "collie_mail", "account_masked": masked}
    if field == "phone":
        row = _load(state_dir).get("google_voice") or {}
        if not row.get("connected"):
            raise RuntimeError("Collie's work phone is not connected")
        number = _normalize_phone(row.get("number") or "")
        if number:
            return number, {"source": "google_voice",
                            "account_masked": "•••-•••-%s" % number[-4:]}
        # Legacy connectors exposed/stored last4 only. Inventing the other digits
        # would create an account identity that cannot be recovered.
        raise RuntimeError(
            "Collie's work phone is connected as masked metadata only; "
            "a secure provider seam is required before it can fill a phone field")
    if field == "display_name":
        value = _companion_name()
        return value, {"source": "collie_identity", "account_masked": _mask_text(value)}
    if field == "username":
        status = _mail_status(state_dir)
        value = str(status.get("dog") or _mailbox_slug(_companion_name()))
        return value, {"source": "collie_identity", "account_masked": _mask_text(value)}
    raise RuntimeError("identity field must be email, phone, display_name, or username")
