import dataclasses
import json
import sqlite3

import pytest

from harness import telephony


def recipient(**overrides):
    values = {
        "number": "+14155550123",
        "consent_basis": telephony.ConsentBasis.USER_DIRECTED,
        "label": "Sam",
        "jurisdiction": "US-CA",
    }
    values.update(overrides)
    return telephony.Recipient(**values)


def message(**overrides):
    values = {
        "collie_id": "rowan-device",
        "idempotency_key": "message:signup:0001",
        "capability_id": "sms.twilio_line",
        "recipient": recipient(),
        "body": "Your appointment is at 3 PM.",
        "disclosure_text": "Hi, this is Rowan, an AI assistant for the account owner.",
        "purpose": telephony.Purpose.SCHEDULING,
        "cost_cap": telephony.MoneyCap("usd", 25),
    }
    values.update(overrides)
    return telephony.MessageIntent(**values)


def call(**overrides):
    values = {
        "collie_id": "rowan-device",
        "idempotency_key": "call:schedule:0001",
        "capability_id": "voice.twilio_outbound",
        "recipient": recipient(),
        "brief": "Move the appointment to Friday afternoon.",
        "disclosure_text": "Hello, I am Rowan, an AI assistant for the account owner.",
        "purpose": telephony.Purpose.SCHEDULING,
        "cost_cap": telephony.MoneyCap("USD", 300),
    }
    values.update(overrides)
    return telephony.CallIntent(**values)


def call_capability(**overrides):
    values = {
        "adapter_id": "voice.twilio_outbound",
        "provider": "twilio",
        "verified_caller_id": "+14155550987",
    }
    values.update(overrides)
    return telephony.programmable_outbound_adapter(**values)


def message_capability(**overrides):
    values = {
        "adapter_id": "sms.twilio_line",
        "provider": "twilio",
        "sender_number": "+14155550987",
        "channels": ("call", "message"),
    }
    values.update(overrides)
    return telephony.programmable_registered_line(**values)


def trusted_registry(capability, *, clock=lambda: 1_700_000_000, ttl=300):
    registry = telephony.CapabilityRegistry(clock=clock)
    registry.register(capability, source="provider_api_probe", ttl_seconds=ttl,
                      evidence_reference="health-check-secret-ref")
    return registry


def durable_ledger(tmp_path, capability, *, name="telephony.db", clock=lambda: 1_700_000_000):
    registry = trusted_registry(capability, clock=clock)
    return telephony.IntentLedger(tmp_path / name, capability_registry=registry, clock=clock), registry


def test_google_voice_is_public_identity_with_manual_handoffs_not_automatic_dispatch(tmp_path):
    capability = telephony.google_voice_assigned_line("+14155550987")
    projection = capability.projection()
    assert projection["ownership"] == "user_owned_assigned_to_collie"
    assert projection["transports"] == {
        "message": "draft_then_user_send", "call": "manual_user_call"}
    assert projection["verified_caller_id"] is False
    assert "+14155550987" not in json.dumps(projection)

    registry = trusted_registry(capability)
    ledger = telephony.IntentLedger(tmp_path / "manual.db", capability_registry=registry)
    manual_message = message(capability_id=capability.capability_id)
    manual_call = call(capability_id=capability.capability_id)
    with pytest.raises(telephony.CapabilityUnavailable, match="manual"):
        ledger.authorize(manual_message, capability)
    with pytest.raises(telephony.CapabilityUnavailable, match="manual"):
        ledger.authorize(manual_call, capability)


def test_verified_external_caller_id_is_call_only_and_registered_line_can_text():
    external = call_capability()
    assert external.projection()["transports"] == {"call": "provider_voice_api"}
    assert external.projection()["registered_sender"] is False
    with pytest.raises(ValueError, match="not SMS"):
        call_capability(channels=("message",))

    line = message_capability()
    assert line.projection()["registered_sender"] is True
    assert line.projection()["transports"] == {
        "call": "provider_voice_api", "message": "provider_sms_api"}
    with pytest.raises(ValueError, match="untrusted"):
        telephony.TelephonyCapability(
            capability_id="bad.adapter", provider="bad", adapter_kind="programmable_line",
            sender_number="+14155550987", ownership="provider_owned_assigned_to_collie",
            transports=(("message", "browser_script"),), registered_sender=True)


def test_registry_rejects_untrusted_source_unhealthy_and_expired_evidence(tmp_path):
    now = [1000]
    clock = lambda: now[0]
    capability = call_capability()
    registry = telephony.CapabilityRegistry(clock=clock)
    with pytest.raises(telephony.CapabilityUnavailable, match="not trusted"):
        registry.register(capability, source="model_claim", ttl_seconds=10)
    registry.register(capability, source="provider_api_probe", ttl_seconds=10)
    with pytest.raises(telephony.ProviderEvidenceRequired, match="not trusted"):
        registry.provider_event(
            capability.capability_id, event_type="call.completed",
            event_reference="local-claim", source="local_vault_config")
    ledger = telephony.IntentLedger(tmp_path / "health.db", capability_registry=registry, clock=clock)
    assert ledger.authorize(call(), capability)["status"] == "authorized"
    now[0] = 1011
    with pytest.raises(telephony.CapabilityUnavailable, match="expired"):
        ledger.authorize(call(idempotency_key="call:schedule:0002"), capability)

    registry.register(capability, source="provider_api_probe", status="degraded", ttl_seconds=10)
    with pytest.raises(telephony.CapabilityUnavailable, match="not healthy"):
        registry.resolve(capability.capability_id)


def test_default_ledger_is_registration_only_and_dispatch_fails_closed():
    intent = call()
    ledger = telephony.IntentLedger()
    assert ledger.register(intent)["status"] == "planned"
    with pytest.raises(telephony.DurableLedgerRequired):
        ledger.authorize(intent, call_capability())


def test_intent_and_persistent_ledger_never_expose_sensitive_content(tmp_path):
    capability = message_capability()
    ledger, registry = durable_ledger(tmp_path, capability)
    intent = message()
    receipt = ledger.authorize(intent, capability)
    assert ledger.claim_dispatch(intent.intent_id)["claimed"] is True
    accepted = registry.provider_event(
        capability.capability_id, event_type="message.accepted",
        event_reference="provider-raw-reference-123", source="provider_signed_webhook",
        payload_sha256=intent.provider_payload().sha256())
    receipt = ledger.transition(intent.intent_id, "sent", provider_evidence=accepted)
    serialized = repr(intent) + json.dumps(intent.receipt_projection()) + json.dumps(receipt)
    for forbidden in ("Your appointment", "this is Rowan", "+14155550123", "Sam",
                      "provider-raw-reference-123"):
        assert forbidden not in serialized
    assert receipt["provider_reference_hash"] != "provider-raw-reference-123"
    ledger.close()

    raw = (tmp_path / "telephony.db").read_bytes()
    for forbidden in (b"Your appointment", b"this is Rowan", b"+14155550123",
                      b"provider-raw-reference-123",
                      intent.provider_payload().sha256().encode("ascii")):
        assert forbidden not in raw
    columns = [row[1] for row in sqlite3.connect(tmp_path / "telephony.db").execute(
        "PRAGMA table_info(telephony_intents)")]
    assert not any(name in columns for name in ("phone", "body", "brief", "disclosure_text",
                                                "provider_reference"))


def test_idempotency_is_stable_across_restart_and_scoped_by_collie(tmp_path):
    path = tmp_path / "stable.db"
    first_ledger = telephony.IntentLedger(path)
    first = first_ledger.register(message())
    first_ledger.close()

    second_ledger = telephony.IntentLedger(path)
    same = second_ledger.register(message())
    assert same["intent_id"] == first["intent_id"]
    assert same["receipt_id"] == first["receipt_id"]
    with pytest.raises(telephony.IdempotencyConflict):
        second_ledger.register(message(body="A different appointment message."))
    other = second_ledger.register(message(collie_id="another-collie"))
    assert other["intent_id"] != first["intent_id"]


def test_durable_ledger_fails_closed_when_stable_hmac_key_is_missing_or_wrong(tmp_path):
    sidecar_path = tmp_path / "sidecar.db"
    ledger = telephony.IntentLedger(sidecar_path)
    ledger.register(message())
    ledger.close()
    (tmp_path / "sidecar.db.hmac").unlink()
    with pytest.raises(telephony.DurableLedgerRequired, match="no stable HMAC key"):
        telephony.IntentLedger(sidecar_path)

    supplied_path = tmp_path / "supplied.db"
    supplied = telephony.IntentLedger(supplied_path, fingerprint_key=b"a" * 32)
    supplied.register(message())
    supplied.close()
    with pytest.raises(telephony.DurableLedgerRequired, match="does not match"):
        telephony.IntentLedger(supplied_path, fingerprint_key=b"b" * 32)


@pytest.mark.parametrize("text,encoding,units,segments", [
    ("a" * 160, "gsm-7", 160, 1),
    ("a" * 161, "gsm-7", 161, 2),
    ("^" * 80, "gsm-7", 160, 1),
    ("^" * 81, "gsm-7", 162, 2),
    ("你" * 70, "ucs-2", 70, 1),
    ("你" * 71, "ucs-2", 71, 2),
    ("😀" * 35, "ucs-2", 70, 1),
    ("😀" * 36, "ucs-2", 72, 2),
])
def test_sms_segment_calculation(text, encoding, units, segments):
    info = telephony.sms_segment_info(text)
    assert (info.encoding, info.units, info.segments) == (encoding, units, segments)


def test_sms_authorization_counts_disclosure_and_enforces_segment_cap(tmp_path):
    capability = message_capability()
    ledger, _registry = durable_ledger(tmp_path, capability)
    intent = message(
        body="你" * 55, disclosure_text="我是经授权的AI助理。" * 2, max_segments=1)
    assert intent.segment_info().encoding == "ucs-2"
    assert intent.segment_info().segments == 2
    with pytest.raises(telephony.SegmentCapExceeded):
        ledger.authorize(intent, capability)
    with pytest.raises(KeyError):
        ledger.receipt(intent.intent_id)


def test_sms_payload_always_prefixes_disclosure_and_acceptance_attests_exact_payload(tmp_path):
    capability = message_capability()
    ledger, registry = durable_ledger(tmp_path, capability)
    intent = message()
    payload = intent.provider_payload()
    request = payload.provider_request()
    assert request["text"].startswith(intent.disclosure_text + "\n")
    assert request["ai_disclosure"] == {
        "required": True, "position": "prefix", "text": intent.disclosure_text}
    ledger.authorize(intent, capability)
    assert ledger.claim_dispatch(intent.intent_id)["claimed"] is True

    without_payload = registry.provider_event(
        capability.capability_id, event_type="message.accepted", event_reference="msg-1",
        source="provider_signed_webhook")
    with pytest.raises(telephony.ProviderEvidenceRequired, match="payload"):
        ledger.transition(intent.intent_id, "sent", provider_evidence=without_payload)
    wrong_payload = registry.provider_event(
        capability.capability_id, event_type="message.accepted", event_reference="msg-1",
        source="provider_signed_webhook", payload_sha256="0" * 64)
    with pytest.raises(telephony.ProviderEvidenceRequired, match="payload"):
        ledger.transition(intent.intent_id, "sent", provider_evidence=wrong_payload)
    accepted = registry.provider_event(
        capability.capability_id, event_type="message.accepted", event_reference="msg-1",
        source="provider_signed_webhook", payload_sha256=payload.sha256())
    assert ledger.transition(intent.intent_id, "sent", provider_evidence=accepted)["status"] == "sent"
    with pytest.raises(telephony.ProviderEvidenceRequired):
        ledger.transition(intent.intent_id, "delivered")
    delivered = registry.provider_event(
        capability.capability_id, event_type="message.delivered", event_reference="msg-2",
        source="provider_signed_webhook")
    with pytest.raises(telephony.ProviderEvidenceRequired, match="another call or message"):
        ledger.transition(intent.intent_id, "delivered", provider_evidence=delivered)
    delivered = registry.provider_event(
        capability.capability_id, event_type="message.delivered", event_reference="msg-1",
        source="provider_signed_webhook")
    assert ledger.transition(intent.intent_id, "delivered", provider_evidence=delivered)["status"] == "delivered"


def test_call_cannot_enter_conversation_without_trusted_disclosure_playback(tmp_path):
    capability = call_capability()
    ledger, registry = durable_ledger(tmp_path, capability)
    intent = call()
    ledger.authorize(intent, capability, estimated_cost_minor=80)
    claim = ledger.claim_dispatch(intent.intent_id)
    ledger.transition(
        intent.intent_id, "dialing", provider_reference="raw-call-ref",
        dispatch_claim_token=claim["claim_token"])
    ledger.transition(
        intent.intent_id, "disclosure_pending",
        dispatch_claim_token=claim["claim_token"], dispatch_lease_seconds=120)
    with pytest.raises(telephony.ProviderEvidenceRequired):
        ledger.transition(intent.intent_id, "in_progress")
    forged = telephony.ProviderEventEvidence(
        capability_id=capability.capability_id, provider="twilio",
        event_type="call.ai_disclosure_played", occurred_at=1_700_000_000,
        source="provider_signed_webhook", event_reference="forged")
    with pytest.raises(telephony.ProviderEvidenceRequired):
        ledger.transition(intent.intent_id, "in_progress", provider_evidence=forged)
    wrong_call = registry.provider_event(
        capability.capability_id, event_type="call.ai_disclosure_played",
        event_reference="another-call-ref", source="provider_signed_webhook")
    with pytest.raises(telephony.ProviderEvidenceRequired, match="another call or message"):
        ledger.transition(intent.intent_id, "in_progress", provider_evidence=wrong_call)
    evidence = registry.provider_event(
        capability.capability_id, event_type="call.ai_disclosure_played",
        event_reference="raw-call-ref", source="provider_signed_webhook")
    active = ledger.transition(intent.intent_id, "in_progress", provider_evidence=evidence)
    assert active["status"] == "in_progress"
    assert active["disclosure_evidence"] is True
    with pytest.raises(telephony.ProviderEvidenceRequired):
        ledger.transition(intent.intent_id, "completed")
    completed = registry.provider_event(
        capability.capability_id, event_type="call.completed", event_reference="raw-call-ref",
        source="provider_signed_webhook")
    assert ledger.transition(intent.intent_id, "completed", provider_evidence=completed,
                             actual_cost_minor=250)["status"] == "completed"


def test_live_provider_failure_requires_evidence_bound_to_the_same_call(tmp_path):
    capability = call_capability()
    ledger, registry = durable_ledger(tmp_path, capability)
    intent = call(idempotency_key="call:schedule:failed-evidence")
    ledger.authorize(intent, capability)
    claim = ledger.claim_dispatch(intent.intent_id)
    ledger.transition(
        intent.intent_id, "disclosure_pending", provider_reference="call-resource-a",
        dispatch_claim_token=claim["claim_token"], dispatch_lease_seconds=120)
    played = registry.provider_event(
        capability.capability_id, event_type="call.ai_disclosure_played",
        event_reference="call-resource-a", source="provider_signed_webhook")
    ledger.transition(intent.intent_id, "in_progress", provider_evidence=played)
    with pytest.raises(telephony.ProviderEvidenceRequired):
        ledger.transition(intent.intent_id, "failed")
    wrong = registry.provider_event(
        capability.capability_id, event_type="call.failed",
        event_reference="call-resource-b", source="provider_signed_webhook")
    with pytest.raises(telephony.ProviderEvidenceRequired, match="another call or message"):
        ledger.transition(intent.intent_id, "failed", provider_evidence=wrong)
    right = registry.provider_event(
        capability.capability_id, event_type="call.failed",
        event_reference="call-resource-a", source="provider_signed_webhook")
    assert ledger.transition(
        intent.intent_id, "failed", provider_evidence=right)["status"] == "failed"


def test_recording_requires_policy_consent_and_disclosure_evidence(tmp_path):
    with pytest.raises(ValueError):
        call(recording_requested=True, recording_policy=telephony.RecordingPolicy.DISABLED)
    capability = call_capability()
    ledger, registry = durable_ledger(tmp_path, capability)
    intent = call(
        idempotency_key="call:schedule:0003", recording_requested=True,
        recording_policy=telephony.RecordingPolicy.EXPLICIT_CONSENT_EACH_CALL)
    ledger.authorize(intent, capability)
    claim = ledger.claim_dispatch(intent.intent_id)
    ledger.transition(
        intent.intent_id, "disclosure_pending",
        dispatch_claim_token=claim["claim_token"], dispatch_lease_seconds=120)
    evidence = registry.provider_event(
        capability.capability_id, event_type="call.ai_disclosure_played",
        event_reference="played-recorded", source="provider_signed_webhook")
    with pytest.raises(ValueError, match="consent outcome"):
        ledger.transition(intent.intent_id, "in_progress", provider_evidence=evidence)
    active = ledger.transition(intent.intent_id, "in_progress", provider_evidence=evidence,
                               recording_consent_obtained=True)
    assert active["recording_consent_obtained"] is True
    assert active["recording_enabled"] is True


def test_restart_marks_submitted_state_uncertain_and_requires_reconciliation(tmp_path):
    capability = message_capability()
    now = [1_700_000_000]
    clock = lambda: now[0]
    registry = trusted_registry(capability, clock=clock)
    path = tmp_path / "crash.db"
    intent = message()
    first = telephony.IntentLedger(path, capability_registry=registry, clock=clock)
    first.authorize(intent, capability)
    assert first.claim_dispatch(intent.intent_id, lease_seconds=5)["claimed"] is True
    first.close()
    now[0] += 6

    recovered = telephony.IntentLedger(path, capability_registry=registry, clock=clock)
    receipt = recovered.receipt(intent.intent_id)
    assert receipt["status"] == "uncertain"
    assert receipt["requires_reconciliation"] is True
    assert receipt["recovery_from_status"] == "sending"
    with pytest.raises(telephony.ReconciliationRequired):
        recovered.authorize(intent, capability)
    wrong_kind = registry.provider_event(
        capability.capability_id, event_type="call.failed",
        event_reference="wrong-kind", source="provider_signed_webhook")
    with pytest.raises(telephony.ProviderEvidenceRequired, match="call evidence"):
        recovered.reconcile(intent.intent_id, provider_evidence=wrong_kind)
    absent = registry.provider_event(
        capability.capability_id, event_type="submission.absent",
        event_reference="lookup-not-found-1", source="provider_signed_webhook")
    reconciled = recovered.reconcile(intent.intent_id, provider_evidence=absent)
    assert reconciled["status"] == "authorized"
    assert reconciled["requires_reconciliation"] is False
    assert recovered.claim_dispatch(intent.intent_id)["receipt"]["status"] == "sending"


def test_accepted_message_cannot_be_retried_on_later_absence(tmp_path):
    capability = message_capability()
    clock = lambda: 1_700_000_000
    registry = trusted_registry(capability, clock=clock)
    path = tmp_path / "accepted-crash.db"
    intent = message()
    first = telephony.IntentLedger(path, capability_registry=registry, clock=clock)
    first.authorize(intent, capability)
    assert first.claim_dispatch(intent.intent_id)["claimed"] is True
    accepted = registry.provider_event(
        capability.capability_id, event_type="message.accepted",
        event_reference="accepted-before-crash", source="provider_signed_webhook",
        payload_sha256=intent.provider_payload().sha256())
    first.transition(intent.intent_id, "sent", provider_evidence=accepted)
    first.close()

    recovered = telephony.IntentLedger(path, capability_registry=registry, clock=clock)
    assert recovered.receipt(intent.intent_id)["recovery_from_status"] == "sent"
    absent = registry.provider_event(
        capability.capability_id, event_type="submission.absent",
        event_reference="late-provider-absence", source="provider_signed_webhook")
    with pytest.raises(telephony.ProviderEvidenceRequired, match="cannot authorize retry"):
        recovered.reconcile(intent.intent_id, provider_evidence=absent)
    delivered = registry.provider_event(
        capability.capability_id, event_type="message.delivered",
        event_reference="accepted-before-crash", source="provider_signed_webhook")
    assert recovered.reconcile(intent.intent_id, provider_evidence=delivered)["status"] == "delivered"


def test_cost_cap_invalid_transition_and_input_guards(tmp_path):
    capability = call_capability()
    ledger, _registry = durable_ledger(tmp_path, capability)
    with pytest.raises(telephony.CostCapExceeded):
        ledger.authorize(call(), capability, estimated_cost_minor=301)
    intent = call(idempotency_key="call:schedule:0004")
    ledger.authorize(intent, capability)
    with pytest.raises(telephony.InvalidTransition):
        ledger.transition(intent.intent_id, "in_progress")
    with pytest.raises(dataclasses.FrozenInstanceError):
        intent.brief = "mutated"
    with pytest.raises(ValueError):
        recipient(do_not_contact=True)
    with pytest.raises(ValueError):
        recipient(consent_basis="scraped_list")


@pytest.mark.parametrize("number", ["4155550123", "+0123", "+1 415 555 0123, +14155550124"])
def test_recipient_is_one_strict_e164_number(number):
    with pytest.raises(ValueError):
        recipient(number=number)
