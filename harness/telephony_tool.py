"""Agent tools for Collie's programmable voice line (verified Twilio caller ID + ElevenLabs agent).

``phone_call`` places exactly ONE outbound Mandarin voice call through the fail-closed
adapter in :mod:`telephony_twilio`.  Everything secret stays in the OS vault; this module
reads only the non-secret binding metadata the host keeps in
``<state_dir>/telephony-provider.json`` and the durable intent ledger beside it.

The tools are registered whenever the package is present so the model can SEE that this
Collie has a phone.  ``phone_call`` is classified EXTERNAL (risk.py) and carries no
standing rule, so every dial is asked for separately in any gate mode that asks at all.
``phone_call_status`` is a local ledger read.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

from .tools import Tool, ToolCtx, _need_str

PROVIDER_FILE = "telephony-provider.json"
LEDGER_FILE = "telephony-intents.db"

# Emergency / short service codes are never automated (docs/voice-telephony.md).
_EMERGENCY = frozenset({"911", "112", "999", "000", "110", "119", "120", "122", "988"})
# Conservative per-minute estimate (ElevenLabs Agents + Twilio outbound, US), in cents.
_CENTS_PER_MINUTE = 15
_MAX_MINUTES = 15

DEFAULT_DISCLOSURE = "你好，我是受用户委托的 AI 语音助理 Collie，用的是合成声音。现在方便聊几句吗？"


def _state_dir() -> str:
    from .controlplane import state_dir
    return state_dir()


def normalize_dial_number(raw: str) -> str:
    """Accept E.164, or a bare US 10/11-digit number, and return strict E.164."""
    from .telephony import normalize_e164
    text = str(raw or "").strip()
    digits = re.sub(r"[^\d+]", "", text)
    if digits and not digits.startswith("+"):
        if len(digits) == 10:
            digits = "+1" + digits
        elif len(digits) == 11 and digits.startswith("1"):
            digits = "+" + digits
    if re.sub(r"\D", "", text) in _EMERGENCY or digits.lstrip("+") in _EMERGENCY:
        raise ValueError("emergency and service numbers are never dialled automatically")
    return normalize_e164(digits)


def load_binding(state_dir: str | None = None) -> dict:
    """Non-secret host binding: caller number, agent ids, vault REFERENCES (never values)."""
    root = state_dir or _state_dir()
    with open(os.path.join(root, PROVIDER_FILE), "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("telephony binding file is not an object")
    return data


def build_stack(binding: dict, *, state_dir: str, api_key=None, transport=None,
                clock=time.time):
    """Config + vault key source + durable ledger + adapter, from host metadata only."""
    from . import telephony
    from . import telephony_twilio as provider
    el = binding.get("elevenlabs") or {}
    tw = binding.get("twilio") or {}
    collie_id = str(binding.get("collie_id") or "")
    profile = el.get("voice_profile") or {}
    config = provider.TwilioElevenLabsConfig(
        collie_id=collie_id,
        caller_number=str(tw.get("caller_number") or ""),
        agent_id=str(el.get("agent_id") or ""),
        agent_phone_number_id=str(el.get("agent_phone_number_id") or ""),
        overrides_enabled=True,
        caller_id_binding_verified=bool(tw.get("caller_id_binding_verified")),
        language=str(profile.get("language") or "zh"),
        tts_model_id=str(profile.get("model_id") or "eleven_v3_conversational"),
        tts_stability=float(profile.get("stability", 0.38)),
        tts_similarity_boost=float(profile.get("similarity_boost", 0.75)),
        tts_speed=float(profile.get("speed", 0.95)),
        llm_temperature=float(profile.get("llm_temperature", 0.45)),
        llm_max_tokens=int(profile.get("llm_max_tokens", 120)),
    )
    if api_key is None:
        from .identityvault import IdentityVault
        api_key = provider.VaultApiKeySource(
            IdentityVault(state_dir=state_dir), ref=str(el.get("api_key_ref") or ""),
            collie_id=collie_id,
            account=str(el.get("vault_account") or "telephony.twilio_elevenlabs"),
            kind=str(el.get("vault_kind") or "elevenlabs_api_key"))
    registry = telephony.CapabilityRegistry(clock=clock)
    ledger = telephony.IntentLedger(os.path.join(state_dir, LEDGER_FILE),
                                    capability_registry=registry, clock=clock)
    adapter = provider.TwilioElevenLabsOutbound(
        config, api_key=api_key, ledger=ledger, registry=registry, transport=transport)
    return adapter, ledger, registry


def _receipt_summary(receipt: dict | None) -> dict:
    if not receipt:
        return {}
    keys = ("intent_id", "receipt_id", "status", "requires_reconciliation",
            "max_duration_seconds", "cost_cap", "created_at", "updated_at")
    return {k: receipt.get(k) for k in keys if k in receipt}


class PhoneCallTool(Tool):
    name = "phone_call"
    risk = "external"
    description = (
        "Place ONE outbound phone call on Collie's own voice line (verified Twilio caller ID + "
        "ElevenLabs realtime agent, Mandarin, the agent's configured voice). The call opens "
        "with a spoken AI disclosure (`disclosure`, first sentence) and then follows `brief` "
        "(Mandarin, >=10 CJK chars: who you are calling, what to talk about, what NOT to "
        "promise, when to hang up). Args: to (E.164 or US 10-digit), brief, optional "
        "disclosure (Mandarin, default discloses an AI assistant), label (recipient name), "
        "max_minutes (1-15, default 5), cost_cap_usd (default 3), jurisdiction (e.g. US-CA), "
        "dry_run (validate + show what would be sent, no network, no ledger row), attempt "
        "(default 1; identical args replay the existing receipt instead of dialling twice — "
        "raise attempt to deliberately place a new call). Never for emergency numbers, "
        "harassment, or impersonation. A number the user typed in their own message is "
        "user-directed and needs no further approval; any other number is asked about. Collie "
        "receives no in-call transcript: a provider-accepted call reports status "
        "disclosure_pending; afterwards phone_call_log (by intent_id) tells whether it was "
        "answered, how long, why it ended, and what was said.")
    schema = {"type": "object", "properties": {
        "to": {"type": "string"},
        "brief": {"type": "string"},
        "disclosure": {"type": "string"},
        "label": {"type": "string"},
        "max_minutes": {"type": "integer"},
        "cost_cap_usd": {"type": "number"},
        "jurisdiction": {"type": "string"},
        "dry_run": {"type": "boolean"},
        "attempt": {"type": "integer"}},
        "required": ["to", "brief"]}

    def __init__(self, *, state_dir: str | None = None, api_key=None, transport=None,
                 clock=time.time):
        self._state_dir = state_dir
        self._api_key = api_key
        self._transport = transport
        self._clock = clock

    def run(self, args: dict, ctx: ToolCtx) -> str:
        from . import telephony
        from . import telephony_twilio as provider
        from .identityvault import VaultError

        to, err = _need_str(args, "to")
        if err:
            return err
        brief, err = _need_str(args, "brief")
        if err:
            return err
        disclosure = str(args.get("disclosure") or "").strip() or DEFAULT_DISCLOSURE
        label = str(args.get("label") or "").strip()
        jurisdiction = str(args.get("jurisdiction") or "").strip()
        dry_run = bool(args.get("dry_run"))
        try:
            # `None` means "default"; an explicit 0 is an explicit (and invalid) value.
            raw_minutes, raw_attempt, raw_cap = (
                args.get("max_minutes"), args.get("attempt"), args.get("cost_cap_usd"))
            minutes = 5 if raw_minutes is None else int(raw_minutes)
            attempt = 1 if raw_attempt is None else int(raw_attempt)
            cap_cents = int(round((3.0 if raw_cap is None else float(raw_cap)) * 100))
        except (TypeError, ValueError):
            return "ERROR: max_minutes, attempt must be integers and cost_cap_usd a number"
        if minutes < 1 or minutes > _MAX_MINUTES:
            return "ERROR: max_minutes must be between 1 and %d" % _MAX_MINUTES
        if attempt < 1 or attempt > 99:
            return "ERROR: attempt must be between 1 and 99"
        try:
            number = normalize_dial_number(to)
        except ValueError as e:
            return "ERROR: %s (got %r)" % (e, to)

        state_dir = self._state_dir or _state_dir()
        try:
            binding = load_binding(state_dir)
        except FileNotFoundError:
            return ("ERROR: no voice line is bound on this Collie (missing %s). Connect Twilio + "
                    "ElevenLabs in Settings → Identity first." % PROVIDER_FILE)
        except (OSError, ValueError) as e:
            return "ERROR: voice line binding is unreadable: %s" % e
        try:
            adapter, ledger, _registry = build_stack(
                binding, state_dir=state_dir, api_key=self._api_key,
                transport=self._transport, clock=self._clock)
        except (ValueError, TypeError, telephony.TelephonyError,
                provider.TwilioElevenLabsError, VaultError) as e:
            return "ERROR: voice line configuration is incomplete or invalid: %s" % e

        estimated = minutes * _CENTS_PER_MINUTE
        if cap_cents < estimated:
            return ("ERROR: cost_cap_usd %.2f is below the %d-minute estimate of $%.2f; raise the "
                    "cap or shorten max_minutes" % (cap_cents / 100.0, minutes, estimated / 100.0))
        key_material = "\0".join([number, brief, disclosure, str(attempt)]).encode("utf-8")
        idempotency_key = "phone_call:" + hashlib.sha256(key_material).hexdigest()[:32]
        try:
            intent = telephony.CallIntent(
                collie_id=str(binding.get("collie_id") or ""),
                idempotency_key=idempotency_key,
                capability_id=provider.CAPABILITY_ID,
                recipient=telephony.Recipient(
                    number, consent_basis=telephony.ConsentBasis.USER_DIRECTED,
                    label=label, jurisdiction=jurisdiction),
                brief=brief, disclosure_text=disclosure,
                purpose=telephony.Purpose.USER_DIRECTED,
                cost_cap=telephony.MoneyCap("USD", cap_cents),
                max_duration_seconds=minutes * 60)
        except ValueError as e:
            return "ERROR: %s" % e

        masked = telephony.mask_number(number)
        try:
            if dry_run:
                out = adapter.dry_run(intent, estimated_cost_minor=estimated)
                return json.dumps({
                    "dry_run": True, "submitted": False, "to": masked, "label": label,
                    "max_minutes": minutes, "estimated_usd": estimated / 100.0,
                    "first_message": disclosure,
                    "provider_request": out.get("provider_request"),
                    "adapter": {k: out["adapter"].get(k) for k in
                                ("provider", "voice_provider", "line_hint", "tts_model",
                                 "language_override")},
                }, ensure_ascii=False)
            result = adapter.dispatch(intent, estimated_cost_minor=estimated)
        except provider.ProviderRejected as e:
            return ("ERROR: the provider rejected the call: %s. receipt=%s" %
                    (e, json.dumps(_receipt_summary(getattr(e, "receipt", None)))))
        except provider.ProviderSubmissionUncertain as e:
            return ("UNCERTAIN: %s. Do NOT redial; the ledger blocks a retry until it is "
                    "reconciled. receipt=%s" %
                    (e, json.dumps(_receipt_summary(getattr(e, "receipt", None)))))
        except telephony.ReconciliationRequired as e:
            return ("ERROR: an earlier attempt with these exact args is in an uncertain state "
                    "(%s); it must be reconciled before the same call can be retried. Pass a "
                    "higher `attempt` only if the user confirms a NEW call is wanted." % e)
        except (telephony.TelephonyError, provider.TwilioElevenLabsError) as e:
            return "ERROR: %s" % e
        except VaultError as e:
            return "ERROR: the OS credential vault refused the ElevenLabs key: %s" % e
        except OSError as e:
            return "ERROR: telephony ledger/IO failure: %s" % e
        receipt = result.get("receipt") or {}
        status = str(receipt.get("status") or "")
        summary = {
            "submitted": bool(result.get("submitted")),
            "replayed": bool(result.get("replayed")),
            "to": masked, "label": label, "max_minutes": minutes,
            "estimated_usd": estimated / 100.0,
            "receipt": _receipt_summary(receipt),
        }
        if result.get("submitted"):
            summary["note"] = (
                "provider accepted the call; it is now ringing/connected. Collie gets no live "
                "transcript — the local receipt stays disclosure_pending until the lease lapses. "
                "After the call, phone_call_log with this intent_id returns the provider record: "
                "answered or not, duration, why it ended, summary and transcript.")
        elif result.get("replayed"):
            summary["note"] = ("identical call already exists (status %s); nothing was dialled. "
                               "Raise `attempt` only if the user wants another call." % status)
        return json.dumps(summary, ensure_ascii=False)


class PhoneCallStatusTool(Tool):
    name = "phone_call_status"
    risk = "read"
    description = (
        "Read the durable receipt of a phone_call by intent_id (status: planned, authorized, "
        "dialing, disclosure_pending, in_progress, completed, failed, cancelled, uncertain). "
        "Local ledger read, no network. Collie receives no provider call events yet, so a "
        "provider-accepted call stays disclosure_pending until its lease lapses, then reads "
        "uncertain — that is not proof the call failed. Args: intent_id.")
    schema = {"type": "object", "properties": {"intent_id": {"type": "string"}},
              "required": ["intent_id"]}

    def __init__(self, *, state_dir: str | None = None, clock=time.time):
        self._state_dir = state_dir
        self._clock = clock

    def run(self, args: dict, ctx: ToolCtx) -> str:
        from . import telephony
        intent_id, err = _need_str(args, "intent_id")
        if err:
            return err
        state_dir = self._state_dir or _state_dir()
        path = os.path.join(state_dir, LEDGER_FILE)
        if not os.path.exists(path):
            return "ERROR: no telephony ledger on this Collie (no call was ever placed here)"
        try:
            ledger = telephony.IntentLedger(
                path, capability_registry=telephony.CapabilityRegistry(clock=self._clock),
                clock=self._clock)
            receipt = ledger.receipt(intent_id)
        except KeyError:
            return "ERROR: unknown intent_id %r" % intent_id
        except (telephony.TelephonyError, OSError, ValueError) as e:
            return "ERROR: telephony ledger is unavailable: %s" % e
        return json.dumps(_receipt_summary(receipt), ensure_ascii=False)


_CONV_PREFIX = "https://api.elevenlabs.io/v1/convai/conversations"
_CONV_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,159}$")
_MAX_LOG_BODY = 1024 * 1024
_TRANSCRIPT_TURNS = 80
_TRANSCRIPT_CHARS = 240


class ConversationTransport:
    """GET-only, allowlisted, redirect-free reader for the provider's conversation records."""

    def __init__(self, *, opener=None):
        import urllib.request
        from .telephony_twilio import _RejectRedirects
        self._opener = opener or urllib.request.build_opener(_RejectRedirects())

    def get_json(self, url: str, *, headers, timeout: float):
        import urllib.error
        import urllib.request
        from .telephony_twilio import ConfigurationUnavailable, ProviderHttpResponse
        if not (url == _CONV_PREFIX or url.startswith(_CONV_PREFIX + "?")
                or (url.startswith(_CONV_PREFIX + "/")
                    and _CONV_ID.fullmatch(url[len(_CONV_PREFIX) + 1:]))):
            raise ConfigurationUnavailable("provider conversation endpoint is not allowlisted")
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        try:
            with self._opener.open(request, timeout=timeout) as response:
                body = response.read(_MAX_LOG_BODY + 1)
                if len(body) > _MAX_LOG_BODY:
                    raise ConfigurationUnavailable("provider conversation record exceeded the safe limit")
                return ProviderHttpResponse(int(response.status), body)
        except urllib.error.HTTPError as exc:
            body = exc.read(_MAX_LOG_BODY + 1)
            return ProviderHttpResponse(int(exc.code), body if len(body) <= _MAX_LOG_BODY else b"")


def _conversation_summary(detail: dict, listed: dict | None = None, *,
                          transcript: bool = True) -> dict:
    listed = listed or {}
    meta = detail.get("metadata") or {}
    phone = meta.get("phone_call") or {}
    analysis = detail.get("analysis") or {}
    turns = detail.get("transcript") or []
    started = int(meta.get("start_time_unix_secs") or listed.get("start_time_unix_secs") or 0)
    duration = meta.get("call_duration_secs")
    if duration is None:
        duration = listed.get("call_duration_secs")
    user_turns = [t for t in turns if isinstance(t, dict) and t.get("role") == "user"
                  and str(t.get("message") or "").strip()]
    status = str(detail.get("status") or listed.get("status") or "")
    termination = str(meta.get("termination_reason") or "")
    external = str(phone.get("external_number") or "")
    out = {
        "conversation_id": str(detail.get("conversation_id") or listed.get("conversation_id") or ""),
        "started_at": started,
        "started_local": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)) if started else "",
        "status": status,
        "duration_seconds": duration,
        "termination_reason": termination,
        # A conversation with real user speech is a call that was answered; zero user turns
        # on an outbound call is voicemail/no-answer/immediate hang-up territory.
        "answered": bool(user_turns),
        "user_turns": len(user_turns),
        "direction": str(listed.get("direction") or phone.get("direction") or ""),
        "to": ("••••••" + external[-4:]) if external else "",
        "from": str(phone.get("agent_number") or ""),
        "call_sid": str(phone.get("call_sid") or ""),
        "call_successful": analysis.get("call_successful") or listed.get("call_successful"),
        "summary": str(analysis.get("transcript_summary") or "")[:1200],
    }
    if transcript:
        shown = []
        for t in turns[:_TRANSCRIPT_TURNS]:
            if not isinstance(t, dict):
                continue
            msg = str(t.get("message") or "").replace("\n", " ").strip()
            if not msg:
                continue
            shown.append({"t": t.get("time_in_call_secs"), "role": t.get("role"),
                          "text": msg[:_TRANSCRIPT_CHARS]})
        out["transcript"] = shown
        out["transcript_truncated"] = len(turns) > _TRANSCRIPT_TURNS
    return out


class PhoneCallLogTool(Tool):
    name = "phone_call_log"
    risk = "read"
    description = (
        "Read the PROVIDER-side record of Collie's outbound calls (ElevenLabs conversation "
        "history for this Collie's voice agent): whether the call was answered, when, how long, "
        "why it ended, the provider's summary, and the turn-by-turn transcript. Read-only. "
        "Args: intent_id (optional — from phone_call; links exactly that call via the ledger's "
        "keyed provider reference), hours (look-back window, default 24), limit (max "
        "conversations, default 5), transcript (bool, default true).")
    schema = {"type": "object", "properties": {
        "intent_id": {"type": "string"}, "hours": {"type": "number"},
        "limit": {"type": "integer"}, "transcript": {"type": "boolean"}}}

    def __init__(self, *, state_dir: str | None = None, api_key=None, transport=None,
                 clock=time.time):
        self._state_dir = state_dir
        self._api_key = api_key
        self._transport = transport
        self._clock = clock

    def run(self, args: dict, ctx: ToolCtx) -> str:
        from . import telephony
        from . import telephony_twilio as provider
        from .identityvault import VaultError
        intent_id = str(args.get("intent_id") or "").strip()
        want_transcript = args.get("transcript")
        want_transcript = True if want_transcript is None else bool(want_transcript)
        try:
            hours = float(args.get("hours") if args.get("hours") is not None else 24)
            limit = int(args.get("limit") if args.get("limit") is not None else 5)
        except (TypeError, ValueError):
            return "ERROR: hours must be a number and limit an integer"
        if hours <= 0 or hours > 24 * 30:
            return "ERROR: hours must be between 0 and 720"
        if limit < 1 or limit > 30:
            return "ERROR: limit must be between 1 and 30"
        state_dir = self._state_dir or _state_dir()
        try:
            binding = load_binding(state_dir)
        except FileNotFoundError:
            return "ERROR: no voice line is bound on this Collie (missing %s)" % PROVIDER_FILE
        except (OSError, ValueError) as e:
            return "ERROR: voice line binding is unreadable: %s" % e
        el = binding.get("elevenlabs") or {}
        agent_id = str(el.get("agent_id") or "")
        collie_id = str(binding.get("collie_id") or "")
        api_key = self._api_key
        if api_key is None:
            try:
                from .identityvault import IdentityVault
                api_key = provider.VaultApiKeySource(
                    IdentityVault(state_dir=state_dir), ref=str(el.get("api_key_ref") or ""),
                    collie_id=collie_id,
                    account=str(el.get("vault_account") or "telephony.twilio_elevenlabs"),
                    kind=str(el.get("vault_kind") or "elevenlabs_api_key"))
            except (ValueError, VaultError) as e:
                return "ERROR: voice line credential reference is invalid: %s" % e
        ledger = None
        if intent_id:
            path = os.path.join(state_dir, LEDGER_FILE)
            if not os.path.exists(path):
                return "ERROR: no telephony ledger on this Collie (no call was ever placed here)"
            try:
                ledger = telephony.IntentLedger(
                    path, capability_registry=telephony.CapabilityRegistry(clock=self._clock),
                    clock=self._clock)
                receipt = ledger.receipt(intent_id)
            except KeyError:
                return "ERROR: unknown intent_id %r" % intent_id
            except (telephony.TelephonyError, OSError, ValueError) as e:
                return "ERROR: telephony ledger is unavailable: %s" % e
        transport = self._transport or ConversationTransport()
        now = float(self._clock())
        since = now - hours * 3600.0

        def _read(secret: bytearray):
            key = bytes(secret).decode("utf-8")
            headers = {"xi-api-key": key, "Accept": "application/json",
                       "User-Agent": "collie-telephony/1"}
            try:
                import urllib.parse
                listing = transport.get_json(
                    _CONV_PREFIX + "?" + urllib.parse.urlencode(
                        {"agent_id": agent_id, "page_size": 30}),
                    headers=headers, timeout=20)
                if listing.status != 200:
                    return {"error": "provider listing was rejected (HTTP %d)" % listing.status}
                try:
                    items = json.loads(listing.body.decode("utf-8")).get("conversations") or []
                except (ValueError, AttributeError):
                    return {"error": "provider listing response is invalid"}
                items = [c for c in items if isinstance(c, dict)
                         and int(c.get("start_time_unix_secs") or 0) >= since]
                items.sort(key=lambda c: int(c.get("start_time_unix_secs") or 0), reverse=True)
                found, matched = [], None
                for c in items:
                    cid = str(c.get("conversation_id") or "")
                    if not _CONV_ID.fullmatch(cid):
                        continue
                    if len(found) >= limit and not intent_id:
                        break
                    detail = transport.get_json(_CONV_PREFIX + "/" + cid, headers=headers, timeout=20)
                    if detail.status != 200:
                        continue
                    try:
                        d = json.loads(detail.body.decode("utf-8"))
                    except (ValueError, AttributeError):
                        continue
                    summary = _conversation_summary(d, c, transcript=want_transcript)
                    if intent_id:
                        sid = summary.get("call_sid") or ""
                        if ((sid and ledger.provider_reference_matches(intent_id, sid))
                                or ledger.provider_reference_matches(intent_id, cid)):
                            matched = summary
                            break
                        continue
                    found.append(summary)
                return {"matched": matched, "found": found, "listed": len(items)}
            finally:
                headers.pop("xi-api-key", None)
                key = ""

        try:
            result = api_key.use(_read)
        except VaultError as e:
            return "ERROR: the OS credential vault refused the ElevenLabs key: %s" % e
        except (provider.TwilioElevenLabsError, telephony.TelephonyError, OSError) as e:
            return "ERROR: provider conversation record could not be read: %s" % e
        if result.get("error"):
            return "ERROR: " + result["error"]
        if intent_id:
            if result["matched"] is None:
                return json.dumps({
                    "intent_id": intent_id, "receipt_status": receipt.get("status"),
                    "matched": False, "listed_in_window": result["listed"],
                    "note": ("no provider conversation in the last %.1f h is bound to this intent; "
                             "the call may not have been placed, may be older than the window, or "
                             "the provider has not published it yet" % hours)},
                    ensure_ascii=False)
            out = {"intent_id": intent_id, "receipt_status": receipt.get("status"),
                   "receipt_note": ("the local receipt records only that the provider accepted the "
                                    "call (it decays to `uncertain` when its lease lapses, because "
                                    "Collie receives no provider events); the provider record "
                                    "below is the authoritative outcome"),
                   "matched": True, "call": result["matched"]}
            return json.dumps(out, ensure_ascii=False)
        return json.dumps({"hours": hours, "calls": result["found"],
                           "listed_in_window": result["listed"]}, ensure_ascii=False)


def register_telephony(registry, *, state_dir: str | None = None) -> bool:
    registry.register(PhoneCallTool(state_dir=state_dir))
    registry.register(PhoneCallStatusTool(state_dir=state_dir))
    registry.register(PhoneCallLogTool(state_dir=state_dir))
    return True


__all__ = ["DEFAULT_DISCLOSURE", "ConversationTransport", "PhoneCallLogTool",
           "PhoneCallStatusTool", "PhoneCallTool", "build_stack", "load_binding",
           "normalize_dial_number", "register_telephony"]
