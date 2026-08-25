"""Small, fail-closed Fish Audio text-to-speech adapter.

The adapter performs one HTTPS request and returns verified 44.1 kHz WAV
bytes.  Credentials are consumed from an explicit host-side source; the
production source binds an opaque reference to :class:`IdentityVault` so the
API key never becomes configuration data or model-visible state.
"""
from __future__ import annotations

import json
import re
import socket
import struct
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol

from .identityvault import IdentityVault, VaultError


ENDPOINT = "https://api.fish.audio/v1/tts"
DEFAULT_MODEL = "s2.1-pro-free"
DEFAULT_SAMPLE_RATE = 44_100
DEFAULT_MAX_RESPONSE_BYTES = 32 * 1024 * 1024

_VAULT_REF = re.compile(r"^cv1_[A-Za-z0-9_-]{24,96}$")
_REFERENCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{5,191}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,79}$")
_WAV_CONTENT_TYPES = frozenset({
    "audio/wav", "audio/wave", "audio/x-wav", "audio/vnd.wave",
    # Fish may serve generated binary output with this conservative generic
    # type.  The bytes still have to pass the full WAV structure check below.
    "application/octet-stream",
})


class FishTtsError(RuntimeError):
    """Base error whose message is safe to show or persist."""


class ConfigurationUnavailable(FishTtsError):
    pass


class ProviderRejected(FishTtsError):
    pass


class ProviderTransportError(FishTtsError):
    pass


class InvalidAudioResponse(FishTtsError):
    pass


class ApiKeySource(Protocol):
    label: str

    def configured(self) -> bool: ...
    def use(self, consumer: Callable[[bytearray], object]): ...


@dataclass(slots=True)
class VaultApiKeySource:
    """Fish API-key source backed by an opaque ``IdentityVault`` reference."""

    vault: IdentityVault = field(repr=False)
    ref: str = field(repr=False)
    collie_id: str
    account: str = "voice.fish_audio"
    kind: str = "fish_audio_api_key"
    label: str = field(default="native_os_vault", init=False)

    def __post_init__(self) -> None:
        if not _VAULT_REF.fullmatch(str(self.ref or "")):
            raise ValueError("invalid Fish Audio credential reference")

    def configured(self) -> bool:
        # The opaque binding is configuration evidence.  Access still fails
        # closed later if the OS vault is unavailable or the item is missing.
        return True

    def use(self, consumer: Callable[[bytearray], object]):
        return self.vault.use(
            self.ref, collie_id=self.collie_id, account=self.account,
            kind=self.kind, consumer=consumer)


@dataclass(frozen=True, slots=True)
class FishTtsConfig:
    """Fixed quality-first output profile for preview generation."""

    model: str = DEFAULT_MODEL
    request_timeout_seconds: float = 60.0
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        model = str(self.model or "").strip()
        if not _MODEL.fullmatch(model):
            raise ValueError("invalid Fish Audio model")
        timeout = float(self.request_timeout_seconds)
        if timeout < 1 or timeout > 120:
            raise ValueError("request timeout must be between 1 and 120 seconds")
        maximum = int(self.max_response_bytes)
        if maximum < 44 or maximum > 128 * 1024 * 1024:
            raise ValueError("response limit must be between 44 bytes and 128 MiB")
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "request_timeout_seconds", timeout)
        object.__setattr__(self, "max_response_bytes", maximum)


@dataclass(frozen=True, slots=True)
class FishTtsRequest:
    """One preview request; ``reference_id`` is deliberately non-printing."""

    text: str
    reference_id: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        text = str(self.text or "").strip()
        if not text or len(text) > 10_000 or "\x00" in text:
            raise ValueError("text must be non-empty and at most 10,000 characters")
        reference = None if self.reference_id is None else str(self.reference_id).strip()
        if reference is not None and not _REFERENCE_ID.fullmatch(reference):
            raise ValueError("invalid Fish Audio reference id")
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "reference_id", reference)


@dataclass(frozen=True, slots=True)
class FishAudio:
    """Verified provider output."""

    data: bytes = field(repr=False)
    content_type: str
    sample_rate: int = DEFAULT_SAMPLE_RATE
    format: str = "wav"


@dataclass(frozen=True, slots=True)
class ProviderHttpResponse:
    status: int
    content_type: str
    body: bytes = field(repr=False)


class FishTransport(Protocol):
    def post_json(self, url: str, *, headers: Mapping[str, str], payload: dict,
                  timeout: float, max_response_bytes: int) -> ProviderHttpResponse: ...


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Never forward the bearer credential to a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibFishTransport:
    """One-shot stdlib HTTPS transport with a bounded response."""

    _MAX_ERROR_BYTES = 64 * 1024

    def __init__(self, *, opener=None):
        self._opener = opener or urllib.request.build_opener(_RejectRedirects())

    def post_json(self, url: str, *, headers: Mapping[str, str], payload: dict,
                  timeout: float, max_response_bytes: int) -> ProviderHttpResponse:
        if url != ENDPOINT:
            raise ConfigurationUnavailable("provider endpoint is not allowlisted")
        request = urllib.request.Request(
            url,
            data=json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers=dict(headers), method="POST")
        try:
            with self._opener.open(request, timeout=timeout) as response:
                body = response.read(max_response_bytes + 1)
                if len(body) > max_response_bytes:
                    raise InvalidAudioResponse("provider audio exceeded the safe size limit")
                content_type = str(response.headers.get("Content-Type") or "")
                return ProviderHttpResponse(int(response.status), content_type, body)
        except urllib.error.HTTPError as exc:
            # Read only a bounded amount, then discard it.  Provider bodies can
            # echo submitted text or diagnostics and must never reach errors.
            exc.read(self._MAX_ERROR_BYTES + 1)
            return ProviderHttpResponse(
                int(exc.code), str(exc.headers.get("Content-Type") or ""), b"")
        except InvalidAudioResponse:
            raise
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            raise ProviderTransportError("Fish Audio request failed") from None


_STREAMING_RIFF_SIZES = frozenset({0xFFFFFFFF, 0xFFFFFF24})
_STREAMING_DATA_SIZES = frozenset({0xFFFFFFFF, 0xFFFFFF00})


def _normalized_wav(body: bytes) -> tuple[bytes, int]:
    """Validate WAV bytes and finalize Fish's non-seekable RIFF header.

    Fish streams WAV responses without seeking back over the header.  Its
    encoder therefore uses the standard FFmpeg-style ``0xffffff24`` RIFF and
    ``0xffffff00`` data-size placeholders.  Accept only those known sentinel
    values (or ``0xffffffff``), then replace them with the bounded response's
    actual sizes before the bytes reach disk or a media player.
    """

    if len(body) < 44 or body[:4] != b"RIFF" or body[8:12] != b"WAVE":
        raise InvalidAudioResponse("Fish Audio returned invalid WAV data")
    riff_size = struct.unpack_from("<I", body, 4)[0]
    streaming = riff_size in _STREAMING_RIFF_SIZES
    if not streaming and riff_size + 8 != len(body):
        raise InvalidAudioResponse("Fish Audio returned an incomplete WAV file")
    normalized = bytearray(body) if streaming else None
    if normalized is not None:
        struct.pack_into("<I", normalized, 4, len(body) - 8)

    position = 12
    sample_rate = None
    has_data = False
    while position + 8 <= len(body):
        chunk_id = body[position:position + 4]
        chunk_size = struct.unpack_from("<I", body, position + 4)[0]
        data_start = position + 8
        if chunk_id == b"data" and chunk_size in _STREAMING_DATA_SIZES:
            if not streaming:
                raise InvalidAudioResponse("Fish Audio returned malformed WAV chunks")
            chunk_size = len(body) - data_start
            if chunk_size <= 0:
                raise InvalidAudioResponse("Fish Audio returned empty WAV data")
            struct.pack_into("<I", normalized, position + 4, chunk_size)
        data_end = data_start + chunk_size
        if data_end > len(body):
            raise InvalidAudioResponse("Fish Audio returned malformed WAV chunks")
        if chunk_id == b"fmt ":
            if chunk_size < 16:
                raise InvalidAudioResponse("Fish Audio returned an invalid WAV format chunk")
            sample_rate = struct.unpack_from("<I", body, data_start + 4)[0]
        elif chunk_id == b"data":
            has_data = True
        position = data_end + (chunk_size & 1)

    if position != len(body) or sample_rate is None or not has_data:
        raise InvalidAudioResponse("Fish Audio returned incomplete WAV chunks")
    return (bytes(normalized) if normalized is not None else body), sample_rate


def _validated_wav_sample_rate(body: bytes) -> int:
    """Compatibility helper for callers that only need the sample rate."""

    return _normalized_wav(body)[1]


class FishTts:
    """Generate one generic or reference-voice Fish Audio preview."""

    def __init__(self, *, api_key: ApiKeySource,
                 config: FishTtsConfig | None = None,
                 transport: FishTransport | None = None):
        self.api_key = api_key
        self.config = config or FishTtsConfig()
        self.transport = transport or UrllibFishTransport()

    def status(self) -> dict:
        ready = bool(self.api_key.configured())
        return {
            "provider": "fish_audio",
            "configured": ready,
            "status": "configured_unprobed" if ready else "credential_missing",
            "credential_source": str(getattr(self.api_key, "label", "host_secret_source")),
            "provider_probe": "not_performed",
            "model": self.config.model,
            "format": "wav",
            "sample_rate": DEFAULT_SAMPLE_RATE,
            "latency": "normal",
        }

    def synthesize(self, request: FishTtsRequest) -> FishAudio:
        if not isinstance(request, FishTtsRequest):
            raise TypeError("request must be FishTtsRequest")
        if not self.api_key.configured():
            raise ConfigurationUnavailable("Fish Audio API credential is not configured")
        try:
            return self.api_key.use(
                lambda secret: self._synthesize_with_secret(request, secret))
        except VaultError:
            raise ConfigurationUnavailable(
                "Fish Audio API credential is unavailable") from None

    def _synthesize_with_secret(self, request: FishTtsRequest,
                                secret: bytearray) -> FishAudio:
        try:
            token = bytes(secret).decode("utf-8")
        except UnicodeDecodeError:
            raise ConfigurationUnavailable("Fish Audio API credential is invalid") from None
        if not token or len(token) > 4096 or any(char in token for char in "\r\n\x00"):
            raise ConfigurationUnavailable("Fish Audio API credential is invalid")

        payload = {
            "text": request.text,
            "format": "wav",
            "sample_rate": DEFAULT_SAMPLE_RATE,
            "latency": "normal",
            "normalize": True,
            # Fish's documented quality-first profile.  Keep these explicit so
            # a provider-side default change cannot silently alter previews.
            "temperature": 0.7,
            "top_p": 0.7,
            "prosody": {
                "speed": 0.98,
                "volume": 0,
                "normalize_loudness": True,
            },
            "chunk_length": 200,
            "max_new_tokens": 1024,
            "repetition_penalty": 1.2,
            "condition_on_previous_chunks": True,
        }
        if request.reference_id is not None:
            payload["reference_id"] = request.reference_id
        response = self.transport.post_json(
            ENDPOINT,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "audio/wav",
                "model": self.config.model,
            },
            payload=payload,
            timeout=self.config.request_timeout_seconds,
            max_response_bytes=self.config.max_response_bytes,
        )
        if response.status != 200:
            raise ProviderRejected(
                "Fish Audio rejected the request (HTTP %d)" % response.status)
        content_type = response.content_type.split(";", 1)[0].strip().lower()
        if content_type not in _WAV_CONTENT_TYPES:
            raise InvalidAudioResponse("Fish Audio returned an unexpected content type")
        normalized, sample_rate = _normalized_wav(response.body)
        if sample_rate != DEFAULT_SAMPLE_RATE:
            raise InvalidAudioResponse("Fish Audio returned an unexpected WAV sample rate")
        return FishAudio(normalized, content_type, sample_rate)


__all__ = [
    "DEFAULT_MAX_RESPONSE_BYTES", "DEFAULT_MODEL", "DEFAULT_SAMPLE_RATE",
    "ENDPOINT", "ApiKeySource", "ConfigurationUnavailable", "FishAudio",
    "FishTransport", "FishTts", "FishTtsConfig", "FishTtsError",
    "FishTtsRequest", "InvalidAudioResponse", "ProviderHttpResponse",
    "ProviderRejected", "ProviderTransportError", "UrllibFishTransport",
    "VaultApiKeySource",
]
