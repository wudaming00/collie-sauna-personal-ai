# Personal AI Protocol v2

Status: local implementation active; remote Sauna transport remains an adapter
Date: 2026-08-24

This protocol connects nodes of the same Personal AI Core. Collie is the local node; Sauna may be
a cloud runtime and product surface. Neither product's private database is the protocol. The
normative object boundaries are in [Personal AI Core](PERSONAL_AI_CORE.md).

```
        ┌──────────────────────── CLOUD NODE ────────────────────────┐
        │ sessions · cloud execution · connected apps · mobile UX   │
        └───────────────▲──────────────────────────┬─────────────────┘
                        │ delta / outcomes         │ context / work
                        │                          ▼
        ┌───────────────┴────── PERSONAL AI CORE ────────────────────┐
        │ knowledge · commitments · identity · review · audit        │
        └───────────────▲──────────────────────────┬─────────────────┘
                        │                          │
        ┌───────────────┴──────── COLLIE NODE ─────▼─────────────────┐
        │ offline state · device context · local tools · permissions │
        └────────────────────────────────────────────────────────────┘
```

## Invariants

1. **Collie remains complete offline.** Network absence cannot make local Tasks, Notes, Memory or
   execution unavailable.
2. **Logical equivalence, physical independence.** Nodes share IDs, states and events, not database
   engines or private implementation tables.
3. **Origin authority is explicit.** Live external data, local device context, user-curated state
   and leased execution receipts have different authoritative sources.
4. **Privacy is enforced at reads and writes.** A withheld category is absent from deltas and from
   cloud-labelled context. Secrets and credential references never enter either path.
5. **Context is untrusted advice.** Remote context cannot grant a capability, approve an action or
   widen a local gate decision.
6. **Observed outcomes only.** Scheduling work is not completion. A verified receipt is not an
   agent claim.
7. **No silent conflict loss.** Divergent edits are preserved for review.
8. **MCP is not replication.** MCP performs live queries/commands. Delta sync transfers state.

## Calls

| Call | Direction | Purpose |
| --- | --- | --- |
| `status()` | local | runtime, peer cursor, schema and conflict health |
| `changes_since(cursor, include)` | ↑ | privacy-filtered immutable delta pages |
| `apply_delta(delta, peer_id)` | ↓ | idempotent apply, tombstone or conflict |
| `set_peer_push_cursor(peer, cursor)` | local | acknowledge only after transport success |
| `enqueue_sync_category(category)` | local | bootstrap current values after opt-in |
| `memory.changes_since(cursor, scopes)` | ↑ | typed claims, evidence and graph receipts |
| `sessions.changes_since(cursor, projects)` | ↑ | explicitly enabled safe conversation archive |
| `person_context(query)` | ↓ | bounded, policy-filtered person context |
| `signals(events)` | ↑ | behavioural outcomes and verification receipts |
| `handoff(request)` | ↑ | create an execution request/session on another runtime |

The legacy `export_snapshot()` / `import_snapshot()` pair remains for portability during the v1
migration window. It is not the ongoing synchronization algorithm.

Sauna-compatible Memory Cards are generated read-only views, not delta entities. Typed Memory
claims now replicate by stable cross-node IDs and revisions; adapters may display card projections
but must never merge card Markdown back or represent it as canonical state.

## Delta format

```jsonc
{
  "format": "collie-personal-delta/2",
  "source_device": "dev_…",
  "from_cursor": 41,
  "cursor": 57,
  "has_more": false,
  "categories": {"tasks": true, "notes": false},
  "withheld": ["notes"],
  "changes": [
    {
      "change_id": "chg_…",
      "entity_type": "task",
      "entity_id": "tsk_…",
      "operation": "upsert",
      "base_revision": 3,
      "revision": 4,
      "origin_device": "dev_…",
      "hlc": "1787593000000:000001:dev_…",
      "changed_at": 1787593000,
      "payload": {"id": "tsk_…", "title": "…", "status": "done"}
    },
    {
      "change_id": "chg_…",
      "entity_type": "note",
      "entity_id": "nte_…",
      "operation": "delete",
      "base_revision": 6,
      "revision": 7,
      "origin_device": "dev_…",
      "hlc": "…",
      "changed_at": 1787593010,
      "payload": {"id": "nte_…", "title": "previous value for review"}
    }
  ]
}
```

Rules:

* `change_id` is the idempotency key.
* Payloads are captured at write time, not reconstructed from the latest row later.
* `base_revision == local revision` is a clean apply.
* A delete leaves an `entity_versions.deleted_at` tombstone.
* A divergent base revision creates `sync_conflicts`; local state remains untouched.
* Resolving with the remote payload creates a new local revision so the decision propagates.
* `this_device` always becomes false on import.
* Project paths travel only when `local_files` is enabled.
* Unknown entity types and columns are rejected by a code-owned allow-list.

### Typed Memory sibling format

`collie-memory-delta/2` uses the same change identity and revision rules, but its entity is a
`MemoryClaim`. A page may attach privacy-safe evidence manifests and graph-extraction receipts.
The claim payload is complete and allow-listed; local integer IDs, embeddings, FTS rows, graph
tables and evidence source paths are absent. Relation values travel only so the receiving node can
rebuild a claim-backed graph. A claim deletion leaves a tombstone.

### Session Memory sibling format

`collie-session-memory-delta/1` is gated by the `conversations` category, which defaults off. An
upsert contains bounded user/assistant episodes plus title/summary metadata; a delete contains only
the stable session ID. Local paths, tool/system messages, sensitive messages and embeddings are
absent. Conflicts are preserved and resolved as a fresh revision. Turning sharing off asks the
remote adapter to purge its safe archive copy without deleting Collie's local session journal.

## Current transport status

`harness/sauna.py` is still `MODE = "prototype"`. It writes a v1 portability snapshot and v2
Personal State delta batch under the local Sauna mock directory, plus
`collie-memory-delta/2` pages and opt-in `collie-session-memory-delta/1` pages. Typed Memory includes
evidence manifests and accepted graph-extraction receipts; Session Memory omits tool/system
messages, credentials, local paths and embeddings. Both preserve divergent edits for review.

Its signed-in browser/MCP bridge is useful for interactive handoff, but is not treated as a sync
API. Production synchronization requires an authenticated Sauna endpoint that acknowledges delta
cursors and honors remote purge requests; screen scraping must never be used as replication.
