import json
from types import SimpleNamespace


def _entry(provider, model, *, tags=(), kind="subscription", auth="ok"):
    return {
        "provider": provider, "model": model, "tags": list(tags),
        "kind": kind, "auth": auth,
    }


CATALOG = [
    _entry("codex-oauth", "gpt-5.6-sol", tags=("coding", "frontier")),
    _entry("claude-agent-sdk", "claude-opus-5", tags=("coding", "frontier")),
    _entry("deepseek", "deepseek-chat", tags=("coding", "cheap"), kind="metered"),
]


def test_auto_selects_best_available_and_persists_explanation_without_prompt(tmp_path):
    from harness.brain_router import BrainRouteStore
    from harness.router import resolve_run_decision

    store = BrainRouteStore(str(tmp_path / "brain.db"))
    secret_prompt = "fix the race with customer-secret-123"
    decision = resolve_run_decision(
        secret_prompt, "auto", route_kind="code", purpose="self",
        catalog_entries=CATALOG, brain_store=store, now=1000)

    assert (decision.provider, decision.model, decision.transport,
            decision.executor) == (
        "codex-oauth", "gpt-5.6-sol", "codex-oauth", "collie")
    assert decision.to_dict()["brain_transport"] == "codex-oauth"
    assert decision.to_dict()["worker_executor"] == "collie"
    assert decision.automatic is True
    assert [item["provider"] for item in decision.fallbacks] == [
        "claude-agent-sdk", "deepseek"]
    assert decision.decision_id.startswith("brain_")
    saved = store.decision(decision.decision_id)
    assert saved["explanation"]["provider"] == "codex-oauth"
    assert saved["explanation"]["reasons"] == list(decision.reasons)
    assert secret_prompt not in (tmp_path / "brain.db").read_bytes().decode(
        "utf-8", "ignore")


def test_concrete_provider_and_model_are_hard_pins_and_have_no_cross_fallback():
    from harness.router import resolve_run_decision

    def must_not_probe(_provider):
        raise AssertionError("a pinned route must not inspect other credentials")

    decision = resolve_run_decision(
        "fix it", "deepseek", model="deepseek-chat", route_kind="code",
        availability=must_not_probe, catalog_entries=CATALOG)

    assert decision.provider == "deepseek"
    assert decision.model == "deepseek-chat"
    assert decision.automatic is False and decision.fallbacks == ()
    assert decision.sources["provider"] == "configured"


def test_only_attested_preference_or_repeated_verified_habit_can_influence_auto(tmp_path):
    from harness.brain_router import BrainRouteStore
    from harness.router import resolve_run_decision

    store = BrainRouteStore(str(tmp_path / "brain.db"))
    proposed = {
        "routing.provider": {
            "kind": "habit", "status": "proposed", "confidence": .99,
            "observations": 99, "value": "claude-agent-sdk",
        }
    }
    baseline = resolve_run_decision(
        "answer carefully", "auto", route_kind="chat", trusted_profile=proposed,
        catalog_entries=CATALOG, brain_store=store, now=1000)
    assert baseline.provider == "codex-oauth"

    verified = {
        "routing.provider": {
            "kind": "habit", "status": "verified", "confidence": .8,
            "observations": 3, "value": "claude-agent-sdk",
        }
    }
    habitual = resolve_run_decision(
        "answer carefully", "auto", route_kind="chat", trusted_profile=verified,
        catalog_entries=CATALOG, brain_store=store, now=1000)
    assert habitual.provider == "claude-agent-sdk"
    assert "trusted habit provider" in " ".join(habitual.reasons)

    attested = {
        "routing.provider": {
            "kind": "preference", "status": "attested", "confidence": 1,
            "observations": 1, "value": "deepseek",
        }
    }
    preferred = resolve_run_decision(
        "answer carefully", "auto", route_kind="chat", trusted_profile=attested,
        catalog_entries=CATALOG, brain_store=store, now=1000)
    assert preferred.provider == "deepseek"


def test_exhausted_route_is_provider_partitioned_and_recovers_after_cooldown(tmp_path):
    from harness.brain_router import BrainRouteStore
    from harness.router import resolve_run_decision

    store = BrainRouteStore(str(tmp_path / "brain.db"))
    decision_id = store.record_decision(
        {"provider": "codex-oauth", "model": "gpt-5.6-sol", "executor": "codex"},
        task="shape", now=1000)
    store.record_outcome(
        decision_id, provider="codex-oauth", model="gpt-5.6-sol",
        success=False, error_class="exhausted", final=True, now=1000)

    during = resolve_run_decision(
        "hard security task", "auto", route_kind="code",
        catalog_entries=CATALOG, brain_store=store, now=1100)
    after = resolve_run_decision(
        "hard security task", "auto", route_kind="code",
        catalog_entries=CATALOG, brain_store=store, now=1000 + 4 * 60 * 60 + 1)
    assert during.provider == "claude-agent-sdk"
    assert after.provider == "codex-oauth"


class _ScriptProvider:
    reports_cache = False
    max_tokens = 4096
    subscription_only = False

    def __init__(self, name, model, *responses):
        self.name, self.model = name, model
        self.responses = list(responses)
        self.calls = 0

    def complete(self, *_args, **_kwargs):
        self.calls += 1
        return self.responses.pop(0)


def test_loop_transparently_crosses_only_an_automatic_route(monkeypatch, tmp_path):
    from harness.brain_router import BrainRouteStore
    from harness.cli import make_harness
    from harness.providers import Completion

    busy = Completion(
        text="overloaded", stop_reason="error", error_status=529,
        error_detail="overloaded_error retry-after 20 seconds")
    ok = Completion(text="fallback answered", stop_reason="end_turn")
    primary = _ScriptProvider("codex-oauth", "gpt-5.6-sol", busy)
    fallback = _ScriptProvider("deepseek", "deepseek-chat", ok)
    monkeypatch.setattr(
        "harness.providers.make_provider",
        lambda provider, model, **_kw: fallback if provider == "deepseek" else primary)

    store = BrainRouteStore(str(tmp_path / "brain.db"))
    did = store.record_decision(
        {"provider": primary.name, "model": primary.model, "executor": "codex"},
        task="shape")
    h = make_harness(str(tmp_path), provider="mock", project="brain", embed="hash")
    h.provider = primary
    h.max_retries = 0
    h.max_turns = 2
    h.brain_automatic = True
    h.brain_fallbacks = [{
        "provider": "deepseek", "model": "deepseek-chat",
        "effort": "medium", "speed": "standard",
    }]
    h.brain_decision_id = did
    h.brain_store = store
    events = []
    h.emit = lambda kind, data: events.append((kind, data))

    result = h.run("brain", "answer")

    assert result.error == ""
    assert result.actual_provider == "deepseek"
    assert result.actual_model == "deepseek-chat"
    assert "automatically switched" in result.answer
    assert any(kind == "provider_fallback" for kind, _ in events)
    assert store.decision(did)["outcome"] == "success"


def test_loop_never_uses_attached_fallback_when_route_is_pinned(monkeypatch, tmp_path):
    from harness.cli import make_harness
    from harness.providers import Completion

    primary = _ScriptProvider(
        "codex-oauth", "gpt-5.6-sol",
        Completion(text="busy", stop_reason="error", error_status=529,
                   error_detail="overloaded_error"))
    monkeypatch.setattr(
        "harness.providers.make_provider",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("pinned run attempted a cross-provider fallback")))
    h = make_harness(str(tmp_path), provider="mock", project="pinned", embed="hash")
    h.provider = primary
    h.max_retries = 0
    h.max_turns = 1
    h.brain_automatic = False
    h.brain_fallbacks = [{"provider": "deepseek", "model": "deepseek-chat"}]

    result = h.run("pinned", "answer")

    assert result.error.startswith("retryable:")
    assert result.actual_provider == "codex-oauth"


def test_agent_backend_probe_is_admission_only_and_deterministic():
    from harness.agent_runners import probe_agent_backends

    statuses = probe_agent_backends(
        which=lambda executable: "/bin/" + executable if executable == "codex" else None,
        auth_probe=lambda provider: "ok" if provider == "codex-oauth" else "not-logged-in")
    by_name = {item.name: item for item in statuses}
    assert by_name["native"].available is True
    assert by_name["codex"].available is True
    assert by_name["claude-code"].available is False
    assert "Mission authority is still required" in by_name["codex"].reason


def _decision(provider="codex-oauth", model="gpt-5.6-sol", *, automatic=True,
              executor="collie"):
    from harness.router import RunDecision
    return RunDecision(
        provider=provider, model=model, effort="high", speed="standard",
        billing_multiplier=1.0, intent="review", quality="thorough",
        verification="auto", workspace="current", strategy="single",
        route_kind="chat", complexity="hard", transport=provider, executor=executor,
        automatic=automatic,
        fallbacks=({"provider": "deepseek", "model": "deepseek-chat"},)
        if automatic else (),
        sources={"provider": "brain-router" if automatic else "configured"},
        reasons=("test route",), decision_id="brain_test")


def test_delegate_inherits_a_concrete_parent_and_is_forced_read_only(monkeypatch, tmp_path):
    from harness.delegate import DelegateTool
    from harness.tools import ToolCtx

    seen = {}
    child_memory = SimpleNamespace(close=lambda: None)
    child_recorder = SimpleNamespace(close=lambda: None)
    registry = SimpleNamespace(retain=lambda names: seen.setdefault("tools", set(names)))
    child = SimpleNamespace(
        memory=child_memory, recorder=child_recorder, registry=registry,
        run=lambda *_a, **_k: SimpleNamespace(answer="investigated", error=""))

    def fake_resolve(task, **kwargs):
        seen["resolve"] = kwargs
        return _decision("deepseek", "deepseek-chat", automatic=False)

    def fake_make(*_args, **kwargs):
        seen["make"] = kwargs
        return child

    monkeypatch.setattr("harness.router.resolve_run_decision", fake_resolve)
    monkeypatch.setattr("harness.cli.make_harness", fake_make)
    memory = SimpleNamespace(trusted_profile=lambda **_kw: {})
    parent = _decision("deepseek", "deepseek-chat", automatic=False).to_dict()
    ctx = ToolCtx(str(tmp_path), "p", memory, route_decision=parent)

    assert DelegateTool().run({"task": "inspect call sites"}, ctx) == "investigated"
    assert seen["resolve"]["provider"] == "deepseek"
    assert seen["resolve"]["model"] == "deepseek-chat"
    assert seen["resolve"]["purpose"] == "delegate"
    assert seen["make"]["provider"] == "deepseek"
    assert seen["make"]["gate"].mode.value == "review"
    assert "write_file" not in seen["tools"] and "bash" not in seen["tools"]


def test_auto_mission_route_is_persisted_then_frozen(monkeypatch, tmp_path):
    from harness.missionweb import MissionService

    brain = _decision(executor="codex-exec")
    monkeypatch.setattr("harness.router.resolve_run_decision", lambda *_a, **_k: brain)
    service = MissionService(
        base=str(tmp_path / "mission"), provider="auto", stub=True)
    try:
        started = service.start("carry out a careful campaign", autonomous=False)
        case = started["case"]
        assert case["brain_route"]["provider"] == "codex-oauth"
        assert "explicit successor" in case["brain_route"]["durable_fallback_policy"]
        assert case["execution_profile"]["provider"] == "codex-oauth"
        assert case["execution_profile"]["allow_provider_fallback"] is False
        assert case["brain_route"]["requested_executor"] == "codex-exec"
        assert case["brain_route"]["worker_executor"] == "collie"
        assert "runner" not in case["execution_profile"]  # v1 means native Collie
        assert service.store.get(started["mission_id"]).leash["execution_profile_sha256"]
        service._activate_execution_profile(service.store.get(started["mission_id"]))
        assert service._executor == "collie"
    finally:
        service.close()


def test_executor_is_independent_from_transport_and_requires_real_mission_adapter(tmp_path):
    from harness.brain_router import BrainRouteStore, RoutingContext
    from harness.router import resolve_run_decision

    store = BrainRouteStore(str(tmp_path / "brain.db"))
    self_context = RoutingContext(purpose="self", allowed_executors=("codex-exec",))
    self_route = resolve_run_decision(
        "fix code", "auto", route_kind="code", purpose="self",
        routing_context=self_context, catalog_entries=CATALOG,
        brain_store=store, now=1000)
    assert self_route.transport == "codex-oauth"
    assert self_route.executor == "collie"

    mission_context = RoutingContext(
        purpose="mission", allowed_executors=("codex-exec",))
    mission_route = resolve_run_decision(
        "fix code", "auto", route_kind="code", purpose="mission",
        routing_context=mission_context, catalog_entries=CATALOG,
        brain_store=store, now=1000)
    assert mission_route.transport == "codex-oauth"
    assert mission_route.executor == "codex-exec"

    claude_only = [_entry(
        "claude-agent-sdk", "claude-opus-5", tags=("coding", "frontier"))]
    claude = resolve_run_decision(
        "fix code", "auto", route_kind="code", purpose="mission",
        routing_context=mission_context, catalog_entries=claude_only,
        brain_store=store, now=1000)
    assert claude.transport == "claude-agent-sdk"
    assert claude.executor == "collie"


def test_unconstrained_auto_chooses_quality_before_billing_class(tmp_path):
    from harness.brain_router import BrainRouteStore, RoutingContext
    from harness.router import resolve_run_decision

    catalog = [
        _entry("ollama", "llama3.1:8b", kind="local", auth="ok"),
        _entry("anthropic", "claude-opus-5", tags=("frontier",),
               kind="metered", auth="ok"),
    ]
    store = BrainRouteStore(str(tmp_path / "brain.db"))

    unconstrained = resolve_run_decision(
        "explain this architecture", "auto", route_kind="chat",
        routing_context=RoutingContext(purpose="self"),
        catalog_entries=catalog, brain_store=store, now=1000)
    assert unconstrained.provider == "anthropic"
    assert "quality ranking precedes billing kind" in " ".join(unconstrained.reasons)

    no_paid = resolve_run_decision(
        "explain this architecture", "auto", route_kind="chat",
        routing_context=RoutingContext(
            purpose="self", paid_overage_disabled=True, remaining_cost_usd=0.0),
        catalog_entries=catalog, brain_store=store, now=1000)
    assert no_paid.provider == "ollama"


def test_budget_and_billing_admission_precede_quality(tmp_path):
    from harness.brain_router import BrainRouteStore, RoutingContext
    from harness.router import resolve_run_decision

    catalog = [
        _entry("codex-oauth", "gpt-5.6-luna", kind="subscription", auth="ok"),
        _entry("anthropic", "claude-opus-5", kind="metered", auth="ok"),
    ]
    store = BrainRouteStore(str(tmp_path / "brain.db"))
    context = RoutingContext(
        purpose="self", paid_overage_disabled=True, remaining_cost_usd=0.0,
        remaining_tokens=1000, remaining_model_calls=1)
    decision = resolve_run_decision(
        "hard security architecture", "auto", route_kind="chat",
        routing_context=context, catalog_entries=catalog,
        brain_store=store, now=1000)
    assert decision.provider == "codex-oauth"
    assert all(row["kind"] != "metered" for row in decision.fallbacks)
    assert "budget admission before quality: no-paid-overage" in " ".join(decision.reasons)

    exhausted = RoutingContext(purpose="self", remaining_model_calls=0)
    import pytest
    with pytest.raises(ValueError, match="budget exhausted"):
        resolve_run_decision(
            "answer", "auto", routing_context=exhausted,
            catalog_entries=catalog, brain_store=store, now=1000)


def test_credential_terminal_fence_survives_until_fingerprint_changes(tmp_path):
    from harness.brain_router import BrainRouteStore, RoutingContext
    from harness.router import resolve_run_decision

    store = BrainRouteStore(str(tmp_path / "brain.db"))
    old = {"fingerprint": "old", "mtime_ns": 10}
    changed = {"fingerprint": "new", "mtime_ns": 11}
    did = store.record_decision(
        {"provider": "codex-oauth", "model": "gpt-5.6-sol",
         "transport": "codex-oauth", "executor": "collie"}, now=1000)
    store.sync_credential("codex-oauth", old, now=1000)
    store.record_outcome(
        did, provider="codex-oauth", model="gpt-5.6-sol", success=False,
        error_class="credential", detail="HTTP 401 invalid token", credential=old,
        now=1000)
    context = RoutingContext(purpose="self")
    blocked = resolve_run_decision(
        "answer", "auto", routing_context=context, catalog_entries=CATALOG,
        credential_states={"codex-oauth": old}, brain_store=store, now=1001)
    assert blocked.provider == "claude-agent-sdk"
    recovered = resolve_run_decision(
        "answer", "auto", routing_context=context, catalog_entries=CATALOG,
        credential_states={"codex-oauth": changed}, brain_store=store, now=1002)
    assert recovered.provider == "codex-oauth"


def test_context_builder_scopes_trusted_claim_to_device_and_receipt(tmp_path):
    from harness.brain_router import BrainRouteStore, build_routing_context
    from harness.router import resolve_run_decision

    seen = {}
    claim = {
        "id": 7, "project": "repo-a", "scope": "project", "device_id": "dev-a",
        "kind": "preference", "status": "attested", "confidence": 1.0,
        "observations": 1, "source": "user", "value": "claude-agent-sdk",
    }
    memory = SimpleNamespace(trusted_profile=lambda **kw: seen.update(kw) or {
        "routing.provider": claim})
    context = build_routing_context(
        memory=memory, project="repo-a", device_id="dev-a", purpose="self")
    store = BrainRouteStore(str(tmp_path / "brain.db"))
    decision = resolve_run_decision(
        "answer", "auto", routing_context=context, catalog_entries=CATALOG,
        brain_store=store, now=1000)
    assert seen == {"project": "repo-a", "device_id": "dev-a"}
    assert decision.provider == "claude-agent-sdk"
    assert decision.routing_claims == ({
        "id": 7, "project": "repo-a", "scope": "project", "device_id": "dev-a",
        "kind": "preference", "status": "attested", "confidence": 1.0,
        "observations": 1, "source": "user", "attribute": "routing.provider",
        "value": "claude-agent-sdk",
    },)


def test_auto_crosses_provider_on_recognized_credential_failure(monkeypatch, tmp_path):
    from harness.brain_router import BrainRouteStore
    from harness.cli import make_harness
    from harness.providers import Completion

    bad = _ScriptProvider(
        "codex-oauth", "gpt-5.6-sol",
        Completion(text="unauthorized", stop_reason="error", error_status=401,
                   error_detail="invalid OAuth token"))
    good = _ScriptProvider(
        "deepseek", "deepseek-chat",
        Completion(text="fallback answered", stop_reason="end_turn"))
    monkeypatch.setattr(
        "harness.providers.make_provider",
        lambda provider, model, **_kw: good if provider == "deepseek" else bad)
    store = BrainRouteStore(str(tmp_path / "brain.db"))
    did = store.record_decision(
        {"provider": bad.name, "model": bad.model,
         "transport": bad.name, "executor": "collie"}, task="shape")
    h = make_harness(str(tmp_path), provider="mock", project="brain", embed="hash")
    h.provider = bad
    h.max_retries = 0
    h.max_turns = 2
    h.brain_automatic = True
    h.brain_fallbacks = [{
        "provider": "deepseek", "model": "deepseek-chat",
        "effort": "medium", "speed": "standard"}]
    h.brain_decision_id = did
    h.brain_store = store

    result = h.run("brain", "answer")

    assert result.error == ""
    assert result.actual_provider == "deepseek"
    assert store.credential_available("codex-oauth") is False


def test_constructor_credential_failure_falls_back_only_under_auto(monkeypatch, tmp_path):
    from harness import cli

    attempts = []

    def construct(provider, model, **_kwargs):
        attempts.append((provider, model))
        if provider == "codex-oauth":
            raise RuntimeError("invalid API key / OAuth credential")
        return SimpleNamespace(name=provider, model=model)

    monkeypatch.setattr(cli, "make_provider", construct)
    monkeypatch.setenv("COLLIE_BRAIN_DB", str(tmp_path / "brain.db"))
    automatic = _decision()
    selected, failures, remaining = cli._construct_routed_provider(automatic)
    assert selected.name == "deepseek"
    assert attempts == [
        ("codex-oauth", "gpt-5.6-sol"),
        ("deepseek", "deepseek-chat")]
    assert failures[0]["error_class"] == "credential"
    assert remaining == []

    attempts.clear()
    concrete = _decision(automatic=False)
    import pytest
    with pytest.raises(RuntimeError, match="invalid API key"):
        cli._construct_routed_provider(concrete)
    assert attempts == [("codex-oauth", "gpt-5.6-sol")]
