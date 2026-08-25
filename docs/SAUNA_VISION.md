# Collie × Sauna — a personal AI system

Status: product thesis and implementation notes for the prototype in this repository
Date: 2026-08-22

> Architecture update (2026-08-24): the product thesis remains, but Personal AI Core—not Sauna's
> private storage—is now the logical system of record. The implemented v2 object and delta
> contracts are documented in [Personal AI Core](PERSONAL_AI_CORE.md) and
> [Personal AI Protocol v2](PERSONAL_AI_PROTOCOL.md). References below to snapshot v1 or Sauna as
> the sole record describe the original prototype and are retained as migration context.

> Your AI shouldn't live in a website. It should live with you.
>
> **Collie is AI for this device. Sauna turns it into AI for this person.**

---

## 1. Why web-first personal AI creates friction

The dominant shape of personal AI today is a tab. To use it you stop what you are doing, switch to
a browser, find the right conversation, re-explain where you are, and paste in the context the AI
cannot see. The cost is not the seconds — it is that **the AI is never present at the moment the
work is happening**, so it can only ever help with work you are willing to describe twice.

Three consequences follow, and they compound:

- **Context is re-typed, not observed.** The file you have open, the text you selected, the tab you
  are reading, the repository you are standing in — all invisible. Every session starts cold.
- **Action stops at the edge of the page.** A web assistant can produce text about your computer;
  it cannot open the app, drive the logged-in browser, run the check, or change the file.
- **Continuity is per-conversation.** "What did we decide?" is answered by scrolling, and only if
  you remember which of ninety threads it was in.

The fix is not a better website. It is putting the AI where the work is: **in the operating
system**, with a shortcut, the current context, and real hands.

## 2. Why Collie is the native OS layer

Collie already lives in the OS. On Windows a hidden native host owns a global chord
(`Ctrl+Shift+Space`, `harness/wallpaper.py` + `harness/wallpaper/Program.cs`); pressing it puts a
small capsule on the active display — Spotlight geometry, not a browser window. It accepts voice or
text, dispatches into the same run engine as every other surface, and gets out of the way.

The prototype adds the missing half: **it knows what you are doing when you press it.**
`harness/localcontext.py` reads, once per summon and only for the channels you enabled: the
foreground application and window title, the text selected *in that window* (Windows UI Automation
`TextPattern`, macOS `AXSelectedText`), optionally the clipboard, the browser tab title, and the
project that window belongs to. Those become chips you can see in the capsule before anything is
sent, and a bounded block in the model's prompt:

```
DEVICE CONTEXT (what the person is doing right now on this computer):
- active app: Chrome
- window: "Sauna — the AI that works while you sleep"
- selected text (214 chars): "…workspace notes and session memory…"
- project: Collie
```

The interaction is: *working normally → shortcut → Collie is already oriented → ask → it acts →
back to work.* No navigation, no re-explaining.

And Collie can actually do the thing. Existing, unchanged: a real logged-in Chrome (19 `browser_*`
tools over a local extension bridge), native app control (Windows UIA / macOS System Events),
terminal, filesystem, screenshots and vision, MCP servers, skills, missions, automations, a phone
surface, and outbound voice calls.

## 3. Why Collie stays open source and local-first

Collie must be genuinely complete on its own, for two reasons — one ethical, one strategic.

The ethical one: an agent that can read your files, drive your logged-in browser and control your
apps has to be inspectable. "Trust us" is not an acceptable answer for software with that reach.
Collie is MIT, has no account, no telemetry, and stores everything under `~/.collie`.

The strategic one: **a paid layer that exists because the free one was crippled teaches users to
resent it.** Sauna should be worth money because person-level intelligence genuinely requires a
cloud, not because Collie withheld something it could have done locally.

So the local product is the whole device product: native AI entry, BYOK **and** local models
(Ollama), local memory, local tasks/goals/notes/calendar/journal, local workflow learning, local
execution, computer use, skills, MCP, automations. No Sauna account is required for any of it.

## 4. Why local execution alone is not enough

A device-bound AI has a hard ceiling, and it is not compute — it is **identity over time**.

- Your intelligence resets when you change machines. A new laptop is a new stranger.
- The AI sees today. It cannot see the trajectory that makes today mean something.
- Long-running work dies with the lid. A closed laptop cannot research overnight.
- Two devices learn the same lesson twice, and disagree.

Concretely: "Finish that report."

| | What it can infer |
| --- | --- |
| **Collie alone** | The window in front of you is `report.md`. It edits that file. |
| **Collie + Sauna** | *Which* report, why it matters, who is waiting, when it is due, what is already done, how you normally write it, what happened yesterday, and what remains after this. |

Same request, same device, same model — different amount of person.

## 5. Person-level intelligence

Sauna is not a sync service. It is the layer that knows **the person**, and it makes Collie more
accurate, more proactive, more consistent and more personal on every device the person touches.

In the prototype this is `harness/sauna.py`, whose `person_context()` returns exactly the kind of
context a device cannot produce alone:

```
- goal "Prepare for Sauna interview" (57%): done — Research Sauna; Research interviewer;
  Define product thesis; Build Collie prototype; remaining — Prepare system design examples…
- upcoming: Sauna interview — Tue Aug 25 11:00 AM · with Jordan Lee (Product Lead) …
- decided: Collie remains open source; Sauna is the paid person-level layer.
- preference: concise answers, no preamble
- workflow "Interview preparation" (confirmed): Research company → Research interviewer →
  Prepare product thesis → Build prototype → Prepare system design → Rehearse → Brief
```

The Today view shows the difference honestly, side by side — what the device contributes, and what
Sauna adds — so the value is visible rather than asserted.

## 6. Native Notes, Tasks, Calendar, Journal

Sauna owns a small number of AI-native personal primitives **directly**. This is a deliberate
architectural line:

```
External world:  Google Calendar · Gmail · Slack · GitHub · Notion · Todoist
                                    │
                              MCP / APIs            ← how the AI reaches the outside world
                                    ▼
       Sauna personal state:  Notes · Tasks · Calendar · Goals · Journal
                                                      ← the person's internal world
```

**MCP connects Sauna to the outside world. It must not be the foundation of the user's inner
world.** If the person's goals live in someone else's product, then the AI's understanding is
rate-limited by an integration, breaks when a token expires, and can never mean more than that
product's schema allows. Integrations are *sources*; the personal state is *canonical*.

An AI-native primitive is also not the same object as its conventional cousin — see §9 and §10.

## 7. The Personal State Model

`harness/personal_state.py` — one SQLite file (`~/.collie/personal.db`), typed and inspectable:

```
Person
 ├── Projects ──────── Collie · Sauna by Wordware
 ├── Goals ─────────── Prepare for Sauna interview (57%, due Tue)
 │     └── Tasks ───── ✓ Research Sauna · ✓ Research interviewer · ✓ Product thesis
 │                     ✓ Build Collie prototype · → Prepare system design · ○ Rehearse
 ├── Events ────────── Sauna interview · Tue 11:00 · goal ↑ · with Jordan
 ├── Notes ─────────── "Sauna product thesis" → project Collie, goal ↑, person Jordan
 ├── People ────────── Jordan Lee (Product Lead, Sauna) · Casey (Recruiting)
 ├── Activities ────── append-only: who did what, when, with which evidence
 ├── Journal ───────── one compressed entry per day
 ├── Summaries ─────── weekly roll-ups, project timelines
 ├── Workflows ─────── learned sequences + their evidence
 ├── Suggestions ───── open proposals and how you answered them
 └── Devices / Cloud tasks
```

Relations are first-class (`relations` table), so a note can point at a project, a person, a
company, a goal and an event at once — and the person never has to choose a folder.

## 8. Markdown is a view, not canonical memory

Collie's culture is transparency: files you can read. But a Markdown file is a bad database — it
cannot be queried, it drifts, and two writers corrupt it.

So the canonical state is structured, and Markdown is a **projection** regenerated from it
(`PersonalState.render_views()` → `~/.collie/state/`):

```
Structured personal state (SQLite)
            ↓ projection
   today.md · profile.md · recent_activity.md · project_summary.md · journal-YYYY-MM-DD.md
```

You can read them, `grep` them, keep them in a notes app. Nothing parses them back. This is the
lesson from watching notes-based memory systems in the wild: eight fixed Markdown documents with no
type, no provenance and no confidence become unfalsifiable — an out-of-date claim looks exactly
like a current one.

Collie's existing memory contract stays in force for anything the model asserts: explicit
preferences are trusted immediately, repeated habits only after an evidence threshold, model
guesses stay `proposed` and can never steer routing or authority (`harness/memory.py`).

## 9. AI-native Calendar

A normal calendar knows **when**. An AI-native calendar knows **what the event means**:

```
Sauna interview
Tuesday · 11:00 AM · Zoom · with Jordan Lee (Product Lead)
Goal          Prepare for Sauna interview
Preparation   57%  ██████░░░░
Related       Jordan Lee · Collie · Sauna by Wordware
Remaining     ○ Prepare system design examples  ○ Rehearse  ○ Interview-day brief
Suggestion    Block 90 minutes Monday evening for "Prepare system design examples"?
```

Every line except the first two is derived: preparation is the goal's task completion, remaining is
the goal's open steps, related comes from the relation graph, and the suggestion is computed from
how much is left and how close the event is (`executive._suggest_for_event`). This semantic layer
matters far more than calendar UI complexity — we deliberately did not build a month grid.

## 10. AI-native Notes

Notes must be creatable from wherever the thought happens — which is never inside a notes app.

Press the shortcut, say *"Remember this"* or *"Add this to my Sauna interview notes."* The
`note_save` tool (`harness/personal_tools.py`) appends to the note you named if it exists, creates
one if it does not, and attaches relations from the project/goal you are in or the task you are
focused on. The person never picks a folder or a database.

A note can also be marked a **decision**, which additionally writes a `kind=decision` claim into
Collie's long-term memory — so "what did we decide about the registry?" is answerable months later
by recall, not by scrolling.

## 11. Journal as AI-maintained state compression

The journal is not a diary you write. It is the timeline Collie maintains, so that understanding
your continuity does not require replaying raw history:

```
August 22
What happened   Completed: Build Collie prototype · Saved note: Sauna product thesis
                Updated the architecture notes · Verified the result with an executed check
Decisions       Personal state is structured; Markdown is a projection, not canonical memory.
                MCP connects external systems; it does not define the person's internal state.
Open loops      Prepare system design examples · Rehearse the demo · Interview-day brief
Next            Prepare system design examples
```

`PersonalState.build_journal()` ranks what the day was *about* (finished work, runs, decisions,
notes, handoffs) above bookkeeping, deduplicates, and caps to a readable page. It is deterministic;
an optional narrator (a model) may add prose, and if that fails the entry survives without it.

## 12. Hierarchical summaries

Compression is layered, so long-range questions do not require long-range reading:

```
Raw activity   (every run, edit, note, decision — append-only)
     ↓ compress per day
Daily journal  (what happened · decisions · open loops · next)
     ↓ compress per week
Weekly summary
     ↓ per project
Project timeline
     ↓ durable claims
Long-term memory (decisions, preferences, habits — with provenance and confidence)
```

This is what makes *"how did the Collie architecture evolve this month?"* answerable at all: the
answer is assembled from four weekly summaries and a decision list, not from thousands of events.

## 13. The executive layer and the closed loop

Collie was excellent at execution and had nothing above it. The executive layer
(`harness/executive.py`) connects execution upward:

```
Observe → Understand → Plan → Schedule → Execute → Verify → Report → Remember → Repeat
```

Concretely, when any run finishes — web, capsule, CLI, editor — `Harness.run()` hands a structured
summary to the executive layer, which:

1. records the activity (with files changed, verification evidence, cost);
2. binds it to a task **honestly** — explicit binding, then the focused task, then a fuzzy match;
   a strong match completes the task, a weak one *asks* instead of assuming;
3. moves the goal, which moves the event's preparation percentage;
4. rebuilds today's journal and regenerates the Markdown projections;
5. asks the Personal Workflow Model what usually comes next.

A failed or cancelled run records the attempt and completes nothing. This is the same evidence
discipline as Collie's verification gate, applied to personal state: **`done_claimed` is not
`done_verified`.**

## 14. Workflow learning — the Personal Workflow Model

Memory answers *what happened before?*. The workflow model (`harness/workflows.py`) answers
**what usually happens next?**

```
1st time    you do X, then Y, then Z by hand                    → observed
2nd time    Collie recognises X→Y and suggests Y                → suggested
3rd time    the sequence is confirmed                            → confirmed
you say so  "automate this"                                      → automated
```

Learning is over task *kinds* (research → write → build → design → review → communicate), counted
across **distinct goals**, so doing the same project twice in one afternoon does not fake evidence.
The threshold (2 goals to suggest, 3 to confirm) deliberately mirrors
`memory.record_habit_observation`'s evidence threshold — Collie already had a rule for "when is a
repeated observation trustworthy", and personal workflows use the same one. Tautologies
("after research you research") are refused: a learned workflow must relate two different kinds of
work, or it predicts nothing.

Two templates ship (interview preparation, bug fix) so a fresh install can already explain its
reasoning; templates never auto-run anything.

## 15. The proactive next-action loop

The moment worth demonstrating:

```
Done
✓ Updated the architecture notes            ✓ Changed 1 file: SAUNA_VISION.md
✓ Task: Build Collie prototype              ✓ Goal 57% ██████░░░░
✓ Regenerated project summary and today's journal

Based on your interview preparation workflow:
  NEXT LIKELY STEP
  Prepare system design examples
  [ Run ]  [ Not now ]
```

Three tiers, and the boundaries are enforced, not suggested:

| Tier | When | Behaviour |
| --- | --- | --- |
| **Safe automatic continuation** | workflow explicitly automated **and** the step is local/reversible (write, research, review, prepare, design) | runs, with a visible countdown you can cancel |
| **Suggestion** | everything else, including all template workflows | shown; nothing happens until you click |
| **Requires approval** | any external effect | Collie's existing gate asks, with the exact target |

Nothing about the personal layer can widen authority. Every action a suggestion triggers is an
ordinary Collie run through the ordinary deterministic gate (`harness/gate.py`, `harness/risk.py`).
"Notify the team" is never automatic, even inside an automated workflow.

## 16. Collie → Sauna: the learning loop runs both ways

```
        Sauna  ──  richer context · priorities · learned workflows · continuity  ──▶  Collie
          ▲                                                                             │
          └────  behaviour · outcomes · accepted/rejected suggestions · what shipped ───┘
```

Collie sends structured signals up (`SaunaClient.signals()`): task completed, suggestion accepted
or declined, workflow modified, run verified or failed. That feedback is what makes personalization
improve rather than ossify — a suggestion the person declines twice should stop appearing.

A one-way architecture would give you sync. The loop is what gives you learning.

## 17. Personal AI Continuity and Portability

**Continuity** — the same person across devices and time:

```
MacBook (Collie) ─┐
Work PC (Collie) ─┼── Sauna ── goals · tasks · notes · calendar · journal · preferences ·
Phone (companion)─┤            relationships · project context · learned workflows · history
Cloud worker ─────┘
```

**Portability** — changing devices should feel like migrating your AI, not starting over:

```
iCloud   → photos          Sauna → personal intelligence
Chrome   → bookmarks
Dropbox  → files
```

Install Collie on a new Mac, connect Sauna, and the first thing it says is *"Welcome back"* —
goals, tasks, notes, journal, preferences and learned workflows restored. In the prototype this is
a real code path (`SaunaClient.restore()` → `PersonalState.import_snapshot()`), tested with two
separate state files; the "cloud" it restores from is a local mock (§20).

## 18. Cloud execution and runtime routing

> "Research the remaining competitors tonight and have a report ready tomorrow morning."

```
Run on   ● This computer      ○ Sauna Cloud
         Long-running or scheduled — can run while this computer is off.
```

The routing rules (`SaunaClient.route()`) are deliberate and legible:

| Task | Runtime |
| --- | --- |
| Private / local files / logged-in browser / on-screen context | **Collie** — it must be this machine |
| Long-running, scheduled, overnight, parallel | **Sauna Cloud** — it must not depend on this machine |
| Enterprise private environment | that org's runtime |
| High-risk / external effect | **human approval**, wherever it runs |

Today the person chooses from an offered default. Long term the system should choose, and only
surface the choice when it is interesting. The user should never have to think about
infrastructure — only about whether something is private and whether it must survive a closed lid.

## 19. Privacy

**Local by default. Sync what you choose.** Collie has no account and no telemetry; the personal
state is a file on your disk. Connecting Sauna opens a granular, per-category choice:

```
SYNC WITH SAUNA
Personal state    ✓ Preferences ✓ Goals ✓ Tasks ✓ Calendar ✓ Notes ✓ Journal ✓ Learned workflows
Activity          ✓ Agent activity      ○ Full conversation history
Sensitive local   ○ Local files  ○ Browser history  ○ Screen history
```

Sensitive categories default **off** and stay off unless turned on, and the split is enforced rather than labelled: "Agent activity" carries what Collie *did* — the task, the files it touched, whether a check passed — while the answer text it produced is conversation content and only travels once "Full conversation history" is on. The same discipline applies to
device context: active window and selection are on, **clipboard is off by default** (clipboards
hold passwords), and a single switch stops all of it from reaching a model while still showing you
the chips. Every sync records what was shared *and what was withheld* in the activity ledger.

## 20. What is real and what is mocked

Stated plainly, because a prototype that blurs this is worthless as evidence:

| Real, running code | Mocked for this prototype |
| --- | --- |
| Global capsule + hotkey, device context capture | The Sauna cloud service itself — `SaunaClient` behaves as the cloud would, storing the "cloud" copy locally under `~/.collie/sauna/` |
| The whole existing Collie runtime: tools, browser, desktop, terminal, gate, audit, receipts, memory, missions | Cloud *execution* — a handoff is scheduled, visible and honest; it is never reported as executed |
| Personal State Model, executive loop, workflow learning, journal compression, Markdown projections | The second device in the continuity demo — rendered from a real exported snapshot, not a second live machine |
| Sauna connect/sync/context/handoff/devices/export/restore — real code paths against a local store | Person-level enrichment is derived from local state *as Sauna would return it* |

The boundary between Collie and Sauna is the real one (`docs/PERSONAL_AI_PROTOCOL.md`). Replacing
the mock with HTTPS calls does not change a single caller.

## 21. Security

Collie controls a real computer, so the rule is absolute: **the LLM is never the security
boundary.**

```
Agent  →  action proposal  →  deterministic policy engine  →  capability  →  operating system
```

That engine already exists and the personal layer plugs into it rather than around it:

- `harness/risk.py` classifies every tool (read / write_local / exec / external) with a
  **fail-closed default** — an unclassified tool is external. The three new tools are classified
  there: `state_today` read, `note_save` and `task_update` write_local.
- `harness/gate.py` decides allow / needs_user / deny before a tool runs, from the tool, its
  arguments, the mode, the granted roots and the user's own words — never from the model's opinion.
- `harness/audit.py` records the rule that let each consequential action through, and if it cannot
  record, the action **does not run**.
- Standing rules are per-target and per-run; the tools that could hand over an account
  (`browser_eval`, `bash`, …) can never carry one.

The personal layer adds no new authority. Its writes are local SQLite; its suggestions produce
ordinary runs that pass the ordinary gate.

## 22. Credential brokering

Agents and skills should not hold raw credentials. Collie has an OS-backed vault
(`harness/identityvault.py`: Windows DPAPI, macOS Keychain, Linux Secret Service) that stores
opaque references bound to a specific Collie, account and factor, hands callers a wipeable buffer,
and has deliberately **no** listing, serialization or model-context API.

The Sauna connector uses it: the connection token is stored by reference, never in settings, never
in the personal state, never in a prompt. The architecture leaves room for OAuth and enterprise
secret stores behind the same broker.

## 23. Skills and MCP

Unchanged and preserved. Skills are `SKILL.md` files discovered from the project, the user, and
enabled Library extensions; repo-sourced skills are marked untrusted and fenced as data. Extensions
declare capabilities and permissions in a manifest, are digest-pinned, and are inert until the
exact version and its authority are approved.

We did not build a marketplace. The interesting idea is an **open capability ecosystem** where a
future registry is primarily a *trust layer* — provenance and revocation — rather than a store.

## 24. Business model

The split follows the architecture instead of fighting it.

**Collie — free, MIT, open source.** Native AI entry, local context, local state, local execution,
computer use, notes/tasks/calendar/journal, local memory, local workflow learning, BYOK, local
models. Complete on its own.

**Sauna Personal — paid cloud.** Person-level intelligence, cross-device continuity, long-term
memory, richer personalization, workflow learning across devices, personal AI portability, backup,
cloud execution, parallel agents, proactive planning, daily and weekly reports.

Sauna monetizes what genuinely requires a cloud or a person-level view. It never monetizes an
artificial limitation:

> **Collie Free — understands this device. Sauna — understands you.**
> Collie sees the current task. Sauna sees the trajectory.

## 25. The product flywheel

```
Install Collie → use native AI daily → it helps with real work → local state becomes valuable
   → Sauna adds person-level context → the AI gets more accurate → it observes how you work
   → Sauna learns your workflows → it suggests the next action → more of it is automated
   → the personal context is richer still → higher retention → ↺
```

This is stronger than an open-source → paid-sync funnel, because the product **gets better the
longer you and the AI work together**, and the thing that improves — your personal state and your
learned workflows — is exactly the thing that is hard to recreate elsewhere.

## 26. Long-term architecture

```
                              SAUNA — person-level intelligence
        identity · long-term context · personal state · workflow learning · goals
        commitments · notes · calendar · journal · reports · cloud agents · multi-device
                                        │
                                        ↕   Personal AI Protocol
                                        │   (docs/PERSONAL_AI_PROTOCOL.md)
                              COLLIE — native device AI
        native entry · device context · local state · local memory · local executive
        local runtime · computer use · browser · files · terminal · desktop · voice
                                        │
                                        ↕   MCP / APIs
                              EXTERNAL — Calendar · Mail · Slack · GitHub · Notion
```

The protocol is small on purpose: status, sync, person context, signals, handoff, devices,
export/restore, route. Clean boundaries, no over-engineering.

## 27. The one-minute story

Personal AI should not live inside a website. Collie brings it into the operating system, where the
work actually happens: one shortcut, the current context, real hands, and evidence for what it did.
Collie stays free, open source and local-first — it is a complete AI for this device.

But a person's intelligence should not reset every time they change machines. Sauna upgrades Collie
from device-level AI to **person-level AI**: it remembers the projects, goals, notes, tasks,
calendar, journal, decisions, preferences and workflows. As Collie and the person work together,
Sauna learns how that person works, and the AI moves from helping with individual tasks to
understanding what should happen next:

> "X is finished. You normally do Y next, so I handled that too. Z probably comes next — should I?"

Not another chatbot. A personal AI system that becomes more useful the longer you work with it.
