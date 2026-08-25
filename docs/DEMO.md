# Collie × Sauna — the 4-minute demo

Status: interview-ready script for the prototype in this repository
Date: 2026-08-24

One scenario, seven beats, 3–5 minutes. Everything below is a real code path unless the line says
*prototype* — and where something is mocked, the demo says so out loud rather than letting the
audience assume.

## The whole product in three sentences

Use these exact lines before touching the UI:

> **Collie is the free, local runtime of Sauna OS: it knows this device, works offline, and can act
> on local files and apps. Sauna is the paid person-level cloud: it adds cross-device memory,
> hosted execution, and cloud connectors. They share goals, tasks, events, memories and receipts;
> the difference is where the data is authoritative and where the work can run.**

The shortest version, if interrupted, is:

> **Collie is AI for this device. Sauna turns it into AI for this person.**

Do not describe Sauna as “Collie's backend.” Collie remains useful without it. The accurate model
is one product schema with two runtimes: local-first by default, cloud-expanded by explicit choice.

---

## The safe setup (one command, off camera)

```bash
collie demo prepare
```

This creates a **new isolated profile**, seeds the interview scenario, starts a separate loopback
server, moves the ambient desktop and global capsule to it, and opens the native app directly on
Today. It never hides, rewrites or deletes the normal profile. That isolation is important: old
missions, chat history, `[Test]` calendar rows and personal to-dos cannot leak into the interview.

The product hierarchy is intentional: **Collie is the universal Claude Code/Codex-style AI work
surface**; **Today** and **Memory** are context lenses over that same composer. Tasks, Calendar,
Sauna and operational tools live under **More** for direct review and correction, not as competing
primary workflows.

Run the receipt immediately before screen sharing:

```bash
collie demo check
```

Every line should be `✓` and the last line should say `READY`. To leave the demo and put back every
native surface that was open beforehand:

```bash
collie demo reset
```

## Recommended interview route · 4 minutes

The seven-beat script below is the complete product tour. For the actual interview, use this tighter
route; it has one idea per screen and does not depend on a live model response.

| Time | Show | Say |
| --- | --- | --- |
| 0:00–0:30 | Start on the quiet desktop. Say the three-sentence product explanation above. | “This is not another dashboard. It is a calm local surface; detail appears only when it becomes relevant or I ask for it.” |
| 0:30–1:15 | Open the **Focus** item. Point to its goal and remaining steps. Press **Add to Collie calendar · 9:00 PM**. | “This suggestion is grounded in one shared task/goal/event graph. The first action is local, instant and reversible—notice the receipt says Local only.” |
| 1:15–1:35 | Point to **View agenda**, **Undo**, and **Also put it on Google Calendar via Sauna** in the receipt. Do not press the Sauna action unless the browser session is ready. | “Cloud reach is a second, explicit step. Collie never pretends a local block changed an external calendar.” |
| 1:35–2:05 | Press `Ctrl+Shift+Space` from VS Code or Chrome. Show current-app, project and selected-text context; do not depend on a live answer. | “The Claude Code/Codex-style interface is still the front door. It already knows where I am, so I do not open a dashboard and restate context.” |
| 2:05–2:45 | Open Collie on **Today**. Point to the goal, upcoming event and Local/Sauna context split. | “Goals, tasks, events, memories and receipts have the same shape in both runtimes. Device facts stay authoritative here; person-wide continuity belongs in Sauna.” |
| 2:45–3:20 | Open **Memory → Learned workflows**, then return to Today. | “Memory is hybrid retrieval plus temporal and relational evidence. It does not only recall; repeated sequences become reviewable next-step suggestions.” |
| 3:20–4:00 | Open **More → Devices** and point to Collie Local and Sauna Cloud. Optionally connect Sauna. | “Sauna adds cross-device continuity, cloud connectors and work that continues while this computer is off. The prototype transport is mocked; the boundary and state transitions are real.” |

Only after this stable route lands, optionally run the live-edit or cloud-task beat. A model call is
a bonus, not a dependency for communicating the product thesis.

### What the calendar click means

The Focus action is deliberately a two-step boundary, not a generic button:

1. **Add to Collie calendar** writes one idempotent local block. Repeated clicks cannot create
   duplicates.
2. The Focus panel immediately shows an in-context receipt with the exact time, **Local only**,
   **View agenda**, and **Undo**.
3. **Also put it on Google Calendar via Sauna** is a separate cloud escalation. It is never implied
   by the local success state.

If asked why this matters: *“The UI exposes authority. A successful local action is not reported as
a successful external action.”*

Checks before sharing the screen:

- `collie demo check` ends in `READY`.
- The capsule opens on `Ctrl+Shift+Space` from *another* app (open VS Code or Chrome first).
- **Settings → Sauna is disconnected.** The demo's turning point is connecting it live.
- Settings → Context: active window and selected text on, clipboard off (the default).
- Close notification pop-ups and pause OS updates. Keep VS Code open on `docs/SAUNA_VISION.md`.
- A live provider is optional. If the preflight warns that none is available, skip Beat 2; do not
  debug credentials while screen sharing.

Do not seed the normal profile with `collie state seed-demo` for the interview. That lower-level
command remains useful to developers, but it intentionally does not isolate old state or native
surfaces.

---

## Beat 1 · Native entry — 40s

**Setup:** you are in your editor or a browser, mid-task. Not in Collie.

Press **`Ctrl+Shift+Space`**. The capsule appears over the current app and already shows what you
are doing:

```
Ask Collie
● VS Code · SAUNA_VISION.md — collie · ● Selected text · 38 words · ● Project · Collie
● Sauna · not connected
```

Say or type:

> **"Where am I with the Sauna interview?"**

Collie answers from the person's actual state, not a guess:

```
Upcoming: Sauna interview — Tuesday · 11:00 AM · goal "Prepare for Sauna interview"
43% prepared · remaining: Build Collie prototype; Prepare system design examples; Rehearse
Working on now: Build Collie prototype
```

Without closing the capsule, type a follow-up:

> **"Turn that into the three things I should do next."**

The composer stays available under the answer and the follow-up continues the same session.

**Say:** *"No tab, no navigation, no re-explaining. The shortcut is the product's front door, and it
already knows the app I was in, the text I had selected, and the project I'm standing in — that's
the `state_today` tool reading the personal state, not the model guessing. And it is a conversation,
not a notification — I can refine the instruction without starting over."*

## Beat 2 · Real local work — 60s

Open Collie (or stay in the capsule) and press **Continue with Collie** on *Build Collie prototype*
in the Today view — or just say:

> **"Finish updating the Collie architecture notes."**

Let it run. This is ordinary Collie: it inspects the project, finds the file, edits it, and — if a
check is configured — runs it. The run timeline and evidence gate on the left are the existing
verification machinery, unchanged.

**Say:** *"Nothing here is a demo shim. Same loop, same tools, same deterministic permission gate,
same evidence receipt Collie has always produced."*

## Beat 3 · State updates itself — 30s

When the run ends, a card appears under the answer:

```
Done
✓ Finish updating the Collie architecture notes
✓ Changed 1 file: SAUNA_VISION.md
✓ Completed: Build Collie prototype
✓ Regenerated project summary and today's journal
```

Click **Today**: the goal moved to **57%**, the interview's *Preparation* moved with it, the task is
struck through, and Recent activity shows the run, the file change and the completion.

**Say:** *"Execution flowed upward into state. The task closed because the run really did the work —
and note the honesty rule: a weak match asks instead of assuming, and a failed run completes
nothing."*

## Beat 4 · The proactive moment — 45s  ★ the key beat

Still on that card:

```
Based on your interview preparation workflow:
  NEXT LIKELY STEP
  Prepare system design examples
  [ Run ]  [ Not now ]
```

**Say:** *"Memory answers 'what happened before'. This is a different question — 'what usually
happens next'. Collie learned it: research → thesis → build → system design, observed across two
separate interview preparations. Two goals to suggest, three to confirm — the same evidence
threshold Collie already uses before it trusts a habit."*

Open **Memory → Learned workflows** for one second to show the evidence (`seen 2× · accepted 1`),
then come back.

**Say:** *"And the boundary is enforced, not promised: it can auto-continue only for a workflow you
explicitly automated, and only for local reversible steps. Anything that reaches outside this
machine — an email, a call — is always a suggestion, and then still goes through the gate."*

Click **Run**. It starts the next step as an ordinary run.

## Beat 5 · What Sauna adds — 45s

Point at the **Context** panel on Today while still disconnected:

```
Local                    Sauna · not connected
✓ Current app            ○ Related project history
✓ Current project        ○ Active goal
✓ Selected text          ○ Upcoming deadline · Previous decisions · User preferences
                         ○ People & relationships · Learned workflows
```

Open **Settings → Sauna → Connect**. Return to Today — the right column fills in.

**Say:** *"Collie understands this device. Sauna adds the person: which report I mean, why it
matters, who is waiting, what I decided last week, how I write these, what I finished yesterday.
Same request, same model — more person."*

If you want the receipt, run `collie sauna context "where am I with the Sauna interview"` in a
terminal and show the block that goes into the prompt.

**Say (honesty):** *"The cloud is mocked in this prototype — that client writes to a local store —
but the protocol boundary is real: eight calls, documented in `docs/PERSONAL_AI_PROTOCOL.md`."*

## Beat 6 · Cloud handoff — 30s

In the composer, type:

> **"Research the remaining competitors tonight and give me a report tomorrow morning."**

A runtime picker appears **because of what you asked**, not always:

```
RUN ON   ○ This computer   ● Sauna Cloud
         Long-running or scheduled — can run while this computer is off.
```

Choose **Sauna Cloud** and send:

```
☁ Scheduled on Sauna Cloud                                   [prototype]
Research the remaining competitors tonight and give me a report tomorrow morning
Starts Saturday 9:00 PM · report by Sunday 8:00 AM
Runs while this computer is off. Cloud execution is mocked in this prototype: it is
recorded and visible in Today, never reported as done.        [ Run here now instead ]
```

**Say:** *"Routing is a rule, not a mood: anything needing local files or my logged-in browser stays
here; long-running work that shouldn't depend on my lid being open goes up; anything with an
external effect needs approval wherever it runs."*

## Beat 7 · Continuity across devices — 30s

Open **More → Devices**:

```
● DESKTOP-…      desktop · Windows · capsule, web, cli          online
◐ Sauna Cloud    cloud · long-running · parallel · offline-ok    mock

Personal AI continuity
Goals · Tasks · Notes · Calendar · Journal · Preferences · Learned workflows · Project context
```

Then show the restore, which is a real code path — in a second terminal, pretending to be a new
laptop:

```bash
# a different state dir is a different machine, as far as Collie is concerned;
# COLLIE_SAUNA_DIR points both of them at the same (mock) cloud copy.
export NEW=/tmp/new-mac  CLOUD=~/.collie/sauna
COLLIE_STATE_DIR=$NEW COLLIE_SAUNA_DIR=$CLOUD collie sauna restore
# welcome back — restored from DESKTOP-…: {"goals": ["Prepare for Sauna interview"],
#   "tasks_open": 4, "notes": 3, "events": 2, "workflows": ["After research, write it up"],
#   "journal_days": 2}
COLLIE_STATE_DIR=$NEW COLLIE_SAUNA_DIR=$CLOUD collie today
```

**Say:** *"That is a different state file — a different machine, as far as Collie is concerned — and
it says 'welcome back' with the goals, tasks, notes, journal and learned workflows intact. iCloud
moved your photos; Sauna moves your intelligence."*

---

## The close — 20s

> Personal AI shouldn't live in a website. Collie puts it in the OS, where the work is: one
> shortcut, real context, real hands, and evidence for what it did. It stays free, open source and
> local-first — a complete AI for this device.
>
> But your intelligence shouldn't reset when you change machines. Sauna upgrades Collie from
> device-level AI to person-level AI, and as you work together it learns how you work — until the
> AI stops helping with tasks and starts knowing what should happen next.
>
> **Collie is AI for this device. Sauna turns it into AI for this person.**

---

## If a beat fails live

| Symptom | Recovery |
| --- | --- |
| The capsule does not open | `collie command --stop && collie command` — or click **Ask Collie** in the top bar; the demo continues identically |
| No selected-text chip | Expected on some apps (no UI Automation text pattern). Say so — it degrades to app + window + project |
| The run in beat 2 is slow | Cut to Today and narrate the state change; the card appears when it lands |
| No suggestion after beat 3 | The task bound weakly, so Collie asked instead of assuming — that *is* the honesty rule. Confirm it, and the suggestion follows |
| Sauna panel empty | `collie sauna status`; reconnect with `collie sauna connect --account you@sauna.ai` |

## Everything the demo touches, in code

| Beat | Files |
| --- | --- |
| 1 | `harness/wallpaper/Program.cs`, `harness/wallpaper.py`, `harness/localcontext.py`, `harness/personal_tools.py` |
| 2 | `harness/loop.py`, `harness/tools.py`, `harness/gate.py`, `harness/verification.py` (all pre-existing) |
| 3 | `harness/executive.py`, `harness/personal_state.py` |
| 4 | `harness/workflows.py`, `harness/personalweb.py` |
| 5 | `harness/sauna.py`, `harness/context.py` |
| 6 | `harness/sauna.py` (`route`, `handoff`), `harness/webui/index.html` |
| 7 | `harness/personal_state.py` (`export_snapshot` / `import_snapshot`), `harness/sauna.py` (`restore`) |
