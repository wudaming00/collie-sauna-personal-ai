import copy
import json

import pytest

from harness.memory_eval import (
    CANDIDATES,
    DEFAULT_SUITE,
    SuiteError,
    load_suite,
    main,
    run_benchmark,
    validate_suite,
)


def _case(report, candidate, query_id):
    return next(row for row in report["candidates"][candidate]["cases"]
                if row["id"] == query_id)


def test_seed_suite_covers_memory_quality_and_safety_axes():
    suite = load_suite()
    categories = {query["category"] for query in suite["queries"]}
    assert {
        "long_term_fact", "temporal_update", "temporal_history",
        "contextual_preference", "project_isolation", "device_isolation",
        "withdrawal", "deletion", "multi_hop_relation", "prompt_injection",
    } <= categories
    assert all(query.get("relevant") is not None for query in suite["queries"])
    assert any(memory.get("relations") for memory in suite["memories"])
    assert any(memory.get("risk_labels") for memory in suite["memories"])


def test_schema_rejects_unknown_gold_and_unversioned_input():
    suite = load_suite()
    broken = copy.deepcopy(suite)
    broken["queries"][0]["support_ids"] = ["does-not-exist"]
    with pytest.raises(SuiteError, match="unknown memory"):
        validate_suite(broken)

    broken = copy.deepcopy(suite)
    broken["schema_version"] = 999
    with pytest.raises(SuiteError, match="schema_version"):
        validate_suite(broken)


def test_offline_ablation_measures_required_metrics_and_has_zero_external_cost():
    report = run_benchmark(load_suite(), k=3)
    assert report["candidate_order"] == list(CANDIDATES)
    assert report["selection_status"] == "regression_only_not_a_production_decision"
    assert report["environment"]["model_calls"] == 0
    assert report["environment"]["network_calls"] == 0
    for result in report["candidates"].values():
        assert {
            "recall_at_k", "mrr", "ndcg_at_k", "answer_support_at_k",
            "false_memory_query_rate", "leakage_query_rate",
            "prompt_injection_exposure_rate", "abstention_accuracy",
            "latency_ms", "cost", "by_category", "cases",
        } <= result.keys()
        assert result["cost"]["model_calls"] == 0
        assert result["cost"]["network_calls"] == 0
        assert result["latency_ms"]["mean"] >= 0


def test_time_filter_graph_expansion_and_guard_are_separate_measurable_gains():
    report = run_benchmark(load_suite(), k=3)

    current_now = _case(report, "current_hybrid_rrf", "q_current_office")
    temporal_now = _case(report, "time_aware_hybrid", "q_current_office")
    temporal_then = _case(report, "time_aware_hybrid", "q_historical_office")
    assert "office_pine" in current_now["forbidden_found"]
    assert temporal_now["forbidden_found"] == []
    assert temporal_now["hits"][0] == "office_cedar"
    assert temporal_then["hits"][0] == "office_pine"
    assert "office_cedar" not in temporal_then["hits"]

    current_graph = _case(report, "current_hybrid_rrf", "q_graph_multihop")
    graph = _case(report, "graph_hybrid", "q_graph_multihop")
    assert current_graph["answer_support_at_k"] == 0
    assert graph["answer_support_at_k"] == 1
    assert graph["hits"] == [
        "graph_alice_orion", "graph_orion_borealis", "graph_borealis_cockroach"]

    unsafe = _case(report, "graph_hybrid", "q_prompt_injection")
    guarded = _case(report, "guarded_graph_hybrid", "q_prompt_injection")
    assert unsafe["prompt_injection_found"] == ["imported_prompt_injection"]
    assert guarded["prompt_injection_found"] == []
    assert guarded["hits"][0] == "atlas_release_checklist"


def test_scope_revocation_and_deletion_are_hard_gates_for_every_candidate():
    report = run_benchmark(load_suite(), k=3)
    forbidden_everywhere = {
        "boreal_package_manager", "desktop_audio", "meeting_before_ten_old",
        "deleted_trip_code", "foreign_scope_prompt_injection",
        "pref_status_emoji_guess",
    }
    for result in report["candidates"].values():
        all_hits = {memory_id for case in result["cases"] for memory_id in case["hits"]}
        assert not (forbidden_everywhere & all_hits)
        assert result["leakage_query_rate"] == 0

    deleted = _case(report, "guarded_graph_hybrid", "q_deleted_fact")
    assert deleted["hits"] == []
    assert report["candidates"]["guarded_graph_hybrid"]["abstention_accuracy"] == 1


def test_rank_and_quality_outputs_are_deterministic_apart_from_observed_latency():
    suite = load_suite()
    first = run_benchmark(suite, k=3)
    second = run_benchmark(suite, k=3)
    for name in first["candidate_order"]:
        a = copy.deepcopy(first["candidates"][name])
        b = copy.deepcopy(second["candidates"][name])
        a.pop("latency_ms")
        b.pop("latency_ms")
        assert a == b
    assert first["seed_leader"] == second["seed_leader"] == "guarded_graph_hybrid"


def test_cli_writes_machine_readable_report(tmp_path, capsys):
    target = tmp_path / "report.json"
    assert main(["--suite", str(DEFAULT_SUITE),
                 "--k", "3", "--output", str(target)]) == 0
    output = capsys.readouterr().out
    assert "seed leader" in output and "regression_only_not_a_production_decision" in output
    report = json.loads(target.read_text(encoding="utf-8"))
    assert report["suite_id"] == "collie-memory-seed-v1"
    assert report["candidates"]["guarded_graph_hybrid"]["cost"]["network_calls"] == 0
