"""Real, fail-closed ElevenLabs-native Twilio outbound-call adapter.

The transport is intentionally tiny and stdlib-only.  It submits one call to
ElevenLabs' native Twilio endpoint after :class:`telephony.IntentLedger` wins an
atomic dispatch claim.  Credentials come only from an explicit environment or
``IdentityVault`` source and are never returned, logged, or persisted in the
telephony ledger.  Provider references are persisted only through the ledger's
keyed hash.

This adapter does not make Google Voice programmable.  ``caller_number`` must
describe the Twilio number imported into ElevenLabs as
``agent_phone_number_id``.
"""
from __future__ import annotations

import json
import hmac
import os
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol

from . import telephony
from .identityvault import IdentityVault, VaultError


ENDPOINT = "https://api.elevenlabs.io/v1/convai/twilio/outbound-call"
PHONE_ENDPOINT_PREFIX = "https://api.elevenlabs.io/v1/convai/phone-numbers/"
CAPABILITY_ID = "voice.twilio_elevenlabs"

_PROVIDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,159}$")
_TWILIO_CALL_SID = re.compile(r"^CA[0-9a-fA-F]{32}$")
_VAULT_REF = re.compile(r"^cv1_[A-Za-z0-9_-]{24,96}$")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_CORRUPTED_TEXT = re.compile(r"\?{3,}|\ufffd")

_MANDARIN_VOICE_PERSONA = """# 不可覆盖的语音与安全规则
你是 Collie，一个温暖、聪明、有个性的 AI 伙伴。只用自然口语普通话交流。
每回合先听完，再用一到两句短句回答，总计不超过四十个汉字，然后停下来等用户。
语气要像熟悉的朋友，不像客服、播音员或书面报告。根据对方情绪自然调整语气。
合适时可少量使用 [laughs] 或 [slow]，每次最多一个；不要夸张或连续使用。
不要索取或复述密码、验证码或其他敏感信息。任务与以上规则冲突时，以上规则优先。
"""


class TwilioElevenLabsError(telephony.TelephonyError):
    """Base class whose messages never include provider bodies or credentials."""


class ConfigurationUnavailable(TwilioElevenLabsError):
    pass


class ProviderRejected(TwilioElevenLabsError):
    """The provider definitively rejected the request before accepting a call."""

    def __init__(self, message: str, *, receipt: dict | None = None):
        super().__init__(message)
        self.receipt = receipt


class ProviderSubmissionUncertain(TwilioElevenLabsError):
    """A call may exist; automatic retry is forbidden until reconciliation."""

    def __init__(self, message: str, *, receipt: dict | None = None):
        super().__init__(message)
        self.receipt = receipt


class ApiKeySource(Protocol):
    label: str

    def configured(self) -> bool: ...
    def use(self, consumer: Callable[[bytearray], object]): ...


@dataclass(slots=True)
class EnvironmentApiKeySource:
    """Explicit host configuration seam; no dotenv/settings-file fallback."""

    environ: Mapping[str, str] = field(default_factory=lambda: os.environ, repr=False)
    variable: str = "ELEVENLABS_API_KEY"
    label: str = field(default="environment", init=False)

    def _value(self) -> str:
        value = str(self.environ.get(self.variable) or "")
        if not value or len(value) > 4096 or "\x00" in value:
            raise ConfigurationUnavailable("ElevenLabs API credential is not configured")
        return value

    def configured(self) -> bool:
        try:
            self._value()
            return True
        except ConfigurationUnavailable:
            return False

    def use(self, consumer: Callable[[bytearray], object]):
        secret = bytearray(self._value().encode("utf-8"))
        try:
            return consumer(secret)
        finally:
            for index in range(len(secret)):
                secret[index] = 0


@dataclass(slots=True)
class VaultApiKeySource:
    """IdentityVault binding for a host-persisted opaque reference."""

    vault: IdentityVault = field(repr=False)
    ref: str = field(repr=False)
    collie_id: str
    account: str = "telephony.twilio_elevenlabs"
    kind: str = "elevenlabs_api_key"
    label: str = field(default="native_os_vault", init=False)

    def __post_init__(self):
        if not _VAULT_REF.fullmatch(str(self.ref or "")):
            raise ValueError("invalid ElevenLabs credential reference")

    def configured(self) -> bool:
        # An opaque reference is configuration evidence, not an operational
        # probe.  ``use`` still fails closed if the native item is absent/locked.
        return True

    def use(self, consumer: Callable[[bytearray], object]):
        return self.vault.use(
            self.ref, collie_id=self.collie_id, account=self.account,
            kind=self.kind, consumer=consumer)


@dataclass(frozen=True, slots=True)
class TwilioElevenLabsConfig:
    collie_id: str
    caller_number: str = field(repr=False)
    agent_id: str = field(repr=False)
    agent_phone_number_id: str = field(repr=False)
    overrides_enabled: bool
    caller_id_binding_verified: bool
    language: str = "zh"
    tts_model_id: str = "eleven_v3_conversational"
    tts_stability: float = 0.38
    tts_similarity_boost: float = 0.75
    tts_speed: float = 0.95
    llm_temperature: float = 0.45
    llm_max_tokens: int = 120
    request_timeout_seconds: float = 10.0
    ringing_timeout_seconds: int = 30
    capability_id: str = CAPABILITY_ID

    def __post_init__(self):
        collie_id = str(self.collie_id or "").strip()
        if not collie_id or len(collie_id) > 256 or "\x00" in collie_id:
            raise ValueError("collie_id must be a non-empty bounded string")
        caller = telephony.normalize_e164(self.caller_number)
        agent = str(self.agent_id or "").strip()
        phone_id = str(self.agent_phone_number_id or "").strip()
        if not _PROVIDER_ID.fullmatch(agent):
            raise ValueError("invalid ElevenLabs agent id")
        if not _PROVIDER_ID.fullmatch(phone_id):
            raise ValueError("invalid ElevenLabs phone-number id")
        if self.overrides_enabled is not True:
            raise ValueError(
                "ElevenLabs first-message, prompt, and duration overrides must be explicitly enabled")
        if self.caller_id_binding_verified is not True:
            raise ValueError(
                "the external caller ID must be verified and bound to the provider phone id")
        timeout = float(self.request_timeout_seconds)
        if timeout < 1 or timeout > 30:
            raise ValueError("provider request timeout must be between 1 and 30 seconds")
        ringing = int(self.ringing_timeout_seconds)
        if ringing < 5 or ringing > 60:
            raise ValueError("ringing timeout must be between 5 and 60 seconds")
        capability_id = str(self.capability_id or "").lower()
        if capability_id != CAPABILITY_ID:
            raise ValueError("unsupported Twilio/ElevenLabs capability id")
        language = str(self.language or "").strip().lower()
        if language != "zh":
            raise ValueError("this Mandarin outbound adapter requires language='zh'")
        tts_model = str(self.tts_model_id or "").strip()
        if tts_model != "eleven_v3_conversational":
            raise ValueError(
                "the natural-voice profile requires eleven_v3_conversational")
        stability = float(self.tts_stability)
        similarity = float(self.tts_similarity_boost)
        speed = float(self.tts_speed)
        temperature = float(self.llm_temperature)
        max_tokens = int(self.llm_max_tokens)
        if not 0.0 <= stability <= 1.0:
            raise ValueError("TTS stability must be between 0 and 1")
        if not 0.0 <= similarity <= 1.0:
            raise ValueError("TTS similarity boost must be between 0 and 1")
        if not 0.7 <= speed <= 1.2:
            raise ValueError("TTS speed must be between 0.7 and 1.2")
        if not 0.0 <= temperature <= 1.0:
            raise ValueError("voice LLM temperature must be between 0 and 1")
        if max_tokens < 32 or max_tokens > 256:
            raise ValueError("voice LLM max tokens must be between 32 and 256")
        object.__setattr__(self, "collie_id", collie_id)
        object.__setattr__(self, "caller_number", caller)
        object.__setattr__(self, "agent_id", agent)
        object.__setattr__(self, "agent_phone_number_id", phone_id)
        object.__setattr__(self, "language", language)
        object.__setattr__(self, "tts_model_id", tts_model)
        object.__setattr__(self, "tts_stability", stability)
        object.__setattr__(self, "tts_similarity_boost", similarity)
        object.__setattr__(self, "tts_speed", speed)
        object.__setattr__(self, "llm_temperature", temperature)
        object.__setattr__(self, "llm_max_tokens", max_tokens)
        object.__setattr__(self, "request_timeout_seconds", timeout)
        object.__setattr__(self, "ringing_timeout_seconds", ringing)
        object.__setattr__(self, "capability_id", capability_id)

    @classmethod
    def from_environment(cls, *, collie_id: str,
                         environ: Mapping[str, str] | None = None):
        env = os.environ if environ is None else environ
        overrides = str(env.get("COLLIE_ELEVENLABS_OVERRIDES_ENABLED") or "").lower()
        if overrides not in {"1", "true", "yes"}:
            raise ConfigurationUnavailable(
                "ElevenLabs first-message/prompt/duration overrides are not attested as enabled")
        caller_binding = str(env.get("COLLIE_TWILIO_CALLER_ID_VERIFIED") or "").lower()
        if caller_binding not in {"1", "true", "yes"}:
            raise ConfigurationUnavailable(
                "the Twilio external caller-ID binding is not attested as verified")
        required = {
            "caller_number": "COLLIE_TWILIO_CALLER_NUMBER",
            "agent_id": "ELEVENLABS_AGENT_ID",
            "agent_phone_number_id": "ELEVENLABS_AGENT_PHONE_NUMBER_ID",
        }
        missing = [name for name in required.values() if not str(env.get(name) or "").strip()]
        if missing:
            raise ConfigurationUnavailable(
                "Twilio/ElevenLabs host configuration is incomplete: " + ", ".join(missing))
        try:
            request_timeout = float(env.get("COLLIE_TELEPHONY_HTTP_TIMEOUT_SECONDS") or 10)
            ringing_timeout = int(env.get("COLLIE_TELEPHONY_RING_TIMEOUT_SECONDS") or 30)
            return cls(
                collie_id=collie_id,
                caller_number=str(env[required["caller_number"]]),
                agent_id=str(env[required["agent_id"]]),
                agent_phone_number_id=str(env[required["agent_phone_number_id"]]),
                overrides_enabled=True,
                caller_id_binding_verified=True,
                request_timeout_seconds=request_timeout,
                ringing_timeout_seconds=ringing_timeout,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigurationUnavailable("Twilio/ElevenLabs host configuration is invalid") from exc

    def capability(self) -> telephony.TelephonyCapability:
        return telephony.programmable_outbound_adapter(
            adapter_id=self.capability_id, provider="twilio",
            verified_caller_id=self.caller_number, channels=("call",))

    def projection(self) -> dict:
        return {
            "capability_id": self.capability_id,
            "provider": "twilio",
            "voice_provider": "elevenlabs",
            "runtime": "elevenlabs_native_twilio",
            "line_hint": telephony.mask_number(self.caller_number),
            "ownership": "user_owned_verified_caller_id",
            "outbound_calls": True,
            "inbound_calls": False,
            "twilio_call_recording": False,
            "provider_conversation_retention": "unprobed_provider_policy",
            "first_message_override": True,
            "language_override": self.language,
            "tts_model": self.tts_model_id,
            "expressive_mode": True,
            "duration_override": True,
            "cost_control": "required_preflight_estimate_plus_provider_duration_cap",
            "request_timeout_seconds": self.request_timeout_seconds,
            "ringing_timeout_seconds": self.ringing_timeout_seconds,
        }


@dataclass(frozen=True, slots=True)
class ProviderHttpResponse:
    status: int
    body: bytes = field(repr=False)


class JsonTransport(Protocol):
    def get_json(self, url: str, *, headers: Mapping[str, str],
                 timeout: float) -> ProviderHttpResponse: ...
    def post_json(self, url: str, *, headers: Mapping[str, str], payload: dict,
                  timeout: float) -> ProviderHttpResponse: ...


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Never carry the API key to a redirect target, even on the same host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibJsonTransport:
    """One-shot HTTPS transport.  It deliberately performs no automatic retry."""

    _MAX_RESPONSE = 64 * 1024

    def __init__(self, *, opener=None):
        self._opener = opener or urllib.request.build_opener(_RejectRedirects())

    def _open(self, request: urllib.request.Request, *, timeout: float) -> ProviderHttpResponse:
        try:
            with self._opener.open(request, timeout=timeout) as response:
                body = response.read(self._MAX_RESPONSE + 1)
                if len(body) > self._MAX_RESPONSE:
                    raise ProviderSubmissionUncertain("provider response exceeded the safe limit")
                return ProviderHttpResponse(int(response.status), body)
        except urllib.error.HTTPError as exc:
            body = exc.read(self._MAX_RESPONSE + 1)
            if len(body) > self._MAX_RESPONSE:
                body = b""
            return ProviderHttpResponse(int(exc.code), body)
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise ProviderSubmissionUncertain(
                "provider request outcome is unknown; reconciliation is required") from exc

    def get_json(self, url: str, *, headers: Mapping[str, str],
                 timeout: float) -> ProviderHttpResponse:
        if (not url.startswith(PHONE_ENDPOINT_PREFIX)
                or not _PROVIDER_ID.fullmatch(url[len(PHONE_ENDPOINT_PREFIX):])):
            raise ConfigurationUnavailable("provider probe endpoint is not allowlisted")
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        return self._open(request, timeout=timeout)

    def post_json(self, url: str, *, headers: Mapping[str, str], payload: dict,
                  timeout: float) -> ProviderHttpResponse:
        if url != ENDPOINT:
            raise ConfigurationUnavailable("provider endpoint is not allowlisted")
        request = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers=dict(headers), method="POST")
        return self._open(request, timeout=timeout)


class TwilioElevenLabsOutbound:
    """Submit one disclosure-bearing outbound call through ElevenLabs + Twilio."""

    def __init__(self, config: TwilioElevenLabsConfig, *, api_key: ApiKeySource,
                 ledger: telephony.IntentLedger,
                 registry: telephony.CapabilityRegistry | None = None,
                 transport: JsonTransport | None = None):
        if not isinstance(config, TwilioElevenLabsConfig):
            raise TypeError("config must be TwilioElevenLabsConfig")
        if not isinstance(ledger, telephony.IntentLedger) or not ledger.durable:
            raise telephony.DurableLedgerRequired(
                "Twilio/ElevenLabs outbound calls require a durable intent ledger")
        self.config = config
        self.api_key = api_key
        self.ledger = ledger
        self.registry = registry or ledger.capability_registry
        if self.registry is None or self.ledger.capability_registry is not self.registry:
            raise telephony.CapabilityUnavailable(
                "outbound adapter and durable ledger require the same trusted registry")
        self.transport = transport or UrllibJsonTransport()
        self.capability = config.capability()

    def _refresh_capability(self, *, evidence_reference: str) -> None:
        self.registry.register(
            self.capability, source="provider_api_probe", status="healthy",
            ttl_seconds=300, evidence_reference=evidence_reference)

    def status(self) -> dict:
        ready = bool(self.api_key.configured())
        result = self.config.projection()
        result.update({
            "configured": ready,
            "status": "configured_unprobed" if ready else "credential_missing",
            "credential_source": str(getattr(self.api_key, "label", "host_secret_source")),
            "credential_operational": None,
            "provider_probe": "not_performed",
            "dry_run_available": True,
        })
        return result

    def dry_run(self, intent: telephony.CallIntent, *,
                estimated_cost_minor: int | None = None) -> dict:
        self._validate_intent(intent, estimated_cost_minor=estimated_cost_minor)
        self.capability.validate_dispatch(intent)
        return {
            "dry_run": True,
            "submitted": False,
            "intent": intent.receipt_projection(),
            "adapter": self.config.projection(),
            "provider_request": {
                "endpoint": "elevenlabs_native_twilio",
                "recipient": "validated_e164_redacted",
                "ai_disclosure_position": "first_message",
                "prompt_override": True,
                "language_override": self.config.language,
                "tts_model": self.config.tts_model_id,
                "expressive_mode": True,
                "duration_override_seconds": intent.max_duration_seconds,
                "twilio_call_recording": False,
            },
        }

    def dispatch(self, intent: telephony.CallIntent, *,
                 estimated_cost_minor: int | None = None) -> dict:
        self._validate_intent(intent, estimated_cost_minor=estimated_cost_minor)
        if not self.api_key.configured():
            raise ConfigurationUnavailable("ElevenLabs API credential is not configured")
        receipt = self.ledger.register(intent)
        if receipt["status"] not in {"planned", "authorized"}:
            try:
                claimed = self.ledger.claim_dispatch(receipt["intent_id"])
                receipt = claimed["receipt"]
            except telephony.ReconciliationRequired:
                # The lease-expiry write commits before this signal, including
                # in a long-running process that never reopens the ledger.
                receipt = self.ledger.receipt(receipt["intent_id"])
            return {"submitted": False, "replayed": True, "receipt": receipt}

        try:
            return self.api_key.use(
                lambda secret: self._dispatch_with_secret(
                    intent, secret, ledger_intent_id=receipt["intent_id"],
                    estimated_cost_minor=int(estimated_cost_minor)))
        except VaultError:
            # Vault access happens before the atomic provider claim; no call was sent.
            raise ConfigurationUnavailable(
                "native credential vault is unavailable or the API credential is absent") from None

    def _validate_intent(self, intent: telephony.CallIntent, *,
                         estimated_cost_minor: int | None) -> None:
        if not isinstance(intent, telephony.CallIntent):
            raise TypeError("Twilio/ElevenLabs adapter accepts CallIntent only")
        if intent.collie_id != self.config.collie_id:
            raise telephony.CapabilityUnavailable("call intent belongs to another Collie")
        if intent.capability_id != self.config.capability_id:
            raise telephony.CapabilityUnavailable("call intent targets another capability")
        if intent.recording_requested:
            # Native Twilio recording begins outside the disclosure/consent state
            # machine, so this adapter never enables it.
            raise telephony.CapabilityUnavailable(
                "recording is disabled for the native Twilio/ElevenLabs adapter")
        self._validate_mandarin_text(
            "AI disclosure", intent.disclosure_text, minimum_cjk=4)
        self._validate_mandarin_text(
            "call brief", intent.brief, minimum_cjk=10)
        if estimated_cost_minor is None:
            raise telephony.CostCapExceeded(
                "a bounded provider cost estimate is required before an outbound call")
        estimated = int(estimated_cost_minor)
        if estimated < 0 or estimated > intent.cost_cap.minor_units:
            raise telephony.CostCapExceeded("estimated telephony cost exceeds the intent cap")

    @staticmethod
    def _validate_mandarin_text(label: str, value: str, *, minimum_cjk: int) -> None:
        """Reject the exact Windows/OEM corruption that can turn CJK into ``?``.

        The adapter is intentionally Mandarin-only today.  Requiring a useful
        amount of actual CJK text makes a corrupted prompt fail before any
        provider claim or paid phone call is submitted.
        """
        text = str(value or "")
        try:
            if text.encode("utf-8").decode("utf-8") != text:
                raise UnicodeError
        except UnicodeError:
            raise ConfigurationUnavailable(
                f"{label} is not valid round-trip UTF-8") from None
        if _CORRUPTED_TEXT.search(text):
            raise ConfigurationUnavailable(
                f"{label} appears to contain corrupted Unicode text")
        if len(_CJK.findall(text)) < int(minimum_cjk):
            raise ConfigurationUnavailable(
                f"{label} must contain a substantive Mandarin message")

    @staticmethod
    def _provider_prompt(intent: telephony.CallIntent) -> str:
        return _MANDARIN_VOICE_PERSONA + "\n# 本次通话任务\n" + intent.brief

    @staticmethod
    def _provider_payload(intent: telephony.CallIntent,
                          config: TwilioElevenLabsConfig) -> dict:
        return {
            "agent_id": config.agent_id,
            "agent_phone_number_id": config.agent_phone_number_id,
            "to_number": intent.recipient.number,
            "conversation_initiation_client_data": {
                "conversation_config_override": {
                    "agent": {
                        "prompt": {
                            "prompt": TwilioElevenLabsOutbound._provider_prompt(intent),
                            "temperature": config.llm_temperature,
                            "max_tokens": config.llm_max_tokens,
                        },
                        "first_message": intent.disclosure_text,
                        "language": config.language,
                    },
                    "conversation": {
                        "max_duration_seconds": intent.max_duration_seconds,
                    },
                    "tts": {
                        "model_id": config.tts_model_id,
                        "stability": config.tts_stability,
                        "similarity_boost": config.tts_similarity_boost,
                        "speed": config.tts_speed,
                    },
                }
            },
            "call_recording_enabled": False,
            "telephony_call_config": {
                "ringing_timeout_secs": config.ringing_timeout_seconds,
            },
        }

    def _probe_provider_binding(self, headers: Mapping[str, str]) -> None:
        """Prove the configured ElevenLabs phone id is this exact Twilio caller ID."""
        try:
            response = self.transport.get_json(
                PHONE_ENDPOINT_PREFIX + self.config.agent_phone_number_id,
                headers=headers, timeout=self.config.request_timeout_seconds)
        except Exception:
            raise ConfigurationUnavailable(
                "provider phone binding could not be verified") from None
        if response.status != 200:
            raise ConfigurationUnavailable(
                "provider phone binding probe was rejected")
        try:
            decoded = json.loads(response.body.decode("utf-8"))
            provider_name = str(decoded.get("provider") or "").lower()
            phone_id = str(decoded.get("phone_number_id") or "")
            phone_number = telephony.normalize_e164(decoded.get("phone_number"))
            assigned = decoded.get("assigned_agent")
        except (AttributeError, TypeError, ValueError, UnicodeDecodeError,
                json.JSONDecodeError):
            raise ConfigurationUnavailable(
                "provider phone binding response is invalid") from None
        if (provider_name != "twilio"
                or not hmac.compare_digest(phone_id, self.config.agent_phone_number_id)
                or not hmac.compare_digest(phone_number, self.config.caller_number)):
            raise ConfigurationUnavailable(
                "provider phone id is not bound to the configured verified caller ID")
        if isinstance(assigned, dict) and assigned.get("agent_id"):
            if not hmac.compare_digest(str(assigned["agent_id"]), self.config.agent_id):
                raise ConfigurationUnavailable(
                    "provider phone id is assigned to another ElevenLabs agent")

    def _dispatch_with_secret(self, intent: telephony.CallIntent,
                              secret: bytearray, *, ledger_intent_id: str,
                              estimated_cost_minor: int) -> dict:
        try:
            api_key = bytes(secret).decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            raise ConfigurationUnavailable("ElevenLabs API credential encoding is invalid") from None
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "collie-telephony/1",
            "xi-api-key": api_key,
        }
        try:
            self._probe_provider_binding(headers)
        except ConfigurationUnavailable:
            headers.pop("xi-api-key", None)
            api_key = ""
            raise
        evidence_reference = json.dumps(
            [self.config.agent_phone_number_id, self.config.caller_number, "twilio"],
            separators=(",", ":"))
        self._refresh_capability(evidence_reference=evidence_reference)
        receipt = self.ledger.authorize(
            intent, self.capability, estimated_cost_minor=estimated_cost_minor)
        if receipt["status"] != "authorized":
            headers.pop("xi-api-key", None)
            api_key = ""
            return {"submitted": False, "replayed": True, "receipt": receipt}
        claim = self.ledger.claim_dispatch(
            ledger_intent_id,
            lease_seconds=min(300, max(15, int(self.config.request_timeout_seconds) + 15)))
        if not claim["claimed"]:
            headers.pop("xi-api-key", None)
            api_key = ""
            return {"submitted": False, "replayed": True, "receipt": claim["receipt"]}
        claim_token = claim["claim_token"]
        payload = self._provider_payload(intent, self.config)
        try:
            response = self.transport.post_json(
                ENDPOINT, headers=headers, payload=payload,
                timeout=self.config.request_timeout_seconds)
        except ProviderSubmissionUncertain:
            receipt = self._safe_transition(
                ledger_intent_id, "uncertain", claim_token=claim_token)
            raise ProviderSubmissionUncertain(
                "provider request outcome is unknown; reconciliation is required",
                receipt=receipt) from None
        except Exception:
            receipt = self._safe_transition(
                ledger_intent_id, "uncertain", claim_token=claim_token)
            raise ProviderSubmissionUncertain(
                "provider transport failed with an unknown outcome", receipt=receipt) from None
        finally:
            # Drop our direct header reference promptly.  urllib/provider clients
            # may make internal copies, but Collie never serializes or logs it.
            headers.pop("xi-api-key", None)
            api_key = ""

        if response.status != 200:
            if response.status in {400, 401, 403, 404, 422, 429}:
                receipt = self._safe_transition(
                    ledger_intent_id, "failed", claim_token=claim_token)
                raise ProviderRejected(
                    "provider rejected the outbound call (HTTP %d)" % response.status,
                    receipt=receipt)
            receipt = self._safe_transition(
                ledger_intent_id, "uncertain", claim_token=claim_token)
            raise ProviderSubmissionUncertain(
                "provider returned an ambiguous HTTP response; reconciliation is required",
                receipt=receipt)

        try:
            decoded = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            receipt = self._safe_transition(
                ledger_intent_id, "uncertain", claim_token=claim_token)
            raise ProviderSubmissionUncertain(
                "provider returned an invalid success response; reconciliation is required",
                receipt=receipt) from None
        if not isinstance(decoded, dict) or not isinstance(decoded.get("success"), bool):
            receipt = self._safe_transition(
                ledger_intent_id, "uncertain", claim_token=claim_token)
            raise ProviderSubmissionUncertain(
                "provider success response schema is invalid; reconciliation is required",
                receipt=receipt)
        call_sid = str(decoded.get("callSid") or "")
        conversation_id = str(decoded.get("conversation_id") or "")
        if decoded["success"] is not True:
            if call_sid or conversation_id:
                receipt = self._safe_transition(
                    ledger_intent_id, "uncertain", claim_token=claim_token)
                raise ProviderSubmissionUncertain(
                    "provider response is contradictory; reconciliation is required",
                    receipt=receipt)
            receipt = self._safe_transition(
                ledger_intent_id, "failed", claim_token=claim_token)
            raise ProviderRejected("provider did not accept the outbound call", receipt=receipt)
        call_sid_valid = bool(_TWILIO_CALL_SID.fullmatch(call_sid))
        conversation_id_valid = bool(_PROVIDER_ID.fullmatch(conversation_id))
        if ((call_sid and not call_sid_valid)
                or (conversation_id and not conversation_id_valid)
                or not (call_sid_valid or conversation_id_valid)):
            receipt = self._safe_transition(
                ledger_intent_id, "uncertain", claim_token=claim_token)
            raise ProviderSubmissionUncertain(
                "provider accepted the call without valid references; reconciliation is required",
                receipt=receipt)

        try:
            receipt = self.ledger.transition(
                ledger_intent_id, "disclosure_pending",
                provider_reference=call_sid if call_sid_valid else conversation_id,
                dispatch_claim_token=claim_token,
                dispatch_lease_seconds=min(7200, intent.max_duration_seconds + 120))
        except Exception:
            # The CAS already says dialing.  Never retry a provider-accepted call
            # merely because the local receipt update failed.
            receipt = self._safe_transition(
                ledger_intent_id, "uncertain", claim_token=claim_token)
            raise ProviderSubmissionUncertain(
                "provider accepted the call but local receipt finalization failed",
                receipt=receipt) from None
        return {
            "submitted": True,
            "replayed": False,
            "receipt": receipt,
            "provider": "twilio",
            "voice_provider": "elevenlabs",
        }

    def _safe_transition(self, intent_id: str, status: str, *,
                         claim_token: str = "") -> dict | None:
        try:
            return self.ledger.transition(
                intent_id, status, dispatch_claim_token=claim_token)
        except Exception:
            return self._receipt_or_none(intent_id)

    def _receipt_or_none(self, intent_id: str) -> dict | None:
        try:
            return self.ledger.receipt(intent_id)
        except Exception:
            return None


def environment_configuration_status(*, collie_id: str,
                                     environ: Mapping[str, str] | None = None) -> dict:
    """Secret-free, network-free status for the identity control plane."""
    env = os.environ if environ is None else environ
    source = EnvironmentApiKeySource(env)
    try:
        config = TwilioElevenLabsConfig.from_environment(collie_id=collie_id, environ=env)
    except ConfigurationUnavailable:
        return {
            "configured": False,
            "status": "not_configured",
            "provider": "twilio",
            "voice_provider": "elevenlabs",
            "runtime": "elevenlabs_native_twilio",
            "credential_source": "environment",
            "provider_probe": "not_performed",
        }
    if not source.configured():
        result = config.projection()
        result.update({
            "configured": False, "status": "credential_missing",
            "credential_source": "environment", "provider_probe": "not_performed",
        })
        return result
    result = config.projection()
    result.update({
        "configured": True, "status": "configured_unprobed",
        "credential_source": "environment", "provider_probe": "not_performed",
    })
    return result


__all__ = [
    "CAPABILITY_ID", "ENDPOINT", "ApiKeySource", "ConfigurationUnavailable",
    "EnvironmentApiKeySource", "JsonTransport", "ProviderHttpResponse",
    "ProviderRejected", "ProviderSubmissionUncertain", "TwilioElevenLabsConfig",
    "TwilioElevenLabsError", "TwilioElevenLabsOutbound", "UrllibJsonTransport",
    "VaultApiKeySource", "environment_configuration_status",
]
