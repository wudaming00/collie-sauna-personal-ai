"""Offline, deterministic evaluation harness for Collie's memory architecture.

This module is deliberately stdlib-only and model-free.  It is a regression and
ablation harness, not a substitute for running production embedders/extractors on
LOCOMO, LongMemEval, and representative (consented, de-identified) Collie traces.

Run from a source checkout::

    python -m harness.memory_eval \
        --suite tests/fixtures/memory_eval_seed_v1.json --k 3

The fixture exposes semantic tags and relation triples so the runner can exercise
dense-fusion and graph-expansion *architecture* without downloading a model.  Those
tags must never be presented as evidence that one production embedding model beats
another; the second-stage protocol in ``docs/memory-evaluation.md`` covers that.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
import json
import math
import os
from pathlib import Path
import platform
import re
import statistics
import time
from typing import Iterable, Mapping


SCHEMA_VERSION = 1
RECALLABLE_STATUSES = frozenset(("active", "attested", "verified"))
DEFAULT_SUITE = (Path(__file__).resolve().parent.parent / "tests" / "fixtures" /
                 "memory_eval_seed_v1.json")
_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]", re.IGNORECASE)
_PROMPT_INJECTION_RE = re.compile(
    r"(?i)(?:ignore|disregard|override).{0,32}(?:previous|prior|system|instructions?|"
    r"safeguards?)|(?:reveal|expose|print).{0,24}(?:secrets?|tokens?|credentials?|"
    r"system prompt)|disable.{0,20}safeguards?"
)


class SuiteError(ValueError):
    """The benchmark fixture is malformed or internally inconsistent."""


@dataclass(frozen=True)
class RetrievalResult:
    ids: tuple[str, ...]
    scores: tuple[float, ...]
    cost: Mapping[str, int]


def _tokens(value: object) -> list[str]:
    return _TOKEN_RE.findall(str(value or "").lower())


def _timestamp(value: object, *, field: str = "timestamp") -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise SuiteError("%s must be an ISO-8601 string or unix timestamp" % field)
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            raise SuiteError("%s must be finite" % field)
        return number
    if not isinstance(value, str):
        raise SuiteError("%s must be an ISO-8601 string or unix timestamp" % field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SuiteError("invalid %s: %s" % (field, value)) from exc
    if parsed.tzinfo is None:
        raise SuiteError("%s must include a timezone: %s" % (field, value))
    return parsed.timestamp()


def load_suite(path: str | os.PathLike | None = None) -> dict:
    """Load and validate one versioned JSON benchmark suite."""
    target = Path(path) if path else DEFAULT_SUITE
    try:
        with target.open(encoding="utf-8") as handle:
            suite = json.load(handle)
    except OSError as exc:
        raise SuiteError("cannot read memory benchmark suite %s: %s" % (target, exc)) from exc
    except json.JSONDecodeError as exc:
        raise SuiteError("invalid JSON in memory benchmark suite %s: %s" % (target, exc)) from exc
    validate_suite(suite)
    return suite


def validate_suite(suite: object) -> None:
    if not isinstance(suite, dict):
        raise SuiteError("suite must be a JSON object")
    if suite.get("schema_version") != SCHEMA_VERSION:
        raise SuiteError("unsupported memory benchmark schema_version")
    memories = suite.get("memories")
    queries = suite.get("queries")
    if not isinstance(memories, list) or not memories:
        raise SuiteError("suite.memories must be a non-empty array")
    if not isinstance(queries, list) or not queries:
        raise SuiteError("suite.queries must be a non-empty array")

    ids: set[str] = set()
    for index, memory in enumerate(memories):
        if not isinstance(memory, dict):
            raise SuiteError("memories[%d] must be an object" % index)
        memory_id = memory.get("id")
        if not isinstance(memory_id, str) or not memory_id.strip() or memory_id in ids:
            raise SuiteError("memory ids must be unique non-empty strings")
        ids.add(memory_id)
        for required in ("text", "project", "scope", "status"):
            if not isinstance(memory.get(required), str) or not memory[required]:
                raise SuiteError("memory %s requires string field %s" % (memory_id, required))
        for field in ("created_at", "valid_from", "valid_to", "expires_at"):
            _timestamp(memory.get(field), field="memory %s.%s" % (memory_id, field))
        for field in ("semantic_tags", "entities", "risk_labels"):
            value = memory.get(field, [])
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise SuiteError("memory %s.%s must be an array of strings" % (memory_id, field))
        relations = memory.get("relations", [])
        if not isinstance(relations, list):
            raise SuiteError("memory %s.relations must be an array" % memory_id)
        for relation in relations:
            if (not isinstance(relation, dict) or
                    not all(isinstance(relation.get(key), str) and relation[key]
                            for key in ("subject", "predicate", "object"))):
                raise SuiteError("memory %s has an invalid relation" % memory_id)

    query_ids: set[str] = set()
    for index, query in enumerate(queries):
        if not isinstance(query, dict):
            raise SuiteError("queries[%d] must be an object" % index)
        query_id = query.get("id")
        if not isinstance(query_id, str) or not query_id.strip() or query_id in query_ids:
            raise SuiteError("query ids must be unique non-empty strings")
        query_ids.add(query_id)
        if not isinstance(query.get("query"), str) or not query["query"].strip():
            raise SuiteError("query %s requires query text" % query_id)
        if not isinstance(query.get("project"), str) or not query["project"]:
            raise SuiteError("query %s requires project" % query_id)
        _timestamp(query.get("as_of"), field="query %s.as_of" % query_id)
        relevant = query.get("relevant")
        if not isinstance(relevant, dict):
            raise SuiteError("query %s.relevant must be an id-to-gain object" % query_id)
        for memory_id, gain in relevant.items():
            if memory_id not in ids or isinstance(gain, bool) or not isinstance(gain, (int, float)) \
                    or not math.isfinite(float(gain)) or float(gain) <= 0:
                raise SuiteError("query %s has an invalid relevance judgment" % query_id)
        for field in ("support_ids", "forbidden_ids", "semantic_tags", "query_entities"):
            values = query.get(field, [])
            if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
                raise SuiteError("query %s.%s must be an array of strings" % (query_id, field))
            if field in ("support_ids", "forbidden_ids") and not set(values) <= ids:
                raise SuiteError("query %s.%s names an unknown memory" % (query_id, field))
        if not set(query.get("support_ids", ())) <= set(relevant):
            raise SuiteError("query %s support_ids must also be relevant" % query_id)


def _as_of(query: Mapping) -> float:
    return _timestamp(query.get("as_of"), field="query.as_of") or float("inf")


def _allowed_scopes(query: Mapping) -> frozenset[str]:
    explicit = query.get("allowed_scopes")
    if explicit is None:
        return frozenset((str(query.get("project") or "global"), "global"))
    return frozenset(str(value) for value in explicit if str(value))


def _authorized(memory: Mapping, query: Mapping) -> bool:
    """Mirror Collie's current status/project/scope/device/expiry admission boundary."""
    project = str(query.get("project") or "global")
    if memory.get("status") not in RECALLABLE_STATUSES:
        return False
    if memory.get("project") not in (project, "global"):
        return False
    if memory.get("scope") not in _allowed_scopes(query):
        return False
    memory_device = str(memory.get("device_id") or "")
    query_device = str(query.get("device_id") or "")
    if memory_device and memory_device != query_device:
        return False
    expires_at = _timestamp(memory.get("expires_at"), field="memory.expires_at")
    if expires_at is not None and expires_at <= _as_of(query):
        return False
    return True


def _admitted_memories(memories: Iterable[Mapping], query: Mapping, *,
                       temporal: bool = False, guarded: bool = False) -> list[Mapping]:
    rows = []
    as_of = _as_of(query)
    for memory in memories:
        if not _authorized(memory, query):
            continue
        if guarded and _PROMPT_INJECTION_RE.search(str(memory.get("text") or "")):
            continue
        if temporal:
            created = _timestamp(memory.get("created_at"), field="memory.created_at")
            valid_from = _timestamp(memory.get("valid_from"), field="memory.valid_from")
            valid_to = _timestamp(memory.get("valid_to"), field="memory.valid_to")
            if created is not None and created > as_of:
                continue
            if valid_from is not None and valid_from > as_of:
                continue
            if valid_to is not None and valid_to <= as_of:
                continue
        rows.append(memory)

    if not temporal:
        return rows
    # A conflict group is a versioned property.  At one evaluation instant only
    # its newest admissible member is current; provenance rows remain in storage.
    newest: dict[str, Mapping] = {}
    passthrough = []
    for memory in rows:
        key = str(memory.get("conflict_key") or "")
        if not key:
            passthrough.append(memory)
            continue
        prior = newest.get(key)
        stamp = (_timestamp(memory.get("valid_from"), field="memory.valid_from") or
                 _timestamp(memory.get("created_at"), field="memory.created_at") or 0)
        prior_stamp = ((_timestamp(prior.get("valid_from"), field="memory.valid_from") or
                        _timestamp(prior.get("created_at"), field="memory.created_at") or 0)
                       if prior else float("-inf"))
        if prior is None or stamp > prior_stamp or (
                stamp == prior_stamp and str(memory["id"]) > str(prior["id"])):
            newest[key] = memory
    return passthrough + list(newest.values())


def _bm25_scores(memories: list[Mapping], query: Mapping) -> dict[str, float]:
    if not memories:
        return {}
    documents = [
        _tokens("%s %s" % (memory.get("text", ""), memory.get("keys", "")))
        for memory in memories
    ]
    query_tokens = list(dict.fromkeys(_tokens(query.get("query", ""))))
    if not query_tokens:
        return {}
    avg_length = sum(len(tokens) for tokens in documents) / max(len(documents), 1)
    document_frequency = Counter()
    for tokens in documents:
        document_frequency.update(set(tokens))
    scores = {}
    n_docs = len(documents)
    k1, b = 1.2, 0.75
    for memory, tokens in zip(memories, documents):
        counts = Counter(tokens)
        length_norm = 1 - b + b * len(tokens) / max(avg_length, 1.0)
        score = 0.0
        for token in query_tokens:
            frequency = counts.get(token, 0)
            if not frequency:
                continue
            df = document_frequency[token]
            inverse = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
            score += inverse * (frequency * (k1 + 1)) / (frequency + k1 * length_norm)
        if score > 0:
            scores[str(memory["id"])] = score
    return scores


def _semantic_scores(memories: list[Mapping], query: Mapping) -> dict[str, float]:
    """Model-free proxy for a dense arm using fixture-provided semantic dimensions."""
    query_tags = {str(tag).strip().lower() for tag in query.get("semantic_tags", ()) if str(tag)}
    if not query_tags:
        return {}
    scores = {}
    for memory in memories:
        tags = {str(tag).strip().lower() for tag in memory.get("semantic_tags", ()) if str(tag)}
        overlap = len(query_tags & tags)
        if overlap:
            scores[str(memory["id"])] = overlap / math.sqrt(len(query_tags) * len(tags))
    return scores


def _rank(scores: Mapping[str, float]) -> list[tuple[str, float]]:
    return sorted(((str(memory_id), float(score)) for memory_id, score in scores.items()
                   if math.isfinite(float(score)) and float(score) > 0),
                  key=lambda item: (-item[1], item[0]))


def _rrf(rank_lists: Iterable[Iterable[str]], constant: int = 60) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranked in rank_lists:
        for position, memory_id in enumerate(ranked):
            scores[memory_id] = scores.get(memory_id, 0.0) + 1.0 / (
                constant + position + 1)
    return scores


def _hybrid_scores(memories: list[Mapping], query: Mapping) -> tuple[dict[str, float], dict]:
    lexical = _bm25_scores(memories, query)
    semantic = _semantic_scores(memories, query)
    fused = _rrf(([memory_id for memory_id, _ in _rank(lexical)],
                  [memory_id for memory_id, _ in _rank(semantic)]))
    return fused, {
        "lexical_documents": len(memories),
        "semantic_documents": len(memories),
        "graph_edges": 0,
        "model_calls": 0,
        "network_calls": 0,
    }


def _result(scores: Mapping[str, float], k: int, cost: Mapping[str, int]) -> RetrievalResult:
    ranked = _rank(scores)[:k]
    return RetrievalResult(tuple(memory_id for memory_id, _ in ranked),
                           tuple(score for _, score in ranked), dict(cost))


class LexicalCandidate:
    name = "lexical_bm25"

    def retrieve(self, memories: list[Mapping], query: Mapping, k: int) -> RetrievalResult:
        admitted = _admitted_memories(memories, query)
        scores = _bm25_scores(admitted, query)
        return _result(scores, k, {
            "lexical_documents": len(admitted), "semantic_documents": 0,
            "graph_edges": 0, "model_calls": 0, "network_calls": 0,
        })


class CurrentHybridCandidate:
    name = "current_hybrid_rrf"

    def retrieve(self, memories: list[Mapping], query: Mapping, k: int) -> RetrievalResult:
        admitted = _admitted_memories(memories, query)
        scores, cost = _hybrid_scores(admitted, query)
        return _result(scores, k, cost)


class TimeAwareCandidate:
    name = "time_aware_hybrid"

    def retrieve(self, memories: list[Mapping], query: Mapping, k: int) -> RetrievalResult:
        admitted = _admitted_memories(memories, query, temporal=True)
        scores, cost = _hybrid_scores(admitted, query)
        as_of = _as_of(query)
        by_id = {str(memory["id"]): memory for memory in admitted}
        rescored = {}
        for memory_id, score in scores.items():
            created = _timestamp(by_id[memory_id].get("created_at"), field="memory.created_at")
            age_days = max(0.0, as_of - (created or as_of)) / 86400.0
            recency = 1.0 + 0.5 * (0.5 ** (age_days / 90.0))
            rescored[memory_id] = score * recency
        return _result(rescored, k, cost)


class GraphCandidate:
    def __init__(self, *, guarded: bool = False):
        self.guarded = bool(guarded)
        self.name = "guarded_graph_hybrid" if guarded else "graph_hybrid"

    @staticmethod
    def _graph_rank(memories: list[Mapping], query: Mapping) -> tuple[list[str], int]:
        seeds = [str(entity).strip().lower() for entity in query.get("query_entities", ())
                 if str(entity).strip()]
        if not seeds:
            return [], 0
        adjacency: dict[str, set[str]] = defaultdict(set)
        edge_count = 0
        for memory in memories:
            for relation in memory.get("relations", ()):
                subject = str(relation.get("subject") or "").strip().lower()
                obj = str(relation.get("object") or "").strip().lower()
                if not subject or not obj:
                    continue
                adjacency[subject].add(obj)
                adjacency[obj].add(subject)
                edge_count += 1
        distance = {seed: 0 for seed in seeds}
        queue = deque(seeds)
        while queue:
            node = queue.popleft()
            if distance[node] >= 3:
                continue
            for neighbor in sorted(adjacency.get(node, ())):
                if neighbor in distance:
                    continue
                distance[neighbor] = distance[node] + 1
                queue.append(neighbor)
        scores = {}
        for memory in memories:
            entities = {str(entity).strip().lower() for entity in memory.get("entities", ())
                        if str(entity).strip()}
            reachable = [distance[entity] for entity in entities if entity in distance]
            if reachable:
                scores[str(memory["id"])] = 1.0 / (1.0 + min(reachable))
        return [memory_id for memory_id, _ in _rank(scores)], edge_count

    def retrieve(self, memories: list[Mapping], query: Mapping, k: int) -> RetrievalResult:
        admitted = _admitted_memories(
            memories, query, temporal=True, guarded=self.guarded)
        base_scores, cost = _hybrid_scores(admitted, query)
        base_rank = [memory_id for memory_id, _ in _rank(base_scores)]
        graph_rank, edges = self._graph_rank(admitted, query)
        # The guarded candidate also models a conservative retrieval-confidence
        # gate.  A query carrying semantic intent but matching no admitted
        # semantic dimension must not return arbitrary stop-word BM25 hits.  It
        # abstains unless an explicit entity path supplies independent support.
        # Production thresholds need calibration; this deterministic rule is a
        # regression lock for deletion/forget semantics, not that calibration.
        if (self.guarded and query.get("semantic_tags") and
                not _semantic_scores(admitted, query) and not graph_rank):
            base_rank = []
        # Rank-normalized fusion deliberately gives the graph arm enough weight
        # to surface a complete three-edge support chain over a semantically
        # tempting but disconnected distractor.  The public-data stage must tune
        # this weight; the seed only locks the intended expansion behavior.
        combined = {memory_id: 1.0 / (position + 1)
                    for position, memory_id in enumerate(base_rank)}
        for position, memory_id in enumerate(graph_rank):
            combined[memory_id] = combined.get(memory_id, 0.0) + 3.0 / (position + 1)
        cost = dict(cost)
        cost["graph_edges"] = edges
        return _result(combined, k, cost)


CANDIDATES = {
    "lexical_bm25": LexicalCandidate(),
    "current_hybrid_rrf": CurrentHybridCandidate(),
    "time_aware_hybrid": TimeAwareCandidate(),
    "graph_hybrid": GraphCandidate(),
    "guarded_graph_hybrid": GraphCandidate(guarded=True),
}


def _ndcg(ids: Iterable[str], relevant: Mapping[str, float], k: int) -> float:
    gains = [float(relevant.get(memory_id, 0.0)) for memory_id in list(ids)[:k]]
    actual = sum((2.0 ** gain - 1.0) / math.log2(position + 2)
                 for position, gain in enumerate(gains) if gain > 0)
    ideal_gains = sorted((float(value) for value in relevant.values()), reverse=True)[:k]
    ideal = sum((2.0 ** gain - 1.0) / math.log2(position + 2)
                for position, gain in enumerate(ideal_gains) if gain > 0)
    return actual / ideal if ideal else 1.0


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def _rounded(value: float) -> float:
    return round(float(value), 6)


def _score_candidate(candidate, suite: Mapping, k: int) -> dict:
    memories = list(suite["memories"])
    by_id = {str(memory["id"]): memory for memory in memories}
    cases = []
    positive = 0
    recall = reciprocal = ndcg = support = 0.0
    false_queries = false_items = leakage_queries = injection_queries = 0
    negative = abstained = 0
    latencies = []
    cost_totals = Counter()
    by_category: dict[str, list[dict]] = defaultdict(list)

    for query in suite["queries"]:
        started = time.perf_counter_ns()
        result = candidate.retrieve(memories, query, k)
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000.0
        latencies.append(elapsed_ms)
        cost_totals.update({key: int(value) for key, value in result.cost.items()})
        ids = list(result.ids)
        relevant = {str(key): float(value) for key, value in query.get("relevant", {}).items()}
        relevant_ids = set(relevant)
        forbidden = set(query.get("forbidden_ids", ()))
        found = relevant_ids & set(ids)
        false_found = forbidden & set(ids)
        leaked = [memory_id for memory_id in ids
                  if memory_id not in by_id or not _authorized(by_id[memory_id], query)]
        injection_found = [memory_id for memory_id in ids
                           if "prompt_injection" in by_id.get(memory_id, {}).get("risk_labels", ())]

        case_recall = None
        case_mrr = None
        case_ndcg = None
        case_support = None
        if relevant_ids:
            positive += 1
            case_recall = len(found) / len(relevant_ids)
            ranks = [ids.index(memory_id) + 1 for memory_id in relevant_ids if memory_id in ids]
            case_mrr = 1.0 / min(ranks) if ranks else 0.0
            case_ndcg = _ndcg(ids, relevant, k)
            required = set(query.get("support_ids", ()))
            case_support = 1.0 if required <= set(ids) else 0.0
            recall += case_recall
            reciprocal += case_mrr
            ndcg += case_ndcg
            support += case_support
        if query.get("expect_abstain"):
            negative += 1
            abstained += int(not ids)
        false_queries += int(bool(false_found))
        false_items += len(false_found)
        leakage_queries += int(bool(leaked))
        injection_queries += int(bool(injection_found))
        case = {
            "id": query["id"], "category": query.get("category", "uncategorized"),
            "hits": ids, "relevant_found": sorted(found),
            "forbidden_found": sorted(false_found), "leakage_found": sorted(leaked),
            "prompt_injection_found": sorted(injection_found),
            "recall_at_k": None if case_recall is None else _rounded(case_recall),
            "mrr": None if case_mrr is None else _rounded(case_mrr),
            "ndcg_at_k": None if case_ndcg is None else _rounded(case_ndcg),
            "answer_support_at_k": None if case_support is None else _rounded(case_support),
        }
        cases.append(case)
        by_category[case["category"]].append(case)

    query_count = len(suite["queries"])
    summary = {
        "queries": query_count,
        "positive_queries": positive,
        "recall_at_k": _rounded(recall / max(positive, 1)),
        "mrr": _rounded(reciprocal / max(positive, 1)),
        "ndcg_at_k": _rounded(ndcg / max(positive, 1)),
        "answer_support_at_k": _rounded(support / max(positive, 1)),
        "false_memory_query_rate": _rounded(false_queries / max(query_count, 1)),
        "false_memory_items": false_items,
        "leakage_query_rate": _rounded(leakage_queries / max(query_count, 1)),
        "prompt_injection_exposure_rate": _rounded(injection_queries / max(query_count, 1)),
        "abstention_accuracy": _rounded(abstained / max(negative, 1)) if negative else None,
        "latency_ms": {
            "mean": _rounded(statistics.fmean(latencies) if latencies else 0.0),
            "p50": _rounded(statistics.median(latencies) if latencies else 0.0),
            "p95": _rounded(_percentile(latencies, 0.95)),
        },
        "cost": {
            "mean_units_per_query": _rounded(sum(
                value for key, value in cost_totals.items()
                if key not in ("model_calls", "network_calls")) / max(query_count, 1)),
            "total_units": int(sum(value for key, value in cost_totals.items()
                                   if key not in ("model_calls", "network_calls"))),
            "model_calls": int(cost_totals.get("model_calls", 0)),
            "network_calls": int(cost_totals.get("network_calls", 0)),
            "operations": {key: int(value) for key, value in sorted(cost_totals.items())
                           if key not in ("model_calls", "network_calls")},
        },
        "by_category": {},
        "cases": cases,
    }
    for category, rows in sorted(by_category.items()):
        positives = [row for row in rows if row["recall_at_k"] is not None]
        summary["by_category"][category] = {
            "queries": len(rows),
            "recall_at_k": _rounded(statistics.fmean(
                row["recall_at_k"] for row in positives)) if positives else None,
            "answer_support_at_k": _rounded(statistics.fmean(
                row["answer_support_at_k"] for row in positives)) if positives else None,
            "false_memory_queries": sum(bool(row["forbidden_found"]) for row in rows),
            "leakage_queries": sum(bool(row["leakage_found"]) for row in rows),
        }
    return summary


def run_benchmark(suite: Mapping, *, k: int = 3,
                  candidates: Iterable[str] | None = None) -> dict:
    validate_suite(suite)
    if isinstance(k, bool) or not isinstance(k, int) or k <= 0 or k > 100:
        raise ValueError("k must be an integer from 1 to 100")
    names = list(candidates or CANDIDATES)
    if not names:
        raise ValueError("at least one candidate is required")
    unknown = [name for name in names if name not in CANDIDATES]
    if unknown:
        raise ValueError("unknown memory candidate: %s" % ", ".join(unknown))
    results = {name: _score_candidate(CANDIDATES[name], suite, k) for name in names}

    # Seed leader is useful for catching architecture regressions but is
    # intentionally not called a production winner.  Safety dominates quality,
    # then full support/recall/nDCG, then measured cost and latency break ties.
    def ordering(name: str):
        row = results[name]
        return (
            row["leakage_query_rate"],
            row["prompt_injection_exposure_rate"],
            row["false_memory_query_rate"],
            -row["answer_support_at_k"],
            -row["recall_at_k"],
            -row["ndcg_at_k"],
            row["cost"]["mean_units_per_query"],
            row["latency_ms"]["mean"],
            name,
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "suite_id": suite.get("suite_id", "unnamed"),
        "k": k,
        "candidate_order": names,
        "seed_leader": min(names, key=ordering),
        "selection_status": "regression_only_not_a_production_decision",
        "selection_note": (
            "Choose a production stack only after the same frozen protocol runs actual "
            "embedders, extractors, rerankers, and graph variants on public and consented "
            "representative data with paired confidence intervals."
        ),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.system().lower(),
            "model_calls": 0,
            "network_calls": 0,
            "latency_is_observational": True,
        },
        "candidates": results,
    }


def _print_report(report: Mapping) -> None:
    k = report["k"]
    columns = (
        ("candidate", 24), ("R@%d" % k, 7), ("MRR", 7), ("nDCG", 7),
        ("support", 8), ("false", 7), ("leak", 7), ("inject", 7),
        ("abstain", 8), ("mean ms", 9), ("units/q", 8),
    )
    print(" ".join(label.ljust(width) for label, width in columns))
    print(" ".join(("-" * width) for _, width in columns))
    for name in report["candidate_order"]:
        row = report["candidates"][name]
        values = (
            name,
            "%.3f" % row["recall_at_k"],
            "%.3f" % row["mrr"],
            "%.3f" % row["ndcg_at_k"],
            "%.3f" % row["answer_support_at_k"],
            "%.3f" % row["false_memory_query_rate"],
            "%.3f" % row["leakage_query_rate"],
            "%.3f" % row["prompt_injection_exposure_rate"],
            ("n/a" if row["abstention_accuracy"] is None else
             "%.3f" % row["abstention_accuracy"]),
            "%.3f" % row["latency_ms"]["mean"],
            "%.1f" % row["cost"]["mean_units_per_query"],
        )
        print(" ".join(str(value).ljust(width) for value, (_, width) in zip(values, columns)))
    print("\nseed leader: %s" % report["seed_leader"])
    print("status: %s" % report["selection_status"])
    print(report["selection_note"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", default=str(DEFAULT_SUITE),
                        help="versioned JSON suite (default: repository seed fixture)")
    parser.add_argument("--k", type=int, default=3)
    parser.add_argument("--candidates", default=",".join(CANDIDATES),
                        help="comma-separated candidate names")
    parser.add_argument("--output", help="optional JSON report path")
    parser.add_argument("--list-candidates", action="store_true")
    args = parser.parse_args(argv)
    if args.list_candidates:
        print("\n".join(CANDIDATES))
        return 0
    try:
        suite = load_suite(args.suite)
        names = [name.strip() for name in args.candidates.split(",") if name.strip()]
        report = run_benchmark(suite, k=args.k, candidates=names)
    except (SuiteError, ValueError) as exc:
        parser.error(str(exc))
    _print_report(report)
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        print("report: %s" % target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CANDIDATES", "DEFAULT_SUITE", "SCHEMA_VERSION", "SuiteError",
    "load_suite", "run_benchmark", "validate_suite",
]
