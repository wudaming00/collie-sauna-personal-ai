# Personal AI Core

Status: implemented local semantic core (Personal State, Memory and Session deltas)
Date: 2026-08-24

Personal AI Core is the shared semantic contract between Collie, a local runtime, and a
person-level cloud runtime such as Sauna.  It is not a shared physical database.  Collie may use
SQLite/FTS locally while a cloud implementation uses a different store; IDs, states, commands,
events, privacy and conflict semantics must remain the same.

## Object boundaries

| Object | Meaning | It is not |
| --- | --- | --- |
| Commitment Task | Work the person intends or owes | an agent's internal todo |
| Session | One agent work attempt and review lifecycle | a personal commitment |
| Mission | A durable, leased execution campaign | a calendar reminder |
| RunStep | An execution step inside a Session/Mission | a global Task |
| Automation | A trigger definition; every fire creates a Session | a Task instance |
| MemoryClaim | Durable knowledge with source, evidence and trust state | a chat summary |
| Memory Card | A generated, read-only compatibility view | canonical memory |
| Activity/Receipt | Immutable evidence of what happened | an inferred fact |

The corresponding constants and sync allow-list live in `harness/personal_core.py`.

## Runtime model

```
Collie UI / CLI / Desktop            Sauna Web / mobile / messaging
              │                                  │
              └────────── Personal AI Core ──────┘
                                  │
       Knowledge · Commitments · Work · Automation · Identity
                                  │
                 Policy · Review · Audit · Domain Events
                                  │
                   Versioned Store + Transactional Outbox
                         │                         │
                Collie SQLite/FTS          Cloud implementation
                         └──── Delta Sync Protocol ────┘
```

MCP and sync are different boundaries.  MCP is for an agent to query or command a live device.
Delta sync is deterministic replication and never depends on model output or browser scraping.

## Authority

There is no universal last writer:

* the connected source owns its live Gmail/Calendar/etc. data;
* the current device owns screen, selection, local files and login state;
* Personal AI Core owns accepted goals, commitments and curated Memory claims;
* the runtime holding a Mission lease owns that execution attempt and its receipts;
* the current user statement and freshly observed source data outrank historical summaries;
* Memory proposals never grant capabilities or become policy before attestation/verification.

## Storage foundation now implemented

`PersonalState` opens the old tables first and then runs an idempotent migration tracked in both
`schema_migrations` and SQLite `user_version`.

For every allow-listed entity, SQLite triggers atomically write:

* `entity_versions`: revision, origin device, HLC and deletion tombstone;
* `sync_changes`: immutable payload-at-write-time delta rows;
* `sync_peers`: independent push/pull cursors per peer;
* `sync_applied`: idempotency fence for received change IDs;
* `sync_conflicts`: divergent edits preserved for explicit review.

The trigger and the domain write are one SQLite transaction.  A process cannot publish an object
without its outbox row or publish a change whose object write rolled back.

## Conflict policy

A remote change applies directly when its `base_revision` equals the current local revision.  A
replay is ignored by `change_id`.  A divergent change never silently overwrites local data: both
payloads enter `sync_conflicts`.  Choosing the remote version is performed as a fresh local write,
which creates a new revision and propagates the resolution to other peers.

This first implementation intentionally does not add a full CRDT.  Structured entities use
revision-based review; concurrent note/journal edits preserve both versions.  A future text
three-way merge can resolve the easy cases before opening a review item.

## Privacy

Categories are filtered before a delta leaves the store.  A cursor may advance over withheld
changes; enabling a category queues its current versions once, without leaking historical private
intermediate edits.  Project filesystem paths require `local_files`.  Snapshot metadata is a
positive allow-list, so credential references, browser state, caches and peer cursors cannot be
exported accidentally.

The same policy gates person-level context.  Turning Notes or Tasks sync off also prevents those
values—and task-derived goal progress—from being labelled as Sauna context.

## Memory compatibility views now implemented

`MemoryCardProjector` deterministically renders Sauna's seven familiar cards from Collie's typed
Memory and Personal State. Only live claims in `active`, `attested` or `verified` state enter a
card; proposals, rejected/invalidated claims, expired claims and superseded claims do not. Each
projected claim retains a visible claim ID, trust status, confidence and source.

The cards live under `~/.collie/state/memory/` and are available from the token-gated
`GET /api/state/memory-cards`. They are never parsed back. A claim that does not fit a card stays
in typed Memory rather than being forced into a misleading category. This is a compatibility
surface for current Sauna UX, not the future Memory replication format.

The completed hybrid-memory design is described in
[Memory Architecture v2](MEMORY_ARCHITECTURE_V2.md). Typed claims carry stable cross-device IDs,
revisions, tombstones, evidence links, effective/observed time and contextual qualifiers. Retrieval
supports `as_of` and `known_at`; accepted claims can support a bounded, retractable relationship
graph; graph extraction and retrieval decisions have receipts. The graph, embeddings and FTS are
rebuildable indexes and never become second sources of truth.

Session Memory supplies the other half of Sauna's documented model: recent thread summaries,
hybrid fragments and an exact local-thread seam. Its replication category defaults off and sends
only safe user/assistant episodes. Contextual preference resolution follows explicit policy/current
request/preference/habit/default precedence and records its decision.

## Compatibility and next migrations

`collie-personal-state/1` remains a full portability snapshot while installed clients migrate.
`collie-personal-delta/2`, `collie-memory-delta/2` and
`collie-session-memory-delta/1` are the replication contracts. The local Sauna prototype writes
all enabled batches beside `person.json`; a real adapter will send the same pages over an
authenticated API and acknowledge their independent cursors.

Next production migrations should add:

1. an authenticated Sauna transport with acknowledgement, purge and retry semantics;
2. one shared Session projection over local sessions, Missions and cloud runs;
3. Automation projections whose fires create Sessions;
4. a three-way text merge worker for easy note/session conflicts;
5. a first-class Review/Needs You UI for Personal State, Memory and Session conflicts;
6. retention compaction and fixed-budget retrieval/extraction evaluation in production telemetry.
