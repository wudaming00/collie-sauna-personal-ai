# Collie voice and telephony

Status: product and implementation contract, 2026-08-20.

## Decision

Every computer is one Collie. Its assigned Google Voice number is a public work
identity, not merely an OTP source. A direct user instruction gives Collie product
authority to make an ordinary call or send an ordinary message inside the leash. That
does not, however, turn Google Voice Web into a programmable carrier API: Google's
current Acceptable Use Policy says not to automate Voice to place calls or send
messages. Collie therefore executes the user's intent through a supported programmable
adapter, or prepares a Google Voice draft/manual handoff. It does not automate the
Google Voice browser UI.

The public number, conversation intelligence, and audio transport are deliberately
separate:

1. **Identity channel** — the Google Voice number assigned by the user remains the
   number people know and the number Collie may put on accounts.
2. **Voice gateway** — Collie owns the call state machine, policy, memory admission,
   Brain Router, tools, receipts, and provider selection.
3. **Media adapter** — Twilio, SIP, ElevenLabs, LiveKit, or another supported provider
   only moves/transcribes/synthesizes media. Google Voice remains an identity/manual
   channel unless Google provides an applicable programmable interface. No media
   provider becomes Collie's identity or durable memory.

This separation lets us ship a fast managed path now and replace any voice vendor
later without changing the account, memory, Mission, or Needs You model.

## Recommended first production path

### Assigned Google Voice line

- Manual SMS and calls: Collie can prepare the exact recipient, disclosure, text, or
  call brief and hand it to the dedicated signed-in Google Voice space. The user
  performs the final send/dial action. Google Voice remains useful for inbound/manual
  communication, voicemail, OTP receipt, and account identity.
- Outbound calls: verify the assigned Google Voice number as a Twilio outgoing caller
  ID, then connect it to a realtime voice agent. A verified non-Twilio caller ID is
  outbound-call-only: it does not enable SMS or inbound calls.
- Automatic SMS: provision a carrier-owned/registered sender, or port the assigned
  number to a programmable carrier if preserving that exact number matters. A verified
  external caller ID is not an SMS sender. Without either option, use the manual draft
  handoff above.
- Inbound calls: first try linking a hidden programmable number and forwarding the
  Google Voice line to it. This needs an explicit compatibility test for the user's
  Voice account and a one-time number purchase. If it cannot be linked, the honest
  fallback is manual Google Voice answering/voicemail, not browser automation.
- Emergency calls are never automated. Google Voice itself says consumer Voice cannot
  make emergency calls.

The hidden carrier number is transport plumbing. Recipients see the assigned Google
Voice identity on outbound calls only after the programmable carrier has separately
verified it. SMS uses the registered sender actually provisioned for messaging.

### Realtime stack

The quickest high-quality version is:

```text
PSTN / Google Voice forwarding
        │
Twilio or SIP media edge
        │  streaming audio / turns / DTMF / interruptions
Collie Voice Gateway
        ├── Brain Router (best eligible self model, voice latency budget)
        ├── Mission / tools / Needs You
        ├── scoped working memory
        └── provider-neutral speech adapter
                 ├── Eleven Flash v2.5 (live Mandarin default)
                 ├── Eleven Multilingual v2 / v3 (quality render)
                 └── future local or alternate providers
```

For the first managed integration, use ElevenLabs Agents with its Twilio integration
and point its Custom LLM endpoint at the Collie Voice Gateway. This is the shortest
path to a verified existing caller ID, custom voices, telephony, turn-taking, and
interruptions while keeping Collie's own model router in charge of the answer.

The implemented one-call bootstrap uses ElevenLabs' native Twilio outbound endpoint
with a Twilio voice identity already imported into ElevenLabs. For the user's assigned
Google Voice identity, Twilio must first verify that exact E.164 as an outgoing caller
ID and the imported ElevenLabs phone ID must resolve back to the same number. Collie
probes ElevenLabs' phone-number endpoint before every initial submit and refuses a
provider, ID, number, or assigned-agent mismatch. The bootstrap accepts only a durable
intent ledger, an OS-Vault or explicit environment API-key seam, an exact E.164
recipient, enabled first-message/prompt/duration overrides, and the following non-secret
host metadata: Collie ID, verified caller E.164, ElevenLabs agent ID, and ElevenLabs
agent-phone-number ID. Its environment names are:

```text
COLLIE_TWILIO_CALLER_NUMBER
ELEVENLABS_AGENT_ID
ELEVENLABS_AGENT_PHONE_NUMBER_ID
COLLIE_ELEVENLABS_OVERRIDES_ENABLED=true
COLLIE_TWILIO_CALLER_ID_VERIFIED=true
ELEVENLABS_API_KEY                  # secret; environment seam only, never settings.json
```

The API key should normally be supplied by `IdentityVault`; the environment seam is
for an explicitly managed host/service environment. Configuration status is
`configured_unprobed` until a provider request succeeds. A dry run performs no network
request and stores no intent. A real submit atomically wins the ledger's dispatch claim,
then makes exactly one provider request with automatic HTTP retry disabled.

In parallel, implement the same gateway protocol over Twilio ConversationRelay. It
accepts streamed text tokens and exposes prompt, interrupt, DTMF, speaker, and
tokens-played events. This is the lower-lock-in path and should become the default
when its custom-voice behavior passes the Mandarin bake-off.

Use raw bidirectional Media Streams only when a managed relay cannot satisfy custom
voice, data-residency, or turn-taking requirements. Raw audio gives maximum control,
but makes Collie responsible for jitter buffering, codecs, echo, VAD, endpointing,
barge-in, audio cancellation, reconnect, and observability.

### Cloud-to-local boundary

Twilio, ElevenLabs, Vapi, and Retell cannot call a `localhost` Custom LLM/WebSocket.
Never solve that by publishing Collie's ordinary web control token or LAN server. A
dedicated Voice Edge must:

- validate the carrier/vendor signature before accepting a call;
- exchange it for a short-lived, single-call, direction-bound capability;
- carry only that call's transcript/events and Collie's streamed response over the
  existing authenticated relay/backhaul;
- expose no shell, browser, Memory, Settings, or general Mission HTTP endpoint;
- fail closed or play an honest unavailable message when this Collie is offline;
- expire the capability at hangup and reject replays, late audio, and a second provider
  call ID using the same intent.

The edge is not durable memory. It keeps only bounded jitter/reconnect state and masked
delivery telemetry. Provider data paths and retention must be shown before the user
enables a voice adapter.

## Speech model policy

### Live Mandarin

Default the managed telephone adapter to Eleven v3 Conversational / Expressive Mode.
It preserves real-time operation while adding context-aware emotion and better
turn-taking. Use Multilingual v2 as the quality fallback when v3 is unavailable.
Flash v2.5 is a degraded, latency-first fallback, not Collie's voice-quality default.
Stream the first speakable phrase immediately; do not wait for the full language-model
response.

Before synthesis, a deterministic Mandarin verbalizer must normalize:

- phone numbers, verification digits, dates, time, money, URLs, English acronyms;
- names and company terminology through a versioned pronunciation dictionary;
- punctuation into short breath groups, normally 6–20 Chinese characters;
- markdown, code fences, citations, and UI labels into spoken language.

Use Multilingual v2 or v3 for voicemail, important pre-recorded introductions, and
other quality-first output. Do not switch TTS model or voice in the middle of a live
sentence.

### Collie voice versus the user's voice

Offer two explicit personas:

- **Collie's voice** — the default, recognizable AI work identity.
- **My authorized voice** — an optional clone of the signed-in user's own voice.

An instant clone is useful for prototyping. A production personal voice should use the
provider's verified professional voice-clone (PVC) flow with clean Mandarin
conversational recordings. ElevenLabs currently recommends roughly 30–180 minutes for
professional cloning and only permits a user to create a professional clone of their
own voice. Collie must not claim it has independently verified a voice when only the
provider can produce that evidence.

The provider credential and opaque voice identifier belong in the account vault. The
public registry stores only the provider, opaque voice reference, provider verification
state, consent version, language, status, and deletion/recovery metadata. In the first
managed version, training samples are uploaded to and retained under the voice
provider's policy; Collie does **not** claim to store, encrypt, export, or delete a copy
it does not hold. “Delete voice” must invoke provider deletion, verify its result, then
clear the local opaque reference. Samples and synthesized audio never enter Memory.

Every external conversation begins with a short disclosure, even when the authorized
voice sounds like the user:

> 你好，我是 Sining 的 AI 助手 Collie，正在用经授权的合成声音协助处理这件事。

The disclosure is a fact, not a Needs You interruption, and its successful playback is
part of the call receipt.

### Emotion

Do not let a language model send arbitrary style sliders. It emits one of four bounded
delivery intents: `neutral`, `warm`, `empathetic`, or `firm`. A deterministic voice
policy maps that intent, call type, and recipient preference to provider settings.

- For the current v3 conversational profile, start around stability 0.38–0.5,
  similarity 0.7–0.8, and speed 0.95–1.0. Allow a single bounded expressive tag such
  as `[laughs]` only when the conversational context calls for it.
- Use lower stability only after that exact voice passes artifact and pronunciation
  tests. Style exaggeration remains off by default because it adds latency and can
  destabilize speech.
- Never mimic panic, distress, crying, authority, or the other participant's voice.
- A legal or safety disclosure is non-interruptible; normal conversation is
  interruptible.

## Natural conversation loop

Naturalness comes more from turn-taking than from an isolated TTS demo.

1. Run acoustic VAD and echo cancellation at the media edge.
2. Use language-aware semantic endpointing for Mandarin. LiveKit's current turn
   detector explicitly includes Chinese; managed platforms without Chinese semantic
   endpointing must use conservative punctuation/silence rules.
3. Start the Brain Router on a stable partial transcript only for harmless preparation;
   commit tools or spoken output only after the turn is final.
4. Stream the best eligible Collie model's first complete phrase to TTS.
5. On barge-in, stop playback and clear queued audio within 150 ms, while retaining the
   exact text already played (not merely generated) in the call context.
6. Use a short neutral acknowledgement only when first audio would otherwise exceed
   900 ms. Do not fabricate progress.
7. Treat `嗯`, `对`, `好`, and similar backchannels differently from a genuine
   interruption; tune this on native Mandarin recordings rather than English defaults.

Initial service-level objectives, measured from real carrier calls:

| Measure | p50 target | p95 target |
| --- | ---: | ---: |
| end of user turn to first Collie audio | <= 650 ms | <= 1,000 ms |
| barge-in to stopped Collie audio | <= 120 ms | <= 200 ms |
| incremental transcript age | <= 250 ms | <= 500 ms |
| reconnect without duplicate speech | <= 1.5 s | <= 3 s |
| false turn cut on Mandarin test set | < 2% | — |

PSTN is 8 kHz and will cap perceived voice quality. It is a reachability and task
channel, not the reference Collie voice experience. The desktop command capsule,
mobile app, web app, and a future Collie-to-Collie call should use WebRTC at
16/24/48 kHz even when telephone calls remain available.

## Call and message authority

Within an accepted Mission, Collie can autonomously call or text when all of these are
true:

- the target is an exact contact/number in scope;
- the purpose is already part of the requested outcome;
- the per-call, daily, and international cost leash has room;
- it is an interactive, non-bulk communication;
- the call does not create a legal, medical, financial, employment, or identity
  attestation on the user's behalf;
- quiet hours and contact-specific preferences allow it.

Needs You appears only at a real boundary: buying/porting a number, accepting provider
terms, recording without standing consent, revealing a sensitive secret, changing a
contract, making a payment, or providing a human/legal attestation. An already-approved
ordinary scheduling or support call should not stop merely because it uses the phone.

Always block emergency numbers, harassment, spam, bulk outreach, caller-ID spoofing,
impersonation without disclosure, and attempts to evade a platform or carrier policy.

## Durable state and privacy

The implemented telephony contract uses a durable SQLite ledger and stable keyed
fingerprints. The HMAC key is separate from the database (production supplies it from
the account vault; the local bootstrap uses a permission-restricted sidecar). SQLite
has a unique `(collie_id, idempotency_key)` constraint and transactionally advances one
of these state machines:

```text
call:    planned -> authorized -> dialing -> disclosure_pending -> in_progress
         in_progress -> completed | failed | uncertain
message: planned -> authorized -> sending -> sent -> delivered
         sending | sent -> failed | uncertain
```

Before a provider request, the adapter must persist `dialing` or `sending`. Opening a
ledger after interruption converts any submitted/active state to `uncertain`. A retry is
blocked until a trusted provider lookup says `submission.absent`, or a signed provider
event reconciles the intent to its observed state. Provider references are stored only
as keyed hashes.

Capabilities also fail closed: an allowlisted transport mode must come from the trusted
capability registry, its provider/API health evidence must be `healthy`, and its TTL
must still be valid. Google Voice manual modes can never pass automatic dispatch. A
verified external caller ID passes calls only; SMS requires a provider-registered line.

For SMS, the disclosure is a mandatory prefix in the provider payload. Authorization
counts the complete payload using GSM-7 septets (including extension-table characters)
or UTF-16/UCS-2 code units for Chinese and emoji, and rejects it before dispatch when it
exceeds `max_segments`. A provider `message.accepted` event must attest the SHA-256 of
that exact disclosure-bearing payload. For calls, only trusted provider playback
evidence can advance `disclosure_pending -> in_progress`.

Current implemented retention truth:

- the native Twilio outbound bootstrap stores no raw audio or transcript and does not
  claim to own ElevenLabs/Twilio provider-side retention;
- it also stores no phone number, message body, call brief, disclosure wording, or raw
  provider reference;
- the durable receipt contains purpose, capability, consent class, segment/duration
  limits, provider-reference hash, disclosure evidence flag, timestamps, cost, outcome,
  and reconciliation state;
- the database does store Collie ID and idempotency key for its uniqueness contract;
- memory extraction: proposed facts only, with the call as provenance. No proposal may
  guide future behavior until it passes the normal Memory trust policy;
- OTP, password, payment data, recovery codes, and full sensitive utterances: never in
  Memory, logs, analytics, or receipts.

For the later Collie-controlled realtime media gateway, the separate target policy is
raw audio off by default and a bounded encrypted live transcript deleted at call end.
Provider-side audio/transcript retention is outside this ledger and must be inventoried,
configured, and shown honestly for each connected vendor before launch.

Recording is a separate feature. It is off by default and requires the configured
jurisdiction/contact consent policy before the recorder starts. The voice gateway must
not infer that consent from ordinary speech.

## UI

Telephony is a small Collie capability, not a standalone product area or primary
navigation item. Settings → Connections / My Collie shows one collapsed status row:
public line, `manual handoff` versus `programmable`, truthful call/SMS directions,
provider health age, voice persona, and a Configure action. Advanced routing, budget,
quiet hours, retention, and voice deletion expand in place only when needed.

During an active call, a temporary compact control bar shows recipient, purpose,
listening/speaking/waiting, elapsed time and cost, plus Take over, Mute, and End. It
disappears at hangup. The outcome and any proposed memories attach to the originating
Mission receipt; there is no separate call-history destination.

The global hotkey remains the fastest way to talk *to* Collie. `Call <contact>` creates
a Mission-backed external call. A local voice conversation must not accidentally dial a
phone number.

## What to adopt from other products

| Product / stack | What it actually contributes | Collie decision |
| --- | --- | --- |
| Tavus CVI | WebRTC video, visual perception (Raven), turn flow (Sparrow), pluggable STT/LLM/TTS, realtime replica (Phoenix) | Optional video-presence surface. Do not make it the phone or identity core. Use a Collie persona/brain and Tavus only when seeing/being seen materially helps. |
| ElevenLabs Agents | Fast multilingual speech, custom voices, interruptions/soft timeout, Twilio inbound/outbound and Custom LLM | Fastest managed v1 and personal-voice path. Keep policy, model, memory, and receipts in Collie. |
| Twilio ConversationRelay | Signed telephony edge, STT/TTS, streamed text, DTMF, token-played and interruption events | Preferred provider-neutral managed carrier adapter if Mandarin/custom voice wins tests. |
| Vapi | Pluggable transcriber/model/voice and fine-grained start/stop-speaking plans | Strong bake-off/reference implementation; useful if its Chinese endpointing and observability beat the direct stack. Avoid adding it merely as another forwarding layer. |
| Retell | Managed calls, custom-LLM WebSocket, latency breakdown, DTMF/transfer | Benchmark for call reliability and observability; not the identity or memory owner. |
| Hume EVI | Prosody/expression measurement and empathic speech/turn behavior | Study its emotion and interruption design. Current published EVI language table does not include Chinese, so it is not the Mandarin default. |
| LiveKit Agents | Open realtime session framework and Chinese-capable turn detector | Best candidate for Collie-controlled desktop/WebRTC and later vendor-independent media plane. |
| Pipecat | Open, provider-pluggable voice/video pipelines and transports | Useful implementation substrate for experiments/adapters, not a product-level source of truth. |
| OpenAI Realtime | Native audio over WebRTC/WebSocket/SIP, speech-to-speech, semantic/server VAD, interruption and tool use | Keep the current `gpt-realtime-2.1` as a speech-to-speech benchmark/fallback. The default call must still use Collie's best eligible Brain Router rather than silently pinning all reasoning to one realtime model. |
| Sesame CSM | Open conversational speech generator with audio context | Research baseline only today: released CSM is generation-only, CUDA-oriented, and its own README says non-English is unreliable. |

## Frozen bake-off

Do not choose a voice vendor from demos. Run the same calls through every candidate.

### Configurations

1. ElevenLabs Agents + Twilio + Collie Custom LLM.
2. Twilio ConversationRelay + best supported Mandarin TTS + Collie gateway.
3. Vapi + strongest Chinese transcriber + Eleven v3/Multilingual + Collie Custom LLM.
4. Retell + Collie Custom LLM.
5. LiveKit/Pipecat + Chinese turn detector + Scribe/alternate ASR + Eleven v3.
6. OpenAI Realtime speech-to-speech as a latency/naturalness ceiling.
7. Multilingual v2/v3 quality render for non-live comparison.

### 2026-08-20 real-call finding

The first valid-Mandarin Flash v2.5 call reached roughly 0.97 seconds median from user
silence to first audio, so latency was not the quality blocker. It produced about 62
seconds of agent audio during a 90-second call and was judged strongly mechanical by
the user. Treat that result as a failed voice-quality baseline: retain its metrics, do
not restore Flash as the default, and compare v3 in both wideband PCM and the same
8 kHz mu-law telephone simulation before another paid call.

### Corpus

- 300 consented Mandarin turns from at least 20 speakers, balanced across Mainland,
  Taiwan, Singapore, code-switching, gender, speaking rate, and noisy/clean audio;
- 100 phone-number/date/address/name utterances;
- 100 backchannel, hesitation, interruption, correction, and long-pause turns;
- 30 ten-minute task calls on both PSTN 8 kHz and WebRTC wideband;
- cloned-voice tests use only the participating user's verified voice.

### Metrics

- ASR character error rate and named-entity exact match;
- endpoint false-cut, missed-end, and median/p95 commit delay;
- first-audio and full end-to-end p50/p95 latency;
- barge-in stop latency, duplicate/post-interrupt audio, overlap and dead air;
- blind naturalness MOS, Mandarin nativeness, voice similarity, emotion
  appropriateness, pronunciation, and listener trust;
- task success, tool/DTMF correctness, disclosure success, handoff recovery;
- drop/reconnect rate, carrier answer rate, spam labeling, cost per connected minute;
- secrets/transcript retention conformance and provider data-path inventory.

Report paired bootstrap confidence intervals. Select separate winners for live Mandarin,
authorized clone fidelity, noisy PSTN, and local WebRTC; one provider need not win all
four. Re-run the frozen suite when a provider/model version changes.

## Primary references

- [Google Voice Acceptable Use Policy](https://support.google.com/voice/answer/9230450)
- [Google Voice: make a call](https://support.google.com/voice/answer/3379129)
- [Google Voice: interactive SMS](https://support.google.com/voice/answer/115116)
- [Google Voice: forwarding rules](https://support.google.com/voice/answer/11420769)
- [Twilio ConversationRelay](https://www.twilio.com/docs/voice/twiml/connect/conversationrelay)
- [Twilio bidirectional Media Streams](https://www.twilio.com/docs/voice/media-streams)
- [Twilio verified outgoing caller IDs](https://www.twilio.com/docs/numbers-and-senders/phone-number-senders)
- [ElevenLabs model comparison](https://elevenlabs.io/docs/overview/models)
- [ElevenLabs Expressive Mode](https://elevenlabs.io/docs/eleven-agents/customization/voice/expressive-mode)
- [ElevenLabs Twilio integration](https://elevenlabs.io/docs/eleven-agents/phone-numbers/twilio-integration/native-integration)
- [ElevenLabs conversation flow](https://elevenlabs.io/docs/eleven-agents/customization/conversation-flow)
- [ElevenLabs professional voice cloning](https://elevenlabs.io/docs/eleven-creative/voices/voice-cloning/professional-voice-cloning)
- [Tavus CVI architecture](https://docs.tavus.io/sections/conversational-video-interface/overview-cvi)
- [Hume EVI](https://dev.hume.ai/docs/speech-to-speech-evi/overview)
- [Vapi voice pipeline](https://docs.vapi.ai/customization/voice-pipeline-configuration)
- [Retell latency metrics](https://docs.retellai.com/reliability/check-actual-latency)
- [LiveKit turn detector](https://docs.livekit.io/agents/logic/turns/turn-detector/)
- [Pipecat](https://github.com/pipecat-ai/pipecat)
- [OpenAI Realtime model](https://developers.openai.com/api/docs/models/gpt-realtime-2.1)
- [Sesame CSM](https://github.com/SesameAILabs/csm)
