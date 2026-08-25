"""Provider protocol and transport: what every backend must do with a request,
a tool call, a stream and a failure — before the loop ever sees it.

Split out of test_core.py — a pure move; no assertion was changed. Stdlib-only, no Opus, fast.
    python tests/test_providers.py     (exit 0 = all pass)
"""
import inspect, io, json, os, re, sys, tempfile, time, types, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _util import _ctx, _Skip, _RecordingMemory, _ScriptProvider, run_module  # noqa: E402,F401

import contextlib
import inspect, io, json, os, re, sys, tempfile, time, types, warnings

# ------------------------------------------------------------------ providers._to_anthropic
def test_to_anthropic_robustness():
    from harness.providers import AnthropicProvider, ToolCall
    ap = AnthropicProvider.__new__(AnthropicProvider)
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": ""},                                   # empty -> coerce
        {"role": "assistant", "tool_calls": [ToolCall("a", "grep", {"p": 1})]}, # ToolCall
        {"role": "assistant", "tool_calls": [{"id": "b", "name": "read", "args": {}}]},  # dict
        {"role": "assistant", "tool_calls": ["ToolCall(id='c'...)"]},           # legacy str -> skip
    ]
    out = ap._to_anthropic(msgs)
    empt = [m for m in out if m["role"] == "assistant" and isinstance(m["content"], str) and not m["content"].strip()]
    assert not empt, "no empty assistant text block allowed"
    ids = [b["id"] for m in out for b in (m["content"] if isinstance(m["content"], list) else []) if b.get("type") == "tool_use"]
    assert "a" in ids and "b" in ids, "ToolCall + dict tool_calls must both yield tool_use ids"


# ------------------------------------------------------------------ history cache breakpoint (caching fix)
def test_history_cache_breakpoint_placement():
    """The rolling history breakpoint lands on the last message of the byte-stable ELIDED prefix
    (stable_upto-1), promoting string content to a block so cache_control has somewhere to live; a
    single breakpoint total; no-op on empty."""
    from harness.providers import _apply_history_cache, _mark_cache_block

    def n_bp(msgs):
        return sum(1 for m in msgs for b in (m["content"] if isinstance(m["content"], list) else [])
                   if isinstance(b, dict) and b.get("cache_control"))

    # stable_upto=3 (elide boundary) -> breakpoint on index 2's last block
    msgs = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": str(i), "content": "x"}]}
            for i in range(6)]
    _apply_history_cache(msgs, 3)
    assert n_bp(msgs) == 1, "exactly one history breakpoint"
    assert msgs[2]["content"][-1].get("cache_control"), "breakpoint must sit at stable_upto-1"
    assert not msgs[5]["content"][-1].get("cache_control"), "the volatile tail must NOT be marked"

    # short/un-elided thread (stable_upto<=0) -> mark the final message; string content is promoted
    m2 = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "there"}]
    _apply_history_cache(m2, 0)
    assert isinstance(m2[-1]["content"], list) and m2[-1]["content"][-1]["cache_control"], "final marked"
    assert m2[-1]["content"][-1]["text"] == "there", "promoted block keeps the text"
    assert n_bp(m2) == 1

    _apply_history_cache([], 5)             # empty: must not raise
    # out-of-range stable_upto is clamped, never indexes past the end
    m3 = [{"role": "user", "content": [{"type": "text", "text": "a"}]}]
    _apply_history_cache(m3, 99)
    assert m3[0]["content"][-1]["cache_control"]

def test_history_cache_does_not_mutate_source_blocks():
    """_mark_cache_block copies — the original message dict/blocks must be untouched so the session
    thread and any retry see clean content (no leaked cache_control)."""
    from harness.providers import _mark_cache_block
    orig = {"role": "user", "content": [{"type": "text", "text": "a"}]}
    marked = _mark_cache_block(orig)
    assert "cache_control" in marked["content"][-1]
    assert "cache_control" not in orig["content"][-1], "source block must not be mutated"

# ------------------------------------------------------------------ every provider tolerates dict tool_calls
def test_all_providers_toolcall_tolerant():
    # the ToolCall-serialization crash existed in _to_anthropic AND _to_openai/_to_ollama/claude-cli.
    # A continued session (esp. deepseek/openai-compat, used by SWE+compare) must not crash on a
    # dict-shaped tool_call in history.
    from harness.providers import (AnthropicProvider, OllamaProvider, OpenAICompatProvider,
                                   ClaudeCliProvider, ToolCall)
    msgs = [{"role": "user", "content": "hi"},
            {"role": "assistant", "tool_calls": [{"id": "d1", "name": "grep", "args": {"p": "x"}}]},  # dict
            {"role": "assistant", "tool_calls": [ToolCall("t2", "read", {"path": "/y"})]}]            # dataclass
    an = AnthropicProvider.__new__(AnthropicProvider)._to_anthropic(msgs)
    ol = OllamaProvider.__new__(OllamaProvider)._to_ollama("sys", msgs)
    oa = OpenAICompatProvider.__new__(OpenAICompatProvider)._to_openai("sys", msgs)
    # each must reference the dict tool_call's name without raising
    assert any("grep" in json.dumps(x) for x in an), "anthropic dropped dict tool_call"
    assert any("grep" in json.dumps(x) for x in ol), "ollama dropped dict tool_call"
    assert any("grep" in json.dumps(x) for x in oa), "openai dropped dict tool_call"
    assert '"id": "d1"' in json.dumps(oa) or "'id': 'd1'" in str(oa), "openai must keep dict tool_call id"

# ------------------------------------------------------------------ provider error paths degrade gracefully
def test_provider_error_paths():
    import io, urllib.error
    from unittest.mock import patch
    from harness.providers import OllamaProvider, OpenAICompatProvider, AnthropicProvider
    def he(code): return urllib.error.HTTPError("u", code, "e", {}, io.BytesIO(b'{"error":"boom"}'))
    class _R:
        def __init__(s, d): s._d = d if isinstance(d, bytes) else json.dumps(d).encode()
        def read(s): return s._d
        def __enter__(s): return s
        def __exit__(s, *a): pass
    msgs = [{"role": "user", "content": "hi"}]

    # ollama + openai-compat must return stop_reason='error' (NOT treat the error as the answer)
    ol = OllamaProvider.__new__(OllamaProvider); ol._model = "m"; ol.url = "http://x"
    with patch("urllib.request.urlopen", side_effect=he(500)):
        assert ol.complete("s", msgs, []).stop_reason == "error"
    os.environ["_COLLIE_TEST_KEY"] = "dummy"
    oa = OpenAICompatProvider("http://x", "_COLLIE_TEST_KEY", "m", name="deepseek")
    with patch("harness.providers._credential_open", side_effect=he(500)):
        assert oa.complete("s", msgs, []).stop_reason == "error"
    with patch("harness.providers._credential_open", lambda *a, **k: _R(b"not-json{{")):
        assert oa.complete("s", msgs, []).stop_reason == "error"

    # anthropic now RETURNS an error completion (point 4 contract flip: never raise for transport) —
    # carrying error_status so the host retry classifier can act, and never a normal answer.
    an = AnthropicProvider.__new__(AnthropicProvider); an.name = "anthropic"; an.model = "m"
    an.api_key = "k"; an.max_tokens = 100; an.API = "http://x"
    with patch("harness.providers._credential_open", side_effect=he(429)):
        c = an.complete("s", msgs, [])
    assert c.stop_reason == "error" and c.error_status == 429, "anthropic HTTP error -> error completion"
    assert c.text.startswith("ERROR("), "error body must not read as a normal answer"
    # Individual usage counters may be omitted, but the terminal envelope itself is explicit.
    with patch("harness.providers._credential_open", lambda *a, **k: _R({
            "type": "message", "content": [{"type": "text", "text": "ok"}],
            "usage": {}, "stop_reason": "end_turn"})):
        r = an.complete("s", msgs, [])
        assert r.text == "ok" and r.stop_reason != "error"


def test_anthropic_nonstream_malformed_responses_are_errors_as_data():
    from unittest.mock import patch
    from harness.providers import AnthropicProvider

    class _R:
        def __init__(self, value): self.value = json.dumps(value).encode()
        def read(self): return self.value
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.name, provider.model = "anthropic", "model"
    provider.api_key, provider.max_tokens, provider.API = "test-key", 100, "https://example.test"
    messages = [{"role": "user", "content": "hi"}]
    malformed = (
        [],
        {},
        {"type": "error", "content": []},
        {"content": {}},
        {"content": [None]},
        {"content": [{"type": "text"}]},
        {"content": [{"type": "text", "text": []}]},
        {"content": [{"type": "unknown"}]},
        {"content": [{"type": "tool_use", "id": 1, "name": "run", "input": {}}]},
        {"content": [{"type": "tool_use", "id": "call", "name": "", "input": {}}]},
        {"content": [{"type": "tool_use", "id": "call", "name": "run", "input": []}]},
        {"content": [], "stop_reason": []},
        {"content": [], "usage": []},
        {"content": [], "usage": {"input_tokens": True}},
        {"content": [], "usage": {"output_tokens": -1}},
        {"content": [], "usage": {"cache_read_input_tokens": "3"}},
        {"content": [], "usage": {"cache_creation_input_tokens": None}},
    )
    for response in malformed:
        with patch("harness.providers._credential_open", return_value=_R(response)):
            completion = provider.complete("system", messages, [])
        assert completion.stop_reason == "error", response
        assert completion.text.startswith("ERROR(anthropic)"), response

    terminal_contracts = (
        ({"content": [], "usage": {}, "stop_reason": "end_turn"}, "type must be message"),
        ({"type": "message", "content": [], "stop_reason": "end_turn"}, "usage object"),
        ({"type": "message", "content": [], "usage": {}}, "include stop_reason"),
        ({"type": "message", "content": [], "usage": {}, "stop_reason": ""},
         "non-empty string"),
        ({"type": "message", "content": [{
            "type": "tool_use", "id": "tool-1", "name": "grep", "input": {}}],
          "usage": {}, "stop_reason": "end_turn"}, "inconsistent with tool_use"),
        ({"type": "message", "content": [{"type": "text", "text": "done"}],
          "usage": {}, "stop_reason": "tool_use"}, "inconsistent with tool_use"),
    )
    for response, expected_detail in terminal_contracts:
        with patch("harness.providers._credential_open", return_value=_R(response)):
            completion = provider.complete("system", messages, [])
        assert completion.stop_reason == "error", response
        assert expected_detail in completion.error_detail, (response, completion.error_detail)

    # Anthropic reports uncached input separately from cache hits. A large cache hit with a small
    # uncached suffix is valid and must not inherit OpenAI's cached<=prompt subset constraint.
    valid_cached = {
        "type": "message",
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 2, "output_tokens": 1,
                  "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0},
        "stop_reason": "end_turn",
    }
    with patch("harness.providers._credential_open", return_value=_R(valid_cached)):
        completion = provider.complete("system", messages, [])
    assert completion.stop_reason == "end_turn"
    assert completion.usage.input_tokens == 2 and completion.usage.cache_read == 100

    length_with_tool = {
        "type": "message",
        "content": [{"type": "tool_use", "id": "tool-2", "name": "edit_file",
                     "input": {"path": "x.py"}}],
        "usage": {"input_tokens": 3, "output_tokens": 100},
        "stop_reason": "max_tokens",
    }
    with patch("harness.providers._credential_open", return_value=_R(length_with_tool)):
        completion = provider.complete("system", messages, [])
    assert completion.stop_reason == "length"
    assert len(completion.tool_calls) == 1
    assert completion.tool_calls[0].name == "edit_file"

    # A length-stopped non-stream tool block may not contain a complete input
    # object. Keep the length boundary, but expose nothing executable.
    length_with_partial_tool = {
        **length_with_tool,
        "content": [{"type": "tool_use", "id": "tool-3", "name": "edit_file",
                     "input": "{\"path\":"}],
    }
    with patch("harness.providers._credential_open",
               return_value=_R(length_with_partial_tool)):
        completion = provider.complete("system", messages, [])
    assert completion.stop_reason == "length" and completion.tool_calls == []


def test_openai_compat_factory_and_malformed_contract(monkeypatch):
    from unittest.mock import patch
    from harness.providers import OllamaProvider, OpenAICompatProvider, make_provider

    monkeypatch.setenv("COLLIE_OPENAI_COMPAT_BASE", "https://models.example.test/v1")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "synthetic-test-key")
    provider = make_provider("openai-compat", "custom-model")
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.base == "https://models.example.test/v1/chat/completions"

    class _R:
        def __init__(self, value): self.value = json.dumps(value).encode()
        def read(self): return self.value
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    for malformed in (
            [], {}, {"choices": []}, {"choices": [{"message": []}]},
            {"choices": [{"message": {}}], "usage": []},
            {"choices": [{"message": {"content": []}}]},
            {"choices": [{"message": {}, "finish_reason": []}]},
             {"choices": [{"message": {}}],
              "usage": {"prompt_tokens_details": []}},
            {"choices": [{"message": {}}],
             "usage": {"prompt_tokens": 2,
                       "prompt_tokens_details": {"cached_tokens": 3}}},
            {"choices": [{"message": {"tool_calls": [
                {"function": {"name": "run", "arguments": []}}]}}]},
            {"choices": [{"message": {"tool_calls": [
                {"function": {"name": "", "arguments": {}}}]}}]}):
        with patch("harness.providers._credential_open", return_value=_R(malformed)):
            completion = provider.complete(
                "system", [{"role": "user", "content": "hi"}], [])
        assert completion.stop_reason == "error", malformed

    ollama = OllamaProvider.__new__(OllamaProvider)
    ollama.name, ollama._model, ollama.url = "ollama", "local", "http://127.0.0.1/api/chat"
    for malformed in (
            {"message": {"content": []}},
            {"message": {"tool_calls": {}}},
            {"message": {"tool_calls": [
                {"function": {"name": "run", "arguments": []}}]}},
            {"message": {"content": ""}, "prompt_eval_count": "3"}):
        with patch("urllib.request.urlopen", return_value=_R(malformed)):
            completion = ollama.complete(
                "system", [{"role": "user", "content": "hi"}], [])
        assert completion.stop_reason == "error", malformed

    monkeypatch.setenv("COLLIE_OPENAI_COMPAT_BASE", "http://remote.example.test/v1")
    import pytest
    with pytest.raises(ValueError, match="use https"):
        make_provider("openai-compat", "custom-model")
    for invalid_base in ("https:///v1", "https://models.example.test/v1?tenant=other"):
        monkeypatch.setenv("COLLIE_OPENAI_COMPAT_BASE", invalid_base)
        with pytest.raises(ValueError, match="query-free"):
            make_provider("openai-compat", "custom-model")

    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-test-key")
    with patch.dict("harness.providers.OPENAI_COMPAT_PRESETS", {
            "deepseek": ("http://remote.example.test/v1", "DEEPSEEK_API_KEY", "model")}):
        with pytest.raises(ValueError, match="must use https"):
            make_provider("deepseek", "model")

def test_classify_error_matrix():
    from harness.providers import classify_error
    assert classify_error("overloaded_error", 529) == "retryable"
    assert classify_error("", 503) == "retryable"
    assert classify_error("insufficient_quota", 429) == "exhausted", "quota beats retryable status"
    assert classify_error("Insufficient Balance", 402) == "terminal"
    assert classify_error("prompt is too long: 213462 tokens > 200000 maximum", 400) == "overflow"
    assert classify_error("request_too_large", 413) == "overflow"
    assert classify_error("ThrottlingException: Too many tokens, please wait", 0) == "retryable", "throttle != overflow"
    assert classify_error("timed out", 0) == "retryable"
    assert classify_error("something novel", 0) == "terminal", "unknown fails fast"


# The 429 a flat plan sends when it is spent, verbatim from a real ChatGPT-plan refusal.
_SPENT = ('{"error":{"type":"usage_limit_reached","message":"The usage limit has been reached",'
          '"plan_type":"pro","resets_at":1787196550,"resets_in_seconds":173470}}')


def test_a_spent_plan_is_not_a_rate_limit():
    """Both arrive as 429 and the remedy inverts: one waits seconds, the other waits days."""
    from harness.providers import classify_error
    assert classify_error(_SPENT, 429) == "exhausted"
    for transient in ("rate limit exceeded, please slow down", "Too Many Requests",
                      "overloaded_error"):
        assert classify_error(transient, 429) == "retryable", transient


def test_exhaustion_wordings_every_vendor_uses():
    from harness.providers import is_exhausted
    for t in ("usage limit reached", "You exceeded your current quota", "quota exhausted",
              "insufficient_quota", "credit balance is too low", "monthly limit reached"):
        assert is_exhausted(t), t
    for t in ("rate limit exceeded", "too many requests", "server error"):
        assert not is_exhausted(t), t


def test_an_unknown_provider_is_assumed_to_cost_money():
    """The one mistake this must never make is waving a metered provider through as free."""
    from harness.providers import provider_kind
    assert provider_kind("anthropic-oauth") == "subscription"
    assert provider_kind("codex-oauth") == "subscription"
    assert provider_kind("ollama") == "local"
    assert provider_kind("anthropic") == "metered", "an API key is metered despite the name"
    assert provider_kind("some-provider-invented-tomorrow") == "metered"
    assert provider_kind("") == "metered"


def test_fallbacks_never_offer_a_metered_provider():
    from harness.providers import subscription_fallbacks, provider_kind
    for current in ("codex-oauth", "anthropic-oauth", ""):
        alts = subscription_fallbacks(current)
        assert current not in alts
        assert all(provider_kind(a) == "subscription" for a in alts), alts


def test_exhausted_message_answers_which_plan_and_when():
    """The vendor envelope names a plan_type and never the provider, nor a readable reset."""
    from harness.providers import explain_exhausted
    msg = explain_exhausted("codex-oauth", _SPENT, 429)
    assert "codex-oauth" in msg
    assert "2026-" in msg, "the reset instant, not just resets_in_seconds"
    assert "429" in msg

def test_provider_error_contract_matrix():
    """Every provider, every transport failure -> stop_reason=='error', text startswith 'ERROR(',
    never raises (point 4). 4 providers x 4 fault kinds."""
    import io as _io, urllib.error
    from unittest.mock import patch
    from harness.providers import (AnthropicProvider, AnthropicOAuthProvider, OllamaProvider,
                                   OpenAICompatProvider)
    faults = [urllib.error.HTTPError("u", 429, "e", {}, _io.BytesIO(b'{"error":"x"}')),
              urllib.error.HTTPError("u", 500, "e", {}, _io.BytesIO(b'busy')),
              urllib.error.URLError("down"), TimeoutError("slow")]
    an = AnthropicProvider.__new__(AnthropicProvider); an.name = "anthropic"; an.model = "m"; an.api_key = "k"; an.max_tokens = 10; an.API = "http://x"
    oa = AnthropicOAuthProvider.__new__(AnthropicOAuthProvider); oa.name = "anthropic-oauth"; oa.model = "claude-opus-4-8"; oa.api_key = ""; oa.max_tokens = 10; oa.API = "http://x"
    ol = OllamaProvider.__new__(OllamaProvider); ol.name = "ollama"; ol._model = "m"; ol.url = "http://x"
    os.environ["_CT_KEY"] = "k"
    oc = OpenAICompatProvider("http://x", "_CT_KEY", "m", name="deepseek")
    provs = [an, ol, oc]
    # A real expired Claude Code credential on the developer's machine must not pre-empt this
    # transport-error matrix. Expiry has its own tests; this one owns the token state completely.
    with patch("harness.providers._read_oauth_token", lambda **_kwargs: "tok"), \
         patch("harness.providers.claude_oauth_expired", lambda *a, **k: False):
        provs.append(oa)
        for p in provs:
            for f in faults:
                target = ("urllib.request.urlopen" if p.name == "ollama" else
                          "harness.providers._credential_open")
                with patch(target, side_effect=f):
                    c = p.complete("s", [{"role": "user", "content": "hi"}], [])
                assert c.stop_reason == "error", "%s did not return error for %r" % (p.name, f)
                assert c.text.startswith("ERROR("), (p.name, c.text[:40])

def test_claude_cli_provider_fails_closed_on_process_and_envelope_errors():
    """A CLI launch/protocol failure must never become an empty successful end_turn."""
    import subprocess
    from unittest.mock import patch
    from harness.providers import ClaudeCliProvider

    failures = [
        subprocess.CompletedProcess(["claude"], 7, "", "authentication failed"),
        subprocess.CompletedProcess(["claude"], 0, "not JSON", ""),
        subprocess.CompletedProcess(["claude"], 0, json.dumps({
            "is_error": True, "result": "quota exhausted", "usage": {}}), ""),
        subprocess.CompletedProcess(["claude"], 0, json.dumps({"usage": {}}), ""),
    ]
    for process in failures:
        provider = ClaudeCliProvider("opus")
        with patch("harness.providers.shutil.which", return_value="C:/bin/claude.cmd"), \
             patch("harness.providers.subprocess.run", return_value=process):
            completion = provider.complete(
                "system", [{"role": "user", "content": "do the work"}], [])
        assert completion.stop_reason == "error", process
        assert completion.text.startswith("ERROR(claude-cli):"), completion.text


def test_claude_cli_format_repair_reports_both_physical_requests(monkeypatch):
    from harness.providers import ClaudeCliProvider, Usage

    provider = ClaudeCliProvider("opus")
    replies = iter([("plain prose", Usage(input_tokens=2)),
                    ('{"answer":"done"}', Usage(input_tokens=3))])
    monkeypatch.setattr(provider, "_call", lambda *_args: next(replies))

    completion = provider.complete(
        "system", [{"role": "user", "content": "do the work"}], [])

    assert completion.text == "done"
    assert completion.request_count == 2
    assert completion.usage.input_tokens == 5


def test_request_authority_is_bound_to_one_provider_instance():
    from harness.providers import ClaudeCliProvider

    first = ClaudeCliProvider("opus")
    second = ClaudeCliProvider("opus")
    gate = lambda _purpose: "request-1"
    complete = lambda *_args: None

    with first.request_authority(gate, complete, request_scope="mission-one"):
        assert first.current_request_authority() == (gate, complete)
        assert first.current_request_scope() == "mission-one"
        assert second.current_request_authority() == (None, None)
        assert second.current_request_scope() == ""
    assert first.current_request_authority() == (None, None)
    assert first.current_request_scope() == ""


def test_claude_cli_subscription_only_requires_authority_before_subprocess(monkeypatch):
    from harness.providers import ClaudeCliProvider

    provider = ClaudeCliProvider("opus", subscription_only=True)
    monkeypatch.setattr("harness.providers.shutil.which", lambda *_args, **_kwargs: "claude")
    monkeypatch.setattr(
        "harness.providers.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("missing authority must stop before subprocess")))

    completion = provider.complete(
        "system", [{"role": "user", "content": "do the work"}], [])

    assert completion.stop_reason == "error"
    assert completion.error_detail == "claude CLI model request authority is missing"


def test_claude_cli_reserves_and_completes_each_format_repair_process(monkeypatch):
    import subprocess
    from harness.providers import ClaudeCliProvider

    provider = ClaudeCliProvider("opus", subscription_only=True)
    replies = iter([
        subprocess.CompletedProcess(
            ["claude"], 0, json.dumps({"result": "plain prose", "usage": {}}), ""),
        subprocess.CompletedProcess(
            ["claude"], 0,
            json.dumps({"result": '{"answer":"done"}', "usage": {}}), ""),
    ])
    launched = []
    reserved = []
    completed = []
    monkeypatch.setattr("harness.providers.shutil.which", lambda *_args, **_kwargs: "claude")
    monkeypatch.setattr(
        "harness.providers.subprocess.run",
        lambda *args, **kwargs: launched.append((args, kwargs)) or next(replies))

    def reserve(purpose):
        reserved.append(purpose)
        return "request-%d" % len(reserved)

    with provider.request_authority(
            reserve, lambda *args: completed.append(args)):
        completion = provider.complete(
            "system", [{"role": "user", "content": "do the work"}], [])

    assert completion.text == "done"
    assert completion.request_count == 2
    assert len(launched) == 2
    assert reserved == ["claude_cli", "claude_cli"]
    assert completed == [
        ("request-1", "completed"),
        ("request-2", "completed"),
    ]


def test_claude_cli_second_reservation_failure_does_not_launch_repair(monkeypatch):
    import subprocess
    from harness.providers import ClaudeCliProvider

    provider = ClaudeCliProvider("opus", subscription_only=True)
    launched = []
    completed = []
    monkeypatch.setattr("harness.providers.shutil.which", lambda *_args, **_kwargs: "claude")
    monkeypatch.setattr(
        "harness.providers.subprocess.run",
        lambda *args, **kwargs: launched.append((args, kwargs)) or subprocess.CompletedProcess(
            ["claude"], 0, json.dumps({"result": "plain prose", "usage": {}}), ""))
    reservations = iter(["request-1", RuntimeError("budget exhausted")])

    def reserve(_purpose):
        result = next(reservations)
        if isinstance(result, Exception):
            raise result
        return result

    with provider.request_authority(
            reserve, lambda *args: completed.append(args)):
        completion = provider.complete(
            "system", [{"role": "user", "content": "do the work"}], [])

    assert completion.stop_reason == "error"
    assert completion.error_detail == "claude CLI model request reservation failed"
    assert len(launched) == 1
    assert completed == [("request-1", "completed")]


def test_claude_cli_process_error_completes_reserved_request(monkeypatch):
    from harness.providers import ClaudeCliProvider

    provider = ClaudeCliProvider("opus", subscription_only=True)
    completed = []
    monkeypatch.setattr("harness.providers.shutil.which", lambda *_args, **_kwargs: "claude")
    monkeypatch.setattr(
        "harness.providers.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("launch failed")))

    with provider.request_authority(
            lambda _purpose: "request-1", lambda *args: completed.append(args)):
        completion = provider.complete(
            "system", [{"role": "user", "content": "do the work"}], [])

    assert completion.stop_reason == "error"
    assert completed == [("request-1", "error")]


def test_make_provider_passes_subscription_only_to_claude_cli():
    from harness.providers import make_provider

    provider = make_provider("claude-cli", "opus", subscription_only=True)

    assert provider.supports_request_gate is True
    assert provider.subscription_only is True


def _authorize_direct(provider):
    provider.request_gate = lambda _purpose: "test-request-1"
    provider.request_complete = lambda *_args: None
    return provider


def _ordinary_oauth_provider():
    from harness.providers import AnthropicOAuthProvider

    provider = AnthropicOAuthProvider.__new__(AnthropicOAuthProvider)
    provider.name = "anthropic-oauth"
    provider.model = "claude-opus-4-8"
    provider.max_tokens = 128
    provider.effort = "default"
    provider.speed = "standard"
    provider.API = provider.OFFICIAL_API
    provider.subscription_only = False
    return provider


def test_oauth_nonstream_malformed_matrix_is_error_and_settles_reservation(monkeypatch):
    class Response:
        def __init__(self, body): self.body = json.dumps(body).encode()
        def read(self): return self.body
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    malformed = (
        [], {}, {"content": {}}, {"content": [None]},
        {"content": [{"type": "text", "text": []}]},
        {"content": [{"type": "tool_use", "id": "call", "name": "run", "input": []}]},
        {"content": [], "usage": []},
        {"content": [], "usage": {"input_tokens": -1}},
        {"content": [], "usage": {"cache_read_input_tokens": "3"}},
        {"content": [], "stop_reason": []},
    )
    provider = _ordinary_oauth_provider()
    settled = []
    provider.request_complete = lambda *args: settled.append(args)
    monkeypatch.setattr("harness.providers._read_oauth_token", lambda **_kwargs: "private-token")
    monkeypatch.setattr("harness.providers.claude_oauth_expired", lambda **_kwargs: False)

    for index, body in enumerate(malformed):
        request_id = "malformed-%d" % index
        provider.request_gate = lambda _purpose, value=request_id: value
        monkeypatch.setattr(
            "harness.providers._credential_open",
            lambda *_args, value=body, **_kwargs: Response(value))
        completion = provider.complete(
            "system", [{"role": "user", "content": "work"}], [])
        assert completion.stop_reason == "error", body
        assert settled[-1] == (request_id, "error"), body

    provider.request_gate = lambda _purpose: "valid-nonstream"
    monkeypatch.setattr(
        "harness.providers._credential_open",
        lambda *_args, **_kwargs: Response({
            "type": "message", "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 2, "output_tokens": 1}, "stop_reason": "end_turn",
        }))
    completion = provider.complete(
        "system", [{"role": "user", "content": "work"}], [])
    assert completion.text == "ok" and completion.stop_reason == "end_turn"
    assert settled[-1] == ("valid-nonstream", "completed")
    assert len(settled) == len(malformed) + 1


def test_oauth_adaptive_thinking_stream_round_trips_signed_blocks(monkeypatch):
    provider = _ordinary_oauth_provider()
    requests, settled = [], []
    provider.request_gate = lambda _purpose: "thinking-request"
    provider.request_complete = lambda *args: settled.append(args)
    monkeypatch.setenv("COLLIE_THINKING", "1")
    monkeypatch.setattr("harness.providers._read_oauth_token", lambda **_kwargs: "private-token")
    monkeypatch.setattr("harness.providers.claude_oauth_expired", lambda **_kwargs: False)

    frames = [
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":2}}}\n',
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":"","vendor":"kept"}}\n',
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"reason "}}\n',
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"carefully"}}\n',
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"signed-value"}}\n',
        b'data: {"type":"content_block_stop","index":0}\n',
        b'data: {"type":"content_block_start","index":1,"content_block":{"type":"redacted_thinking","data":"opaque","vendor":"also-kept"}}\n',
        b'data: {"type":"content_block_stop","index":1}\n',
        b'data: {"type":"content_block_start","index":2,"content_block":{"type":"tool_use","id":"tool-1","name":"grep"}}\n',
        b'data: {"type":"content_block_delta","index":2,"delta":{"type":"input_json_delta","partial_json":"{\\"pattern\\":\\"TODO\\"}"}}\n',
        b'data: {"type":"content_block_stop","index":2}\n',
        b'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":3}}\n',
        b'data: {"type":"message_stop"}\n',
    ]

    class Stream:
        def __iter__(self): return iter(frames)
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    def open_stream(request, timeout):
        requests.append((request, timeout))
        return Stream()

    monkeypatch.setattr("harness.providers._credential_open", open_stream)
    completion = provider.complete(
        "system", [{"role": "user", "content": "work"}],
        [{"name": "grep", "description": "search"}], on_text=lambda _text: None)

    assert completion.stop_reason == "tool_use"
    assert completion.thinking_blocks == [
        {"type": "thinking", "thinking": "reason carefully", "vendor": "kept",
         "signature": "signed-value"},
        {"type": "redacted_thinking", "data": "opaque", "vendor": "also-kept"},
    ]
    assert len(completion.tool_calls) == 1 and completion.tool_calls[0].args == {"pattern": "TODO"}
    assert settled == [("thinking-request", "completed")]
    assert json.loads(requests[0][0].data)["thinking"] == {"type": "adaptive"}

    replay = provider._to_anthropic([{
        "role": "assistant", "content": completion.text,
        "thinking_blocks": completion.thinking_blocks, "tool_calls": completion.tool_calls,
    }])[0]["content"]
    assert replay[:2] == completion.thinking_blocks
    assert replay[2] == {"type": "tool_use", "id": "tool-1", "name": "grep",
                         "input": {"pattern": "TODO"}}


def test_oauth_stream_error_settles_reservation_as_error(monkeypatch):
    provider = _ordinary_oauth_provider()
    settled = []
    provider.request_gate = lambda _purpose: "stream-error"
    provider.request_complete = lambda *args: settled.append(args)
    monkeypatch.setattr("harness.providers._read_oauth_token", lambda **_kwargs: "private-token")
    monkeypatch.setattr("harness.providers.claude_oauth_expired", lambda **_kwargs: False)

    class Stream:
        def __iter__(self):
            return iter([
                b'data: {"type":"message_start","message":{"usage":{}}}\n',
                b'data: {"type":"error","error":{"type":"overloaded_error","message":"busy"}}\n',
            ])
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    monkeypatch.setattr("harness.providers._credential_open", lambda *_args, **_kwargs: Stream())
    completion = provider.complete(
        "system", [{"role": "user", "content": "work"}], [], on_text=lambda _text: None)
    assert completion.stop_reason == "error" and completion.error_detail == "busy"
    assert settled == [("stream-error", "error")]


def test_ordinary_oauth_does_not_forward_authorization_across_redirect(monkeypatch):
    import http.server
    import threading

    source_authorization, target_hits = [], []

    class QuietHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

    class TargetHandler(QuietHandler):
        def _hit(self):
            target_hits.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
        do_GET = _hit
        do_POST = _hit

    target = http.server.ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
    target_url = "http://127.0.0.1:%d/stolen" % target.server_port

    class RedirectHandler(QuietHandler):
        def do_POST(self):
            source_authorization.append(self.headers.get("Authorization"))
            self.send_response(302)
            self.send_header("Location", target_url)
            self.end_headers()

    source = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (source, target)
    ]
    for thread in threads:
        thread.start()

    provider = _ordinary_oauth_provider()
    provider.OFFICIAL_API = "http://127.0.0.1:%d/messages" % source.server_port
    monkeypatch.setattr("harness.providers._read_oauth_token", lambda **_kwargs: "private-token")
    monkeypatch.setattr("harness.providers.claude_oauth_expired", lambda **_kwargs: False)
    try:
        completion = provider.complete(
            "system", [{"role": "user", "content": "work"}], [])
    finally:
        for server in (source, target):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)

    assert completion.stop_reason == "error" and completion.error_status == 302
    assert source_authorization == ["Bearer private-token"]
    assert target_hits == [], "OAuth Authorization must never reach the redirect origin"


def test_direct_oauth_overnight_uses_only_collie_system_and_official_proxy_free_route(
        monkeypatch):
    from harness.providers import AnthropicOAuthProvider

    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({
                "type": "message",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {}, "stop_reason": "end_turn",
            }).encode()

    class Opener:
        def open(self, request, timeout):
            seen["request"] = request
            seen["timeout"] = timeout
            return Response()

    def build_opener(*handlers):
        seen["handlers"] = handlers
        return Opener()

    provider = AnthropicOAuthProvider.__new__(AnthropicOAuthProvider)
    provider.name = "anthropic-oauth"
    provider.model = "claude-opus-4-8"
    provider.max_tokens = 128
    provider.effort = "default"
    provider.speed = "standard"
    provider.API = provider.OFFICIAL_API
    provider.subscription_only = True
    _authorize_direct(provider)
    monkeypatch.setattr(
        "harness.providers._read_oauth_token", lambda **_kwargs: "private-token")
    monkeypatch.setattr(
        "harness.providers.claude_oauth_expired", lambda **_kwargs: False)
    monkeypatch.setattr("harness.providers.urllib.request.build_opener", build_opener)
    monkeypatch.setattr(
        "harness.providers.urllib.request.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("direct overnight must not use ambient-proxy urlopen")))

    completion = provider.complete(
        "COLLIE SYSTEM ONLY", [{"role": "user", "content": "work"}], [])

    request = seen["request"]
    body = json.loads(request.data)
    assert completion.text == "ok"
    assert body["system"] == [{
        "type": "text", "text": "COLLIE SYSTEM ONLY",
        "cache_control": {"type": "ephemeral"},
    }]
    assert "Claude Code" not in json.dumps(body)
    assert request.full_url == provider.OFFICIAL_API
    assert request.headers["User-agent"] == "collie/anthropic-oauth-experimental"
    assert "X-app" not in request.headers
    assert request.headers["Anthropic-beta"] == "oauth-2025-04-20"
    assert "claude-code" not in request.headers["Anthropic-beta"]
    assert len(seen["handlers"]) == 2
    redirect = next(h for h in seen["handlers"]
                    if type(h).__name__ == "_NoRedirectHandler")
    assert redirect.redirect_request(
        request, None, 302, "Found", {}, "https://redirect.invalid/steal") is None


def test_direct_oauth_overnight_refuses_endpoint_or_fast_route_drift(monkeypatch):
    from harness.providers import AnthropicOAuthProvider

    provider = AnthropicOAuthProvider.__new__(AnthropicOAuthProvider)
    provider.name = "anthropic-oauth"
    provider.model = "claude-opus-4-8"
    provider.max_tokens = 128
    provider.effort = "default"
    provider.speed = "standard"
    provider.API = "https://example.invalid/messages"
    provider.subscription_only = True
    _authorize_direct(provider)
    monkeypatch.setattr(
        "harness.providers._read_oauth_token", lambda **_kwargs: "private-token")
    monkeypatch.setattr(
        "harness.providers.claude_oauth_expired", lambda **_kwargs: False)

    completion = provider.complete(
        "system", [{"role": "user", "content": "work"}], [])

    assert completion.stop_reason == "error"
    assert "route is invalid" in completion.error_detail


def test_direct_oauth_never_falls_back_to_ambient_oauth_token(monkeypatch):
    from harness.providers import AnthropicOAuthProvider

    provider = AnthropicOAuthProvider.__new__(AnthropicOAuthProvider)
    provider.name = "anthropic-oauth"
    provider.model = "claude-opus-4-8"
    provider.max_tokens = 128
    provider.effort = "default"
    provider.speed = "standard"
    provider.API = provider.OFFICIAL_API
    provider.subscription_only = True
    _authorize_direct(provider)
    monkeypatch.setattr(
        "harness.providers._read_oauth_token", lambda **_kwargs: "")
    monkeypatch.setattr(
        "harness.providers.claude_oauth_expired", lambda **_kwargs: False)
    monkeypatch.setattr(
        "harness.providers.urllib.request.build_opener",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("missing direct token must fail before HTTP")))

    completion = provider.complete(
        "system", [{"role": "user", "content": "work"}], [])

    assert completion.stop_reason == "error"
    assert "login-store token is unavailable" in completion.error_detail


def test_direct_oauth_request_reservation_failure_stops_before_http(monkeypatch):
    from harness.providers import AnthropicOAuthProvider

    provider = AnthropicOAuthProvider.__new__(AnthropicOAuthProvider)
    provider.name = "anthropic-oauth"
    provider.model = "claude-opus-4-8"
    provider.max_tokens = 128
    provider.effort = "default"
    provider.speed = "standard"
    provider.API = provider.OFFICIAL_API
    provider.subscription_only = True
    provider.request_gate = lambda _purpose: (_ for _ in ()).throw(
        RuntimeError("sqlite unavailable and must not leak"))
    monkeypatch.setattr(
        "harness.providers._read_oauth_token", lambda **_kwargs: "private-token")
    monkeypatch.setattr(
        "harness.providers.claude_oauth_expired", lambda **_kwargs: False)
    monkeypatch.setattr(
        "harness.providers.urllib.request.build_opener",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("failed reservation must stop before HTTP")))

    completion = provider.complete(
        "system", [{"role": "user", "content": "work"}], [])

    assert completion.stop_reason == "error"
    assert completion.error_detail == "model request reservation failed"


def test_direct_oauth_constructor_cannot_be_admitted_by_ambient_token(monkeypatch):
    import pytest
    from harness.providers import AnthropicOAuthProvider

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "ambient-token-must-be-ignored")
    monkeypatch.setattr("harness.providers.claude_credentials", lambda: {})

    with pytest.raises(RuntimeError, match="no Claude OAuth token"):
        AnthropicOAuthProvider(subscription_only=True)

def test_claude_cli_text_protocol_handles_braces_inside_json_strings():
    from harness.providers import _parse_answer_json, _parse_tool_json

    content = '}\nfunction f() { return {"nested": true}; }\n{'
    encoded_tool = "prose before\n```json\n%s\n```" % json.dumps({
        "tool": "write_file", "args": {"path": "x.js", "content": content}})
    call = _parse_tool_json(encoded_tool)
    assert call is not None and call.name == "write_file"
    assert call.args == {"path": "x.js", "content": content}

    answer = 'kept a closing brace } and an opening brace { inside the summary'
    assert _parse_answer_json(json.dumps({"answer": answer})) == answer

def test_openai_compat_surfaces_finish_length():
    """AUDIT #7 second half: finish_reason='length' must surface as stop_reason='length' with the
    tool_calls PRESERVED (regression that would have caught the truncation-invisible DeepSeek path)."""
    from unittest.mock import patch
    from harness.providers import OpenAICompatProvider
    os.environ["_CT_KEY2"] = "k"
    oc = OpenAICompatProvider("http://x", "_CT_KEY2", "m", name="deepseek")
    class _R:
        def __init__(s, d): s._d = json.dumps(d).encode()
        def read(s): return s._d
        def __enter__(s): return s
        def __exit__(s, *a): pass
    body = {"choices": [{"finish_reason": "length", "message": {"content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "edit_file", "arguments": "{\"path\":"}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    with patch("harness.providers._credential_open", lambda *a, **k: _R(body)):
        c = oc.complete("s", [{"role": "user", "content": "hi"}], [{"name": "edit_file", "description": "edit"}])
    assert c.stop_reason == "length" and c.tool_calls, "length must surface + keep tool_calls"
    body["choices"][0]["finish_reason"] = "stop"; body["choices"][0]["message"]["tool_calls"] = []
    body["choices"][0]["message"]["content"] = "hi"
    with patch("harness.providers._credential_open", lambda *a, **k: _R(body)):
        assert oc.complete("s", [{"role": "user", "content": "hi"}], []).stop_reason == "end_turn"

def test_anthropic_normalizes_max_tokens():
    from harness.providers import _norm_stop
    assert _norm_stop("max_tokens") == "length" and _norm_stop("length") == "length"
    assert _norm_stop("end_turn") == "end_turn" and _norm_stop("tool_use") == "tool_use"

def test_anthropic_max_tokens_default():
    from harness.providers import AnthropicProvider
    old = os.environ.pop("COLLIE_MAX_TOKENS", None)
    os.environ["ANTHROPIC_API_KEY"] = "dummy"
    try:
        assert AnthropicProvider(model="m").max_tokens == 8192, "default 8192 (raised from 4096: 4k truncated big edits into a retry loop)"
        os.environ["COLLIE_MAX_TOKENS"] = "16384"
        assert AnthropicProvider(model="m").max_tokens == 16384
    finally:
        os.environ.pop("COLLIE_MAX_TOKENS", None)
        if old is not None: os.environ["COLLIE_MAX_TOKENS"] = old

def test_ollama_done_reason_length():
    from unittest.mock import patch
    from harness.providers import OllamaProvider
    ol = OllamaProvider.__new__(OllamaProvider); ol.name = "ollama"; ol._model = "m"; ol.url = "http://x"
    class _R:
        def __init__(s, d): s._d = json.dumps(d).encode()
        def read(s): return s._d
        def __enter__(s): return s
        def __exit__(s, *a): pass
    with patch("urllib.request.urlopen", lambda *a, **k: _R(
            {"message": {"content": "partial"}, "done_reason": "length"})):
        assert ol.complete("s", [{"role": "user", "content": "hi"}], []).stop_reason == "length"

# ------------------------------------------------------------------ ollama synthetic tool-ids unique
def test_ollama_unique_tool_ids():
    from unittest.mock import patch
    from harness.providers import OllamaProvider
    class _R:
        def __init__(s, d): s._d = json.dumps(d).encode()
        def read(s): return s._d
        def __enter__(s): return s
        def __exit__(s, *a): pass
    oll = OllamaProvider.__new__(OllamaProvider); oll._model = "x"; oll.url = "http://x"
    resp = {"message": {"tool_calls": [{"function": {"name": "a", "arguments": {}}},
                                       {"function": {"name": "b", "arguments": {}}}]}}
    with patch("urllib.request.urlopen", lambda *a, **k: _R(resp)):
        ids = [tc.id for _ in range(2) for tc in oll.complete("s", [], []).tool_calls]  # 2 "turns"
    assert len(ids) == len(set(ids)), "ollama tool ids must be globally unique (were 'oll_0' colliding across turns): %r" % ids

# ------------------------------------------------------------------ providers._parse_anthropic_stream
def test_stream_parser():
    from harness.providers import _parse_anthropic_stream
    frames = [
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":10}}}\n',
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}\n',
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hel"}}\n',
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"lo"}}\n',
        b'data: {"type":"content_block_stop","index":0}\n',
        b'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"t1","name":"grep"}}\n',
        b'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"pat"}}\n',
        b'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"tern\\":\\"x\\"}"}}\n',
        b'data: {"type":"content_block_stop","index":1}\n',
        b'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":5}}\n',
        b'data: {"type":"message_stop"}\n',
    ]
    got = []
    text, calls, usage, sr, edetail, thinks = _parse_anthropic_stream(
        iter(frames), lambda t: got.append(t))
    assert text == "Hello", text
    assert got == ["Hel", "lo"], got                             # on_text streamed each delta
    assert len(calls) == 1 and calls[0].name == "grep" and calls[0].args == {"pattern": "x"}, calls
    assert sr == "tool_use" and usage["input_tokens"] == 10 and usage["output_tokens"] == 5
    assert edetail == "", "clean stream carries no error_detail"
    assert thinks == []

def test_stream_parser_error_event():
    from harness.providers import _parse_anthropic_stream
    frames = [b'data: {"type":"message_start","message":{"usage":{}}}\n',
              b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}\n',
              b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n',
              b'data: {"type":"error","error":{"type":"overloaded_error","message":"busy"}}\n']
    visible = []
    text, calls, usage, sr, edetail, thinks = _parse_anthropic_stream(iter(frames), visible.append)
    assert sr == "error" and "ERROR" in text, "mid-stream error must surface stop_reason=error"
    assert edetail == "busy", "stream error must thread error_detail out for classify_error"
    assert visible == ["hi"], "transport/parser errors must never masquerade as answer tokens"


def test_stream_parser_rejects_premature_eof_and_malformed_events():
    from harness.providers import _parse_anthropic_stream

    start = b'data: {"type":"message_start","message":{"usage":{"input_tokens":1}}}\n'
    terminal = b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}\n'
    stop = b'data: {"type":"message_stop"}\n'
    cases = (
        ([start], "terminal events"),
        ([start, terminal], "terminal events"),            # message_stop is independently required
        ([start, b'data: {not-json}\n'], "invalid event JSON"),
        ([b'data: {"type":"message_start","message":{"usage":{"input_tokens":-1}}}\n'],
         "non-negative integer"),
        ([start, b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
                 b'"usage":{"output_tokens":"1"}}\n', stop], "non-negative integer"),
        ([start, b'data: {"type":"message_delta","delta":{},"usage":{"output_tokens":1}}\n',
          stop], "stop_reason"),
    )
    for frames, expected in cases:
        text, calls, usage, reason, detail, thinks = _parse_anthropic_stream(iter(frames), None)
        assert reason == "error" and not calls, (frames, reason, calls)
        assert expected in detail and "ERROR" in text, (expected, detail, text)


def test_stream_terminal_rejects_content_events_and_enforces_tool_stop_contract():
    from harness.providers import _parse_anthropic_stream

    start = b'data: {"type":"message_start","message":{"usage":{}}}\n'
    terminal = b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{}}\n'
    stop = b'data: {"type":"message_stop"}\n'
    text_block = [
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}\n',
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}\n',
        b'data: {"type":"content_block_stop","index":0}\n',
    ]
    forbidden_after_terminal = (
        b'data: {"type":"content_block_start","index":1,"content_block":{"type":"text"}}\n',
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"late"}}\n',
        b'data: {"type":"content_block_stop","index":0}\n',
    )
    for forbidden in forbidden_after_terminal:
        result = _parse_anthropic_stream(
            iter([start, *text_block, terminal, forbidden, stop]), None)
        assert result[3] == "error"
        assert "after terminal message_delta" in result[4], (forbidden, result[4])

    # Ping is the sole harmless event permitted between the terminal delta and message_stop.
    result = _parse_anthropic_stream(iter([
        start, terminal, b'data: {"type":"ping"}\n', stop,
    ]), None)
    assert result[3] == "end_turn" and result[4] == ""

    no_tool_but_tool_stop = [
        start,
        b'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{}}\n',
        stop,
    ]
    tool_but_end_turn = [
        start,
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"t1","name":"grep"}}\n',
        b'data: {"type":"content_block_stop","index":0}\n',
        terminal, stop,
    ]
    for frames in (no_tool_but_tool_stop, tool_but_end_turn):
        result = _parse_anthropic_stream(iter(frames), None)
        assert result[3] == "error"
        assert "inconsistent with tool_use" in result[4], result[4]


def test_stream_parser_rejects_malformed_tool_json_at_terminal_boundary():
    from harness.providers import _parse_anthropic_stream

    frames = [
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":1}}}\n',
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"t1","name":"grep"}}\n',
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{bad"}}\n',
        b'data: {"type":"content_block_stop","index":0}\n',
        b'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":1}}\n',
        b'data: {"type":"message_stop"}\n',
    ]
    text, calls, usage, reason, detail, thinks = _parse_anthropic_stream(iter(frames), None)
    assert reason == "error" and not calls
    assert "not valid JSON" in detail and "ERROR" in text


def test_anthropic_stream_max_tokens_is_length_data_not_an_executable_partial():
    from unittest.mock import patch
    from harness.providers import AnthropicProvider

    def frames(partial_json):
        encoded = json.dumps({
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": partial_json},
        }).encode()
        return [
            b'data: {"type":"message_start","message":{"usage":{"input_tokens":1}}}\n',
            b'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"t1","name":"edit_file"}}\n',
            b"data: " + encoded + b"\n",
            b'data: {"type":"content_block_stop","index":0}\n',
            b'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"},"usage":{"output_tokens":100}}\n',
            b'data: {"type":"message_stop"}\n',
        ]

    class _Stream:
        def __init__(self, values): self.values = values
        def __iter__(self): return iter(self.values)
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.name, provider.model = "anthropic", "model"
    provider.api_key, provider.max_tokens = "test-key", 100
    provider.API = "https://example.test"
    messages = [{"role": "user", "content": "hi"}]

    with patch("harness.providers._credential_open",
               return_value=_Stream(frames('{"path":"x.py"}'))):
        valid = provider.complete("system", messages, [], on_text=lambda _text: None)
    assert valid.stop_reason == "length"
    assert len(valid.tool_calls) == 1 and valid.tool_calls[0].args == {"path": "x.py"}

    with patch("harness.providers._credential_open",
               return_value=_Stream(frames('{"path":'))):
        partial = provider.complete("system", messages, [], on_text=lambda _text: None)
    assert partial.stop_reason == "length"
    assert partial.tool_calls == [] and partial.error_detail == ""


def test_anthropic_provider_surfaces_truncated_stream_as_error_completion():
    from unittest.mock import patch
    from harness.providers import AnthropicProvider

    class _Stream:
        def __iter__(self):
            return iter([
                b'data: {"type":"message_start","message":{"usage":{"input_tokens":1}}}\n',
                b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}\n',
                b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"partial"}}\n',
            ])
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.name, provider.model = "anthropic", "model"
    provider.api_key, provider.max_tokens, provider.API = "test-key", 100, "https://example.test"
    streamed = []
    with patch("harness.providers._credential_open", return_value=_Stream()):
        completion = provider.complete(
            "system", [{"role": "user", "content": "hi"}], [], on_text=streamed.append)
    assert streamed[0] == "partial"
    assert completion.stop_reason == "error" and completion.error_detail
    assert "terminal events" in completion.error_detail

def test_unrecognised_provider_error_says_it_was_not_recognised():
    """'terminal' covers both 'we know this is fatal' and 'we have never seen this'.

    They printed identically, so a run stopped by an unknown error looked as settled as one stopped
    by a bad API key. That is how the mcp_ naming failure — reported by the API as a billing
    message — read as a quota problem for hours.
    """
    from harness.providers import classify_error, is_known_terminal
    known = "authentication_error: invalid x-api-key"
    unknown = "You're out of extra usage. Add more at claude.ai/settings/usage and keep going."
    assert classify_error(known) == "terminal" and classify_error(unknown) == "terminal", \
        "both still classify as terminal — that is the point"
    assert is_known_terminal(known), "a bad key is a recognised fatal error"
    assert not is_known_terminal(unknown), \
        "this message matches no pattern; it must NOT be reported with the confidence of one"

def test_provider_error_keeps_status_and_limit_headers():
    """A recorded failure has to be diagnosable later: 400, 429 and 529 must not look identical."""
    import urllib.error, io as _io
    from harness.providers import _error_completion
    hdrs = {"anthropic-ratelimit-unified-5h-utilization": "0.08",
            "anthropic-ratelimit-unified-status": "allowed",
            "content-type": "application/json"}
    err = urllib.error.HTTPError("https://api", 400, "Bad Request", hdrs,
                                 _io.BytesIO(b'{"error":{"message":"boom"}}'))
    comp = _error_completion("anthropic-oauth", err)
    assert comp.error_status == 400, "the status must survive into the record"
    assert "5h-utilization" in comp.error_detail, "the rate-limit headers must survive too"
    assert "content-type" not in comp.error_detail, "only limit-related headers, not every header"

def test_provider_default_is_auto():
    """Collie Auto is the front door; concrete billing transports remain explicit choices."""
    from harness import settings as S
    import tempfile
    row = next(s for s in S.SCHEMA if s["key"] == "PROVIDER")
    assert row["default"] == "auto"
    # options may be plain strings OR {value,label} dicts (the panel renders friendly labels);
    # assert the semantic invariant on the values, not the serialization shape.
    opt_vals = [o["value"] if isinstance(o, dict) else o for o in row["options"]]
    assert opt_vals[0] == "auto", "panel leads with Collie Auto, not a vendor identity"
    assert "anthropic" in opt_vals, "the metered API remains an explicit choice"
    assert "anthropic-oauth" not in opt_vals, "unsupported raw OAuth is not a product choice"
    assert "claude-agent-sdk" in opt_vals, "the official embedded SDK remains available"
    assert "claude-cli" in opt_vals, "the official Claude Code CLI remains available"
    # a Provider saved in the Settings panel must reach the settings.get() fallback path
    # (delegate/pack/acp read it directly — no dependence on settings.apply() timing)
    p = os.path.join(tempfile.gettempdir(), "collie_settings_prov.json")
    old = S._PATH
    env_had = os.environ.pop("COLLIE_PROVIDER", None)
    try:
        S._PATH = p; S._cache["mtime"] = -1.0
        assert S.get("PROVIDER", "auto") == "auto", "nothing saved -> Collie Auto"
        S.save({"PROVIDER": "claude-agent-sdk"})
        assert S.get("PROVIDER", "auto") == "claude-agent-sdk", "panel choice wins over default"
    finally:
        if env_had is not None:
            os.environ["COLLIE_PROVIDER"] = env_had
        S._PATH = old; S._cache["mtime"] = -1.0
        try: os.remove(p)
        except OSError: pass

if __name__ == "__main__":                 # LAST, always: a guard with definitions after it
    sys.exit(run_module(globals(), "PROVIDERS"))  # silently skips every one of them.
