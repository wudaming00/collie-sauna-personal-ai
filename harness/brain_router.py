"""Collie-first model/backend selection with durable, privacy-safe health feedback.

The brain router chooses *how Collie thinks*; it is not a second product persona.
Only the special ``auto`` provider grants it permission to cross a credential,
billing, or data-policy boundary.  A concrete provider/model remains an exact pin.

Selection is deliberately deterministic and local.  Auth probes establish whether a
route can run, a small curated quality score chooses the strongest suitable route,
and successful prior use is only a tie-breaker.  Prompts are never stored: the durable
ledger contains the decision explanation and a hash of the task shape, plus bounded
health/cooldown counters used for transparent fallback on later turns.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from typing import Any, Callable, Iterable, Mapping


_QUALITY = {
    "gpt-5.6-sol": 100.0,
    "claude-opus-5": 99.0,
    "claude-opus-4-8": 98.0,
    "gpt-5.6-terra": 93.0,
    "claude-sonnet-5": 92.0,
    "gemini-2.5-pro": 90.0,
    "deepseek-reasoner": 87.0,
    "gpt-5.6-luna": 84.0,
    "claude-haiku-4-5-20251001": 83.0,
    "deepseek-chat": 80.0,
    "gemini-2.5-flash": 79.0,
    "gpt-4o-mini": 70.0,
}
_EXPERIMENTAL = frozenset({"anthropic-oauth"})
_RETRY_AFTER = re.compile(
    r"(?i)(?:retry[- ]?after|try again in|available in)\D{0,12}(\d+(?:\.\d+)?)\s*(s|sec|seconds?|m|min|minutes?)?"
)


def transport_for(provider: str) -> str:
    """Return the model transport, which is deliberately not an executor label."""
    return str(provider or "").strip().lower()


def executor_for(provider: str, *, purpose: str = "self", route_kind: str = "chat",
                 allowed_executors: Iterable[str] = ()) -> str:
    """Return an executor only when the caller can really dispatch it.

    A provider is a credential/billing transport.  It does not become Codex or
    Claude Code merely because it speaks to the same account.  At present the
    only external workspace adapter is ``codex-exec`` and Mission is the only
    authority container allowed to advertise it.
    """
    allowed = {str(value or "").strip().lower() for value in allowed_executors}
    provider = transport_for(provider)
    if (provider in ("codex-oauth", "codex-sub", "codex") and
            str(purpose or "").lower() == "mission" and
            str(route_kind or "").lower() == "code" and
            "codex-exec" in allowed):
        return "codex-exec"
    return "collie"


@dataclass(frozen=True)
class RoutingContext:
    """Host-owned facts admitted before model quality is considered."""

    project: str = "global"
    device_id: str = ""
    purpose: str = "self"
    trusted_profile: Mapping[str, Mapping] = field(default_factory=dict)
    paid_overage_disabled: bool = False
    subscription_only: bool = False
    remaining_cost_usd: float | None = None
    remaining_tokens: int | None = None
    remaining_model_calls: int | None = None
    allowed_executors: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "project": self.project,
            "device_id": self.device_id,
            "purpose": self.purpose,
            "paid_overage_disabled": self.paid_overage_disabled,
            "subscription_only": self.subscription_only,
            "remaining_cost_usd": self.remaining_cost_usd,
            "remaining_tokens": self.remaining_tokens,
            "remaining_model_calls": self.remaining_model_calls,
            "allowed_executors": list(self.allowed_executors),
        }


def collie_device_id() -> str:
    """Stable local identity used only to scope already-trusted preferences."""
    try:
        from .remote_identity import load_or_create
        return str(load_or_create().device_id or "")[:128]
    except Exception as exc:
        from .remote_identity import IdentityCorrupt
        if isinstance(exc, IdentityCorrupt):
            # A new/fallback id would detach the account vault and device-scoped
            # memory from their real owner. Surface recovery instead.
            raise
        # Read-only/locked homes still receive a stable process-machine scope;
        # this value is not an authentication identity.
        from .remote_identity import fallback_device_id
        return fallback_device_id()


def _remaining_budget(shared_budget=None, budget: Mapping | None = None):
    values = dict(budget or {})
    if shared_budget is not None:
        snapshot = getattr(shared_budget, "snapshot", None)
        try:
            snap = snapshot() if callable(snapshot) else {}
        except Exception:
            snap = {}
        values.setdefault("spent_cost_usd", snap.get("cost_usd", 0))
        values.setdefault("spent_tokens", snap.get("tokens", 0))
        values.setdefault("max_cost_usd", getattr(shared_budget, "max_cost", 0))
        values.setdefault("max_tokens", getattr(shared_budget, "max_tokens", 0))
        if snap.get("exhausted"):
            values["exhausted"] = True

    def remaining(max_name, spent_name, *, integer=False):
        raw = values.get("remaining_" + ("tokens" if integer else "cost_usd"))
        if raw is not None:
            try:
                return max(0, int(raw)) if integer else max(0.0, float(raw))
            except (TypeError, ValueError):
                return 0 if integer else 0.0
        try:
            cap = int(values.get(max_name) or 0) if integer else float(values.get(max_name) or 0)
            spent = int(values.get(spent_name) or 0) if integer else float(values.get(spent_name) or 0)
        except (TypeError, ValueError):
            return None
        return max(0, cap - spent) if cap > 0 else None

    cost = remaining("max_cost_usd", "spent_cost_usd")
    tokens = remaining("max_tokens", "spent_tokens", integer=True)
    calls = values.get("remaining_model_calls")
    try:
        calls = None if calls is None else max(0, int(calls))
    except (TypeError, ValueError):
        calls = 0
    if values.get("exhausted"):
        tokens = 0
    return cost, tokens, calls


def build_routing_context(*, memory=None, project: str = "global", device_id: str = "",
                          purpose: str = "self", shared_budget=None,
                          budget: Mapping | None = None,
                          paid_overage_disabled: bool = False,
                          subscription_only: bool = False,
                          allowed_executors: Iterable[str] = ()) -> RoutingContext:
    """Build the single routing context used by every non-web surface.

    Memory is read through its typed ``trusted_profile`` seam.  Proposed claims
    never reach the router, and the router validates trust metadata again.
    """
    device_id = str(device_id or collie_device_id())[:128]
    project = str(project or "global")[:256]
    profile = {}
    reader = getattr(memory, "trusted_profile", None)
    if callable(reader):
        try:
            profile = reader(project=project, device_id=device_id) or {}
        except Exception:
            profile = {}
    cost, tokens, calls = _remaining_budget(shared_budget, budget)
    return RoutingContext(
        project=project, device_id=device_id, purpose=str(purpose or "self")[:24],
        trusted_profile=profile if isinstance(profile, Mapping) else {},
        paid_overage_disabled=bool(paid_overage_disabled),
        subscription_only=bool(subscription_only),
        remaining_cost_usd=cost, remaining_tokens=tokens,
        remaining_model_calls=calls,
        allowed_executors=tuple(sorted({str(x).strip().lower()
                                        for x in allowed_executors if str(x).strip()})),
    )


@dataclass(frozen=True)
class BrainCandidate:
    provider: str
    model: str
    transport: str
    executor: str
    kind: str
    score: float
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "transport": self.transport,
            "executor": self.executor,
            "brain_transport": self.transport,
            "worker_executor": self.executor,
            "kind": self.kind,
            "score": round(float(self.score), 3),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class BrainSelection:
    primary: BrainCandidate
    fallbacks: tuple[BrainCandidate, ...] = ()
    reasons: tuple[str, ...] = ()
    claims: tuple[dict, ...] = ()
    routing_context: dict = field(default_factory=dict)


class BrainRouteStore:
    """Small cross-process SQLite ledger for decisions, habits, and route health.

    Every operation opens its own connection, which keeps the object safe when a web
    request hands a decision to the loop's worker thread.  Only the database file is
    chmodded; an explicitly supplied parent directory may be shared (for example /tmp).
    """

    def __init__(self, path: str | None = None):
        self.path = os.path.realpath(os.path.abspath(os.path.expanduser(
            path or os.environ.get("COLLIE_BRAIN_DB") or os.path.join(
                os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie"),
                "brain_routes.db"))))
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _initialize(self) -> None:
        with self._lock, self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS brain_decisions (
                    decision_id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    task_hash TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    transport TEXT NOT NULL DEFAULT '',
                    executor TEXT NOT NULL,
                    explanation_json TEXT NOT NULL,
                    outcome TEXT NOT NULL DEFAULT '',
                    actual_provider TEXT NOT NULL DEFAULT '',
                    actual_model TEXT NOT NULL DEFAULT '',
                    error_class TEXT NOT NULL DEFAULT '',
                    finished_at REAL
                );
                CREATE TABLE IF NOT EXISTS brain_route_health (
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    successes INTEGER NOT NULL DEFAULT 0,
                    failures INTEGER NOT NULL DEFAULT 0,
                    last_used REAL NOT NULL DEFAULT 0,
                    cooldown_until REAL NOT NULL DEFAULT 0,
                    exhausted_until REAL NOT NULL DEFAULT 0,
                    last_error_class TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(provider, model)
                );
                CREATE TABLE IF NOT EXISTS brain_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    decision_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    final INTEGER NOT NULL,
                    error_class TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS brain_outcomes_decision
                    ON brain_outcomes(decision_id, id);
                CREATE TABLE IF NOT EXISTS brain_provider_credentials (
                    provider TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    credential_mtime_ns INTEGER NOT NULL DEFAULT 0,
                    unavailable_until REAL NOT NULL DEFAULT 0,
                    last_error_class TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL DEFAULT 0
                );
                """)
            columns = {row[1] for row in db.execute(
                "PRAGMA table_info(brain_decisions)").fetchall()}
            if "transport" not in columns:
                db.execute("ALTER TABLE brain_decisions ADD COLUMN "
                           "transport TEXT NOT NULL DEFAULT ''")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def health(self, now: float | None = None) -> dict[tuple[str, str], dict]:
        now = float(time.time() if now is None else now)
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT * FROM brain_route_health").fetchall()
        out = {}
        for row in rows:
            item = dict(row)
            item["available"] = not (
                float(item.get("cooldown_until") or 0) > now or
                float(item.get("exhausted_until") or 0) > now)
            out[(row["provider"], row["model"])] = item
        return out

    def sync_credential(self, provider: str, state: Mapping | None = None,
                        *, now: float | None = None) -> bool:
        """Clear a credential terminal fence only after the credential changes."""
        now = float(time.time() if now is None else now)
        provider = str(provider or "")[:80]
        state = dict(state or credential_state(provider))
        fingerprint = str(state.get("fingerprint") or "")[:128]
        try:
            mtime_ns = max(0, int(state.get("mtime_ns") or 0))
        except (TypeError, ValueError):
            mtime_ns = 0
        with self._lock, self._connect() as db:
            old = db.execute(
                "SELECT * FROM brain_provider_credentials WHERE provider=?",
                (provider,)).fetchone()
            if old is None:
                db.execute(
                    "INSERT INTO brain_provider_credentials(provider,fingerprint,"
                    "credential_mtime_ns,updated_at) VALUES(?,?,?,?)",
                    (provider, fingerprint, mtime_ns, now))
                return True
            changed = (str(old["fingerprint"] or "") != fingerprint or
                       int(old["credential_mtime_ns"] or 0) != mtime_ns)
            if changed:
                db.execute(
                    "UPDATE brain_provider_credentials SET fingerprint=?,"
                    "credential_mtime_ns=?,unavailable_until=0,last_error_class='',"
                    "updated_at=? WHERE provider=?",
                    (fingerprint, mtime_ns, now, provider))
                # A changed login/config is new evidence.  Old auth failures must
                # not poison it, while rate/quota windows remain intact.
                db.execute(
                    "UPDATE brain_route_health SET cooldown_until=0,"
                    "last_error_class='' WHERE provider=? AND last_error_class='credential'",
                    (provider,))
                return True
            return float(old["unavailable_until"] or 0) <= now

    def credential_available(self, provider: str, *, now: float | None = None) -> bool:
        now = float(time.time() if now is None else now)
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT unavailable_until FROM brain_provider_credentials WHERE provider=?",
                (str(provider or "")[:80],)).fetchone()
        return row is None or float(row["unavailable_until"] or 0) <= now

    def record_decision(self, payload: Mapping, *, task: str = "",
                        purpose: str = "self", now: float | None = None) -> str:
        now = float(time.time() if now is None else now)
        decision_id = "brain_" + secrets.token_hex(16)
        safe = dict(payload or {})
        # The explanation is useful; the user's prompt is not.  Defensively remove
        # common payload keys if an embedding caller passed more than RunDecision.
        for key in ("task", "prompt", "messages", "history", "text"):
            safe.pop(key, None)
        task_hash = hashlib.sha256(str(task or "").encode(
            "utf-8", "replace")).hexdigest()[:24]
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO brain_decisions
                   (decision_id,created_at,task_hash,purpose,provider,model,transport,
                    executor,explanation_json) VALUES(?,?,?,?,?,?,?,?,?)""",
                (decision_id, now, task_hash, str(purpose or "self")[:24],
                 str(safe.get("provider") or "")[:80],
                 str(safe.get("model") or "")[:160],
                 str(safe.get("transport") or safe.get("provider") or "")[:80],
                 str(safe.get("executor") or "collie")[:40],
                 json.dumps(safe, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"))))
        return decision_id

    def record_outcome(self, decision_id: str, *, provider: str, model: str,
                       success: bool, error_class: str = "", detail: str = "",
                       final: bool = True, now: float | None = None,
                       credential: Mapping | None = None) -> None:
        if not str(decision_id or "").startswith("brain_"):
            return
        now = float(time.time() if now is None else now)
        provider, model = str(provider or "")[:80], str(model or "")[:160]
        error_class = str(error_class or "")[:32]
        cooldown = exhausted = 0.0
        if not success:
            if error_class == "exhausted":
                exhausted = now + 4 * 60 * 60
            elif error_class == "retryable":
                delay = 60.0
                match = _RETRY_AFTER.search(str(detail or ""))
                if match:
                    delay = float(match.group(1))
                    if (match.group(2) or "").lower().startswith("m"):
                        delay *= 60
                cooldown = now + max(15.0, min(delay, 60 * 60))
            elif error_class == "credential":
                # Terminal auth/config failures stay unavailable across turns.
                # sync_credential() releases the fence immediately after a
                # fingerprint/mtime change; otherwise it expires conservatively.
                state = dict(credential or credential_state(provider))
                fingerprint = str(state.get("fingerprint") or "")[:128]
                mtime_ns = max(0, int(state.get("mtime_ns") or 0))
                with self._lock, self._connect() as db:
                    db.execute(
                        """INSERT INTO brain_provider_credentials
                           (provider,fingerprint,credential_mtime_ns,unavailable_until,
                            last_error_class,updated_at) VALUES(?,?,?,?,?,?)
                           ON CONFLICT(provider) DO UPDATE SET
                             fingerprint=excluded.fingerprint,
                             credential_mtime_ns=excluded.credential_mtime_ns,
                             unavailable_until=excluded.unavailable_until,
                             last_error_class=excluded.last_error_class,
                             updated_at=excluded.updated_at""",
                        (provider, fingerprint, mtime_ns, now + 24 * 60 * 60,
                         "credential", now))
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO brain_outcomes
                   (decision_id,created_at,provider,model,success,final,error_class)
                   VALUES(?,?,?,?,?,?,?)""",
                (decision_id, now, provider, model, int(bool(success)),
                 int(bool(final)), error_class))
            db.execute(
                """INSERT INTO brain_route_health
                   (provider,model,successes,failures,last_used,cooldown_until,
                    exhausted_until,last_error_class)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(provider,model) DO UPDATE SET
                     successes=successes+excluded.successes,
                     failures=failures+excluded.failures,
                     last_used=excluded.last_used,
                     cooldown_until=MAX(cooldown_until,excluded.cooldown_until),
                     exhausted_until=MAX(exhausted_until,excluded.exhausted_until),
                     last_error_class=excluded.last_error_class""",
                (provider, model, int(bool(success)), int(not success), now,
                 cooldown, exhausted, error_class))
            if final:
                db.execute(
                    """UPDATE brain_decisions SET outcome=?,actual_provider=?,actual_model=?,
                              error_class=?,finished_at=? WHERE decision_id=?""",
                    ("success" if success else "error", provider, model,
                     error_class, now, decision_id))

    def decision(self, decision_id: str) -> dict | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM brain_decisions WHERE decision_id=?", (decision_id,)).fetchone()
        if not row:
            return None
        out = dict(row)
        try:
            out["explanation"] = json.loads(out.pop("explanation_json"))
        except (ValueError, TypeError):
            out["explanation"] = {}
        return out


_default_store = None
_default_store_path = ""
_default_store_lock = threading.Lock()


def default_store() -> BrainRouteStore | None:
    """Return the process-local store, or None when durable state is read-only."""
    global _default_store, _default_store_path
    wanted = os.path.realpath(os.path.abspath(os.path.expanduser(
        os.environ.get("COLLIE_BRAIN_DB") or os.path.join(
            os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie"),
            "brain_routes.db"))))
    with _default_store_lock:
        if _default_store is not None and _default_store_path == wanted:
            return _default_store
        try:
            _default_store = BrainRouteStore(wanted)
            _default_store_path = wanted
        except (OSError, sqlite3.Error):
            _default_store = None
            _default_store_path = wanted
        return _default_store


_CREDENTIAL_FAILURE = re.compile(
    r"(?i)(?:invalid|missing|expired|revoked|unavailable).{0,28}(?:api[ _-]?key|"
    r"credential|oauth|token|login)|(?:api[ _-]?key|credential|oauth|token|login)"
    r".{0,28}(?:invalid|missing|expired|revoked|unavailable)|authentication_error|"
    r"unauthorized|not[- ]logged[- ]in|forbidden|unsupported provider|invalid base.?url|"
    r"configuration error|direct login-store token is unavailable"
)


def is_credential_failure(detail: str = "", status: int = 0) -> bool:
    """Recognize terminal credential/config failures without conflating quota."""
    if int(status or 0) in (401, 403):
        return True
    text_value = str(detail or "")
    if re.search(r"(?i)(?:insufficient|exceeded|spent|usage).{0,20}(?:quota|credits?)",
                 text_value):
        return False
    return bool(_CREDENTIAL_FAILURE.search(text_value))


def credential_state(provider: str) -> dict:
    """Return a non-secret fingerprint of the provider's effective credential/config."""
    provider = transport_for(provider)
    material = [provider]
    mtime_ns = 0

    def add_file(path):
        nonlocal mtime_ns
        try:
            st = os.stat(path)
            mtime_ns = max(mtime_ns, int(st.st_mtime_ns))
            material.extend([os.path.realpath(path), str(st.st_size), str(st.st_mtime_ns)])
            if st.st_size <= 2_000_000:
                with open(path, "rb") as fh:
                    material.append(hashlib.sha256(fh.read()).hexdigest())
        except OSError:
            material.extend([os.path.realpath(path), "missing"])

    if provider in ("codex-oauth", "codex-sub", "codex"):
        try:
            from .agent_runners import codex_worker_credential_state
            state = codex_worker_credential_state()
            # This is deliberately the same credential/config fingerprint the
            # hardened worker sees. Ambient OPENAI_API_KEY/CODEX_BASE_URL are
            # stripped, so they must neither keep nor clear this cooldown.
            return {"fingerprint": str(state.get("fingerprint") or ""),
                    "mtime_ns": int(state.get("mtime_ns") or 0),
                    "available": bool(state.get("admitted")),
                    "reason": str(state.get("reason") or "")}
        except Exception:
            material.append("codex-worker-credential-unreadable")
    elif provider in ("anthropic-oauth", "claude-sub", "claude-agent-sdk", "claude-cli"):
        path = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or
                            os.path.expanduser("~/.claude"), ".credentials.json")
        # Legacy Claude uses ~/.claude/.credentials.json even when CONFIG_DIR is
        # unset; binding both the store metadata and any env token handles Linux,
        # Windows and Keychain-backed macOS without persisting the token itself.
        add_file(path)
        try:
            from .providers import _read_oauth_token
            material.append(str(_read_oauth_token() or ""))
        except Exception:
            material.append("claude-auth-unreadable")
        material.append(os.environ.get("ANTHROPIC_BASE_URL", ""))
    else:
        try:
            from .catalog import _KEY_ENV
            from .providers import OPENAI_COMPAT_PRESETS
            key_env = _KEY_ENV.get(provider)
            preset = OPENAI_COMPAT_PRESETS.get(provider)
            if not key_env and preset:
                key_env = preset[1]
            if key_env:
                material.append(key_env)
                material.append(os.environ.get(key_env, ""))
            if preset:
                material.append(str(preset[0] or ""))
        except Exception:
            pass
        material.append(os.environ.get("COLLIE_BASE_URL", ""))
    encoded = "\0".join(material).encode("utf-8", "replace")
    return {"fingerprint": hashlib.sha256(encoded).hexdigest(),
            "mtime_ns": mtime_ns}


def _claim_receipt(attribute: str, meta: Mapping | None) -> dict | None:
    if not isinstance(meta, Mapping):
        return None
    # Do not persist free-form evidence/provenance; the claim id points at the
    # full local audit row while this bounded receipt explains the route.
    return {key: meta.get(key) for key in (
        "id", "project", "scope", "device_id", "kind", "status",
        "confidence", "observations", "source") if meta.get(key) not in (None, "")} | {
            "attribute": attribute, "value": meta.get("value")}


def _trusted_value(profile: Mapping | None, *names: str):
    profile = profile if isinstance(profile, Mapping) else {}
    for name in names:
        item = profile.get(name)
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("kind") or "")
        status = str(item.get("status") or "")
        try:
            confidence = float(item.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        try:
            observations = int(item.get("observations") or 0)
        except (TypeError, ValueError):
            observations = 0
        trusted = ((kind == "preference" and status in ("attested", "verified")) or
                   (kind == "habit" and status == "verified" and
                    observations >= 3))
        if trusted and confidence >= .7:
            return item.get("value"), kind, item
    return None, "", None


def _quality(model: str, tags: Iterable[str]) -> float:
    if model in _QUALITY:
        return _QUALITY[model]
    low = str(model or "").lower()
    for needle, value in (("opus", 96), ("sol", 96), ("pro", 88),
                          ("sonnet", 88), ("reason", 84), ("coder", 82),
                          ("flash", 76), ("haiku", 75), ("mini", 68)):
        if needle in low:
            return float(value)
    tags = {str(tag).lower() for tag in (tags or ())}
    return 85.0 if "frontier" in tags else 72.0


def _availability_status(provider: str, row: Mapping,
                         availability: Mapping | Callable | None) -> str:
    if callable(availability):
        value = availability(provider)
    elif isinstance(availability, Mapping) and provider in availability:
        value = availability[provider]
    else:
        value = row.get("auth", "unknown")
    if value is True:
        return "ok"
    if value is False or value is None:
        return "unavailable"
    return str(value).strip().lower()


def choose_brain(*, purpose: str = "self", complexity: str = "standard",
                 quality: str = "balanced", route_kind: str = "chat",
                 pinned_model: str = "", profile: Mapping | None = None,
                 routing_context: RoutingContext | Mapping | None = None,
                 availability: Mapping | Callable | None = None,
                 catalog_entries: Iterable[Mapping] | None = None,
                 credential_states: Mapping[str, Mapping] | None = None,
                 store: BrainRouteStore | None = None,
                 now: float | None = None) -> BrainSelection:
    """Choose one runnable brain and ordered cross-provider fallbacks.

    ``catalog_entries`` and ``availability`` are injectable so tests never depend on
    credentials installed on the developer's machine.
    """
    if isinstance(routing_context, RoutingContext):
        context = routing_context
    elif isinstance(routing_context, Mapping):
        raw_context = dict(routing_context)
        context = RoutingContext(
            project=str(raw_context.get("project") or "global"),
            device_id=str(raw_context.get("device_id") or ""),
            purpose=str(raw_context.get("purpose") or purpose or "self"),
            trusted_profile=(raw_context.get("trusted_profile") or profile or {}),
            paid_overage_disabled=bool(raw_context.get("paid_overage_disabled")),
            subscription_only=bool(raw_context.get("subscription_only")),
            remaining_cost_usd=raw_context.get("remaining_cost_usd"),
            remaining_tokens=raw_context.get("remaining_tokens"),
            remaining_model_calls=raw_context.get("remaining_model_calls"),
            allowed_executors=tuple(raw_context.get("allowed_executors") or ()))
    else:
        context = RoutingContext(purpose=str(purpose or "self"),
                                 trusted_profile=profile or {})
    purpose = str(context.purpose or purpose or "self").lower()
    if purpose not in ("self", "delegate", "mission"):
        purpose = "self"
    complexity = str(complexity or "standard").lower()
    quality = str(quality or "balanced").lower()
    route_kind = str(route_kind or "chat").lower()
    now = float(time.time() if now is None else now)
    if context.remaining_model_calls is not None and context.remaining_model_calls <= 0:
        raise ValueError("Auto budget exhausted: no model calls remain")
    if context.remaining_tokens is not None and context.remaining_tokens <= 0:
        raise ValueError("Auto budget exhausted: no tokens remain")
    if catalog_entries is None:
        from .catalog import list_entries
        catalog_entries = list_entries(discover_live=False)
    store = store if store is not None else default_store()
    catalog_entries = list(catalog_entries or ())
    providers = {str(row.get("provider") or "") for row in catalog_entries
                 if isinstance(row, Mapping) and row.get("provider")}
    if store is not None:
        for credential_provider in providers:
            try:
                injected = ((credential_states or {}).get(credential_provider)
                            if isinstance(credential_states, Mapping) else None)
                store.sync_credential(credential_provider, injected, now=now)
            except Exception:
                # A read-only credential source must not make a catalog test or
                # local route unusable; its catalog auth badge still gates it.
                pass
    health = store.health(now) if store is not None else {}
    provider_health = {}
    for (health_provider, _health_model), state in health.items():
        agg = provider_health.setdefault(
            health_provider, {"successes": 0, "failures": 0, "available": True})
        agg["successes"] += max(0, int(state.get("successes") or 0))
        agg["failures"] += max(0, int(state.get("failures") or 0))
        if not state.get("available", True):
            # Quota and account-side rate limits generally apply to the route,
            # not merely one model id.  Keep the same-provider in-run downgrade,
            # but do not choose that provider again on the next decision until
            # its bounded cooldown expires.
            agg["available"] = False

    profile = context.trusted_profile or profile
    preferred_provider, provider_kind, provider_meta = _trusted_value(
        profile, "routing.provider", "brain.provider", "provider")
    preferred_model, model_kind, model_meta = _trusted_value(
        profile, "routing.model", "brain.model", "model")
    preferred_executor, executor_kind, executor_meta = _trusted_value(
        profile, "routing.executor", "brain.executor", "executor")

    candidates = []
    seen = set()
    for raw in catalog_entries or ():
        row = dict(raw) if isinstance(raw, Mapping) else {}
        provider = str(row.get("provider") or "").strip()
        model = str(row.get("model") or "").strip()
        if not provider or not model or (provider, model) in seen:
            continue
        seen.add((provider, model))
        if pinned_model and model != pinned_model:
            continue
        if _availability_status(provider, row, availability) != "ok":
            continue
        if store is not None and not store.credential_available(provider, now=now):
            continue
        if (provider in _EXPERIMENTAL and not (
                provider_kind == "preference" and
                str(preferred_provider or "") == provider)):
            # Merely finding an experimental credential on disk is not consent
            # to make an unverified billing/data route the default.  A concrete
            # provider pin or attested Auto preference can still opt in.
            continue
        state = health.get((provider, model), {})
        aggregate = provider_health.get(provider, {})
        if (state and not state.get("available", True)) or \
                (aggregate and not aggregate.get("available", True)):
            continue
        billing_kind = str(row.get("kind") or "metered").lower()
        if billing_kind == "metered" and (context.subscription_only or
                                   context.paid_overage_disabled or
                                   context.remaining_cost_usd == 0):
            continue
        tags = {str(tag).lower() for tag in (row.get("tags") or ())}
        transport = transport_for(provider)
        executor = executor_for(
            provider, purpose=purpose, route_kind=route_kind,
            allowed_executors=context.allowed_executors)
        score = _quality(model, tags)
        reasons = ["%s route admitted before quality" % billing_kind,
                   "quality %.0f" % score]
        if quality == "quick":
            fast = "fast" in tags or any(x in model.lower() for x in ("luna", "haiku", "flash"))
            score += 12.0 if fast else -4.0
            reasons.append("quick-task latency fit")
        elif complexity == "hard":
            if "frontier" in tags or any(x in model.lower() for x in ("sol", "opus")):
                score += 5.0
                reasons.append("frontier fit for hard task")
        if route_kind == "code" or purpose == "delegate":
            if "coding" in tags:
                score += 3.0
                reasons.append("coding backend fit")
        if purpose == "mission" and executor == "codex-exec":
            score += 2.0
            reasons.append("Mission-authorized codex-exec adapter available")

        # Trusted explicit preferences are decisive within Auto.  Verified habits
        # are intentionally small tie-breakers and can never revive an unavailable route.
        for preferred, preference_kind, actual, label, meta in (
                (preferred_provider, provider_kind, provider, "provider", provider_meta),
                (preferred_model, model_kind, model, "model", model_meta),
                (preferred_executor, executor_kind, executor, "executor", executor_meta)):
            if preferred is None or str(preferred) != actual:
                continue
            bonus = 30.0 if preference_kind == "preference" else 1.5
            score += bonus
            reasons.append("trusted %s %s" % (preference_kind, label))
        successes = max(0, int(aggregate.get("successes") or state.get("successes") or 0))
        failures = max(0, int(aggregate.get("failures") or state.get("failures") or 0))
        if successes:
            score += min(1.25, math.log1p(successes) * .25)
            reasons.append("successful-use tie-breaker")
        if failures:
            score -= min(2.0, math.log1p(failures) * .35)
        candidates.append(BrainCandidate(
            provider, model, transport, executor, billing_kind, score, tuple(reasons)))

    if not candidates:
        suffix = " matching model %s" % pinned_model if pinned_model else ""
        raise ValueError(
            "Auto found no currently authenticated model%s; connect a provider or choose one explicitly"
            % suffix)
    # An attested provider/model preference is explicit user authority within
    # Auto. Without a user-owned cost/subscription constraint, "Auto" means best
    # quality rather than cheapest billing class: a tiny local model must not beat
    # an authenticated frontier model merely because it is local. Cost ordering
    # becomes hard only when the caller supplied a cost/no-paid/subscription fence.
    billing_constrained = bool(
        context.subscription_only or context.paid_overage_disabled or
        context.remaining_cost_usd is not None)

    def ordering(item):
        preferred = bool(
            provider_kind == "preference" and str(preferred_provider) == item.provider or
            model_kind == "preference" and str(preferred_model) == item.model or
            executor_kind == "preference" and str(preferred_executor) == item.executor)
        billing = {"subscription": 0, "local": 1, "metered": 2}.get(item.kind, 3)
        if billing_constrained:
            return (0 if preferred else 1, billing, -item.score,
                    item.provider, item.model)
        return (0 if preferred else 1, -item.score, billing,
                item.provider, item.model)
    candidates.sort(key=ordering)
    primary = candidates[0]
    fallbacks = []
    providers = {primary.provider}
    if not pinned_model:
        for item in candidates[1:]:
            # Same-provider capacity downgrade remains catalog.fallback_model's job.
            # Brain fallbacks cross only after that bounded local attempt is spent.
            if item.provider in providers:
                continue
            providers.add(item.provider)
            fallbacks.append(item)
            if len(fallbacks) >= 4:
                break
    policy = ("subscription-only" if context.subscription_only else
              "no-paid-overage" if context.paid_overage_disabled else
              "cost ceiling" if context.remaining_cost_usd is not None else
              "quality-first (no cost constraint)")
    first_reason = (
        "budget admission before quality: %s; remaining cost=%s tokens=%s calls=%s" % (
            policy, context.remaining_cost_usd,
            "unbounded" if context.remaining_tokens is None else context.remaining_tokens,
            "unbounded" if context.remaining_model_calls is None else
            context.remaining_model_calls)
        if billing_constrained else
        "quality ranking precedes billing kind: no cost/subscription constraint was supplied")
    reasons = (
        first_reason,
        "Collie selected the highest-scoring currently usable route",
        ("availability, credential fingerprints, quota cooldowns, and the explicit "
         "billing fence were checked before quality" if billing_constrained else
         "availability, credential fingerprints, and quota cooldowns were checked "
         "before quality; billing kind was not used as an implicit preference"),
        "fallbacks are ordered and used only for automatic capacity or credential failures",
    )
    claims = []
    for attribute, value, actual, meta in (
            ("routing.provider", preferred_provider, primary.provider, provider_meta),
            ("routing.model", preferred_model, primary.model, model_meta),
            ("routing.executor", preferred_executor, primary.executor, executor_meta)):
        if value is not None and str(value) == actual:
            receipt = _claim_receipt(attribute, meta)
            if receipt:
                claims.append(receipt)
    return BrainSelection(primary, tuple(fallbacks), reasons, tuple(claims),
                          context.to_dict())


__all__ = [
    "BrainCandidate", "BrainRouteStore", "BrainSelection", "RoutingContext",
    "build_routing_context", "choose_brain", "collie_device_id",
    "credential_state", "default_store", "executor_for",
    "is_credential_failure", "transport_for",
]
