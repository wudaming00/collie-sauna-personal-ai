import io
import json
import struct
import urllib.error

import pytest

from harness import tts_fish as fish


API_KEY = "fish-secret-value-that-must-not-leak"
REFERENCE_ID = "reference_private_123456"


def wav(*, sample_rate=44_100, data=b"\x00\x00"):
    fmt = struct.pack("<HHIIHH", 1, 1, sample_rate, sample_rate * 2, 2, 16)
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    chunks += b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(chunks) + 4) + b"WAVE" + chunks


def streaming_wav(*, sample_rate=44_100, data=b"\x00\x00"):
    result = bytearray(wav(sample_rate=sample_rate, data=data))
    struct.pack_into("<I", result, 4, 0xFFFFFF24)
    data_at = result.index(b"data")
    struct.pack_into("<I", result, data_at + 4, 0xFFFFFF00)
    return bytes(result)


class FakeSource:
    label = "fake_vault"

    def __init__(self, configured=True):
        self.ready = configured
        self.after = None

    def configured(self):
        return self.ready

    def use(self, consumer):
        secret = bytearray(API_KEY.encode())
        try:
            return consumer(secret)
        finally:
            secret[:] = b"\x00" * len(secret)
            self.after = bytes(secret)


class FakeTransport:
    def __init__(self, response=None, error=None):
        self.response = response or fish.ProviderHttpResponse(
            200, "audio/wav; charset=binary", wav())
        self.error = error
        self.calls = []

    def post_json(self, url, *, headers, payload, timeout, max_response_bytes):
        self.calls.append({
            "url": url, "headers": dict(headers), "payload": payload,
            "timeout": timeout, "max_response_bytes": max_response_bytes,
        })
        if self.error:
            raise self.error
        return self.response


def adapter(*, response=None, source=None, config=None, transport=None):
    return fish.FishTts(
        api_key=source or FakeSource(), config=config,
        transport=transport or FakeTransport(response=response))


def test_generic_preview_uses_quality_profile_utf8_and_one_request():
    transport = FakeTransport()
    source = FakeSource()
    client = adapter(source=source, transport=transport)
    result = client.synthesize(fish.FishTtsRequest("你好，我是 Collie。"))

    assert result.data == wav()
    assert result.sample_rate == 44_100
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == fish.ENDPOINT
    assert call["headers"] == {
        "Authorization": "Bearer " + API_KEY,
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "audio/wav",
        "model": "s2.1-pro-free",
    }
    assert call["payload"] == {
        "text": "你好，我是 Collie。", "format": "wav",
        "sample_rate": 44_100, "latency": "normal", "normalize": True,
        "temperature": 0.7, "top_p": 0.7,
        "prosody": {
            "speed": 0.98, "volume": 0, "normalize_loudness": True,
        },
        "chunk_length": 200, "max_new_tokens": 1024,
        "repetition_penalty": 1.2, "condition_on_previous_chunks": True,
    }
    encoded = json.dumps(
        call["payload"], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert "你好".encode("utf-8") in encoded
    assert source.after == b"\x00" * len(API_KEY)


def test_private_reference_is_sent_but_never_in_dataclass_repr():
    transport = FakeTransport()
    request = fish.FishTtsRequest("请自然地说普通话。", REFERENCE_ID)
    client = adapter(transport=transport)
    client.synthesize(request)
    assert transport.calls[0]["payload"]["reference_id"] == REFERENCE_ID
    assert REFERENCE_ID not in repr(request)


def test_streaming_wav_placeholders_are_finalized_before_output():
    raw = streaming_wav(data=b"\x01\x02\x03\x04")
    response = fish.ProviderHttpResponse(200, "audio/wav", raw)
    result = adapter(response=response).synthesize(fish.FishTtsRequest("你好"))

    assert struct.unpack_from("<I", result.data, 4)[0] == len(result.data) - 8
    data_at = result.data.index(b"data")
    assert struct.unpack_from("<I", result.data, data_at + 4)[0] == 4
    assert result.data[-4:] == b"\x01\x02\x03\x04"


def test_unknown_streaming_placeholder_is_rejected():
    raw = bytearray(streaming_wav())
    struct.pack_into("<I", raw, 4, 0xFFFFFF23)
    response = fish.ProviderHttpResponse(200, "audio/wav", bytes(raw))
    with pytest.raises(fish.InvalidAudioResponse, match="incomplete WAV"):
        adapter(response=response).synthesize(fish.FishTtsRequest("你好"))


def test_vault_source_binds_opaque_reference_and_repr_hides_it():
    class FakeVault:
        def __init__(self):
            self.bound = None

        def use(self, ref, *, collie_id, account, kind, consumer):
            self.bound = (ref, collie_id, account, kind)
            value = bytearray(API_KEY.encode())
            try:
                return consumer(value)
            finally:
                value[:] = b"\x00" * len(value)

    vault = FakeVault()
    opaque = "cv1_" + "a" * 32
    source = fish.VaultApiKeySource(vault, opaque, collie_id="rowan-device")
    assert source.use(lambda value: bytes(value).decode()) == API_KEY
    assert vault.bound == (
        opaque, "rowan-device", "voice.fish_audio", "fish_audio_api_key")
    assert opaque not in repr(source)
    assert API_KEY not in repr(source)


@pytest.mark.parametrize("factory", [
    lambda: fish.FishTtsRequest(""),
    lambda: fish.FishTtsRequest("valid", "bad ref"),
])
def test_request_validation_fails_before_transport(factory):
    with pytest.raises(ValueError):
        factory()


def test_unconfigured_source_fails_without_transport():
    transport = FakeTransport()
    client = adapter(source=FakeSource(False), transport=transport)
    with pytest.raises(fish.ConfigurationUnavailable, match="not configured"):
        client.synthesize(fish.FishTtsRequest("你好"))
    assert transport.calls == []


@pytest.mark.parametrize("response", [
    fish.ProviderHttpResponse(200, "application/json", b'{"secret":"echo"}'),
    fish.ProviderHttpResponse(200, "audio/wav", b"not a wave"),
    fish.ProviderHttpResponse(200, "audio/wav", wav(sample_rate=8_000)),
])
def test_response_must_be_allowlisted_complete_44100_wav(response):
    with pytest.raises(fish.InvalidAudioResponse) as caught:
        adapter(response=response).synthesize(fish.FishTtsRequest("你好"))
    assert "echo" not in str(caught.value)
    assert API_KEY not in str(caught.value)


def test_provider_error_does_not_leak_body_text_reference_or_key_and_does_not_retry():
    body = (API_KEY + REFERENCE_ID + "用户文本").encode()
    transport = FakeTransport(fish.ProviderHttpResponse(401, "application/json", body))
    client = adapter(transport=transport)
    with pytest.raises(fish.ProviderRejected) as caught:
        client.synthesize(fish.FishTtsRequest("用户文本", REFERENCE_ID))
    rendered = str(caught.value)
    assert API_KEY not in rendered
    assert REFERENCE_ID not in rendered
    assert "用户文本" not in rendered
    assert len(transport.calls) == 1


def test_transport_failure_is_not_retried():
    transport = FakeTransport(error=fish.ProviderTransportError("Fish Audio request failed"))
    with pytest.raises(fish.ProviderTransportError):
        adapter(transport=transport).synthesize(fish.FishTtsRequest("你好"))
    assert len(transport.calls) == 1


def test_default_transport_posts_exact_utf8_json_and_refuses_redirect():
    class CapturingOpener:
        def __init__(self):
            self.requests = []

        def open(self, request, timeout):
            self.requests.append((request, timeout))
            raise urllib.error.HTTPError(
                request.full_url, 302, "redirect",
                {"Location": "https://evil.invalid/steal", "Content-Type": "text/html"},
                io.BytesIO((API_KEY + REFERENCE_ID).encode()))

    opener = CapturingOpener()
    transport = fish.UrllibFishTransport(opener=opener)
    response = transport.post_json(
        fish.ENDPOINT,
        headers={"Authorization": "Bearer " + API_KEY,
                 "Content-Type": "application/json; charset=utf-8"},
        payload={"text": "普通话", "reference_id": REFERENCE_ID},
        timeout=7, max_response_bytes=1024)
    assert response.status == 302 and response.body == b""
    assert len(opener.requests) == 1
    request, timeout = opener.requests[0]
    assert request.full_url == fish.ENDPOINT and timeout == 7
    assert "普通话".encode("utf-8") in request.data
    assert fish._RejectRedirects().redirect_request(
        request, None, 302, "redirect", {}, "https://evil.invalid/steal") is None


def test_default_transport_enforces_response_size_before_adapter_parsing():
    class Response:
        status = 200
        headers = {"Content-Type": "audio/wav"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size):
            return b"x" * size

    class Opener:
        def open(self, request, timeout):
            return Response()

    transport = fish.UrllibFishTransport(opener=Opener())
    with pytest.raises(fish.InvalidAudioResponse, match="safe size limit"):
        transport.post_json(
            fish.ENDPOINT, headers={}, payload={"text": "x"}, timeout=1,
            max_response_bytes=64)


def test_status_and_result_repr_are_secret_free():
    client = adapter()
    status = client.status()
    assert status["configured"] is True
    assert status["model"] == "s2.1-pro-free"
    assert API_KEY not in json.dumps(status)
    result = client.synthesize(fish.FishTtsRequest("你好"))
    assert repr(result) == (
        "FishAudio(content_type='audio/wav', sample_rate=44100, format='wav')")
