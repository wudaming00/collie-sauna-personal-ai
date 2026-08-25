"""An address of a dog's own, and the ability to wait for a letter.

Why this exists. Every service on the internet proves you are a person by mailing you something —
a verification link, an invite, a code. A dog without an address must borrow its owner's inbox,
which means a human reads the mail and a human clicks the link, which is exactly the interruption
that makes an agent an assistant rather than a colleague. `wait_for` is therefore the point of this
module; `list` and `read` are conveniences around it.

Why the relay cannot read it. Mail for every user's dogs passes through one hosted Worker. A design
where that operator can read verification codes contradicts the thing collie is — so the Worker
seals each message to the receiving dog's public key the moment it arrives and stores only
ciphertext. What it keeps is unreadable to it, by construction rather than by policy.

Be precise about the limits, because "end-to-end encrypted" would be a lie here:
  · SMTP is a cleartext protocol. The message exists in plaintext in Worker memory for the instant
    between delivery and sealing. The promise is that it is never STORED in the clear.
  · The relay sees metadata: which address received something, when, and how big.
  · The private key lives on this machine. A compromised machine means that dog's mail is readable;
    what the encryption buys is that a compromised RELAY is not.

Identity, and what binds a dog to its address:

  handle "daming"   claimed once, by proving control of a real mailbox (a code is mailed there),
                    and bound from then on to a handle key. Only that key can create addresses
                    under `*.daming@…`, which is what stops someone else claiming your dog's name.
        │
        ├── dog "rowan" — its own keypair, generated on ITS machine and never moved. Registered
        └── dog "juno"    with a tag the handle key makes; retiring one is a revocation, not an
                          address left behind in a stranger's account records.

Authentication carries no bearer token: a token on disk is a token that can be copied. Every
request is stamped with an HMAC over a key derived from X25519(dog_private, relay_public) — the
relay can recompute it because it is a party to that exchange, and nobody else can.

    K_auth = HKDF(X25519(dog_priv, relay_pub), salt=address, info="collie-mail-auth")
    mac    = HMAC(K_auth, method ‖ path ‖ ts ‖ nonce)

KNOWN LIMIT of doing it with key agreement rather than signatures: the relay operator, holding its
own private key, could register a different key for an address it hosts — i.e. redirect future mail
for an address, though never read what has already been delivered. Closing that needs a signature
scheme (Ed25519) so the handle's authority is checkable without the relay being a party to it.
Written down here rather than left as an assumption.
"""
import base64
import email
import email.policy
import hashlib
import html
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from . import e2e

RELAY = os.environ.get("COLLIE_MAIL_RELAY", "https://mail.collie.run")
DOMAIN = os.environ.get("COLLIE_MAIL_DOMAIN", "collie.run")
STORE = os.path.expanduser("~/.collie/mail.json")
_DEFAULT_STORE = STORE


class MailStateCorrupt(RuntimeError):
    """The mailbox key store is malformed and cannot be treated as empty."""


class MailStateUnavailable(RuntimeError):
    """The mailbox key store exists but cannot be read safely."""

INFO_AUTH = b"collie-mail-auth"
INFO_SEAL = b"collie-mail-seal"
INFO_CERT = b"collie-mail-cert"
SKEW = 120                      # seconds a request stamp may be off before the relay refuses it


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def ub64(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


# ---------------------------------------------------------------- the store

def _store_path(state_dir=None) -> str:
    """Resolve mail keys inside the same Collie state root as their identity.

    ``STORE`` remains an explicit legacy/test override when no state root is
    supplied.  A caller-provided root always wins, preventing process A's
    import-time home path from leaking a mailbox into Collie B.
    """
    if state_dir is not None:
        return os.path.join(
            os.path.abspath(os.path.expanduser(str(state_dir))), "mail.json")
    if STORE != _DEFAULT_STORE:
        return os.path.abspath(os.path.expanduser(STORE))
    try:
        from .controlplane import state_dir as current_state_dir
        return os.path.join(current_state_dir(), "mail.json")
    except Exception:
        return _DEFAULT_STORE


def _preserve_corrupt(path: str, raw: bytes) -> str:
    digest = hashlib.sha256(raw).hexdigest()[:16]
    backup = "%s.corrupt-%s.bak" % (path, digest)
    try:
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return backup
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
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


def load(state_dir=None) -> dict:
    path = _store_path(state_dir)
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise MailStateUnavailable(
            "Collie Mail key state cannot be read safely") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        _preserve_corrupt(path, raw)
        raise MailStateCorrupt(
            "Collie Mail key state is corrupt; the original was preserved") from exc
    if not isinstance(data, dict):
        _preserve_corrupt(path, raw)
        raise MailStateCorrupt(
            "Collie Mail key state is corrupt; the original was preserved")
    return data


def save(d: dict, state_dir=None) -> None:
    if not isinstance(d, dict):
        raise TypeError("Collie Mail state must be an object")
    path = _store_path(state_dir)
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    # Never let a mutation turn malformed key state into an apparently fresh
    # mailbox.  Recovery is explicit: repair/remove the original after using
    # the preserved backup, then retry.
    if os.path.exists(path):
        load(state_dir)
    fd, tmp = tempfile.mkstemp(
        prefix=os.path.basename(path) + ".", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        try:
            from . import plat
            plat.chmod_private(tmp)          # private keys live in here
        except Exception:
            pass
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def address_for(dog: str, handle: str) -> str:
    """`rowan.daming@collie.run` — flat, so one MX and one catch-all Worker serve every user and
    adding a dog is a row rather than a DNS change."""
    return "%s.%s@%s" % (dog.strip().lower(), handle.strip().lower(), DOMAIN)


def mask_address(address: str) -> str:
    """Return a receipt-safe hint without disclosing the mailbox address.

    The mailbox itself is Collie's public work identity and may be shown in UI/model
    context; audit metadata still uses this masked form to avoid needless repetition.
    """
    local, sep, domain = str(address or "").partition("@")
    if not sep:
        return ""
    dog = local.split(".", 1)[0]
    if not dog:
        masked = "•••"
    elif len(dog) == 1:
        masked = dog + "•••"
    else:
        masked = dog[0] + "•••" + dog[-1]
    return "%s@%s" % (masked, domain)


def public_mailbox(name: str = "", state_dir=None) -> dict:
    """Describe local mailbox readiness without ever returning key material.

    This deliberately reads only the encrypted-mail key store on this machine;
    it does not contact the relay and cannot leak a handle/dog private key. The
    full address is intentional: it belongs to Collie, not to the human user.
    """
    st = load(state_dir)
    handle = st.get("handle") or {}
    dogs = st.get("dogs") or {}
    dog = dogs.get(name) if name else None
    dog_name = name if dog else ""
    if not dog and not name and len(dogs) == 1:
        dog_name, dog = next(iter(dogs.items()))
    dog = dog or {}
    connected = bool(dog.get("address") and dog.get("priv") and dog.get("pub"))
    return {
        "connected": connected,
        "provisionable": bool(handle.get("verified")) and not connected,
        "handle_verified": bool(handle.get("verified")),
        "dog": dog_name if connected else (name or ""),
        "account": str(dog.get("address") or "") if connected else "",
        "account_masked": mask_address(dog.get("address", "")) if connected else "",
    }


def mailbox_address(name: str = "", state_dir=None) -> str:
    """Resolve the public work address owned by this Collie.

    It may be supplied to Collie's model and used for account registration. It is
    never confused with or substituted for the human user's personal identity.
    """
    dog = _dog(name, state_dir=state_dir)
    address = str(dog.get("address") or "")
    if not address:
        raise RuntimeError("Collie Mail is not provisioned for this Collie")
    return address


# ---------------------------------------------------------------- keys and envelopes

def _derive(private: bytes, peer_public: bytes, salt: bytes, info: bytes) -> bytes:
    return e2e._hkdf(e2e.shared_secret(private, peer_public), salt, info)


def auth_key(dog_priv: bytes, relay_pub: bytes, address: str) -> bytes:
    return _derive(dog_priv, relay_pub, address.encode("utf-8"), INFO_AUTH)


def cert_tag(handle_priv: bytes, relay_pub: bytes, address: str, dog_pub: bytes) -> bytes:
    """The handle's authority over one address, in a form the relay can check.

    Keyed by the handle↔relay agreement, so a claim is only accepted for an address whose handle
    key made this tag — that is what stops one user creating a dog under another's handle.
    """
    k = _derive(handle_priv, relay_pub, b"handle", INFO_CERT)
    return _mac(k, e2e.lp(address) + e2e.lp(dog_pub))


def _mac(key: bytes, message: bytes) -> bytes:
    c = e2e._crypto()
    m = c["hmac"].HMAC(key, c["hashes"].SHA256())
    m.update(message)
    return m.finalize()


def seal_to_dog(dog_pub: bytes, plaintext: bytes) -> dict:
    """What the Worker does on delivery, mirrored here so the tests exercise the real path.

    Ephemeral-static: a throwaway keypair per message, so the sender needs no long-term identity
    and nothing links two messages to one another.
    """
    eph_priv, eph_pub = e2e.keypair()
    key = _derive(eph_priv, dog_pub, b"", INFO_SEAL)
    env = e2e.seal(key, plaintext, e2e.lp(eph_pub))
    env["epk"] = b64(eph_pub)
    return env


def open_from_relay(dog_priv: bytes, env: dict) -> bytes:
    eph_pub = ub64(env["epk"])
    key = _derive(dog_priv, eph_pub, b"", INFO_SEAL)
    return e2e.open_(key, env, e2e.lp(eph_pub))


# ---------------------------------------------------------------- the relay

# urllib's default User-Agent ("Python-urllib/3.x") is refused by Cloudflare's bot protection with
# `error code: 1010` — the relay never sees the request. Found the hard way: the same URL answered
# from PowerShell and not from here. A client that identifies itself is also the polite thing.
UA = "collie-mail/1.0 (+https://collie.run)"


def _post(path: str, payload: dict, headers: dict = None, relay: str = "") -> dict:
    url = (relay or RELAY).rstrip("/") + path
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers=dict({"content-type": "application/json", "user-agent": UA}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return dict(json.loads(body or "{}"), ok=False, status=e.code)
        except ValueError:
            return {"ok": False, "status": e.code, "error": body[:200]}


def _get(path: str, headers: dict = None, relay: str = "") -> dict:
    url = (relay or RELAY).rstrip("/") + path
    req = urllib.request.Request(url, headers=dict({"user-agent": UA}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "error": e.read().decode("utf-8", "replace")[:200]}


def relay_public(relay: str = "", state_dir=None) -> bytes:
    """The relay's X25519 public key. Cached after the first fetch — but note what trusting it on
    first use means: whoever answers this endpoint becomes the party every auth key is derived
    against. Pin it in the store rather than fetching it fresh each time."""
    st = load(state_dir)
    if st.get("relay_pub"):
        return ub64(st["relay_pub"])
    d = _get("/pubkey", relay=relay)
    if not d.get("pub"):
        raise RuntimeError("relay did not publish a public key: %s" % json.dumps(d)[:200])
    st["relay_pub"] = d["pub"]
    save(st, state_dir)
    return ub64(d["pub"])


def _signed_headers(dog: dict, method: str, path: str, relay_pub: bytes) -> dict:
    ts = str(int(time.time()))
    nonce = b64(os.urandom(12))
    k = auth_key(ub64(dog["priv"]), relay_pub, dog["address"])
    mac = _mac(k, e2e.lp(method) + e2e.lp(path) + e2e.lp(ts) + e2e.lp(nonce))
    return {"x-collie-addr": dog["address"], "x-collie-ts": ts,
            "x-collie-nonce": nonce, "x-collie-mac": b64(mac)}


# ---------------------------------------------------------------- claiming

def claim_handle(handle: str, email: str, relay: str = "", state_dir=None) -> dict:
    """Step one, once per person: prove you control a real mailbox, and bind the handle to a key."""
    st = load(state_dir)
    priv, pub = e2e.keypair()
    st.setdefault("handle", {})
    st["handle"].update({"name": handle, "priv": b64(priv), "pub": b64(pub), "verified": False})
    save(st, state_dir)
    return _post("/handle/claim", {"handle": handle, "pub": b64(pub), "email": email}, relay=relay)


def verify_handle(code: str, relay: str = "", state_dir=None) -> dict:
    st = load(state_dir)
    h = st.get("handle") or {}
    if not h.get("name"):
        return {"ok": False, "error": "no handle claimed on this machine yet"}
    d = _post("/handle/verify", {"handle": h["name"], "code": code, "pub": h["pub"]}, relay=relay)
    if d.get("ok"):
        h["verified"] = True
        save(st, state_dir)
    return d


def claim_dog(name: str, relay: str = "", state_dir=None) -> dict:
    """Give one dog an address. Its key is made HERE and never leaves."""
    st = load(state_dir)
    h = st.get("handle") or {}
    if not h.get("verified"):
        return {"ok": False, "error": "claim and verify a handle first (collie mail claim)"}
    dogs = st.setdefault("dogs", {})
    if name in dogs and dogs[name].get("address"):
        return {"ok": True, "address": dogs[name]["address"], "note": "already had one"}
    rp = relay_public(relay, state_dir=state_dir)
    priv, pub = e2e.keypair()
    address = address_for(name, h["name"])
    tag = cert_tag(ub64(h["priv"]), rp, address, pub)
    d = _post("/dog/claim", {"address": address, "pub": b64(pub), "handle": h["name"],
                             "cert": b64(tag)}, relay=relay)
    if not d.get("ok"):
        return d
    dogs[name] = {"address": address, "priv": b64(priv), "pub": b64(pub), "cursor": 0}
    save(st, state_dir)
    return {"ok": True, "address": address}


# ---------------------------------------------------------------- reading

def _dog(name: str = "", state_dir=None) -> dict:
    st = load(state_dir)
    dogs = st.get("dogs") or {}
    if name:
        return dogs.get(name) or {}
    return (list(dogs.values()) or [{}])[0]


def fetch(name: str = "", since: int = None, relay: str = "", advance_cursor: bool = True,
          state_dir=None) -> list:
    """Everything waiting, decrypted here. The relay hands over ciphertext and a delivery time."""
    dog = _dog(name, state_dir=state_dir)
    if not dog.get("address"):
        return []
    rp = relay_public(relay, state_dir=state_dir)
    cursor = dog.get("cursor", 0) if since is None else since
    path = "/mail?since=%d" % cursor
    d = _get(path, headers=_signed_headers(dog, "GET", path, rp), relay=relay)
    out = []
    for m in d.get("messages") or []:
        try:
            raw = open_from_relay(ub64(dog["priv"]), m["env"])
        except Exception as e:
            out.append({"at": m.get("at"), "error": "could not open: %s" % type(e).__name__})
            continue
        try:
            msg = json.loads(raw.decode("utf-8"))
        except Exception:
            msg = {"raw": raw.decode("utf-8", "replace")}
        msg["at"] = m.get("at")
        out.append(msg)
    if out and advance_cursor:
        st = load(state_dir)
        for k, v in (st.get("dogs") or {}).items():
            if v.get("address") == dog["address"]:
                v["cursor"] = max([m.get("at") or 0 for m in d.get("messages") or []] + [cursor])
        save(st, state_dir)
    return out


_CODE_AFTER = re.compile(
    r"\b(?:verification|security|login|sign[ -]?in|one[ -]?time|otp|passcode|auth(?:entication)?)"
    r"(?:\s+(?:verification|security|login|sign[ -]?in|one[ -]?time|otp|passcode|auth(?:entication)?))*"
    r"\s+code\b\s*(?:is|:|=|-)?\s*([A-Z0-9]{4,8})\b|"
    r"\b(?:otp|passcode)\b\s*(?:is|:|=|-)?\s*([A-Z0-9]{4,8})\b",
    re.IGNORECASE,
)
_CODE_BEFORE = re.compile(
    r"\b([A-Z0-9]{4,8})\b\s+(?:is\s+)?(?:your\s+)?"
    r"(?:verification|security|login|sign[ -]?in|one[ -]?time|otp|passcode|auth(?:entication)?)"
    r"(?:\s+(?:verification|security|login|sign[ -]?in|one[ -]?time|otp|passcode|auth(?:entication)?))*"
    r"(?:\s+code)?\b",
    re.IGNORECASE,
)
_PLAIN_CODE_AFTER = re.compile(
    r"\b(?:your\s+)?code\b\s*(?:is|:|=|-)\s*([A-Z0-9]{4,8})\b",
    re.IGNORECASE,
)
_USE_CODE = re.compile(
    r"\b(?:use|enter)\s+([A-Z0-9]{4,8})\s+(?:to\s+)?"
    r"(?:verify|authenticate|sign[ -]?in|log[ -]?in)\b",
    re.IGNORECASE,
)


def _decoded_message_text(message: dict) -> str:
    """Extract human-readable text locally from the sealed RFC-822 payload."""
    chunks = []
    for key in ("subject", "text"):
        value = message.get(key)
        if isinstance(value, str) and value:
            chunks.append(value)
    encoded = message.get("raw")
    if not isinstance(encoded, str) or not encoded:
        return "\n".join(chunks)
    try:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
        parsed = email.message_from_bytes(raw, policy=email.policy.default)
        parts = parsed.walk() if parsed.is_multipart() else [parsed]
        for part in parts:
            if part.get_content_disposition() == "attachment":
                continue
            content_type = part.get_content_type()
            if content_type not in ("text/plain", "text/html"):
                continue
            try:
                value = part.get_content()
            except Exception:
                payload = part.get_payload(decode=True) or b""
                value = payload.decode(part.get_content_charset() or "utf-8", "replace")
            if not isinstance(value, str):
                continue
            if content_type == "text/html":
                value = re.sub(r"<[^>]+>", " ", value)
                value = html.unescape(value)
            chunks.append(value)
    except Exception:
        # A malformed/truncated RFC-822 payload is not evidence from which to guess
        # an authentication code. Header/text fields already supplied remain usable.
        pass
    return "\n".join(chunks)


def _expected_sender_matches(actual: str, expected: str) -> bool:
    expected = str(expected or "").strip().lower()
    if not expected:
        return True
    actual = str(actual or "").strip().lower()
    actual_addr = email.utils.parseaddr(actual)[1].lower()
    if expected.startswith("@"):
        return actual_addr.rpartition("@")[2] == expected[1:]
    if "@" in expected:
        return actual_addr == email.utils.parseaddr(expected)[1].lower()
    # A bare dotted value is treated as an exact domain, not a substring of an
    # attacker-controlled lookalike domain ("notstripe.com").
    if "." in expected and " " not in expected:
        return actual_addr.rpartition("@")[2] == expected
    return expected in actual


def _service_matches(service: str, sender: str, subject: str) -> bool:
    haystack = "%s %s" % (str(sender or "").lower(), str(subject or "").lower())
    tokens = re.findall(r"[a-z0-9]+", str(service or "").lower())
    return bool(tokens) and all(re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(token),
                                          haystack) for token in tokens)


def _verification_codes(text: str) -> set:
    found = set()
    for pattern in (_CODE_AFTER, _CODE_BEFORE, _PLAIN_CODE_AFTER, _USE_CODE):
        for match in pattern.finditer(str(text or "")):
            token = next((group for group in match.groups() if group), "").upper()
            # Requiring at least one digit avoids interpreting nearby words such
            # as "ready" or "secure" as an alphanumeric OTP.
            if token and any(ch.isdigit() for ch in token):
                found.add(token)
    return found


def take_verification_code(name: str, service: str, sender: str = "", subject: str = "",
                           max_age_seconds: int = 600, relay: str = "",
                           state_dir=None) -> tuple:
    """Return exactly one fresh, strongly-bound mailbox code, transiently.

    Matching is intentionally conservative: the service must occur in a trusted
    header, optional sender/subject constraints must match, the delivery timestamp
    must be fresh, and all matching messages together must yield one unique code.
    The full message and code are never written back to the local store.
    """
    service = str(service or "").strip()
    if not service or len(service) > 100:
        raise RuntimeError("the expected service name is required")
    try:
        max_age = int(max_age_seconds)
    except (TypeError, ValueError):
        raise RuntimeError("max_age_seconds must be an integer")
    max_age = max(30, min(900, max_age))
    address = mailbox_address(name, state_dir=state_dir)
    now = int(time.time())
    messages = fetch(
        name, since=now - max_age, relay=relay, advance_cursor=False,
        state_dir=state_dir)
    candidates = []
    for message in messages:
        if not isinstance(message, dict) or message.get("error"):
            continue
        try:
            received_at = int(message.get("at") or 0)
        except (TypeError, ValueError):
            continue
        if received_at < now - max_age or received_at > now + SKEW:
            continue
        actual_sender = str(message.get("from") or "")
        actual_subject = str(message.get("subject") or "")
        if not _service_matches(service, actual_sender, actual_subject):
            continue
        if not _expected_sender_matches(actual_sender, sender):
            continue
        if subject and str(subject).strip().lower() not in actual_subject.lower():
            continue
        for code in _verification_codes(_decoded_message_text(message)):
            candidates.append((code, received_at))
    unique = {code for code, _ in candidates}
    if not unique:
        raise RuntimeError("no fresh, uniquely matching Collie Mail verification code found")
    if len(unique) != 1:
        raise RuntimeError("multiple fresh matching Collie Mail verification codes found; specify sender or subject")
    code = next(iter(unique))
    received_at = max(at for value, at in candidates if value == code)
    masked = mask_address(address)
    return code, {"source": "collie_mail", "account_masked": masked,
                  "account": masked, "received_at": received_at}


def wait_for(name: str = "", subject: str = "", sender: str = "", timeout: int = 180,
             poll: float = 5.0, relay: str = "", state_dir=None) -> dict:
    """Block until a matching letter arrives, or the time runs out.

    This is the one that changes what an agent can finish on its own: a signup that ends in "check
    your email" stops being a handover to a human.
    """
    deadline = time.time() + max(1, int(timeout))
    subject, sender = (subject or "").lower(), (sender or "").lower()
    while time.time() < deadline:
        for m in fetch(name, relay=relay, state_dir=state_dir):
            if subject and subject not in (m.get("subject") or "").lower():
                continue
            if sender and sender not in (m.get("from") or "").lower():
                continue
            return m
        time.sleep(min(poll, max(0.5, deadline - time.time())))
    return {}
