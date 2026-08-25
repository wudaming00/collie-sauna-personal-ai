# Collie Workbench Product Model and IA

Status: UI/UX implementation contract  
Date: 2026-08-12

## Product definition

Collie is a personal AI operations system for delegating real outcomes. It is not primarily a chat
client, model picker, dashboard, or collection of agent cards.

The durable product identity is **one computer = one Collie**. Collie is the accountable operator
the user speaks to; models, Codex, Claude Code, browser/desktop automation, skills, connections, and
other workers are replaceable resources it assigns. Changing a provider must not appear to change
who the user is talking to. The computer-bound Collie identity is stable across restarts and may be
supervised from a phone, but credentials and execution authority remain scoped to that computer.

The user-facing loop is:

```text
Outcome -> Run or Mission -> Work in progress -> Needs You -> Verification -> Receipt
                                      \-> Wait / retry / recovery -/
```

The interface should answer four questions before exposing configuration:

1. What can I hand off?
2. What is moving now?
3. What needs my decision or authority?
4. What finished, and what evidence supports that result?

## Collie intelligence contract

Collie answers the user through the best trustworthy conversational route currently available, then
plans and delegates execution separately. “Best” is a policy decision, not a hard-coded model name.
For every turn the router considers:

- task complexity, modality, context size, and the required tool surface;
- provider/model health and current availability;
- subscription quota, remaining budget, rate limits, and paid-overage policy;
- the user's confirmed quality/speed/cost preferences and observed working habits;
- privacy, locality, verification requirements, and failure history.

Automatic routing is the default. A manual model/provider pin remains an advanced override and must
be visible in the decision explanation. The receipt records the chosen brain/worker, the decisive
reasons, fallback or degradation, and verification result. A fallback may reduce speed or capability,
but must never silently widen authority or spend policy.

## Memory contract

Memory is typed, scoped, inspectable, and revocable—not an opaque transcript summary. Supported
classes include facts, explicit preferences, repeated habits, procedures, decisions, identity, and
observations. Each claim carries subject, project/device scope, source, confidence, timestamps, and
optional expiry.

- Explicit local preferences are trusted immediately and outrank inferred habits.
- A repeated habit becomes usable only after deterministic observations meet its evidence threshold.
- Model guesses remain proposed and cannot steer routing, authority, or budgets.
- Expired, rejected, or invalidated claims are excluded from prompts and policy decisions.
- The user can review, confirm, reject, or forget an exact claim, and can see which memory influenced
  a routing or execution decision.

Memory should make Collie increasingly personal without making it unpredictable. The current request,
explicit safety policy, and exact authority boundaries always outrank remembered preference.

## Ambient command surface

The Workbench is the control center, not the only way to speak to Collie. On supported desktop
platforms a global shortcut opens a small command capsule on the active display:

```text
Voice on:  Ctrl+Shift+Space -> open/focus and begin listening
Voice on:  Ctrl+Shift+Space while listening -> stop and submit
Voice off: Ctrl+Shift+Space -> open/focus the typed command field
Escape or completed handoff -> close the capsule
```

The capsule accepts voice or text, shows the understood outcome and immediate handoff, and then gets
out of the way. It creates the same Run/Mission objects as the Workbench; it is not a second chat
history or a separate agent. The native host is independent of the wallpaper and full application,
starts hidden at logon, and exposes a configurable/disable-able shortcut. Microphone state must always
be visible, never imply listening while idle, and respect OS permission and reduced-motion settings.
Shortcut registration and voice input are independent controls: disabling the shortcut removes the
global entry point, while disabling voice keeps that shortcut and the typed capsule usable. The native
host grants microphone access only when voice is enabled and only to the exact loopback capsule origin;
otherwise it denies the request. Any browser/OS cloud speech service must be disclosed before the
setting, and the host status/receipt records whether voice was enabled.

## Functional inventory

### Primary work objects

| Object | User meaning | UI treatment |
| --- | --- | --- |
| **Run** | Immediate, interactive execution that streams progress | Starts from the composer; can be steered, stopped, checked, or rewound; details open in context |
| **Mission** | Durable ownership of an outcome across waits, restarts, approvals, and retries | Appears in Work and Missions; has lifecycle, next step, authority boundary, evidence, and receipt |
| **Specialist** | A focused worker operating inside a Mission | Shown as a Mission child or assignment, never as a competing top-level destination |
| **Automation** | A timer, file, page, or webhook trigger that starts durable work | Shown in Activity until complete authoring and management justify a dedicated destination |
| **Pack run** | A Best-of-N execution strategy for one outcome | Labeled **Pack run · Best of N** or **Best of N**, so it cannot be confused with the ecosystem Pack |

### Supporting product objects

| Object | User meaning | UI treatment |
| --- | --- | --- |
| **Pack** | Available brains, workers, connections, and devices | Operational ecosystem view: availability, assignments, routing, capability, and boundaries |
| **Session** | A conversation/history entry into work | Lives in the rail and search; does not own lifecycle or replace Run/Mission truth |
| **Job** | Internal unit used by execution and scheduling | Kept out of primary navigation; exposed only in technical audit and diagnostics |
| **Receipt** | Scoped record of actions, evidence, cost, and unresolved risk | Appears with completed outcomes and in Activity; states facts without upgrading uncertainty |

The model is a resource, not Collie's identity. A phone is a supervision surface, not a worker.
A service is infrastructure and should be promoted only when degraded or when the user explicitly
opens diagnostics.

## Information architecture

The desktop rail has six product destinations plus utilities. **Work** is the visible Home label.

### Work (Home)

The default work queue, ordered by required attention:

1. **Needs You**, shown only when non-empty.
2. **Open Missions**, including active, waiting, paused, and decision-blocked work, with state, next
   step, location, and elapsed time.
3. **Recent outcomes**, with verification/acceptance language and receipt access.
4. **System issues**, shown only when they affect execution.

Work contains the product's only persistent outcome composer. There is no hero, fake input,
starter-template gallery, or second composer. A lightweight New Outcome action may focus the same
composer; it must not create another submission surface.

### Missions

The durable work index. Recommended filters are Active, Waiting, Needs You, Review, Completed,
and All. Selecting a Mission opens its outcome timeline and contextual details; it does not turn the
main surface into a chat transcript.

Mission detail prioritizes:

```text
Outcome -> Plan -> Assignment -> Actions -> Waits / decisions -> Evidence -> Receipt
```

Messages, worker transcripts, files, diffs, and raw logs are secondary evidence panels.

### Needs You

A global priority queue for actions Collie cannot safely continue without. It is an exception lane,
not a routine confirmation step. Normal reversible work, tool choice, model routing, retries within
budget, and previously granted scoped authority proceed without interruption. Needs You is reserved
for human-only identity/authentication, new or materially widened authority, disclosure of sensitive
data, destructive/irreversible external effects, spend beyond the user's declared boundary, ambiguous
high-impact targets, or recovery where the actual outcome cannot be established independently.

Each item must show:

- what is waiting and why;
- the exact target and payload;
- risk or consequence;
- the decision being requested;
- what continues after approval, rejection, or human assistance.

Opening a decision that belongs to a Run or Session must select that work context. Navigation must
not claim the Needs You page is still open when the interface has moved to another destination.

### Activity

The canonical audit surface for receipts, evidence, action history, approvals, Mission and worker
events, Automations, cost, duration, device usage, and policy decisions. Filters may include Mission,
device, connection, event type, and time.

The Run inspector uses **Run timeline** rather than **Activity** to avoid naming two different scopes
with the same label.

### Library

The capability catalog for Skills, Connections, Templates, and Discover. Installation and activation
must show provenance, requested capabilities, and authentication separately. Installed does not mean
trusted, and a Skill cannot silently widen authority.

### Pack

The operational view of Collie's ecosystem: brains/models, specialist workers, connections, and
devices. It shows availability, capabilities, assignments, routing policy, queue depth, and resource
conflicts. It must not imply an offline device can accept immediate work.

### Utilities

Settings, Search/Command, Map, Recorder, Ambient, Browser setup, and Update are utilities, not peers
of Work and Missions. Sessions remain a compact history list in the rail. Runtime health becomes
prominent only when degraded.

## Desktop interaction model

Use a **Work Queue + contextual inspector** layout:

```text
Product rail | Primary work surface | Selected-object inspector
```

- The rail owns destinations, New Outcome, sessions, and utilities.
- The primary surface owns the current queue or index.
- The inspector owns Run/Mission controls, timeline, evidence, and technical detail for the selected
  object. It may be closed without changing the selected destination.
- The composer is docked to the bottom only where outcome submission is relevant.
- Rows and dividers represent ongoing work. Bounded approval decisions or evidence artifacts may use
  a card when the boundary itself conveys meaning.

At approximately 1180×820, a useful target is a 184–208 px rail, a flexible main queue, and a
roughly 320–380 px inspector. These are layout guides, not fixed content assumptions.

## Mobile and narrow-window behavior

- Use one column with a compact header and bottom composer.
- Product navigation becomes a drawer; product names remain the same.
- Selected-object detail becomes a full-screen sheet/page, not a squeezed side panel.
- Needs You remains globally reachable and keeps its count.
- Primary actions remain reachable without horizontal scrolling at 390 px and 320 px widths.
- Phone interaction supervises the same Run or Mission; it does not create a separate work object.

## State and trust language

Use one lifecycle vocabulary across surfaces:

| State | Meaning shown to the user |
| --- | --- |
| Draft | Outcome not started |
| Queued | Accepted and waiting to execute |
| Running | Collie currently owns the next action |
| Waiting | Waiting for time or an external condition |
| Needs You | A human decision, authority, or person-required step is blocking progress |
| Pausing | The current action is reaching a safe boundary |
| Paused | Execution is durably stopped and resumable |
| Review required | Work is ready for human review but is not independently verified |
| Recovery required | State cannot safely advance without reconciliation |
| Failed | Execution ended without satisfying the outcome |
| Cancelled | Ownership was explicitly ended |
| Verified against contract | An independent verifier passed the stated contract (`done_verified`) |
| Completed without independent verification | The user ended/took over the Mission (`done_accepted`) |

`done_verified` and `done_accepted` are different terminal facts. They must never share the same
green-success label, icon, or receipt wording. A model's self-report is progress evidence, not
independent verification.

Approval controls must remain bound to the exact payload, target, risk, and nonce. Generic “Allow”
language is insufficient when the action has irreversible effects.

## Visual and interaction principles

- Design a workbench, not a marketing landing page inside an application window.
- Use one visual hierarchy: window, work surface, row, optional inspector.
- Prefer whitespace, typography, dividers, and state marks over nested rounded containers.
- Reserve shadows and elevation for transient overlays, menus, and sheets.
- Use compact controls with 4–8 px radii; avoid pill-shaped containers as the default structure.
- Show exceptions and decisions before passive metrics.
- Keep primary labels outcome-oriented; move provider and runtime vocabulary to details.
- Every status must answer what happens next. Decorative “thinking” is not evidence.
- Preserve current context when the user opens or closes details.

## Non-goals

- A second composer disguised as onboarding or an empty-state card.
- A dashboard of decorative metrics before actionable work.
- Sessions, chats, or jobs as the top-level ownership model.
- Provider/model configuration as the first step of delegation.
- Cards nested inside panels merely to create visual separation.
- Treating every approval, acceptance, and verification result as “complete.”
- Giving Automations a top-level destination before lifecycle management is coherent.
- Mixing Pack ecosystem inventory with Pack run/Best-of-N strategy.

## Migration guardrails

1. Preserve existing Run, Mission, approval, SSE, CSRF, and accessibility behavior while changing
   presentation.
2. Keep one real composer and route every New Outcome affordance to it.
3. Keep Mission creation explicit; `/mission ` remains a supported entry path.
4. Preserve current Mission backend semantics and expose technical/audit detail without making it the
   default reading order.
5. Do not translate internal jobs into new user-facing nouns.
6. Preserve identity, selected session, in-flight Run, Mission state, and unread decisions across
   navigation and refresh.
7. Keep Needs You badge counts and selection in sync with the page or context actually opened.
8. Keep exact approval bindings and distinguish temporary human assistance from terminal take-over.
9. Verify no horizontal overflow at 390 px and 320 px; overlays and mode selectors must stay inside
   the viewport.
10. Roll the redesign out surface by surface without inventing placeholder capability. Hidden or
    staged features must not be represented as available.

## Acceptance check

The redesign is coherent when a user can open Collie and, without interpreting a dashboard:

- submit one outcome through one composer;
- understand whether it is an immediate Run or durable Mission;
- see everything currently moving in one queue;
- resolve authority-bound decisions with exact context;
- inspect progress without losing navigation context;
- distinguish independently verified work from accepted/taken-over work; and
- find receipts, capabilities, and infrastructure without confusing them with the primary workflow.
