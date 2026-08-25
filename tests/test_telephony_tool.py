"""phone_call / phone_call_status: the agent-facing seam over the fail-closed voice adapter.

The adapter itself is covered in test_telephony_twilio.py.  These tests pin what the TOOL
adds: host-binding lookup without secrets, dial-number normalisation, the emergency
blocklist, dry-run without network or ledger rows, replay-instead-of-redial on identical
args, clean ERROR strings instead of tracebacks, and registration + risk classification.
"""
import json
import os
import sqlite3
import threading

import pytest

from harness import telephony
from harness import telephony_twilio as provider
from harness import telephony_tool as tool_mod
from harness.tools import ToolCtx, ToolRegistry


API_KEY = "xi-secret-value-that-must-never-be-persisted"
CALL_SID = "CA" + "b" * 32
CONVERSATION_ID = "conv_0987654321"
CALLER = "+14155550987"


class FakeTransport:
    def __init__(self, response=None, probe_response=None):
        self.response = response or provider.ProviderHttpResponse(200, json.dumps({
            "success": True, "message": "accepted",
            "conversation_id": CONVERSATION_ID, "callSid": CALL_SID}).encode())
        self.probe_response = probe_response or provider.ProviderHttpResponse(200, json.dumps({
            "provider": "twilio", "phone_number": CALLER,
            "phone_number_id": "phnum_1234567890",
            "assigned_agent": {"agent_id": "agent_1234567890"}}).encode())
        self.posts, self.gets = [], []
        self._lock = threading.Lock()

    def get_json(self, url, *, headers, timeout):
        with self._lock:
            self.gets.append(url)
        return self.probe_response

    def post_json(self, url, *, headers, payload, timeout):
        with self._lock:
            self.posts.append(payload)
        return self.response


def binding(**overrides):
    data = {
        "collie_id": "rowan-device", "version": 1,
        "elevenlabs": {
            "agent_id": "agent_1234567890", "agent_phone_number_id": "phnum_1234567890",
            "api_key_ref": "cv1_" + "a" * 30, "vault_account": "telephony.twilio_elevenlabs",
            "vault_kind": "elevenlabs_api_key",
            "voice_profile": {"model_id": "eleven_v3_conversational", "language": "zh",
                              "stability": 0.38, "similarity_boost": 0.75, "speed": 0.95,
                              "llm_temperature": 0.45, "llm_max_tokens": 120},
        },
        "twilio": {"caller_number": CALLER, "caller_id_binding_verified": True,
                   "caller_id_status": "verified"},
    }
    data.update(overrides)
    return data


def state_dir(tmp_path, data=None):
    root = tmp_path / "state"
    root.mkdir(parents=True)
    if data is not None:
        (root / tool_mod.PROVIDER_FILE).write_text(json.dumps(data), encoding="utf-8")
    return str(root)


def make_tool(root, transport=None):
    return tool_mod.PhoneCallTool(
        state_dir=root, transport=transport or FakeTransport(),
        api_key=provider.EnvironmentApiKeySource({"ELEVENLABS_API_KEY": API_KEY}),
        clock=lambda: 1_700_000_000)


def ctx(root):
    return ToolCtx(root, "p", None)


ARGS = {"to": "6505550123", "label": "Kobe",
        "brief": "请用自然的普通话和对方聊聊最近开源项目的进展，不要替用户做任何承诺，对方不想聊就礼貌结束。",
        "max_minutes": 3}


def test_dial_number_accepts_us_ten_digits_and_e164_but_never_emergency():
    assert tool_mod.normalize_dial_number("650-555-0123") == "+16505550123"
    assert tool_mod.normalize_dial_number("1 (650) 555 0123") == "+16505550123"
    assert tool_mod.normalize_dial_number("+44 20 7946 0958") == "+442079460958"
    for bad in ("911", "112", "+1911", "abc", "12345"):
        with pytest.raises(ValueError):
            tool_mod.normalize_dial_number(bad)


def test_dry_run_touches_no_network_and_writes_no_ledger_row(tmp_path):
    root = state_dir(tmp_path, binding())
    transport = FakeTransport()
    out = json.loads(make_tool(root, transport).run(dict(ARGS, dry_run=True), ctx(root)))
    assert out["dry_run"] is True and out["submitted"] is False
    assert out["to"] == "••••••0123" and out["first_message"] == tool_mod.DEFAULT_DISCLOSURE
    assert out["provider_request"]["ai_disclosure_position"] == "first_message"
    assert transport.posts == [] and transport.gets == []
    db = os.path.join(root, tool_mod.LEDGER_FILE)
    assert (not os.path.exists(db)
            or sqlite3.connect(db).execute("select count(*) from telephony_intents").fetchone()[0] == 0)


def test_dispatch_submits_once_and_reports_the_receipt_without_the_number(tmp_path):
    root = state_dir(tmp_path, binding())
    transport = FakeTransport()
    text = make_tool(root, transport).run(ARGS, ctx(root))
    out = json.loads(text)
    assert out["submitted"] is True and out["replayed"] is False
    assert out["receipt"]["status"] == "disclosure_pending"
    assert out["receipt"]["intent_id"].startswith("call_")
    assert "6505550123" not in text and API_KEY not in text
    assert len(transport.posts) == 1
    sent = transport.posts[0]
    assert sent["to_number"] == "+16505550123"
    agent = sent["conversation_initiation_client_data"]["conversation_config_override"]["agent"]
    assert agent["first_message"] == tool_mod.DEFAULT_DISCLOSURE
    assert ARGS["brief"] in agent["prompt"]["prompt"]
    assert sent["conversation_initiation_client_data"]["conversation_config_override"][
        "conversation"]["max_duration_seconds"] == 180
    assert sent["call_recording_enabled"] is False


def test_identical_args_replay_instead_of_dialling_twice(tmp_path):
    root = state_dir(tmp_path, binding())
    transport = FakeTransport()
    first = json.loads(make_tool(root, transport).run(ARGS, ctx(root)))
    again = json.loads(make_tool(root, transport).run(ARGS, ctx(root)))
    assert first["submitted"] is True
    assert again["submitted"] is False and again["replayed"] is True
    assert again["receipt"]["intent_id"] == first["receipt"]["intent_id"]
    assert len(transport.posts) == 1
    # A deliberate second call is a new intent.
    third = json.loads(make_tool(root, transport).run(dict(ARGS, attempt=2), ctx(root)))
    assert third["submitted"] is True and len(transport.posts) == 2


def test_status_tool_reads_the_durable_receipt(tmp_path):
    root = state_dir(tmp_path, binding())
    placed = json.loads(make_tool(root).run(ARGS, ctx(root)))
    status = tool_mod.PhoneCallStatusTool(state_dir=root, clock=lambda: 1_700_000_000)
    got = json.loads(status.run({"intent_id": placed["receipt"]["intent_id"]}, ctx(root)))
    assert got["status"] == "disclosure_pending"
    assert status.run({"intent_id": "call_nope"}, ctx(root)).startswith("ERROR: unknown intent_id")


def test_missing_binding_and_bad_inputs_are_clean_errors(tmp_path):
    root = state_dir(tmp_path, None)
    transport = FakeTransport()
    assert make_tool(root, transport).run(ARGS, ctx(root)).startswith("ERROR: no voice line is bound")
    root2 = state_dir(tmp_path / "b", binding())
    t = make_tool(root2, transport)
    assert t.run(dict(ARGS, to="911"), ctx(root2)).startswith("ERROR: emergency")
    assert t.run(dict(ARGS, max_minutes=0), ctx(root2)).startswith("ERROR: max_minutes")
    assert t.run(dict(ARGS, cost_cap_usd=0.10), ctx(root2)).startswith("ERROR: cost_cap_usd")
    assert t.run(dict(ARGS, brief="hello there, no mandarin"), ctx(root2)).startswith("ERROR:")
    assert t.run({"to": "6505550123"}, ctx(root2)).startswith("ERROR: missing required arg")
    assert transport.posts == []


def test_unverified_caller_id_fails_closed(tmp_path):
    data = binding()
    data["twilio"]["caller_id_binding_verified"] = False
    root = state_dir(tmp_path, data)
    transport = FakeTransport()
    out = make_tool(root, transport).run(ARGS, ctx(root))
    assert out.startswith("ERROR: voice line configuration is incomplete or invalid")
    assert transport.posts == []


def test_provider_rejection_is_reported_not_raised(tmp_path):
    root = state_dir(tmp_path, binding())
    transport = FakeTransport(response=provider.ProviderHttpResponse(401, b"{}"))
    out = make_tool(root, transport).run(ARGS, ctx(root))
    assert out.startswith("ERROR: the provider rejected the call")
    assert '"status": "failed"' in out


def test_tools_register_and_are_classified():
    from harness import risk
    reg = ToolRegistry()
    assert tool_mod.register_telephony(reg) is True
    assert {"phone_call", "phone_call_status"} <= set(reg.names())
    assert risk.classify("phone_call") is risk.RiskClass.EXTERNAL
    assert risk.classify("phone_call_status") is risk.RiskClass.READ
    # One number is one target: a rule (or the user's own words) can pin exactly that number.
    assert "phone_call" not in risk.NO_STANDING_RULE
    assert risk.target_for("phone_call", {"to": "+16505550123"}) == "+16505550123"
    assert risk.target_for("phone_call", {"to": "650-555-0123"}) == "+16505550123"
    assert risk.target_for("phone_call", {"to": "1 (650) 555 0123"}) == "+16505550123"
    assert risk.target_for("phone_call", {"to": "911"}) is None


def test_default_registry_advertises_the_phone():
    from harness.tools import default_registry
    reg = default_registry(code_search=False, web_search=False, exec_code=False)
    assert "phone_call" in reg.names() and "phone_call_status" in reg.names()
    assert reg.get("phone_call").tier == "always"


# -- phone_call_log: the provider-side record, linked through the ledger's keyed reference ----
LIST_URL = tool_mod._CONV_PREFIX
OTHER_SID = "CA" + "c" * 32


class FakeConversationTransport:
    """Serves a conversation listing + details; records every URL and never sees a redirect."""

    def __init__(self, conversations, *, list_status=200):
        self.conversations = conversations          # id -> detail dict
        self.list_status = list_status
        self.urls = []

    def get_json(self, url, *, headers, timeout):
        self.urls.append(url)
        assert headers.get("xi-api-key") == API_KEY
        if url.startswith(LIST_URL + "?"):
            items = [{"conversation_id": cid, "status": d.get("status"),
                      "start_time_unix_secs": d["metadata"]["start_time_unix_secs"],
                      "call_duration_secs": d["metadata"]["call_duration_secs"],
                      "call_successful": "success", "direction": "outbound",
                      "message_count": len(d.get("transcript") or [])}
                     for cid, d in self.conversations.items()]
            return provider.ProviderHttpResponse(
                self.list_status, json.dumps({"conversations": items}).encode())
        cid = url[len(LIST_URL) + 1:]
        if cid in self.conversations:
            return provider.ProviderHttpResponse(200, json.dumps(self.conversations[cid]).encode())
        return provider.ProviderHttpResponse(404, b"{}")


def conversation(cid, *, call_sid, started, duration=156, user_turns=True,
                 termination="Call ended by remote party"):
    transcript = [{"role": "agent", "message": "你好，我是受用户委托的 AI 语音助理。", "time_in_call_secs": 0}]
    if user_turns:
        transcript += [{"role": "user", "message": "好。你谁？", "time_in_call_secs": 6},
                       {"role": "agent", "message": "我是 Collie。", "time_in_call_secs": 12}]
    return {"conversation_id": cid, "status": "done", "transcript": transcript,
            "metadata": {"start_time_unix_secs": started, "call_duration_secs": duration,
                         "termination_reason": termination,
                         "phone_call": {"direction": "outbound", "external_number": "+16505550123",
                                        "agent_number": CALLER, "call_sid": call_sid}},
            "analysis": {"call_successful": "success", "transcript_summary": "The user answered and chatted briefly."}}


def test_log_links_the_call_to_its_intent_through_the_ledger_hash(tmp_path):
    root = state_dir(tmp_path, binding())
    placed = json.loads(make_tool(root).run(ARGS, ctx(root)))          # dispatched with CALL_SID
    iid = placed["receipt"]["intent_id"]
    convs = {
        "conv_older000000": conversation("conv_older000000", call_sid=OTHER_SID, started=1_700_000_000 - 3600),
        "conv_thisone0000": conversation("conv_thisone0000", call_sid=CALL_SID, started=1_700_000_000 + 9),
    }
    transport = FakeConversationTransport(convs)
    log = tool_mod.PhoneCallLogTool(
        state_dir=root, transport=transport, clock=lambda: 1_700_000_000 + 60,
        api_key=provider.EnvironmentApiKeySource({"ELEVENLABS_API_KEY": API_KEY}))
    text = log.run({"intent_id": iid}, ctx(root))
    out = json.loads(text)
    assert out["matched"] is True and out["receipt_status"] == "disclosure_pending"
    assert "provider record" in out["receipt_note"]
    call = out["call"]
    assert call["conversation_id"] == "conv_thisone0000" and call["call_sid"] == CALL_SID
    assert call["answered"] is True and call["user_turns"] == 1 and call["duration_seconds"] == 156
    assert call["termination_reason"] == "Call ended by remote party"
    assert call["to"] == "••••••0123" and "+16505550123" not in text and API_KEY not in text
    assert call["transcript"][1]["role"] == "user" and "你谁" in call["transcript"][1]["text"]
    # Newest first, stop at the match: the older conversation was never fetched.
    assert not any(u.endswith("conv_older000000") for u in transport.urls)


def test_log_reports_no_match_honestly_and_lists_recent_calls(tmp_path):
    root = state_dir(tmp_path, binding())
    placed = json.loads(make_tool(root).run(ARGS, ctx(root)))
    iid = placed["receipt"]["intent_id"]
    convs = {"conv_unrelated00": conversation("conv_unrelated00", call_sid=OTHER_SID,
                                              started=1_700_000_000 + 5, user_turns=False)}
    log = tool_mod.PhoneCallLogTool(
        state_dir=root, transport=FakeConversationTransport(convs), clock=lambda: 1_700_000_000 + 60,
        api_key=provider.EnvironmentApiKeySource({"ELEVENLABS_API_KEY": API_KEY}))
    miss = json.loads(log.run({"intent_id": iid}, ctx(root)))
    assert miss["matched"] is False and miss["listed_in_window"] == 1
    listed = json.loads(log.run({"hours": 1, "transcript": False}, ctx(root)))
    assert len(listed["calls"]) == 1
    assert listed["calls"][0]["answered"] is False and "transcript" not in listed["calls"][0]
    assert log.run({"intent_id": "call_nope"}, ctx(root)).startswith("ERROR: unknown intent_id")
    assert log.run({"hours": 0}, ctx(root)).startswith("ERROR: hours")


def test_log_provider_rejection_is_a_clean_error(tmp_path):
    root = state_dir(tmp_path, binding())
    log = tool_mod.PhoneCallLogTool(
        state_dir=root, transport=FakeConversationTransport({}, list_status=401),
        clock=lambda: 1_700_000_000,
        api_key=provider.EnvironmentApiKeySource({"ELEVENLABS_API_KEY": API_KEY}))
    assert log.run({}, ctx(root)).startswith("ERROR: provider listing was rejected (HTTP 401)")


def test_conversation_transport_allowlists_only_conversation_endpoints():
    t = tool_mod.ConversationTransport(opener=None)
    for bad in ("https://api.elevenlabs.io/v1/convai/agents/x", "http://api.elevenlabs.io/v1/convai/conversations",
                tool_mod._CONV_PREFIX + "/../secrets", tool_mod._CONV_PREFIX + "/bad id"):
        with pytest.raises(provider.ConfigurationUnavailable):
            t.get_json(bad, headers={}, timeout=1)


def test_log_tool_is_registered_and_read_only():
    from harness import risk
    reg = ToolRegistry()
    tool_mod.register_telephony(reg)
    assert "phone_call_log" in reg.names()
    assert risk.classify("phone_call_log") is risk.RiskClass.READ


def test_log_still_links_after_the_local_lease_lapsed(tmp_path):
    """Collie gets no provider events, so the local receipt decays to `uncertain` once its lease
    lapses. That must not hide the provider record: the link is by keyed reference, not status."""
    root = state_dir(tmp_path, binding())
    placed = json.loads(make_tool(root).run(ARGS, ctx(root)))
    iid = placed["receipt"]["intent_id"]
    convs = {"conv_thisone0000": conversation("conv_thisone0000", call_sid=CALL_SID, started=1_700_000_000 + 9)}
    log = tool_mod.PhoneCallLogTool(
        state_dir=root, transport=FakeConversationTransport(convs), clock=lambda: 1_700_000_000 + 3600,
        api_key=provider.EnvironmentApiKeySource({"ELEVENLABS_API_KEY": API_KEY}))
    out = json.loads(log.run({"intent_id": iid, "hours": 2}, ctx(root)))
    assert out["matched"] is True and out["receipt_status"] == "uncertain"
    assert out["call"]["answered"] is True and out["call"]["conversation_id"] == "conv_thisone0000"
