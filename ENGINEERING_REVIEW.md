# Collie × Sauna — engineering review guide

This repository is a clean review snapshot of a local-first personal AI system. Collie is the
device runtime: it owns local state, context, tools, permissions and verified execution. The Sauna
adapter represents a person-level cloud that can add continuity, connected services and hosted
work without making the local product incomplete.

The prototype is designed to be judged as code, not only as a UI demo. The fastest review path is
the five slices below.

## Architecture in five slices

| Slice | Start here | What to inspect |
|---|---|---|
| Personal state | `harness/personal_state.py`, `harness/personal_core.py` | Typed SQLite state, relations, revisions, tombstones, projections and conflict preservation |
| Memory and sync | `harness/memory_sync.py`, `harness/session_sync.py`, `harness/memory_retrieval.py` | Privacy-filtered deltas, evidence manifests, safe session archives, bounded hybrid retrieval and receipts |
| Executive loop | `harness/executive.py`, `harness/workflows.py`, `harness/brain_router.py` | Run → activity → task/goal → journal → next-action loop and local/cloud routing |
| Trust boundary | `harness/sauna.py`, `harness/mcpserve.py`, `harness/identityvault.py`, `harness/gate.py` | Explicit prototype adapter, token-gated MCP, read-only default, fail-closed writes, OS-backed secrets and deterministic authorization |
| Product surfaces | `harness/personalweb.py`, `harness/webapp.py`, `harness/webui/index.html`, `harness/webui/ambient.html` | Today/Memory/Devices views, global command context, correction paths and honest scheduled-vs-completed receipts |

The product thesis and complete boundary are documented in
[`docs/SAUNA_VISION.md`](docs/SAUNA_VISION.md) and
[`docs/PERSONAL_AI_PROTOCOL.md`](docs/PERSONAL_AI_PROTOCOL.md). The short, deterministic product
walkthrough is [`docs/DEMO.md`](docs/DEMO.md).

## What is real and what is a prototype

| Path | Real implementation | Prototype boundary |
|---|---|---|
| Local runtime | State, memory, retrieval, workflow learning, permissions, receipts, browser/desktop tools and UI | None required for local use |
| Personal AI protocol | Versioned deltas, cursoring, category privacy, conflicts, tombstones, memory/session envelopes and tests | The remote Sauna store is a local adapter rather than a private production API |
| Signed-in browser | Live bridge to the user's existing browser session with localhost fencing and explicit authorization | It is interactive handoff, never represented as a sync API |
| MCP | Live Streamable HTTP server, secret path, bounded responses, audit log, nine default read tools and four opt-in write tools | Public reachability is supplied by the operator's tunnel or deployment |
| Cloud execution | Scheduling, runtime selection, handoff state and honest receipts | The prototype does not claim a hosted job completed; completion requires a returned outcome |

## Invariants worth challenging

1. Collie remains useful offline; a cloud account is optional.
2. Remote context can advise but cannot grant authority or widen a local permission decision.
3. A scheduled job is never reported as completed without an observed outcome.
4. Divergent edits remain reviewable; synchronization cannot silently discard either side.
5. Conversation sharing is opt-in and excludes tool/system messages, credentials, local paths and
   embeddings.
6. MCP is read-only by default. Opt-in task/note writes are refused before mutation when the audit
   trail is unavailable; computer-operation requests still require the owner's approval.
7. Markdown is an inspectable projection; canonical state is typed and queryable.

## Focused verification

The clean snapshot was verified on 2026-08-25 with:

```bash
python -m compileall -q harness
python -m pytest -q
# 1924 passed, 5 skipped

python tests/browser_suite.py demo_journey_check
# 10/10 checks passed
python tests/browser_suite.py personal_ui_check
# 28 checks, 0 failed
python tests/browser_suite.py memory_ui_check
# all green; no page errors
```

For a shorter code-review gate:

```bash
python -m pytest -q \
  tests/test_personal_state.py \
  tests/test_memory_sync_v2.py \
  tests/test_session_memory.py \
  tests/test_state_web_api.py \
  tests/test_mcpserve.py \
  tests/test_runtime_hardening.py
```

## Run the deterministic demo

```bash
pip install -e ".[local,dev]"
collie demo prepare
collie demo check
```

`demo prepare` creates an isolated profile and local server, so existing tasks, sessions and
credentials cannot leak into the walkthrough. `collie demo reset` restores the prior surfaces and
removes only the isolated demo state.
