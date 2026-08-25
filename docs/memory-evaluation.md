# Collie memory architecture and evaluation protocol

> Implementation update (2026-08-24): claim-level `valid_from`, `valid_to`, `observed_at` and
> `conflict_key`, bitemporal `as_of` / `known_at` retrieval, and a bounded claim-backed graph index
> are now present in production code. Extraction, entity resolution, query planning and support-set
> selection remain experiment-gated. See [Memory Architecture v2](MEMORY_ARCHITECTURE_V2.md).

This document separates three questions that are often collapsed into one:

1. **What is allowed to become memory?** This is a trust, provenance, scope, and deletion problem.
2. **Which memories should be recalled?** This is a retrieval, time, relationship, and ranking problem.
3. **How should recalled memory affect a response or action?** This is a support, preference,
   prompt-safety, and audit problem.

A higher embedding recall score cannot compensate for a failure in either of the other two layers.
The production choice therefore needs a multi-objective experiment, not a single vendor headline.

## Current implementation audit

### End-to-end chain

```text
Explicit local preference ───────────────► attested preference ─┐
Repeated deterministic user choice (3+) ► verified habit ──────┤
Host/import write ───────────────────────► active claim ────────┤
Model remember tool / run summary ───────► proposed claim       │
                                                │               │
                               user attestation │ verification  │ rejection
                                                ▼               │
                                      attested / verified ──────┤
                                                                ▼
SQLite claim ledger: project + scope + device + mission + provenance + time
        │
        ├── hard admission: accepted status, project/global, allowed scope,
        │                   matching device, unexpired, not superseded
        │
        ├── FTS5 BM25 ─┐
        ├── embedding ─┼── reciprocal-rank fusion ── optional reranker
        │              └── mild created-at recency boost
        │
        ├── top-k auto-prefetch ──► RELEVANT MEMORY prompt block
        ├── explicit memory_search tool
        └── trusted_profile ───────► response defaults + Brain Router
```

The important existing invariants are real and worth preserving:

- `SqliteMemory` keeps model-authored claims in `proposed`; ordinary recall accepts only
  `active`, `attested`, and `verified`.
- `Harness.settle_run_memory` binds promotion or rejection to the exact producing run,
  project/scope, provider, model, and immutable provenance. A transported integer ID alone cannot
  promote another claim.
- Explicit preferences become `attested` with confidence 1.0. A habit becomes `verified` only
  after three matching deterministic observations. `trusted_profile` excludes guesses, expired
  rows, foreign devices/scopes, and low-confidence claims.
- Project/global scope, device identity, expiry, status, and supersession are filtered before
  sparse and dense scoring. Pack workers use a throwaway overlay so one candidate cannot teach a
  concurrent candidate its own answer.
- `ContextComposer` automatically recalls memory on every non-empty turn and separately injects
  the confirmed owner profile. Current-request and safety rules explicitly outrank preferences.
- The Brain Router consumes only the typed trusted profile, validates its trust metadata again,
  and records which claim influenced a route.
- The web and CLI review surfaces expose exact attest/reject/invalidate actions within their
  authorized project/device boundary.

### Reproducibility and product gaps

The current implementation has strong admission mechanics but no complete experiment contract:

- `harness/reval.py` is ten synthetic queries with one gold row each and reports only P@1, P@5,
  and MRR. It cannot measure stale facts, multi-hop support, preference context, isolation,
  withdrawal, injection exposure, or abstention.
- This developer checkout contains LOCOMO/LongMemEval scratch runners under ignored `bench/*`, but
  those scripts are not a shipped or CI-enforced benchmark. Their dataset, model/judge revisions,
  exclusions, prompt hashes, and confidence intervals are not one frozen manifest.
- General recall has no `valid_from`, `valid_to`, or conflict-set model. `created_at` produces only
  a mild boost, so an old and a new contradictory fact can both reach the prompt. Import time and
  time-of-truth are also different concepts.
- There is no entity resolution or relationship index. An answer requiring `Alice → Orion →
  Borealis → CockroachDB` succeeds only if lexical/dense search independently finds every support
  row.
- The owner profile is a flat attribute winner. Context exists only if callers encode it into the
  attribute name; there is no first-class predicate for task type, channel, urgency, audience,
  project, or device, and no negative evidence or confidence decay.
- `invalidate` is logical withdrawal, not physical erasure. The text remains in SQLite for audit.
  Product copy and tests must distinguish **stop using this claim** from **delete these bytes and
  derived indexes/backups**.
- Accepted memory text is inserted into `RELEVANT MEMORY` as plain text. The grounding prompt calls
  it a lead rather than a fact, and proposal quarantine helps, but an active legacy/imported row
  containing “ignore previous instructions” has no content firewall and no data-only structured
  envelope.
- General fact recall does not use confidence or source trust in ranking. The model receives text
  but not the claim ID, source, confidence, effective interval, or conflict explanation needed for
  a grounded answer receipt.
- There is no calibrated no-answer threshold. FTS can return a common-word match after the only
  real answer was invalidated, which undermines forget semantics even when the withdrawn row itself
  stays filtered.
- Equal-weight RRF, the reranker candidate pool, recency half-life, and top-k are global settings;
  no query planner chooses temporal, preference, entity, or multi-hop behavior per request.
- Embeddings are stored with a model name and can be rebuilt, but changing or partially failing an
  embedder still depends on an explicit re-embed operation. There is no dual-index rollout and
  atomic cutover for experiments.

## Recommended architecture

Keep the existing claim lifecycle as the source of truth. Do not replace it with an opaque vector
database or make a graph an independent source of facts. Add four derived layers around it.

### 1. Evidence journal

Store consented raw events locally with retention and sensitivity metadata. Raw conversations,
tool output, email, and browser text are evidence—not automatically prompt-visible memories. Each
event has a source, timestamp, project/device/Mission boundary, content hash, and retention policy.
Secrets are redacted before any downstream extraction.

### 2. Typed claim ledger

Evolve the current `facts` rows rather than discarding them. Add:

- `effective_from`, `effective_to`, and `observed_at` separately from `created_at`;
- a `conflict_key` or canonical subject–predicate identity;
- source-trust and sensitivity labels;
- an explicit retention mode: audit-retained withdrawal versus physical erase;
- derivation links from each distilled claim to evidence hashes;
- retraction links that invalidate every derived embedding and graph edge atomically.

Only this ledger owns truth status. Dense vectors and graph edges are disposable indexes.

### 3. Contextual owner model

Preferences deserve a first-class view over accepted claims:

```text
preference = {
  attribute, value,
  when: {task_type, channel, project, device, audience, urgency},
  strength: explicit | verified_habit,
  confidence, observations, counter_observations,
  effective_from, effective_to,
  provenance
}
```

Precedence should be deterministic:

```text
current request
  > Leash / safety / legal boundaries
  > explicit matching project-or-device preference
  > explicit matching global preference
  > verified matching habit
  > product default
```

An inferred preference remains proposed. Habits need repeated independent evidence and decay or
counter-evidence. Every applied default should appear in the run receipt; a non-matching preference
should not be retrieved merely because it uses similar words.

### 4. Retrieval planner and derived graph

Use a staged pipeline:

1. **Authorization and lifecycle filter first:** status, project, scope, device, Mission,
   sensitivity, deletion, and time validity. A later ranker must never be asked to hide an
   unauthorized row.
2. **Query analysis:** detect requested time, entities, task type, preference intent, and whether
   multi-hop support is likely.
3. **Candidate generation:** BM25 plus a production embedding remain the universal base. Add
   temporal/conflict lookup for time-bearing queries and one-to-three-hop graph expansion only for
   entity/relation queries.
4. **Fusion and reranking:** fuse independent ranks, rerank jointly, then select a *support set*
   rather than merely the most similar snippets. Penalize contradictory, disconnected, and
   context-mismatched rows.
5. **Confidence gate:** abstain or ask for verification when the admitted set does not support an
   answer. Similarity alone is not evidence.
6. **Prompt boundary:** pass structured records (`claim_id`, fact, source, status, effective time,
   scope) in an explicitly data-only envelope. Reject or quarantine instruction-shaped imported
   content; never let memory text alter system/tool authority.
7. **Receipt:** record candidate generators used, claim IDs supplied, preferences applied, stale or
   conflicting claims suppressed, and the answer's support coverage.

The graph contains canonical entities and typed edges, each pointing back to one or more accepted
claim IDs and effective intervals. Retraction of a claim retracts its unsupported edges. Graph
expansion should be query-gated: always-on expansion increases latency and false associations on
simple preference or fact questions.

## What to adopt from other memory systems

The systems below solve different problems and their published numbers use different data,
prompts, models, judges, and context budgets. They are architectural references and Stage-B
candidates, not a comparable leaderboard.

| System | Useful mechanism | Collie decision |
| --- | --- | --- |
| [Mem0 / Mem0g](https://arxiv.org/html/2504.19413) | LLM fact extraction with ADD/UPDATE/DELETE/NOOP consolidation; the paper's graph variant adds entities and typed relations | Reproduce its write/consolidation arm. Do not make its graph universal: in the paper's own LoCoMo breakdown graph helped temporal/open-domain questions but reduced single-hop and multi-hop scores. |
| [Graphiti / Zep](https://arxiv.org/html/2501.13956) | Non-lossy episodes, bitemporal entity/fact graph, invalidation rather than history deletion, hybrid vector/BM25/graph search | Best reference for Collie's derived temporal graph and provenance links. Keep Collie's ledger authoritative and add conformance tests before adopting an implementation. |
| [Letta / MemGPT](https://arxiv.org/abs/2310.08560) | Agent-controlled core, recall, and archival tiers; current implementations also use versioned file/context repositories | Adopt the materialized working-set pattern for identity, active preferences, and procedures. Do not let agent-edited blocks replace immutable evidence/provenance. |
| [LangGraph / LangMem](https://docs.langchain.com/oss/python/langchain/long-term-memory) | Explicit semantic/episodic/procedural split; profile versus collection; hot-path or background extraction | Adopt profile **plus** evidence collection and background consolidation. Collie's current flat winner is not enough for contextual preferences. |
| [HippoRAG 2](https://arxiv.org/html/2502.14802) | OpenIE graph, query seeds, Personalized PageRank, passage retrieval | Test as a project/document multi-hop arm. It is not a replacement for personal preference or temporal conflict semantics. |
| [Microsoft GraphRAG](https://arxiv.org/abs/2404.16130) | Entity/relation indexing, hierarchical Leiden communities, map-reduce over community reports | Reserve for static workspace/document global synthesis. Its expensive corpus indexing is a poor default for rapidly changing personal memory. |
| [LightRAG](https://arxiv.org/html/2410.05779) | Incremental entity/relation graph with local/detail and global/theme retrieval | Include only as a project-KB candidate. Do not infer personal-memory quality from its global document QA experiments. |

Mem0's 2025 paper is a useful warning against a single graph headline. On its stated LoCoMo
harness, overall LLM-judge score moved from 66.88 (Mem0) to 68.44 (Mem0g), but single-hop moved
67.13 to 65.71 and multi-hop 51.15 to 47.19; temporal and open-domain improved. That supports a
query-gated graph, not an always-on one. Graphiti's bitemporal representation is a stronger match
for updates, but its retrieval/runtime still needs to win Collie's frozen budgeted experiment.

No vendor-reported score selects the production model. Pin the exact code/SDK revision, dataset
hash, extraction prompt, responder, judge, hardware, and token budget, and run every candidate
through the same manifest. Vendor dashboards may change algorithms without changing the product
name.

## Versioned benchmark schema

`tests/fixtures/memory_eval_seed_v1.json` is a small, impersonal regression suite. Its top-level
fields are:

```json
{
  "schema_version": 1,
  "suite_id": "...",
  "memories": [],
  "queries": []
}
```

A memory supplies stable `id`, text/keys, status/kind/subject, project/scope/device, timestamps,
optional validity/conflict metadata, semantic fixture tags, entities, and provenance-bearing
relations. A query supplies project/device/allowed scopes, `as_of`, semantic fixture tags and
query entities, graded `relevant` IDs, the complete `support_ids` needed to answer, explicitly
`forbidden_ids`, and optional `expect_abstain`.

Semantic tags are a deterministic stand-in for a dense model. They test fusion behavior without a
download; they do **not** measure a production embedder. Query entities similarly stand in for a
perfect entity linker so graph value and entity-extraction accuracy can be tested separately.

The seed covers:

- long-term paraphrased facts;
- current and historical time queries over an updated fact;
- conflicting context-specific response preferences;
- cross-project and cross-device isolation;
- preference withdrawal and deleted-memory abstention;
- a three-edge entity relationship question;
- active imported prompt injection plus an unauthorized-scope injection.

## Offline runner

Run all deterministic candidates:

```powershell
python -m harness.memory_eval `
  --suite tests/fixtures/memory_eval_seed_v1.json `
  --k 3 `
  --output data/memory-seed-report.json
```

Implemented candidates are:

| Candidate | Purpose |
| --- | --- |
| `lexical_bm25` | Sparse baseline after current trust/scope admission |
| `current_hybrid_rrf` | Architecture proxy for BM25 + dense + RRF |
| `time_aware_hybrid` | Hybrid plus validity/conflict filtering and recency |
| `graph_hybrid` | Time-aware hybrid plus bounded relationship expansion |
| `guarded_graph_hybrid` | Graph candidate plus instruction-shaped-content and no-support gates |

The runner makes zero network or model calls. It reports Recall@K, MRR, nDCG@K, complete
answer-support@K, false-memory rate, leakage, prompt-injection exposure, abstention accuracy,
latency, and transparent operation-based cost units. Per-query outputs make every aggregate
auditable.

### Seed result on 2026-08-20

At `k=3`, the deterministic quality result was:

| Candidate | Recall@3 | MRR | nDCG@3 | Full support | False-memory query rate | Injection exposure |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| lexical BM25 | 0.967 | 1.000 | 0.955 | 0.900 | 0.636 | 0.091 |
| current hybrid proxy | 0.967 | 1.000 | 0.955 | 0.900 | 0.636 | 0.091 |
| time-aware hybrid | 0.967 | 0.900 | 0.882 | 0.900 | 0.455 | 0.091 |
| graph hybrid | 1.000 | 1.000 | 0.963 | 1.000 | 0.364 | 0.091 |
| guarded graph hybrid | 1.000 | 1.000 | 0.963 | 1.000 | 0.273 | 0.000 |

All candidates had zero project/device/scope leakage. Only the guarded candidate abstained after
the deleted fact had no remaining support. The graph arm recovered the full three-row support
chain. Naively adding recency lowered MRR on two queries, which is precisely why recency cannot be
accepted from intuition alone. Broad top-3 retrieval still surfaced contextually wrong but
authorized distractors often; a support selector is required even after graph expansion.

The runner names a **seed leader**, not a production winner. The suite is small and exposes its
semantic dimensions. Its job is to prevent architectural regressions and demonstrate which claims
the full experiment must test.

### Directional real-component check

Real local components were checked on 2026-08-20 with the checkout's existing LOCOMO retrieval
runner. This is an **evidence-retrieval** experiment, not the end-to-end LLM-judge protocol used in
vendor LOCOMO reports. Raw turns were ingested without distillation; `k=10`, sparse/dense pool 50,
RRF constant 60, and the first three file-ordered conversations were used for the model sweep.
There is no shuffle or random sampling, so the seed is `N/A`. Those three conversations contain
1,451 turns and 497 questions; the runner scored the 383 questions carrying evidence after
excluding 112 category-5 questions and two other no-evidence questions. It reports evidence
Recall@10 and Hit@10, but not MRR, nDCG, a per-query latency distribution, or answer accuracy.

#### Embedding sweep: first three conversations

Every row below used BM25 + one dense model + RRF with reranking off. `query seconds` is the
runner's aggregate sequential time for 383 recalls. `command wall` additionally includes model
construction, all turn embeddings, SQLite writes, and process overhead; it is an observational
device measurement rather than a latency SLO. In this harness, BGE-M3 means its ONNX dense
CLS-pooled output; the model's native sparse and ColBERT-style outputs were not used. Granite and
GTE were also CLS-pooled, while E5 used mean pooling and query/passage prefixes.

| Dense model | evidence Recall@10 | evidence Hit@10 | query seconds | command wall |
| --- | ---: | ---: | ---: | ---: |
| Granite 107m multilingual | 0.603 | 0.661 | 20.9 s | 85.170 s |
| multilingual E5-small | 0.516 | 0.567 | 16.9 s | 72.992 s |
| GTE multilingual-base | 0.603 | 0.663 | 41.7 s | 180.195 s |
| BGE-M3 | **0.628** | **0.689** | 60.4 s | 270.919 s |

BGE-M3 was the strongest base retriever on this fixed subset, but its command wall was 3.18 times
Granite's. GTE added only 0.002 Hit@10 over Granite at roughly twice the query and wall time. The
subset is directional: conversation-level results in the later full check ranged widely, so these
three conversations must not be treated as a random or representative sample.

#### Cross-encoder candidate-cap frontier

The BGE reranker scores fused candidates jointly with the query. The frontier reused three
conversation-isolated temporary indexes and one loaded BGE-M3/reranker instance; each cap still
performed a complete retrieval pass. The fused pool remained 24 for all rows. Cap 20 comes from a
separate ordinary runner invocation; caps 4, 8, 12, and 24 came from one reusable-index sweep.

| Embedder + reranker | cap | evidence Recall@10 | evidence Hit@10 | query seconds |
| --- | ---: | ---: | ---: | ---: |
| Granite + BGE cross-encoder | 20 | 0.706 | 0.752 | 182.4 s |
| BGE-M3 + BGE cross-encoder | 4 | 0.624587 | 0.689295 | 83.886 s |
| BGE-M3 + BGE cross-encoder | 8 | 0.632637 | 0.689295 | 115.638 s |
| BGE-M3 + BGE cross-encoder | 12 | 0.651349 | 0.715405 | 130.679 s |
| BGE-M3 + BGE cross-encoder | 20 | 0.746 | 0.812 | 182.1 s |
| BGE-M3 + BGE cross-encoder | 24 | **0.763185** | **0.825065** | 197.159 s |

The exact Granite-rerank command wall was not captured and is deliberately not estimated. The
BGE-M3 cap-20 command wall was 387.129 seconds, but that stock runner also executed and timed its
base pass in the same process. The four-cap reusable-index command took 671.616 seconds total,
including one 140.515-second ingest plus every cap's query pass; that total is not a cap-24-only
wall time. Cap 24 improved over cap 20 by 0.017185 Recall@10 and 0.013065 Hit@10 for 15.059 more
aggregate query seconds, so it was frozen for the full check. Caps below `k` did not provide a
useful quality/latency point in this implementation.

#### Frozen full-data check

Only the selected BGE-M3 + BGE-reranker cap-24 configuration was then run over all ten LOCOMO
conversations. The same exclusions left 1,536 scored questions from 1,986 total: 446 category-5
and four other no-evidence questions were excluded. The ten isolated indexes contained 5,882 raw
turns. With `COLLIE_EMBED_THREADS=8` and cached artifacts in offline mode, the result was:

| Configuration | conversations | scored questions | evidence Recall@10 | evidence Hit@10 | ingest | query | total wall |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| BGE-M3 + BGE cross-encoder, cap 24 | 10 | 1,536 | **0.745027** | **0.815755** | 622.823 s | 846.522 s | 1472.717 s |

The first-three cap-24 result (0.763185/0.825065) was therefore optimistic relative to the frozen
ten-conversation result. The full run took 24.55 minutes on Windows 11, Python 3.14.4, an AMD Ryzen
9 9950X3D, and 64 GB RAM. Its wall/query times remain observational: the workstation was not an
isolated benchmark host and p50/p95 per-query latency was not recorded. The quality values are
deterministic for the captured code, data, and local model artifacts, but a second run and paired
confidence intervals are still required before selection.

Reproducibility identifiers for these checks:

- checkout base revision: `b13506b1bd01746fc67b2690ab5d825089c86892`; the working retrieval
  files were modified, so the file hashes below, rather than the base revision alone, identify the
  executed implementation;
- data SHA-256: `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`;
- `bench/locomo_eval.py`: `17b150b63b2b07aefed22bfd62dbedc90029dc685776a90d1ec88ee87a014d29`;
- `harness/memory.py`: `b7a59c3590b93455a15ca671ea087ac5d4e36c8b6e24c46291359dd5c2022080`;
- `harness/embeddings.py`: `6ea202981165e7e4b6bde89d4e6c08f34336e6d9aa443690bb6987b4f277d479`;
- ignored full-run runner SHA-256:
  `34c44fdbc8b52a0fa53f7ba7121d3af77fb6b8c9ab5195deab085648b28bc8a0`; successful stdout
  SHA-256: `4e03f2c7049947b25802b3aa5eca5c7d13a8dfd3597feed0ce4a54558585c1b5`;
- Granite snapshot `d6cffd338414d6a1c1f5decfad5fec62eebc90d5`, E5 snapshot
  `614241f622f53c4eeff9890bdc4f31cfecc418b3`, GTE snapshot
  `2edbf5e672aab465f9ed4c154a8b61791c082c69`, BGE-M3 snapshot
  `5617a9f61b028005a4858fdac845db406aefb181`, and BGE-reranker snapshot
  `6f5ff65298512715a1e669753bc754d2bc8f367b`.

These measurements suggest a **working** mode split, not a production or SOTA claim: Granite
without a reranker is the current default-efficiency candidate, while BGE-M3 plus cap-24 reranking
is the current local quality candidate. The experiment still does not isolate sparse-only versus
dense-only retrieval, write extraction, temporal validity, graph traversal, preference/profile
application, safety leakage, or end-to-end answer support. Those remain Stage B/C decisions below.

## Production experiment

### Stage A: deterministic gates

Run the seed on every change. Leakage, withdrawn/deleted-row recall, and instruction exposure are
release blockers. Preserve per-query artifacts in CI so an aggregate improvement cannot conceal a
new safety failure.

### Stage B: retrieval and extraction ablations

Freeze a manifest containing dataset hash, train/dev/test split, code revision, embedding and
reranker artifact revisions, extraction prompt hash, random seed, `k`, candidate-pool size,
recency/graph parameters, hardware tier, concurrency, and context budget.

Use three data families:

1. full public long-conversation benchmarks such as LOCOMO and LongMemEval, reporting every
   category and reporting unanswerable/adversarial questions separately rather than silently
   excluding them;
2. a consented, de-identified Collie set with Chinese/English paraphrases, project/device
   boundaries, voice transcription noise, preferences, corrections, and real task follow-ups;
3. mutation suites generated from the same histories: change one fact over time, revoke it, move
   it to another project/device, inject an instruction-shaped distractor, and require two or three
   supporting relations.

Cross the retrieval variants with write/extraction variants:

- raw turns;
- per-turn distilled facts;
- session-window extraction;
- rolling extraction where later evidence can revise earlier state;
- current Granite embedding, BM25-only, and separately versioned candidate embedders;
- no reranker versus the current local cross-encoder;
- no graph, one-hop, two-hop, and three-hop graph expansion;
- no temporal model, recency only, and explicit validity/conflict resolution.

The minimum retrieval/profile ablation matrix is larger than the five seed-runner candidates. Keep
these arms separate so a gain is attributable:

| Arm | Retrieval/profile configuration | Question answered |
| --- | --- | --- |
| A | lexical-only (BM25) | What does sparse retrieval buy? |
| B | dense-only, one pinned embedding revision | What does semantic retrieval buy without lexical help? |
| C | BM25 + dense with RRF | Does the current hybrid beat both individual arms? |
| D | C + one pinned cross-encoder and fixed candidate pool | Is joint query/document scoring worth its latency? |
| E | D + explicit validity/conflict resolution | Do updates improve without a generic recency regression? |
| F | D + graph expansion, no temporal logic | What is the isolated value and false-association cost of graph recall? |
| G | D + temporal graph expansion | Does time-bounded graph traversal improve current and historical multi-hop support? |
| H | trusted profile only, no evidence recall | How well does the current flat preference profile predict the next choice? |
| I | contextual profile + accepted supporting evidence | Does evidence/context improve preference precision and correction recovery? |
| J | I + guarded support selection and abstention | Can personalization avoid wrong-context defaults and unsupported answers? |

Do not label Arm D in the deterministic seed: semantic tags cannot emulate a cross-encoder. Run it
only with the pinned real reranker. Likewise, compare F and G against D/E respectively; otherwise a
temporal improvement can be misreported as a graph improvement.

Use one predeclared online-read budget for every arm. A recommended first reference-tier contract
(to be confirmed on the minimum supported device before becoming a product SLO) is:

```yaml
memory_context_tokens_per_query: 512
recall_latency_p95_ms: 250
retrieval_model_calls_per_query: 0
retrieval_network_calls_per_query: 0
retrieval_incremental_cost_usd_per_query: 0
dense_query_embeddings_per_query: 1
rerank_candidates_max: 20
graph_hops_max: 3
graph_nodes_visited_max: 100
```

Freeze these values in the manifest; do not let one arm buy better recall with more prompt tokens,
more candidates, or a network model. Report results outside the latency ceiling as infeasible, not
as a slow winner. Write-time extraction has a separate, also-frozen budget because it is amortized:
`input_tokens`, `output_tokens`, model calls, wall time, and USD per ingested session. Sweep that
budget explicitly instead of hiding an expensive extractor inside the retrieval score.

Tune on development data only. Evaluate the frozen choice once on a blind holdout. Use paired
bootstrap confidence intervals per query and per user/conversation; a mean without uncertainty is
not a selection result.

### Stage C: end-to-end answers and preferences

Hold the responding model, prompt, temperature, context budget, and judge fixed while changing
memory. Measure whether the answer is supported, not just whether a gold snippet appeared. Use a
deterministic evidence-overlap grader where possible, a blinded human sample for preference fit and
false assertions, and a fixed judge model only as a secondary measure.

For preference adaptation, replay chronological choices and evaluate the *next* decision:

- **explicit-preference precision:** applied explicit preferences that match the held-out next
  choice divided by all applied explicit preferences;
- **explicit-preference recall:** eligible matching explicit preferences applied divided by all
  eligible matching explicit preferences;
- **habit-promotion precision:** promoted habits that correctly predict subsequent independent
  choices, with false promotion and time-to-promotion reported separately;
- **contradiction/update resolution:** current-value accuracy, historical-value accuracy, stale
  preference exposure, and turns to recover after a correction;
- **wrong-context regret:** overrides caused by using a preference from another task type, channel,
  audience, project, or device;
- **cross-project/device leakage:** any foreign preference or supporting evidence supplied to the
  decision, a hard zero-tolerance metric;
- **preference abstention:** accuracy when no preference is applicable or evidence is insufficient;
- explanations that correctly identify the applied evidence, ignored contradictory evidence, and
  precedence rule.

### Selection rule

Use lexicographic gates rather than a single weighted score:

1. zero unauthorized-scope and deleted-memory leakage;
2. zero instruction-to-authority escalation and no regression in false-memory rate;
3. highest complete answer-support and Recall@K with a statistically supported gain;
4. nDCG/MRR and end-to-end supported-answer accuracy;
5. p95 recall latency, index size, write amplification, local CPU/RAM, model/network calls, and
   dollar cost within the declared device-tier budget.

If graph expansion does not improve multi-hop support on the blind set, do not ship it for its own
sake. If it helps only relationship questions, gate it by query type. Likewise, the production
embedding winner must come from the frozen real-data run; the deterministic seed cannot select it.

## Decision today

The safest working hypothesis is:

- retain the existing evidence-backed claim ledger and hybrid BM25+dense base;
- implement validity/conflict semantics and physical-erasure plumbing before relying on recency;
- build the entity graph as a retractable derived index, queried only for relationship/multi-hop
  requests;
- make contextual preferences a typed view with explicit precedence and receipts;
- add an instruction-shaped-content boundary, structured data-only prompt records, support-set
  selection, and calibrated abstention;
- select the embedding/reranker/extractor combination only after the frozen Stage B/C experiment.

That architecture can become more familiar without becoming less predictable: user intent and
Leash remain sovereign, preferences are explainable defaults, and every recalled relationship can
be traced back to an accepted, revocable claim.
