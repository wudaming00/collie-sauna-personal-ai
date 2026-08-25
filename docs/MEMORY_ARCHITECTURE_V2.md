# Memory Architecture v2: Collie × Sauna

Status: local/cloud semantic contract and local implementation complete
Date: 2026-08-24

## What Sauna publicly documents

Sauna describes a useful **product architecture**, but does not publish its storage engine or
retrieval implementation.

Its [Memory guide](https://www.sauna.ai/learn/memory) documents two layers:

1. **Workspace memory** — curated Markdown under `memory/` and selected documents. It stores what
   should remain true: identity, preferences, agreements and conventions.
2. **Session memory** — indexed conversation history. Automatic context includes recent-thread
   summaries and roughly related fragments; for important questions Sauna can search the archive,
   browse threads, open a full conversation or synthesize across sessions.

Its [Knowledge guide](https://www.sauna.ai/learn/web-app/knowledge) exposes workspace memory as
seven fixed, editable cards: User Preferences, Rules, User Profile, Your Tools, Sauna Identity,
User Relationships and Recent Activity. [Spaces](https://www.sauna.ai/learn/multiplayer/spaces)
isolate each team's documents, skills, schedules and connections from personal Memory and other
Spaces.

Sauna also states the right precedence rule: the live conversation and current observations beat
automatic historical reminders. The reminders are orientation, not proof.

The public material does **not** say whether Sauna uses a particular embedding model, vector
database, knowledge graph, reranker, graph traversal algorithm, temporal database or conflict
model. “Roughly related fragments” is consistent with semantic retrieval, but calling it a known
embedding implementation would be inference. There is no public basis for claiming a graph.

## Collie's decision

Adopt Sauna's two-layer user experience, not its seven Markdown files as the internal database.
Use one authoritative evidence-and-claim system with several disposable indexes:

```text
consented episodes / tool receipts / source events
                      │
                      ▼
           typed, evidence-backed Claim Ledger
       trust · provenance · scope · bitemporal validity
          │              │                 │
          ▼              ▼                 ▼
     BM25 / FTS     dense vectors    derived entity graph
          └──────────────┬─────────────────┘
                         ▼
                  retrieval planner
        lexical · semantic · temporal · multi-hop
                         ▼
             supported context set + receipt
                         │
             ┌───────────┴───────────┐
             ▼                       ▼
     Sauna-compatible cards     session/archive recall
```

The Claim Ledger is the only layer allowed to say whether something is accepted, current,
historical, conflicted or withdrawn. Embeddings and graph edges are rebuildable indexes. A graph
edge is not a fact merely because an extractor emitted it.

## Why the hybrid is query-gated

BM25 and dense retrieval are complementary universal candidate generators. Time and graph are
different:

* temporal filtering should run before ranking whenever a claim has an effective interval;
* graph expansion is valuable for relationship and multi-hop questions, but always-on traversal
  adds unrelated neighbors and latency to ordinary preference or single-fact questions;
* recency is not truth. A late observation may describe an older valid period;
* a similarity score is not evidence and cannot override trust, scope, deletion or a live user
  correction.

The planner therefore receives explicit `as_of` (world/effective time) and `known_at`
(transaction/knowledge time). It enables bounded graph traversal only when entities and a
multi-hop intent are present. All arms run after authorization filtering and before support-set
selection.

## Implemented system

### 1. Authoritative claim ledger

`SqliteMemory` stores typed claims with:

* `valid_from` / `valid_to` — when the claim is true in the world;
* `observed_at` — when the supporting observation occurred;
* `conflict_key` — the logical property whose overlapping versions must be resolved.
* stable `claim_id`, `revision`, `origin_device`, `supersedes_claim_id` and deletion tombstones;
* trust status, scope, subject, provenance, confidence, contextual qualifiers and counter-evidence;
* stable evidence IDs whose manifests can replicate without device-local source paths.

`recall(..., as_of=..., known_at=...)` performs bitemporal admission. It can answer a historical
question using evidence learned later, while an expired claim cannot be resurrected merely by
choosing an old `as_of`. Overlapping versions with the same conflict key admit only the latest
valid version for that instant.

`MemoryGraph` stores normalized entities and typed edges backed by accepted claim IDs. Traversal
is limited to three hops and a bounded node count. Every read joins back through claim status,
project, scope, device, expiry, supersession and bitemporal validity. Invalidating a claim makes
its edges immediately non-traversable without pretending the audit history never existed.

Relations are persisted on the supporting claim so a peer can rebuild the graph. Extraction
receipts retain the extractor, model, input hash and exact accepted relation set. Graph entities,
edges, FTS rows and embeddings remain disposable indexes, never parallel sources of truth.

Callers opt in through:

```python
memory.set_claim_relations(claim_id, relations, extraction_receipt={...})
memory.recall(query, graph_entities=["Alice"], graph_hops=2)
```

The Sauna-compatible seven cards remain generated views over live trusted claims and Personal
State. They are not parsed back into Memory.

### 2. Session / episode memory

`SessionMemory` is a rebuildable archive over durable session journals. It indexes only safe
user/assistant speech, retains recent summaries and moment-linked fragments, supports lexical and
optional dense search, and opens the original local session JSON for exact-thread review. Tool and
system messages are never indexed. Messages containing credential material are omitted.

When conversation sharing is explicitly enabled, `collie-session-memory-delta/1` replicates the
safe archive using stable session/episode IDs, revisions, tombstones, idempotency keys, peer
cursors and conflict review. It never sends local paths, embeddings or omitted content. A peer
without the original journal exposes a visibly labelled `archive_only` thread rather than
pretending the safe projection is the complete transcript.

### 3. Query planning and support receipts

`MemoryRetriever` plans lexical, dense, temporal, bounded graph and session arms. It applies
authorization and validity before ranking, suppresses instruction-shaped historical text, selects
a bounded support set and emits a structured `collie-memory-context/2` data envelope. Live input
wins over recalled context; session fragments are labelled historical speech rather than truth.
Every retrieval records selected claim/episode IDs, suppression reasons and abstention in a
durable receipt.

### 4. Contextual preferences

Preferences and habits are resolved against bounded context keys (`task_type`, `channel`,
`project`, `device`, `audience`, `urgency`). Deterministic precedence is:

1. policy boundary;
2. explicit value in the current request;
3. accepted contextual preference;
4. accepted contextual habit;
5. caller default.

Specificity, confidence, recency and counter-observations break ties. Each decision has a receipt,
so remembered behaviour never silently becomes invisible policy.

### 5. Replication and Sauna boundary

`collie-memory-delta/2` transfers complete allow-listed claim versions, evidence manifests, graph
extraction receipts and tombstones. Writes create an outbox revision atomically. Apply is
idempotent; divergent bases create reviewable conflicts; accepting either side emits a fresh
revision. Generated cards, local row IDs, embeddings and graph tables do not cross the wire.
Physical erasure removes prior claim/session payloads from the local outbox, orphaned evidence and
extraction receipts; only an opaque ID plus minimal routing metadata remains in a tombstone so a
peer that never saw the original object can still prevent resurrection.

The local Sauna adapter now emits three independent batches:

* Personal AI state (`collie-personal-delta/2`) for Tasks, Goals, Notes and related objects;
* typed Memory (`collie-memory-delta/2`);
* opt-in Session Memory (`collie-session-memory-delta/1`).

This gives Collie and a future Sauna adapter the same semantic architecture without requiring the
same physical database. Turning a category off produces an explicit remote-purge instruction in
the prototype batch.

## Production hardening still outside this repository

The remaining work is deployment rather than a missing local architecture: Sauna must expose an
authenticated delta endpoint with cursor acknowledgements and category purge semantics; keys and
transport encryption must be production-managed; model-based relation/claim extraction needs a
versioned evaluation gate; and larger deployments need retention compaction plus latency/quality
telemetry. Browser automation is deliberately not a substitute for that transport.
