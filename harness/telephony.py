"""Fail-closed, provider-neutral contracts for Collie calls and messages.

This module never contacts a carrier. It validates a trusted transport, builds a
disclosure-bearing provider payload, and durably records only bounded metadata
and keyed hashes. Phone numbers, content, disclosure wording, and raw provider
references are never written to SQLite.

``IntentLedger()`` without a path is deliberately registration-only. Automatic
dispatch needs a durable path and a source-trusted ``CapabilityRegistry`` so a
restart cannot turn an uncertain submission into a duplicate.
"""
from __future__ import annotations

import enum
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterable


_E164 = re.compile(r"^\+[1-9][0-9]{7,14}$")
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{1,95}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")

_TRANSPORT_MODES = {
    "call": frozenset({"manual_user_call", "provider_voice_api"}),
    "message": frozenset({"draft_then_user_send", "provider_sms_api"}),
}
_AUTOMATIC_MODES = frozenset({"provider_voice_api", "provider_sms_api"})
_MANUAL_MODES = frozenset({"manual_user_call", "draft_then_user_send"})
_PROVIDER_EVENT_TYPES = frozenset({
    "call.submitted", "call.ai_disclosure_played", "call.completed", "call.failed",
    "message.accepted", "message.delivered", "message.failed", "submission.absent",
})
_RECOVERY_STATUSES = frozenset({
    "dialing", "disclosure_pending", "in_progress", "sending", "sent",
})
_EVIDENCE_TOKEN = object()


class TelephonyError(RuntimeError):
    pass


class IdempotencyConflict(TelephonyError):
    pass


class InvalidTransition(TelephonyError):
    pass


class CapabilityUnavailable(TelephonyError):
    pass


class CostCapExceeded(TelephonyError):
    pass


class SegmentCapExceeded(TelephonyError):
    pass


class DurableLedgerRequired(TelephonyError):
    pass


class ProviderEvidenceRequired(TelephonyError):
    pass


class ReconciliationRequired(TelephonyError):
    pass


class IntentKind(str, enum.Enum):
    CALL = "call"
    MESSAGE = "message"


class Purpose(str, enum.Enum):
    USER_DIRECTED = "user_directed"
    TRANSACTIONAL = "transactional"
    SCHEDULING = "scheduling"
    CUSTOMER_SERVICE = "customer_service"
    ACCOUNT_VERIFICATION = "account_verification"
    DOCUMENTED_OPT_IN = "documented_opt_in"


class ConsentBasis(str, enum.Enum):
    USER_DIRECTED = "user_directed"
    EXISTING_RELATIONSHIP = "existing_relationship"
    TRANSACTIONAL_REQUEST = "transactional_request"
    DOCUMENTED_OPT_IN = "documented_opt_in"


class AIDisclosurePolicy(str, enum.Enum):
    REQUIRED_AT_START = "required_at_start"


class RecordingPolicy(str, enum.Enum):
    DISABLED = "disabled"
    EXPLICIT_CONSENT_EACH_CALL = "explicit_consent_each_call"


CALL_STATUSES = (
    "planned", "authorized", "dialing", "disclosure_pending", "in_progress",
    "completed", "failed", "cancelled", "uncertain",
)
MESSAGE_STATUSES = (
    "planned", "authorized", "sending", "sent", "delivered", "failed",
    "cancelled", "uncertain",
)
_CALL_TRANSITIONS = {
    "planned": {"authorized", "cancelled"},
    "authorized": {"dialing", "cancelled", "failed"},
    "dialing": {"disclosure_pending", "failed", "cancelled", "uncertain"},
    "disclosure_pending": {"in_progress", "failed", "cancelled", "uncertain"},
    "in_progress": {"completed", "failed", "uncertain"},
    "completed": set(), "failed": set(), "cancelled": set(), "uncertain": set(),
}
_MESSAGE_TRANSITIONS = {
    "planned": {"authorized", "cancelled"},
    "authorized": {"sending", "cancelled", "failed"},
    "sending": {"sent", "failed", "cancelled", "uncertain"},
    "sent": {"delivered", "failed", "uncertain"},
    "delivered": set(), "failed": set(), "cancelled": set(), "uncertain": set(),
}


def _bounded(label: str, value: str, *, maximum: int = 512) -> str:
    value = str(value or "").strip()
    if not value or len(value) > maximum or "\x00" in value:
        raise ValueError("%s must be a non-empty bounded string" % label)
    return value


def normalize_e164(number: str) -> str:
    number = str(number or "").strip().replace(" ", "").replace("-", "")
    if not _E164.fullmatch(number):
        raise ValueError("phone number must be E.164")
    return number


def mask_number(number: str) -> str:
    number = normalize_e164(number)
    return "••••••%s" % number[-4:]


@dataclass(frozen=True, slots=True)
class MoneyCap:
    currency: str
    minor_units: int

    def __post_init__(self):
        currency = str(self.currency or "").upper()
        if not _CURRENCY.fullmatch(currency):
            raise ValueError("currency must be a three-letter ISO code")
        amount = int(self.minor_units)
        if amount < 0 or amount > 100_000:
            raise ValueError("cost cap must be between 0 and 100000 minor units")
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "minor_units", amount)

    def projection(self) -> dict:
        return {"currency": self.currency, "minor_units": self.minor_units}


@dataclass(frozen=True, slots=True)
class Recipient:
    """One explicit recipient; lists, ranges, and generated targets are rejected."""

    number: str = field(repr=False)
    consent_basis: ConsentBasis
    label: str = field(default="", repr=False)
    jurisdiction: str = ""
    do_not_contact: bool = False

    def __post_init__(self):
        object.__setattr__(self, "number", normalize_e164(self.number))
        try:
            basis = (self.consent_basis if isinstance(self.consent_basis, ConsentBasis)
                     else ConsentBasis(str(self.consent_basis)))
        except ValueError as exc:
            raise ValueError("unsupported recipient consent basis") from exc
        object.__setattr__(self, "consent_basis", basis)
        label = str(self.label or "").strip()
        if len(label) > 160 or "\x00" in label:
            raise ValueError("recipient label is too long")
        object.__setattr__(self, "label", label)
        jurisdiction = str(self.jurisdiction or "").strip().upper()
        if jurisdiction and not re.fullmatch(r"[A-Z]{2}(?:-[A-Z0-9]{1,3})?", jurisdiction):
            raise ValueError("jurisdiction must be an ISO country or country-region code")
        object.__setattr__(self, "jurisdiction", jurisdiction)
        if self.do_not_contact:
            raise ValueError("a do-not-contact recipient cannot be targeted")

    def receipt_projection(self) -> dict:
        # Even a last-four hint is omitted from durable receipts.
        result = {"consent_basis": self.consent_basis.value}
        if self.jurisdiction:
            result["jurisdiction"] = self.jurisdiction
        return result


@dataclass(frozen=True, slots=True)
class TelephonyCapability:
    capability_id: str
    provider: str
    adapter_kind: str
    sender_number: str = field(repr=False)
    ownership: str
    transports: tuple[tuple[str, str], ...]
    connected: bool = True
    outbound_only: bool = False
    verified_caller_id: bool = False
    registered_sender: bool = False

    def __post_init__(self):
        capability_id = str(self.capability_id or "").lower()
        provider = str(self.provider or "").lower()
        if not _SAFE_ID.fullmatch(capability_id) or not _SAFE_ID.fullmatch(provider):
            raise ValueError("capability and provider ids must be safe identifiers")
        if self.adapter_kind not in {"assigned_line", "programmable_outbound", "programmable_line"}:
            raise ValueError("unsupported telephony adapter kind")
        if self.ownership not in {
            "user_owned_assigned_to_collie", "user_owned_verified_caller_id",
            "organization_owned_assigned_to_collie", "provider_owned_assigned_to_collie",
        }:
            raise ValueError("unsupported telephony ownership class")
        number = normalize_e164(self.sender_number)
        modes = tuple((str(kind), str(mode)) for kind, mode in self.transports)
        if not modes or len({kind for kind, _mode in modes}) != len(modes):
            raise ValueError("transports must contain unique call/message entries")
        for kind, mode in modes:
            if kind not in _TRANSPORT_MODES or mode not in _TRANSPORT_MODES[kind]:
                raise ValueError("untrusted telephony transport mode")
        if self.adapter_kind == "programmable_outbound":
            if (not self.outbound_only or not self.verified_caller_id or self.registered_sender
                    or modes != (("call", "provider_voice_api"),)):
                raise ValueError("a verified external caller id is outbound-call-only")
        if self.adapter_kind == "programmable_line":
            if self.verified_caller_id or not self.registered_sender:
                raise ValueError("a programmable line must be a registered provider sender")
        if self.adapter_kind == "assigned_line" and self.registered_sender:
            raise ValueError("an assigned manual line is not a programmable sender")
        object.__setattr__(self, "capability_id", capability_id)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "sender_number", number)
        object.__setattr__(self, "transports", modes)

    def mode_for(self, kind: IntentKind | str) -> str:
        value = kind.value if isinstance(kind, IntentKind) else str(kind)
        return dict(self.transports).get(value, "")

    def projection(self) -> dict:
        return {
            "capability_id": self.capability_id, "provider": self.provider,
            "adapter_kind": self.adapter_kind, "line_hint": mask_number(self.sender_number),
            "ownership": self.ownership, "connected": bool(self.connected),
            "outbound_only": bool(self.outbound_only),
            "verified_caller_id": bool(self.verified_caller_id),
            "registered_sender": bool(self.registered_sender),
            "transports": {kind: mode for kind, mode in self.transports},
        }

    def validate_dispatch(self, intent: "CallIntent | MessageIntent") -> str:
        if not self.connected:
            raise CapabilityUnavailable("telephony capability is not connected")
        if intent.capability_id != self.capability_id:
            raise CapabilityUnavailable("intent is bound to another telephony capability")
        mode = self.mode_for(intent.kind)
        if not mode:
            raise CapabilityUnavailable("capability does not support this intent kind")
        if mode in _MANUAL_MODES:
            raise CapabilityUnavailable(
                "assigned line requires a draft/manual user handoff, not automatic dispatch")
        if mode not in _AUTOMATIC_MODES:
            raise CapabilityUnavailable("transport mode is not trusted for automatic dispatch")
        return mode


def google_voice_assigned_line(number: str, *, connected: bool = True) -> TelephonyCapability:
    """Google Voice identity/manual handoff contract; never an automatic adapter."""
    return TelephonyCapability(
        capability_id="google_voice.assigned_line", provider="google_voice",
        adapter_kind="assigned_line", sender_number=number,
        ownership="user_owned_assigned_to_collie",
        transports=(("message", "draft_then_user_send"), ("call", "manual_user_call")),
        connected=connected, outbound_only=False, verified_caller_id=False,
        registered_sender=False)


def programmable_outbound_adapter(*, adapter_id: str, provider: str,
                                  verified_caller_id: str,
                                  channels=("call",),
                                  connected: bool = True) -> TelephonyCapability:
    """Call-only adapter that presents an externally verified caller ID."""
    if tuple(channels) != ("call",):
        raise ValueError("a verified external caller id can be used for calls, not SMS")
    return TelephonyCapability(
        capability_id=str(adapter_id).lower(), provider=str(provider).lower(),
        adapter_kind="programmable_outbound", sender_number=verified_caller_id,
        ownership="user_owned_verified_caller_id",
        transports=(("call", "provider_voice_api"),), connected=connected,
        outbound_only=True, verified_caller_id=True, registered_sender=False)


def programmable_registered_line(*, adapter_id: str, provider: str,
                                 sender_number: str,
                                 channels=("call", "message"),
                                 connected: bool = True,
                                 outbound_only: bool = False,
                                 organization_owned: bool = False) -> TelephonyCapability:
    """Provider-provisioned/registered sender that may support calls and SMS."""
    values = tuple(str(channel) for channel in channels)
    if not values or len(values) != len(set(values)) or any(
            channel not in {"call", "message"} for channel in values):
        raise ValueError("channels must be unique call/message values")
    modes = tuple((channel, "provider_voice_api" if channel == "call" else "provider_sms_api")
                  for channel in values)
    return TelephonyCapability(
        capability_id=str(adapter_id).lower(), provider=str(provider).lower(),
        adapter_kind="programmable_line", sender_number=sender_number,
        ownership=("organization_owned_assigned_to_collie" if organization_owned
                   else "provider_owned_assigned_to_collie"),
        transports=modes, connected=connected, outbound_only=bool(outbound_only),
        verified_caller_id=False, registered_sender=True)


@dataclass(frozen=True, slots=True)
class CapabilityHealthEvidence:
    capability_id: str
    source: str
    status: str
    observed_at: int
    expires_at: int
    evidence_hash: str = ""

    def projection(self) -> dict:
        return {
            "capability_id": self.capability_id, "source": self.source,
            "status": self.status, "observed_at": self.observed_at,
            "expires_at": self.expires_at, "evidence_hash": self.evidence_hash,
        }


@dataclass(frozen=True, slots=True)
class ProviderEventEvidence:
    capability_id: str
    provider: str
    event_type: str
    occurred_at: int
    source: str
    event_reference: str = field(repr=False)
    payload_sha256: str = ""
    _token: object = field(default=None, repr=False, compare=False)


class CapabilityRegistry:
    """In-process seam fed only by configured, signature-validating sources."""

    def __init__(self, *, trusted_sources: Iterable[str] = (
            "local_vault_config", "provider_api_probe", "provider_signed_webhook"),
            provider_event_sources: Iterable[str] = (
                "provider_api_probe", "provider_signed_webhook"),
            clock: Callable[[], float] = time.time):
        sources = frozenset(str(source).lower() for source in trusted_sources)
        if not sources or any(not _SAFE_ID.fullmatch(source) for source in sources):
            raise ValueError("trusted_sources must contain safe source identifiers")
        event_sources = frozenset(str(source).lower() for source in provider_event_sources)
        if (not event_sources or not event_sources.issubset(sources)
                or any(not _SAFE_ID.fullmatch(source) for source in event_sources)):
            raise ValueError("provider_event_sources must be trusted source identifiers")
        self._trusted_sources = sources
        self._provider_event_sources = event_sources
        self._clock = clock
        self._records: dict[str, tuple[TelephonyCapability, CapabilityHealthEvidence]] = {}

    def register(self, capability: TelephonyCapability, *, source: str,
                 status: str = "healthy", observed_at: int | None = None,
                 ttl_seconds: int = 300, evidence_reference: str = "") -> dict:
        if not isinstance(capability, TelephonyCapability):
            raise TypeError("capability must be a TelephonyCapability")
        source = str(source).lower()
        if source not in self._trusted_sources:
            raise CapabilityUnavailable("capability source is not trusted")
        status = str(status).lower()
        if status not in {"healthy", "degraded", "unavailable"}:
            raise ValueError("unsupported capability health status")
        ttl = int(ttl_seconds)
        if ttl < 1 or ttl > 3600:
            raise ValueError("capability health TTL must be between 1 and 3600 seconds")
        observed = int(self._clock() if observed_at is None else observed_at)
        now = int(self._clock())
        if observed > now + 30:
            raise ValueError("capability evidence cannot come from the future")
        evidence_hash = ""
        if evidence_reference:
            reference = _bounded("health evidence reference", evidence_reference, maximum=512)
            evidence_hash = hashlib.sha256(reference.encode("utf-8")).hexdigest()
        evidence = CapabilityHealthEvidence(
            capability_id=capability.capability_id, source=source, status=status,
            observed_at=observed, expires_at=observed + ttl, evidence_hash=evidence_hash)
        self._records[capability.capability_id] = (capability, evidence)
        return {"capability": capability.projection(), "health": evidence.projection()}

    def resolve(self, capability_id: str) -> TelephonyCapability:
        try:
            capability, evidence = self._records[str(capability_id).lower()]
        except KeyError as exc:
            raise CapabilityUnavailable("capability is absent from the trusted registry") from exc
        now = int(self._clock())
        if evidence.status != "healthy":
            raise CapabilityUnavailable("capability health is not healthy")
        if evidence.expires_at < now:
            raise CapabilityUnavailable("capability health evidence has expired")
        return capability

    def validate(self, capability: TelephonyCapability,
                 intent: "CallIntent | MessageIntent") -> str:
        registered = self.resolve(capability.capability_id)
        if registered != capability:
            raise CapabilityUnavailable("capability does not match trusted registry evidence")
        return registered.validate_dispatch(intent)

    def provider_event(self, capability_id: str, *, event_type: str,
                       event_reference: str, source: str,
                       occurred_at: int | None = None,
                       payload_sha256: str = "") -> ProviderEventEvidence:
        capability = self.resolve(capability_id)
        source = str(source).lower()
        if source not in self._provider_event_sources:
            raise ProviderEvidenceRequired("provider event source is not trusted")
        event_type = str(event_type).lower()
        if event_type not in _PROVIDER_EVENT_TYPES:
            raise ValueError("unsupported provider event type")
        reference = _bounded("provider event reference", event_reference, maximum=512)
        occurred = int(self._clock() if occurred_at is None else occurred_at)
        if occurred > int(self._clock()) + 30:
            raise ValueError("provider evidence cannot come from the future")
        digest = str(payload_sha256 or "").lower()
        if digest and not _SHA256.fullmatch(digest):
            raise ValueError("payload_sha256 must be a lowercase SHA-256 digest")
        return ProviderEventEvidence(
            capability_id=capability.capability_id, provider=capability.provider,
            event_type=event_type, occurred_at=occurred, source=source,
            event_reference=reference, payload_sha256=digest, _token=_EVIDENCE_TOKEN)


def _enum_value(enum_type, value, label):
    try:
        return value if isinstance(value, enum_type) else enum_type(str(value))
    except ValueError as exc:
        raise ValueError("unsupported %s" % label) from exc


# GSM 03.38 default and extension tables. Extension characters consume two septets.
_GSM7_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ "
    "!\"#¤%&'()*+,-./0123456789:;<=>?¡"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM7_EXTENSION = frozenset("\f^{}\\[~]|€")


@dataclass(frozen=True, slots=True)
class SmsSegmentInfo:
    encoding: str
    units: int
    segments: int
    single_segment_limit: int
    multipart_segment_limit: int

    def projection(self) -> dict:
        return {
            "encoding": self.encoding, "units": self.units, "segments": self.segments,
            "single_segment_limit": self.single_segment_limit,
            "multipart_segment_limit": self.multipart_segment_limit,
        }


def sms_segment_info(text: str) -> SmsSegmentInfo:
    """Return carrier segment counts for GSM-7 or UTF-16/UCS-2-style SMS."""
    text = str(text)
    gsm_units = 0
    for char in text:
        if char in _GSM7_BASIC:
            gsm_units += 1
        elif char in _GSM7_EXTENSION:
            gsm_units += 2
        else:
            break
    else:
        segments = 1 if gsm_units <= 160 else max(1, math.ceil(gsm_units / 153))
        return SmsSegmentInfo("gsm-7", gsm_units, segments, 160, 153)
    # Providers call this UCS-2; astral characters occupy a UTF-16 surrogate pair.
    units = len(text.encode("utf-16-be")) // 2
    segments = 1 if units <= 70 else max(1, math.ceil(units / 67))
    return SmsSegmentInfo("ucs-2", units, segments, 70, 67)


@dataclass(frozen=True, slots=True)
class MessageProviderPayload:
    """Sensitive, short-lived payload for the provider adapter only."""

    to: str = field(repr=False)
    text: str = field(repr=False)
    disclosure_text: str = field(repr=False)
    disclosure_position: str = "prefix"

    def __post_init__(self):
        number = normalize_e164(self.to)
        disclosure = _bounded("AI disclosure text", self.disclosure_text, maximum=500)
        text = _bounded("SMS provider payload", self.text, maximum=4501)
        if self.disclosure_position != "prefix":
            raise ValueError("SMS disclosure position must be prefix")
        if not text.startswith(disclosure + "\n"):
            raise ValueError("SMS payload must begin with the AI disclosure")
        object.__setattr__(self, "to", number)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "disclosure_text", disclosure)

    def provider_request(self) -> dict:
        return {
            "to": self.to, "text": self.text,
            "ai_disclosure": {"required": True, "position": self.disclosure_position,
                              "text": self.disclosure_text},
        }

    def sha256(self) -> str:
        canonical = json.dumps(self.provider_request(), ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class MessageIntent:
    collie_id: str
    idempotency_key: str
    capability_id: str
    recipient: Recipient = field(repr=False)
    body: str = field(repr=False, compare=False)
    disclosure_text: str = field(repr=False, compare=False)
    purpose: Purpose = Purpose.USER_DIRECTED
    disclosure_policy: AIDisclosurePolicy = AIDisclosurePolicy.REQUIRED_AT_START
    cost_cap: MoneyCap = field(default_factory=lambda: MoneyCap("USD", 100))
    max_segments: int = 4
    intent_id: str = field(default_factory=lambda: "msg_" + secrets.token_urlsafe(18), init=False)
    kind: IntentKind = field(default=IntentKind.MESSAGE, init=False)

    def __post_init__(self):
        _validate_common(self)
        body = _bounded("message body", self.body, maximum=4000)
        disclosure = _bounded("AI disclosure text", self.disclosure_text, maximum=500)
        if int(self.max_segments) < 1 or int(self.max_segments) > 10:
            raise ValueError("max_segments must be between 1 and 10")
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "disclosure_text", disclosure)
        object.__setattr__(self, "max_segments", int(self.max_segments))

    def provider_payload(self) -> MessageProviderPayload:
        return MessageProviderPayload(
            to=self.recipient.number, text=self.disclosure_text + "\n" + self.body,
            disclosure_text=self.disclosure_text)

    def segment_info(self) -> SmsSegmentInfo:
        return sms_segment_info(self.provider_payload().text)

    def receipt_projection(self) -> dict:
        info = self.segment_info()
        return _intent_projection(self, extra={
            "max_segments": self.max_segments, "sms_encoding": info.encoding,
            "estimated_segments": info.segments,
        })

    def _sensitive_material(self) -> bytes:
        return (self.recipient.number + "\0" + self.body + "\0" +
                self.disclosure_text).encode("utf-8")


@dataclass(frozen=True, slots=True)
class CallIntent:
    collie_id: str
    idempotency_key: str
    capability_id: str
    recipient: Recipient = field(repr=False)
    brief: str = field(repr=False, compare=False)
    disclosure_text: str = field(repr=False, compare=False)
    purpose: Purpose = Purpose.USER_DIRECTED
    disclosure_policy: AIDisclosurePolicy = AIDisclosurePolicy.REQUIRED_AT_START
    recording_policy: RecordingPolicy = RecordingPolicy.DISABLED
    recording_requested: bool = False
    cost_cap: MoneyCap = field(default_factory=lambda: MoneyCap("USD", 500))
    max_duration_seconds: int = 900
    intent_id: str = field(default_factory=lambda: "call_" + secrets.token_urlsafe(18), init=False)
    kind: IntentKind = field(default=IntentKind.CALL, init=False)

    def __post_init__(self):
        _validate_common(self)
        brief = _bounded("call brief", self.brief, maximum=8000)
        disclosure = _bounded("AI disclosure text", self.disclosure_text, maximum=500)
        recording = _enum_value(RecordingPolicy, self.recording_policy, "recording policy")
        duration = int(self.max_duration_seconds)
        if duration < 15 or duration > 3600:
            raise ValueError("max_duration_seconds must be between 15 and 3600")
        if bool(self.recording_requested) != (recording == RecordingPolicy.EXPLICIT_CONSENT_EACH_CALL):
            raise ValueError("recording requires explicit consent on each call; otherwise disable it")
        object.__setattr__(self, "brief", brief)
        object.__setattr__(self, "disclosure_text", disclosure)
        object.__setattr__(self, "recording_policy", recording)
        object.__setattr__(self, "max_duration_seconds", duration)

    def receipt_projection(self) -> dict:
        return _intent_projection(self, extra={
            "max_duration_seconds": self.max_duration_seconds,
            "recording_policy": self.recording_policy.value,
            "recording_requested": bool(self.recording_requested),
        })

    def _sensitive_material(self) -> bytes:
        return (self.recipient.number + "\0" + self.brief + "\0" +
                self.disclosure_text).encode("utf-8")


def _validate_common(intent) -> None:
    collie_id = _bounded("collie_id", intent.collie_id, maximum=256)
    key = str(intent.idempotency_key or "")
    if not _IDEMPOTENCY.fullmatch(key):
        raise ValueError("idempotency_key must be 8-128 safe characters")
    capability = str(intent.capability_id or "").lower()
    if not _SAFE_ID.fullmatch(capability):
        raise ValueError("capability_id must be a safe identifier")
    if not isinstance(intent.recipient, Recipient):
        raise TypeError("recipient must be a Recipient")
    purpose = _enum_value(Purpose, intent.purpose, "purpose")
    disclosure = _enum_value(AIDisclosurePolicy, intent.disclosure_policy,
                             "AI disclosure policy")
    if disclosure != AIDisclosurePolicy.REQUIRED_AT_START:
        raise ValueError("AI disclosure is required at the start of contact")
    if not isinstance(intent.cost_cap, MoneyCap):
        raise TypeError("cost_cap must be MoneyCap")
    object.__setattr__(intent, "collie_id", collie_id)
    object.__setattr__(intent, "capability_id", capability)
    object.__setattr__(intent, "purpose", purpose)
    object.__setattr__(intent, "disclosure_policy", disclosure)


def _intent_projection(intent, *, extra: dict) -> dict:
    result = {
        "intent_id": intent.intent_id, "kind": intent.kind.value,
        "collie_id": intent.collie_id, "capability_id": intent.capability_id,
        "recipient": intent.recipient.receipt_projection(),
        "purpose": intent.purpose.value, "ai_disclosure": intent.disclosure_policy.value,
        "cost_cap": intent.cost_cap.projection(),
    }
    result.update(extra)
    return result


class IntentLedger:
    """SQLite idempotency/state ledger with explicit crash reconciliation."""

    def __init__(self, path: str | os.PathLike | None = None, *,
                 fingerprint_key: bytes | None = None,
                 capability_registry: CapabilityRegistry | None = None,
                 clock: Callable[[], float] = time.time):
        self.path = None if path is None else str(Path(path).expanduser().resolve())
        self.durable = self.path is not None
        self.capability_registry = capability_registry
        self._clock = clock
        self._lock = threading.RLock()
        self._fingerprint_key = self._load_fingerprint_key(fingerprint_key)
        self.db = sqlite3.connect(self.path if self.durable else ":memory:", timeout=30,
                                  check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=30000")
        if self.durable:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
        self._init_schema()
        if self.durable:
            self._recover_interrupted_submissions()

    def _load_fingerprint_key(self, supplied: bytes | None) -> bytes:
        if supplied is not None:
            value = bytes(supplied)
            if len(value) < 32:
                raise ValueError("fingerprint_key must contain at least 32 bytes")
            return value
        if not self.durable:
            return secrets.token_bytes(32)
        db_path = Path(self.path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        key_path = Path(str(db_path) + ".hmac")
        if key_path.exists():
            value = key_path.read_bytes()
            if len(value) != 32:
                raise DurableLedgerRequired("telephony HMAC sidecar is invalid")
            if os.name != "nt" and key_path.stat().st_mode & 0o077:
                raise DurableLedgerRequired("telephony HMAC sidecar permissions are too broad")
            return value
        if db_path.exists() and db_path.stat().st_size:
            raise DurableLedgerRequired("existing telephony ledger has no stable HMAC key")
        value = secrets.token_bytes(32)
        try:
            descriptor = os.open(str(key_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            value = key_path.read_bytes()
            if len(value) != 32:
                raise DurableLedgerRequired("telephony HMAC sidecar is invalid")
        return value

    def _init_schema(self) -> None:
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS telephony_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS telephony_intents (
                intent_id TEXT PRIMARY KEY, receipt_id TEXT NOT NULL UNIQUE,
                collie_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                fingerprint BLOB NOT NULL, kind TEXT NOT NULL, capability_id TEXT NOT NULL,
                recipient_consent_basis TEXT NOT NULL,
                recipient_jurisdiction TEXT NOT NULL DEFAULT '', purpose TEXT NOT NULL,
                disclosure_policy TEXT NOT NULL, cost_currency TEXT NOT NULL,
                cost_cap_minor INTEGER NOT NULL, max_segments INTEGER, sms_encoding TEXT,
                estimated_segments INTEGER, message_payload_hash TEXT NOT NULL DEFAULT '',
                max_duration_seconds INTEGER,
                recording_policy TEXT, recording_requested INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL, actual_cost_minor INTEGER NOT NULL DEFAULT 0,
                provider_reference_hash TEXT NOT NULL DEFAULT '',
                disclosure_evidence_hash TEXT NOT NULL DEFAULT '',
                recording_consent_obtained INTEGER NOT NULL DEFAULT 0,
                recording_enabled INTEGER NOT NULL DEFAULT 0,
                requires_reconciliation INTEGER NOT NULL DEFAULT 0,
                recovery_from_status TEXT NOT NULL DEFAULT '',
                dispatch_claim_token TEXT NOT NULL DEFAULT '',
                dispatch_lease_expires_at INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL, UNIQUE(collie_id, idempotency_key)
            )
        """)
        # One additive migration keeps pre-contract development ledgers readable.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(telephony_intents)")}
        if "message_payload_hash" not in columns:
            self.db.execute(
                "ALTER TABLE telephony_intents ADD COLUMN message_payload_hash "
                "TEXT NOT NULL DEFAULT ''")
        if "dispatch_claim_token" not in columns:
            self.db.execute(
                "ALTER TABLE telephony_intents ADD COLUMN dispatch_claim_token "
                "TEXT NOT NULL DEFAULT ''")
        if "dispatch_lease_expires_at" not in columns:
            self.db.execute(
                "ALTER TABLE telephony_intents ADD COLUMN dispatch_lease_expires_at "
                "INTEGER NOT NULL DEFAULT 0")
        verifier = hmac.new(
            self._fingerprint_key, b"collie-telephony-ledger-key-v1",
            hashlib.sha256).hexdigest()
        stored = self.db.execute(
            "SELECT value FROM telephony_meta WHERE key='hmac_key_check'").fetchone()
        if stored and not hmac.compare_digest(stored[0], verifier):
            self.db.close()
            raise DurableLedgerRequired("telephony ledger HMAC key does not match")
        self.db.execute(
            "INSERT OR IGNORE INTO telephony_meta(key,value) VALUES('hmac_key_check',?)",
            (verifier,))
        self.db.execute(
            "INSERT OR REPLACE INTO telephony_meta(key,value) VALUES('schema_version','2')")
        self.db.commit()

    @contextmanager
    def _transaction(self):
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self.db.rollback()
                raise
            else:
                self.db.commit()

    def _recover_interrupted_submissions(self) -> None:
        placeholders = ",".join("?" for _ in _RECOVERY_STATUSES)
        now = int(self._clock())
        with self._transaction():
            self.db.execute(
                "UPDATE telephony_intents SET recovery_from_status=status, status='uncertain', "
                "requires_reconciliation=1, dispatch_claim_token='', "
                "dispatch_lease_expires_at=0, updated_at=? WHERE status IN (%s) "
                "AND dispatch_lease_expires_at<=?" % placeholders,
                (now, *sorted(_RECOVERY_STATUSES), now))

    def _recover_row_if_expired_tx(self, row: sqlite3.Row) -> sqlite3.Row:
        """Fence an expired in-flight claim even when the ledger process stays alive."""
        now = int(self._clock())
        if (row["status"] in _RECOVERY_STATUSES
                and int(row["dispatch_lease_expires_at"] or 0) <= now):
            self.db.execute(
                "UPDATE telephony_intents SET recovery_from_status=status, status='uncertain', "
                "requires_reconciliation=1, dispatch_claim_token='', "
                "dispatch_lease_expires_at=0, updated_at=? WHERE intent_id=? "
                "AND status=? AND dispatch_lease_expires_at<=?",
                (now, row["intent_id"], row["status"], now))
            row = self.db.execute(
                "SELECT * FROM telephony_intents WHERE intent_id=?", (row["intent_id"],)).fetchone()
        return row

    def _fingerprint(self, intent: CallIntent | MessageIntent) -> bytes:
        projection = intent.receipt_projection()
        projection.pop("intent_id", None)
        safe = json.dumps(projection, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hmac.new(self._fingerprint_key, safe + b"\0" + intent._sensitive_material(),
                        hashlib.sha256).digest()

    def _hash_provider_reference(self, reference: str) -> str:
        return hmac.new(self._fingerprint_key, b"provider-reference\0" + reference.encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def _hash_payload_digest(self, digest: str) -> str:
        return hmac.new(self._fingerprint_key, b"provider-payload\0" + digest.encode("ascii"),
                        hashlib.sha256).hexdigest()

    def _register_tx(self, intent: CallIntent | MessageIntent) -> sqlite3.Row:
        fingerprint = self._fingerprint(intent)
        existing = self.db.execute(
            "SELECT * FROM telephony_intents WHERE collie_id=? AND idempotency_key=?",
            (intent.collie_id, intent.idempotency_key)).fetchone()
        if existing:
            if not hmac.compare_digest(bytes(existing["fingerprint"]), fingerprint):
                raise IdempotencyConflict("idempotency key is bound to another telephony intent")
            return existing
        projection = intent.receipt_projection()
        now = int(self._clock())
        values = (
            intent.intent_id, "telrcpt_" + secrets.token_urlsafe(16), intent.collie_id,
            intent.idempotency_key, fingerprint, intent.kind.value, intent.capability_id,
            intent.recipient.consent_basis.value, intent.recipient.jurisdiction,
            intent.purpose.value, intent.disclosure_policy.value, intent.cost_cap.currency,
            intent.cost_cap.minor_units, projection.get("max_segments"),
            projection.get("sms_encoding"), projection.get("estimated_segments"),
            (self._hash_payload_digest(intent.provider_payload().sha256())
             if isinstance(intent, MessageIntent) else ""),
            projection.get("max_duration_seconds"), projection.get("recording_policy"),
            int(bool(projection.get("recording_requested"))), "planned", now, now)
        try:
            self.db.execute("""
                INSERT INTO telephony_intents (
                    intent_id, receipt_id, collie_id, idempotency_key, fingerprint, kind,
                    capability_id, recipient_consent_basis, recipient_jurisdiction, purpose,
                    disclosure_policy, cost_currency, cost_cap_minor, max_segments, sms_encoding,
                    estimated_segments, message_payload_hash, max_duration_seconds, recording_policy,
                    recording_requested, status, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, values)
        except sqlite3.IntegrityError:
            existing = self.db.execute(
                "SELECT * FROM telephony_intents WHERE collie_id=? AND idempotency_key=?",
                (intent.collie_id, intent.idempotency_key)).fetchone()
            if not existing or not hmac.compare_digest(bytes(existing["fingerprint"]), fingerprint):
                raise IdempotencyConflict("idempotency key is bound to another telephony intent")
            return existing
        return self.db.execute(
            "SELECT * FROM telephony_intents WHERE intent_id=?", (intent.intent_id,)).fetchone()

    def register(self, intent: CallIntent | MessageIntent) -> dict:
        if not isinstance(intent, (CallIntent, MessageIntent)):
            raise TypeError("intent must be CallIntent or MessageIntent")
        with self._transaction():
            row = self._register_tx(intent)
        return self._receipt_row(row)

    def authorize(self, intent: CallIntent | MessageIntent,
                  capability: TelephonyCapability, *, estimated_cost_minor: int = 0) -> dict:
        if not self.durable:
            raise DurableLedgerRequired("automatic telephony requires a durable SQLite ledger")
        if self.capability_registry is None:
            raise CapabilityUnavailable("automatic telephony requires a trusted capability registry")
        self.capability_registry.validate(capability, intent)
        estimated = int(estimated_cost_minor)
        if estimated < 0 or estimated > intent.cost_cap.minor_units:
            raise CostCapExceeded("estimated telephony cost exceeds the intent cap")
        if isinstance(intent, MessageIntent):
            segments = intent.segment_info().segments
            if segments > intent.max_segments:
                raise SegmentCapExceeded(
                    "SMS payload including AI disclosure exceeds max_segments (%d > %d)" %
                    (segments, intent.max_segments))
        reconciliation_required = False
        with self._transaction():
            row = self._register_tx(intent)
            row = self._recover_row_if_expired_tx(row)
            if row["requires_reconciliation"]:
                reconciliation_required = True
            elif row["status"] == "planned":
                self.db.execute(
                    "UPDATE telephony_intents SET status='authorized', updated_at=? WHERE intent_id=?",
                    (int(self._clock()), row["intent_id"]))
                row = self.db.execute(
                    "SELECT * FROM telephony_intents WHERE intent_id=?", (row["intent_id"],)).fetchone()
        if reconciliation_required:
            raise ReconciliationRequired("uncertain provider state must be reconciled before retry")
        return self._receipt_row(row)

    def claim_dispatch(self, intent_id: str, *, lease_seconds: int = 60) -> dict:
        """Atomically reserve exactly one provider submission for an authorized intent.

        Adapter code must not implement this as ``receipt(); transition()``: two host
        processes can both observe ``authorized`` and submit the same call.  This CAS
        moves a call to ``dialing`` (or a message to ``sending``) before any provider
        request.  A process crash is intentionally recovered as ``uncertain`` on the
        next durable-ledger open, so callers reconcile instead of redialing blindly.
        """
        if not self.durable:
            raise DurableLedgerRequired("automatic telephony requires a durable SQLite ledger")
        lease = int(lease_seconds)
        if lease < 5 or lease > 300:
            raise ValueError("dispatch claim lease must be between 5 and 300 seconds")
        reconciliation_required = False
        with self._transaction():
            row = self.db.execute(
                "SELECT * FROM telephony_intents WHERE intent_id=?", (str(intent_id),)).fetchone()
            if not row:
                raise KeyError("unknown telephony intent")
            row = self._recover_row_if_expired_tx(row)
            if row["requires_reconciliation"]:
                reconciliation_required = True
            target = "dialing" if row["kind"] == "call" else "sending"
            claimed = False
            claim_token = ""
            if not reconciliation_required and row["status"] == "authorized":
                claim_token = "telclaim_" + secrets.token_urlsafe(24)
                now = int(self._clock())
                changed = self.db.execute(
                    "UPDATE telephony_intents SET status=?, dispatch_claim_token=?, "
                    "dispatch_lease_expires_at=?, updated_at=? "
                    "WHERE intent_id=? AND status='authorized' AND requires_reconciliation=0",
                    (target, claim_token, now + lease, now, row["intent_id"]))
                claimed = changed.rowcount == 1
                if not claimed:
                    claim_token = ""
            row = self.db.execute(
                "SELECT * FROM telephony_intents WHERE intent_id=?", (row["intent_id"],)).fetchone()
        if reconciliation_required:
            raise ReconciliationRequired("uncertain provider state requires reconcile()")
        return {"claimed": claimed, "claim_token": claim_token,
                "receipt": self._receipt_row(row)}

    def _validated_evidence(self, row: sqlite3.Row,
                            evidence: ProviderEventEvidence | None,
                            expected: str) -> ProviderEventEvidence:
        if not isinstance(evidence, ProviderEventEvidence) or evidence._token is not _EVIDENCE_TOKEN:
            raise ProviderEvidenceRequired("trusted provider evidence is required for %s" % expected)
        if evidence.capability_id != row["capability_id"] or evidence.event_type != expected:
            raise ProviderEvidenceRequired("provider evidence does not match intent/event")
        if self.capability_registry is None:
            raise ProviderEvidenceRequired("provider evidence has no trusted registry")
        capability = self.capability_registry.resolve(row["capability_id"])
        if evidence.provider != capability.provider:
            raise ProviderEvidenceRequired("provider evidence came from another provider")
        now = int(self._clock())
        if evidence.occurred_at > now + 30 or evidence.occurred_at < now - 900:
            raise ProviderEvidenceRequired("provider evidence is stale")
        bound_reference = str(row["provider_reference_hash"] or "")
        if bound_reference:
            observed_reference = self._hash_provider_reference(evidence.event_reference)
            if not hmac.compare_digest(bound_reference, observed_reference):
                raise ProviderEvidenceRequired(
                    "provider evidence belongs to another call or message")
        return evidence

    def transition(self, intent_id: str, status: str, *, actual_cost_minor: int | None = None,
                   provider_reference: str = "",
                   recording_consent_obtained: bool | None = None,
                   provider_evidence: ProviderEventEvidence | None = None,
                   dispatch_claim_token: str = "",
                   dispatch_lease_seconds: int | None = None) -> dict:
        with self._transaction():
            row = self.db.execute(
                "SELECT * FROM telephony_intents WHERE intent_id=?", (str(intent_id),)).fetchone()
            if not row:
                raise KeyError("unknown telephony intent")
            if row["requires_reconciliation"]:
                raise ReconciliationRequired("uncertain provider state requires reconcile()")
            kind = row["kind"]
            transitions = _CALL_TRANSITIONS if kind == "call" else _MESSAGE_TRANSITIONS
            status = str(status)
            current = row["status"]
            if (current == "authorized"
                    and status == ("dialing" if kind == "call" else "sending")):
                raise InvalidTransition(
                    "provider submission must begin with claim_dispatch()")
            if status != current and status not in transitions[current]:
                raise InvalidTransition("illegal %s transition: %s -> %s" %
                                        (kind, current, status))
            if recording_consent_obtained is not None and kind != "call":
                raise ValueError("recording consent applies only to calls")
            expected_evidence = {
                ("call", "in_progress"): "call.ai_disclosure_played",
                ("call", "completed"): "call.completed",
                ("message", "sent"): "message.accepted",
                ("message", "delivered"): "message.delivered",
            }.get((kind, status))
            if kind == "call" and status == "failed" and (
                    current in {"disclosure_pending", "in_progress"}
                    or provider_evidence is not None):
                expected_evidence = "call.failed"
            if kind == "message" and status == "failed" and (
                    current == "sent" or provider_evidence is not None):
                expected_evidence = "message.failed"
            evidence_hash = ""
            if expected_evidence:
                evidence = self._validated_evidence(row, provider_evidence, expected_evidence)
                if (kind, status) == ("message", "sent"):
                    if (not evidence.payload_sha256
                            or not hmac.compare_digest(
                                self._hash_payload_digest(evidence.payload_sha256),
                                                       row["message_payload_hash"])):
                        raise ProviderEvidenceRequired(
                            "provider acceptance must attest the disclosure-bearing SMS payload")
                evidence_hash = self._hash_provider_reference(evidence.event_reference)
            active_claim = str(row["dispatch_claim_token"] or "")
            supplied_claim = str(dispatch_claim_token or "")
            if supplied_claim and (len(supplied_claim) > 160 or "\x00" in supplied_claim):
                raise ValueError("dispatch claim token is invalid")
            if (active_claim and not evidence_hash
                    and not hmac.compare_digest(active_claim, supplied_claim)):
                raise InvalidTransition("active provider submission requires its dispatch claim")
            lease_extension = None
            if dispatch_lease_seconds is not None:
                lease_extension = int(dispatch_lease_seconds)
                if (status != "disclosure_pending" or not active_claim
                        or not hmac.compare_digest(active_claim, supplied_claim)):
                    raise ValueError("only the active call claim can extend its provider lease")
                if lease_extension < 15 or lease_extension > 7200:
                    raise ValueError("active call lease must be between 15 and 7200 seconds")
            recording_consent = int(row["recording_consent_obtained"])
            recording_enabled = int(row["recording_enabled"])
            disclosure_hash = row["disclosure_evidence_hash"]
            if kind == "call" and status == "in_progress":
                requested = bool(row["recording_requested"])
                if requested and recording_consent_obtained is None:
                    raise ValueError("recording consent outcome is required before the call proceeds")
                if not requested and recording_consent_obtained:
                    raise ValueError("recording consent cannot enable an unrequested recording")
                recording_consent = int(bool(recording_consent_obtained))
                recording_enabled = int(bool(requested and recording_consent_obtained))
                disclosure_hash = evidence_hash
            actual = int(row["actual_cost_minor"])
            if actual_cost_minor is not None:
                actual = int(actual_cost_minor)
                if actual < 0 or actual > int(row["cost_cap_minor"]):
                    raise CostCapExceeded("reported telephony cost exceeds the intent cap")
            reference_hash = row["provider_reference_hash"]
            if provider_reference:
                reference = _bounded("provider reference", provider_reference, maximum=256)
                reference_hash = self._hash_provider_reference(reference)
            if evidence_hash:
                reference_hash = evidence_hash
            requires_reconciliation = int(status == "uncertain")
            recovery_from = current if status == "uncertain" else ""
            next_claim = active_claim
            next_lease = int(row["dispatch_lease_expires_at"] or 0)
            if status in {"failed", "cancelled", "uncertain", "completed", "delivered", "sent"}:
                next_claim, next_lease = "", 0
            elif status == "in_progress":
                # The provider owns the live session now. Drop the worker token
                # but preserve the duration-bound lease established at submit.
                next_claim = ""
            elif evidence_hash:
                next_claim, next_lease = "", 0
            elif lease_extension is not None:
                next_lease = int(self._clock()) + lease_extension
            self.db.execute("""
                UPDATE telephony_intents SET status=?, actual_cost_minor=?,
                    provider_reference_hash=?, disclosure_evidence_hash=?,
                    recording_consent_obtained=?, recording_enabled=?,
                    requires_reconciliation=?, recovery_from_status=?, updated_at=?
                    , dispatch_claim_token=?, dispatch_lease_expires_at=?
                WHERE intent_id=?
            """, (status, actual, reference_hash, disclosure_hash, recording_consent,
                  recording_enabled, requires_reconciliation, recovery_from,
                  int(self._clock()), next_claim, next_lease, row["intent_id"]))
            result = self.db.execute(
                "SELECT * FROM telephony_intents WHERE intent_id=?", (row["intent_id"],)).fetchone()
        return self._receipt_row(result)

    def reconcile(self, intent_id: str, *,
                  provider_evidence: ProviderEventEvidence) -> dict:
        """Resolve an uncertain submit from trusted evidence before retrying."""
        with self._transaction():
            row = self.db.execute(
                "SELECT * FROM telephony_intents WHERE intent_id=?", (str(intent_id),)).fetchone()
            if not row:
                raise KeyError("unknown telephony intent")
            if row["status"] != "uncertain" or not row["requires_reconciliation"]:
                raise InvalidTransition("only uncertain intents can be reconciled")
            mapping = {
                "submission.absent": "authorized",
                "call.completed": "completed", "call.failed": "failed",
                "message.accepted": "sent", "message.delivered": "delivered",
                "message.failed": "failed",
            }
            if not isinstance(provider_evidence, ProviderEventEvidence):
                raise ProviderEvidenceRequired("trusted provider evidence is required")
            target = mapping.get(provider_evidence.event_type)
            if not target:
                raise ProviderEvidenceRequired("provider evidence cannot reconcile this state")
            if (provider_evidence.event_type.startswith("message.")
                    and row["kind"] != "message"):
                raise ProviderEvidenceRequired("message evidence cannot reconcile a call")
            if (provider_evidence.event_type.startswith("call.")
                    and row["kind"] != "call"):
                raise ProviderEvidenceRequired("call evidence cannot reconcile a message")
            if (provider_evidence.event_type == "submission.absent"
                    and row["recovery_from_status"] not in {"dialing", "sending"}):
                raise ProviderEvidenceRequired(
                    "absence cannot authorize retry after an active/accepted provider state")
            evidence = self._validated_evidence(row, provider_evidence,
                                                provider_evidence.event_type)
            reference_hash = ("" if evidence.event_type == "submission.absent"
                              else self._hash_provider_reference(evidence.event_reference))
            disclosure_hash = row["disclosure_evidence_hash"]
            self.db.execute("""
                UPDATE telephony_intents SET status=?, provider_reference_hash=?,
                    disclosure_evidence_hash=?, requires_reconciliation=0,
                    recovery_from_status='', dispatch_claim_token='',
                    dispatch_lease_expires_at=0, updated_at=? WHERE intent_id=?
            """, (target, reference_hash, disclosure_hash, int(self._clock()), row["intent_id"]))
            result = self.db.execute(
                "SELECT * FROM telephony_intents WHERE intent_id=?", (row["intent_id"],)).fetchone()
        return self._receipt_row(result)

    def receipt(self, intent_id: str) -> dict:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM telephony_intents WHERE intent_id=?", (str(intent_id),)).fetchone()
        if not row:
            raise KeyError("unknown telephony intent")
        return self._receipt_row(row)

    def provider_reference_matches(self, intent_id: str, reference: str) -> bool:
        """Constant-time: is `reference` (a call SID / conversation id seen at the provider)
        the provider reference this intent was bound to?  The ledger stores only the keyed
        hash, so this is the one way to link a durable receipt back to a provider record
        without ever persisting the reference itself."""
        ref = str(reference or "").strip()
        if not ref or len(ref) > 512:
            return False
        with self._lock:
            row = self.db.execute(
                "SELECT provider_reference_hash FROM telephony_intents WHERE intent_id=?",
                (str(intent_id),)).fetchone()
        if not row or not row[0]:
            return False
        return hmac.compare_digest(str(row[0]), self._hash_provider_reference(ref))

    @staticmethod
    def _receipt_row(row: sqlite3.Row) -> dict:
        recipient = {"consent_basis": row["recipient_consent_basis"]}
        if row["recipient_jurisdiction"]:
            recipient["jurisdiction"] = row["recipient_jurisdiction"]
        result = {
            "intent_id": row["intent_id"], "receipt_id": row["receipt_id"],
            "kind": row["kind"], "collie_id": row["collie_id"],
            "capability_id": row["capability_id"], "recipient": recipient,
            "purpose": row["purpose"], "ai_disclosure": row["disclosure_policy"],
            "cost_cap": {"currency": row["cost_currency"],
                         "minor_units": row["cost_cap_minor"]},
            "status": row["status"],
            "actual_cost": {"currency": row["cost_currency"],
                            "minor_units": row["actual_cost_minor"]},
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "requires_reconciliation": bool(row["requires_reconciliation"]),
        }
        if row["kind"] == "message":
            result.update({
                "max_segments": row["max_segments"], "sms_encoding": row["sms_encoding"],
                "estimated_segments": row["estimated_segments"],
            })
        else:
            result.update({
                "max_duration_seconds": row["max_duration_seconds"],
                "recording_policy": row["recording_policy"],
                "recording_requested": bool(row["recording_requested"]),
                "recording_consent_obtained": bool(row["recording_consent_obtained"]),
                "recording_enabled": bool(row["recording_enabled"]),
                "disclosure_evidence": bool(row["disclosure_evidence_hash"]),
            })
        if row["provider_reference_hash"]:
            result["provider_reference_hash"] = row["provider_reference_hash"]
        if row["recovery_from_status"]:
            result["recovery_from_status"] = row["recovery_from_status"]
        return result

    def close(self) -> None:
        with self._lock:
            if self.durable:
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


__all__ = [
    "AIDisclosurePolicy", "CALL_STATUSES", "CallIntent", "CapabilityHealthEvidence",
    "CapabilityRegistry", "CapabilityUnavailable", "ConsentBasis", "CostCapExceeded",
    "DurableLedgerRequired", "IdempotencyConflict", "IntentKind", "IntentLedger",
    "InvalidTransition", "MESSAGE_STATUSES", "MessageIntent", "MessageProviderPayload",
    "MoneyCap", "ProviderEventEvidence", "ProviderEvidenceRequired", "Purpose",
    "Recipient", "RecordingPolicy", "ReconciliationRequired", "SegmentCapExceeded",
    "SmsSegmentInfo", "TelephonyCapability", "TelephonyError",
    "google_voice_assigned_line", "mask_number", "normalize_e164",
    "programmable_outbound_adapter", "programmable_registered_line", "sms_segment_info",
]
