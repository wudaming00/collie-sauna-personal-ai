# Collie × Sauna prototype — engineering assessment

Date: 2026-08-25 · Tree: `feat/collie-sauna-personal-ai` (0.21.27)
Status: **implemented** — this was the pre-work assessment; see `SAUNA_VISION.md` for the thesis,
`PERSONAL_AI_PROTOCOL.md` for the boundary, `MEMORY_ARCHITECTURE_V2.md` for the completed shared
memory design, and `DEMO.md` for the walkthrough.

This is the audit-first assessment requested before evolving Collie into the Collie × Sauna
prototype. It records what already exists (and is reused), the genuine gaps, the architecture the
new layers fit into, and what is real versus mocked in the prototype.

## 1. Existing architecture

Collie is a Python package (`harness/`, ~81k lines, stdlib-only core) with one runtime and many
surfaces:

| Layer | Modules | What it does today |
| --- | --- | --- |
| Execution loop | `loop.py` (`Harness.run`), `tools.py` (`default_registry`), `providers.py`, `router.py`, `brain_router.py` | Tool-using agent loop; ~45 tools; Auto routing across Anthropic/OpenAI-compatible/Codex/Claude subscriptions and Ollama; token/cost/verification receipt per run |
| Policy & evidence | `gate.py`, `risk.py`, `audit.py`, `actions.py`, `leash.py`, `inbox.py`, `verification.py`, `verifier.py`, `hooks.py`, `checkpoints.py` | Deterministic gate before every tool call (`allow / needs_user / deny`), four risk classes with fail-closed default, append-only audit ledger that refuses consequential actions when it cannot write, HMAC receipts, Needs-You inbox, executed verification evidence, git checkpoints |
| Memory & context | `memory.py`, `context.py`, `embeddings.py`, `mem_import.py`, `sessions.py`, `recorder.py` | Typed claims (fact/preference/habit/procedure/decision/identity/observation) with status, confidence, evidence threshold (`record_habit_observation`, verify after 3), hybrid BM25+dense recall, cache-ordered prompt composer, per-session JSON transcripts, `runs.db` telemetry |
| Durable work | `mission.py`, `missionweb.py`, `tasktree.py`, `primitives.py`, `jobs.py`, `scheduler.py`, `automations.py`, `pack.py`, `agent_runners.py` | Missions (durable campaigns), specialist run trees, leased background execution, timer/file/page/webhook automations, best-of-N packs, Codex runner |
| Surfaces | `webapp.py` + `webui/index.html` (dispatch desk), `webui/mobile.html` (phone), `webui/ambient.html` (desktop), `wallpaper/Program.cs` (WebView2 host: wallpaper / app window / **global command capsule**), `tui.py`, `acp_agent.py`, `vscode-collie/`, `menubar_mac.py` | Same runtime behind every door; the capsule is a real Spotlight-geometry overlay on `Ctrl+Shift+Space` (Windows), installed by default |
| OS integration | `native.py`/`native_mac.py` (UIA / System Events), `desktop.py`, `screenshot.py`, `record.py`, `capture.py` | Drive any native app (opt-in), launch apps/URLs, media keys, screenshots, screen recording, dictation → diary/calendar |
| Integrations & identity | `browserbridge.py` + Chrome extension (19 `browser_*` tools), `mcpclient.py` (OAuth 2.1 MCP, 10 preconfigured servers), `slackbot.py`, `dogmail.py`, `workidentity.py`, `identityvault.py` (DPAPI/Keychain/Secret Service, fail-closed), `accounts.py`, `telephony*.py` | Real logged-in browser, MCP, Slack, Collie-owned email/phone identity, OS credential vault (wired to Twilio/Fish/accounts, not to sign-in) |
| Multi-device | `remote.py`, `remote_identity.py`, `e2e.py`, `relay/`, `presence.py`, `collie-ios` | Phone supervises desktop through an E2E relay; device registry = phones paired to *this* desktop; no desktop↔desktop state sync |

Background services: `supervisor.py` keeps `web`, `jobd`, `automations`, `bridge` alive as a per-user
scheduled task; the installer also installs `collie command` (capsule host) at logon.

## 2. Existing capabilities reused as-is

- **Native entry**: the global command capsule (`Program.cs --command` → `index.html?capsule=1`).
  Reused; only extended with device + person context chips.
- **Computer use**: `browser_*`, `desktop_*`, `screenshot`, `bash`, files — unchanged.
- **Security**: `gate.py`/`risk.py`/`audit.py` are the boundary. New tools are classified in
  `risk._BASE`; Sauna cloud handoff is `external` (needs approval); nothing bypasses the gate.
- **Memory**: `SqliteMemory` remains the claim store; personal-state *decisions* are also written as
  `kind=decision` claims so Collie recall can find them (long-term memory tier).
- **Activity sources**: `sessions.recent()`, `controlplane.activity()`, `runs.db`, mission/tasktree
  events — the new Activity view unions them with the structured personal-state activity log.
- **Verification/receipts**: every run's receipt (verified, tool calls, files) is the input to the
  executive loop; nothing new is invented for "did it actually run".
- **Voice**: capsule Web Speech + `capture.py` (dictation → diary/calendar) unchanged.
- **Automations / Missions / Jobs**: the execution substrate a learned workflow eventually becomes.
- **Credential vault**: `identityvault.py` brokers the (mock) Sauna credential — the agent never
  sees it.
- **Remote/relay/devices**: `remote_identity.device_id`, pairing, phone surface reused by the
  Devices view; Sauna continuity is layered on top, not in place of it.

## 3. Gaps (only what the thesis needs)

| Gap | Evidence |
| --- | --- |
| No personal state primitives (Task/Goal/Note/Event/Journal/Project) | zero `class Task/Goal/Note/Event` in `harness/`; `plantool` todos are per-run; `capture.py` writes diary lines only |
| No executive/Today view, no daily brief | no `today.md`/`profile.md`/daily or weekly summary anywhere |
| No activity → state → journal loop | `SessionEnd`/receipt carry run stats but nothing updates tasks/projects/journal |
| No workflow learning / next-action suggestion | greps for suggest/proactive/next_action/workflow return nothing in this sense; only `record_habit_observation` (routing habits) exists |
| No device context collection | `native.foreground_pid()` implemented but unused; no selection/clipboard/active-app capture; capsule chips are UI state only |
| No person-level (cloud) context, no cross-device state sync, no cloud execution | `remote.py` is phone→desktop control; `brain_router.executor_for` is local-only; no sync toggles |
| No OS notifications | none on any platform (phone push only) — out of scope here except where the UI needs it |

## 4. Proposed architecture (new layers around existing Collie)

```
                     Sauna (person-level, cloud)   ← mocked locally in this prototype
                     person context · continuity · cloud tasks · sync
                                  ↕  Personal AI Protocol (sauna.py boundary)
  ┌────────────────────────────────────────────────────────────────────┐
  │ executive.py   Today brief · closed loop (run → activity → task →   │   NEW
  │                project → journal → suggestion) · next actions      │
  │ workflows.py   Personal Workflow Model (observe → suggest → auto)   │   NEW
  │ personal_state.py  Personal State Model (SQLite) + Markdown views   │   NEW
  │ localcontext.py    device context (app · window · selection · proj) │   NEW
  │ personal_tools.py  note_save / task_update / state_today tools      │   NEW
  ├────────────────────────────────────────────────────────────────────┤
  │ EXISTING: loop · tools · gate/risk/audit · memory · sessions ·      │
  │ missions/jobs/automations · browser/desktop/files/terminal ·        │   REUSED
  │ webapp + dispatch desk + capsule host · remote/relay · vault        │
  └────────────────────────────────────────────────────────────────────┘
```

Integration points (surgical):

- `loop.py`: one optional callback (`Harness.activity_sink`) fired with the run's structured
  summary at the same place the `receipt` event is emitted. Default `None` → zero behaviour change.
- `context.py`: one optional volatile section (`ContextComposer.situation`) so device context and
  Sauna person context reach the model per turn without busting the cached prefix.
- `webapp.py`: new `/api/state/*`, `/api/context/local`, `/api/sauna/*` routes (token-gated), and
  `done{personal_state}` so the UI can render the proactive card.
- `index.html`: rail destinations Today · Tasks · Notes · Calendar · Journal · Memory · Devices (in
  addition to Work/Missions/Needs You/Activity/Library/Pack), capsule context chips, Settings →
  Context + Sauna, Run-on picker.
- `cli.py`: `collie today | note | task | journal | sauna | state` subcommands.
- `settings.py`: Context and Sauna groups (privacy toggles, default-off for sensitive context).
- `risk.py`: classify the new tools.

## 5. Minimum viable prototype

1. Personal State Model in SQLite (`~/.collie/personal.db`) with projects, goals, tasks, events,
   notes, journal, activities, relations, workflows, suggestions, devices, cloud tasks, sync prefs.
2. Today view + executive brief API; Tasks/Notes/Calendar/Journal/Memory/Devices pages.
3. Real run → activity → task/journal/project update loop (web and CLI paths).
4. Device context in the capsule and in the model prompt.
5. Workflow learning (rule/pattern prototype) + proactive next-action card with Run / Not now.
6. Mock Sauna: connect/disconnect, granular sync, person-level context block, cloud handoff queue,
   devices, export/import snapshot ("Welcome back").
7. Docs: `SAUNA_VISION.md`, `DEMO.md`, `PERSONAL_AI_PROTOCOL.md`; demo seed data.

## 6. Files / modules

New: `harness/personal_state.py`, `harness/executive.py`, `harness/workflows.py`,
`harness/localcontext.py`, `harness/sauna.py`, `harness/personal_tools.py`, `harness/demo_seed.py`,
tests `tests/test_personal_state.py`, `tests/test_executive_loop.py`, `tests/test_workflows.py`,
`tests/test_localcontext.py`, `tests/test_sauna.py`, `tests/test_state_web_api.py`,
docs `docs/SAUNA_VISION.md`, `docs/DEMO.md`, `docs/PERSONAL_AI_PROTOCOL.md`.

Modified (surgical): `harness/loop.py`, `harness/context.py`, `harness/cli.py`, `harness/webapp.py`,
`harness/webui/index.html`, `harness/settings.py`, `harness/risk.py`, `harness/tools.py`.

## 7. Mock vs real

| Real (existing Collie functionality) | Newly implemented (real, local) | Mocked for the prototype |
| --- | --- | --- |
| Global capsule + hotkey host | Personal State Model + Markdown projections | Sauna cloud backend (a local `SaunaClient` that behaves as the cloud would) |
| Run loop, tools, browser/desktop/files/terminal | Executive loop (activity → task → project → journal → suggestion) | Cloud task execution (queued + scheduled record; not executed remotely) |
| Gate/risk/audit/inbox approvals | Workflow learning (pattern rules, evidence threshold, feedback) | Second device (rendered from an exported snapshot; no live second machine) |
| Memory claims + recall | Device context capture (foreground app/window/selection/project) | Person-level enrichment (derived from local state "as Sauna would return it") |
| Sessions/receipts/verification evidence | Today brief, Tasks/Notes/Calendar/Journal/Memory/Devices views | — |
| Credential vault | Sauna credential via vault; sync preference store | — |
