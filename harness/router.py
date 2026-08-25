"""Front-door router — the classifying "head" that types each message and routes it.

Ordinary messages get ONE cheap model call that classifies them into a small,
principled set of interactive task kinds; the caller then routes to the right executor. The
taxonomy is not ad-hoc — it is two orthogonal axes from the literature,
discretized into three kinds (see docs/ROUTER_DESIGN.md for citations):

  AXIS 1 — know vs do (Parasuraman/Sheridan/Wickens 2000 information-vs-action
           stages; Kirsh & Maglio 1994 epistemic-vs-pragmatic action; Searle
           assertives-vs-directives): separates CHAT from CODE+MISSION.
  AXIS 2 — reversibility of the action (Amodei et al. 2016 side-effects/safe-
           exploration; Krakovna et al. 2019 reachability): separates reversible
           workspace edits (CODE) from consequential, possibly irreversible
           real-world action (MISSION).

  chat    — produce information (answer / explain / find out on the web). Research
            lives HERE (epistemic, read-only) — never its own top-level kind.
  code    — create/modify/debug code or files in the workspace (reversible).
  mission — a durable, multi-step real-world errand, entered ONLY through an
            explicit `/mission ...` (or legacy `/delegate ...`) command.

The irreversible route is command-only. Even if the classifier calls ordinary
language a mission, it is defensively collapsed to chat. This makes starting a
durable campaign an unambiguous user action rather than a probabilistic guess.

Honesty about the model: the model is a HARD dependency of every route (chat,
code, and mission all need it). So if the model is genuinely unavailable, we do
NOT silently fall back to a heuristic — we raise ModelUnavailable and the caller
says so. The ONLY fallback is: the model responded but its label was unparseable
(the model IS up) -> route chat, the cheapest working path.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import re
import time

KINDS = ("chat", "code", "mission")

# Retained for import compatibility with older clients. Mission is now command-only,
# so no model confidence value can activate it.
MISSION_THRESHOLD = 0.7

# The router's default model on anthropic providers (Sonnet: fast + capable; the
# whole haiku/sonnet/opus set scored 28/28 on the battery, so this trades latency,
# not accuracy). Override up (opus) or down (haiku) via COLLIE_ROUTER_MODEL.
DEFAULT_ROUTER_MODEL = "claude-sonnet-4-6"


# The front-door label is only one input to execution.  Keeping the rest of the
# decision in one value prevents the web, mobile, and Pack paths from each
# inventing a slightly different meaning for "Auto".
_AXES = ("intent", "quality", "verification", "workspace", "strategy", "effort", "speed")
_VALID = {
    "intent": ("build", "plan", "test", "review"),
    "quality": ("quick", "balanced", "thorough"),
    "verification": ("auto", "required"),
    "workspace": ("current", "isolated"),
    "strategy": ("single", "pack"),
}

_HARD_TASK = re.compile(
    r"\b(?:security|auth(?:entication|orization)?|permission|migration|schema|database|"
    r"architecture|concurren(?:cy|t)|race condition|deadlock|distributed|production|release|"
    r"refactor|multi[- ]?file|breaking change|rollback|data loss|performance regression|"
    r"flaky|intermittent|root cause|end[- ]to[- ]end)\b|"
    r"(?:安全|认证|授权|权限|迁移|架构|并发|竞态|死锁|分布式|生产|发布|重构|"
    r"多文件|破坏性变更|回滚|数据丢失|性能回归|不稳定|根因|端到端|系统性|全面)",
    re.I,
)
_CODE_ACTION = re.compile(
    r"\b(?:add|build|change|create|debug|delete|edit|fix|implement|modify|patch|refactor|"
    r"remove|rename|replace|ship|update|write)\b|(?:^|[\\/])[^\s]+\.[a-z0-9]{1,8}\b|"
    r"(?:添加|新增|构建|创建|调试|删除|编辑|修复|实现|修改|补丁|重构|移除|重命名|替换|更新|编写)",
    re.I,
)
_BEHAVIORAL = re.compile(
    r"\b(?:bug|fix|regression|security|migration|schema|auth|permission|api|database|"
    r"concurren(?:cy|t)|race|test|flaky|production|release)\b|"
    r"(?:缺陷|修复|回归|安全|迁移|架构|认证|授权|权限|接口|数据库|并发|竞态|测试|不稳定|生产|发布)",
    re.I,
)
_FAILURE = re.compile(
    r"\b(?:failed|failure|error|verification required|turn limit|timed? out|did not pass|"
    r"could not complete|no winner)\b|(?:失败|报错|错误|验证未通过|没有通过|未通过|超时|无法完成)",
    re.I,
)
_TINY_TASK = re.compile(
    r"\b(?:typo|copy|comment|docs?|string|label|rename|one[- ]line|tiny|small|"
    r"format(?:ting)?)\b|(?:错别字|文案|注释|文档|字符串|标签|重命名|一行|小改|微调|格式化)",
    re.I,
)


@dataclass(frozen=True)
class RunDecision:
    """The resolved, auditable contract for one interactive execution.

    ``sources`` says who chose each field (user/config/router/policy/safety).
    ``reasons`` is intentionally plain text so it can be shown in a receipt and
    persisted without needing the policy code that produced it.
    """

    provider: str
    model: str
    effort: str
    speed: str
    billing_multiplier: float | None
    intent: str
    quality: str
    verification: str
    workspace: str
    strategy: str
    route_kind: str
    complexity: str
    explicit: tuple[str, ...] = ()
    sources: dict[str, str] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    # Model transport and task executor are independent authority axes.  Most
    # turns use Collie's loop even when their transport is Codex OAuth or the
    # Claude SDK.  External executor names are emitted only by a surface that
    # owns a real, bounded adapter.
    transport: str = ""
    executor: str = "collie"
    fallbacks: tuple[dict, ...] = ()
    automatic: bool = False
    decision_id: str = ""
    routing_claims: tuple[dict, ...] = ()
    routing_context: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "effort": self.effort,
            "speed": self.speed,
            "billing_multiplier": self.billing_multiplier,
            "intent": self.intent,
            "quality": self.quality,
            "verification": self.verification,
            "workspace": self.workspace,
            "strategy": self.strategy,
            "route_kind": self.route_kind,
            "complexity": self.complexity,
            "explicit": list(self.explicit),
            "sources": dict(self.sources),
            "reasons": list(self.reasons),
            "transport": self.transport or self.provider,
            "executor": self.executor,
            "brain_transport": self.transport or self.provider,
            "worker_executor": self.executor,
            "fallbacks": [dict(item) for item in self.fallbacks],
            "automatic": self.automatic,
            "decision_id": self.decision_id,
            "routing_claims": [dict(item) for item in self.routing_claims],
            "routing_context": dict(self.routing_context),
        }


def parse_explicit_axes(value) -> tuple[str, ...]:
    """Normalize a query-string/list/set of explicitly chosen run axes."""
    if isinstance(value, str):
        value = value.split(",")
    return tuple(sorted({str(v).strip().lower() for v in (value or ())
                         if str(v).strip().lower() in _AXES}))


def _text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(str(x.get("text", "")) if isinstance(x, dict) else str(x)
                        for x in value)
    return str(value or "")


def _recent_failures(history) -> int:
    # Only the tail matters: a failure twenty successful turns ago should not pin
    # every future message to the strongest model forever.
    count = 0
    for msg in list(history or [])[-8:]:
        if msg.get("role") == "assistant" and _FAILURE.search(_text(msg.get("content"))):
            count += 1
    return count


def _infer_kind(text: str, route_kind: str | None) -> str:
    kind = (route_kind or "").strip().lower()
    if kind in ("chat", "code"):
        return kind
    # Mobile and API clients may not call /api/route.  This is not allowed to
    # create a mission; it only chooses the read-only vs workspace executor.
    return "code" if _CODE_ACTION.search(text or "") else "chat"


def _complexity(text: str, kind: str, history) -> tuple[str, int]:
    failures = _recent_failures(history)
    if failures or _HARD_TASK.search(text or "") or len(text or "") > 900:
        return "hard", failures
    if len(text or "") < 180 and (kind == "chat" or _TINY_TASK.search(text or "")):
        return "simple", failures
    return "standard", failures


def _automatic_model(provider: str, kind: str, complexity: str, quality: str) -> str:
    """Choose capability inside *provider*; never cross a trust/billing boundary."""
    p = (provider or "").lower()
    if p in ("codex-oauth", "codex-sub", "codex"):
        # Terra handles fast scans/everyday tasks; Sol is reserved for work whose
        # ambiguity or prior failure justifies its latency.  A pinned Sol still
        # wins below, and can run at low/medium effort when the task is small.
        if quality == "quick" or complexity == "simple":
            return "gpt-5.6-luna"
        return "gpt-5.6-sol" if complexity == "hard" else "gpt-5.6-terra"
    if p in ("anthropic-oauth", "claude-sub"):
        if quality == "quick" or complexity == "simple":
            return "claude-haiku-4-5-20251001"
        return "claude-opus-5" if complexity == "hard" else "claude-sonnet-5"
    from .providers import provider_default_model
    return provider_default_model(provider)


def resolve_run_decision(text: str, provider: str, model: str | None = None,
                         effort: str = "auto", speed: str = "standard",
                         route_kind: str | None = None,
                         intent: str = "build", quality: str = "balanced",
                         verification: str = "auto", workspace: str = "current",
                         strategy: str = "single", explicit_axes=(), history=None,
                         purpose: str = "self", trusted_profile=None,
                         routing_context=None,
                         availability=None, catalog_entries=None,
                         credential_states=None, brain_store=None,
                         now: float | None = None,
                         policy_quality: str | None = None) -> RunDecision:
    """Resolve one task-aware execution policy.

    Priority is explicit per-run choice > configured model/effort > trusted
    preference > task policy > verified habit.  A concrete provider is never
    changed here because it is a credential/billing/data-policy pin.  Only the
    special ``auto`` provider grants Collie permission to choose across currently
    authenticated routes and attach transparent fallbacks.
    """
    provider = (provider or "").strip()
    if not provider:
        raise ValueError("provider is required")
    requested = {
        "intent": (intent or "build").strip().lower(),
        "quality": (quality or "balanced").strip().lower(),
        "verification": (verification or "auto").strip().lower(),
        "workspace": (workspace or "current").strip().lower(),
        "strategy": (strategy or "single").strip().lower(),
    }
    for axis, allowed in _VALID.items():
        if requested[axis] not in allowed:
            raise ValueError("%s must be %s" % (axis, " or ".join(allowed)))
    policy_quality = (policy_quality or "").strip().lower()
    if policy_quality and policy_quality not in _VALID["quality"]:
        raise ValueError("policy_quality must be %s" %
                         " or ".join(_VALID["quality"]))
    explicit = set(parse_explicit_axes(explicit_axes))
    kind = _infer_kind(text or "", route_kind)
    complexity, failures = _complexity(text or "", kind, history)
    configured_model = (model or "").strip()
    routing_quality = (requested["quality"] if "quality" in explicit else
                       policy_quality or
                       ("thorough" if complexity == "hard" else "balanced"))
    automatic_provider = provider.lower() == "auto"
    selection = None
    resolved_store = brain_store
    if automatic_provider:
        from .brain_router import choose_brain, default_store
        if resolved_store is None:
            resolved_store = default_store()
        selection = choose_brain(
            purpose=purpose, complexity=complexity, quality=routing_quality,
            route_kind=kind, pinned_model=configured_model,
            profile=trusted_profile, availability=availability,
            routing_context=routing_context,
            catalog_entries=catalog_entries, credential_states=credential_states,
            store=resolved_store, now=now)
        provider = selection.primary.provider
        resolved_model = selection.primary.model
        transport = selection.primary.transport
        executor = selection.primary.executor
        fallbacks = tuple(item.to_dict() for item in selection.fallbacks)
        sources = {
            "provider": "brain-router",
            "model": "configured" if configured_model else "brain-router",
            "transport": "brain-router",
            "executor": "brain-router",
        }
        reasons = [
            "provider: Auto selected %s because it is available and best fits this task" % provider,
            "model: %s%s" % (
                resolved_model,
                " (exact model pin)" if configured_model else " (quality-ranked)"),
            "transport: model requests use %s" % transport,
            "executor: task execution uses %s" % executor,
        ]
        reasons.extend(selection.reasons)
        if selection.primary.reasons:
            reasons.append("ranking: " + "; ".join(selection.primary.reasons))
    else:
        from .brain_router import executor_for, transport_for
        context_allowed = (
            routing_context.get("allowed_executors", ())
            if isinstance(routing_context, dict) else
            getattr(routing_context, "allowed_executors", ())
            if routing_context is not None else ())
        transport = transport_for(provider)
        executor = executor_for(
            provider, purpose=purpose, route_kind=kind,
            allowed_executors=context_allowed)
        fallbacks = ()
        sources = {"provider": "configured", "transport": "configured",
                   "executor": "surface-authority"}
        reasons = [
            "provider: kept configured %s; automatic routing never crosses providers" % provider,
            "transport: model requests use %s" % transport,
            "executor: task execution uses %s" % executor,
        ]
        if configured_model:
            resolved_model = configured_model
            sources["model"] = "configured"
            reasons.append("model: kept the explicitly configured model %s" % resolved_model)
        else:
            resolved_model = _automatic_model(provider, kind, complexity, routing_quality)
            sources["model"] = "task-policy"
            reasons.append("model: selected %s inside %s for a %s %s task" %
                           (resolved_model, provider, complexity, kind))

    # Selecting a Build-only execution axis is also an unambiguous request to
    # build, even when a cheap classifier called the prose informational.
    build_implied = (("workspace" in explicit and requested["workspace"] == "isolated") or
                     ("verification" in explicit and requested["verification"] == "required") or
                     ("strategy" in explicit and requested["strategy"] == "pack"))
    if "intent" in explicit:
        resolved_intent = requested["intent"]
        sources["intent"] = "user"
    elif build_implied:
        resolved_intent = "build"
        sources["intent"] = "implied-user-choice"
    else:
        # Plan, Review, and Test are deliberate read-only ROLES the user picks; the router
        # never infers them.  A "chat"-kind message is a question, a lookup, or a real-world
        # action such as a phone call — none of which wants a read-only plan artifact.  It
        # runs as Build so the tools that answer or act are available; the GATE, not the
        # intent, bounds what that run may do (consequential calls still ask).
        resolved_intent = "build"
        sources["intent"] = "router"
    reasons.append("intent: %s (%s)" % (resolved_intent, sources["intent"]))

    if "quality" in explicit:
        resolved_quality = requested["quality"]
        sources["quality"] = "user"
    elif policy_quality:
        resolved_quality = policy_quality
        sources["quality"] = "surface-policy"
    else:
        resolved_quality = routing_quality
        sources["quality"] = "task-policy"
    reasons.append("quality: %s (%s)" % (resolved_quality, sources["quality"]))

    if "verification" in explicit:
        resolved_verification = requested["verification"]
        sources["verification"] = "user"
    else:
        resolved_verification = (
            "required" if resolved_intent == "build" and complexity == "hard" and
            _BEHAVIORAL.search(text or "") else "auto")
        sources["verification"] = "task-policy"
    reasons.append("verification: %s (%s)" %
                   (resolved_verification, sources["verification"]))

    # Isolation and Pack affect where writes land and how much work is spent.
    # They therefore stay conservative unless the user explicitly chose them.
    for axis, safe in (("workspace", "current"), ("strategy", "single")):
        if axis in explicit:
            value, source = requested[axis], "user"
        else:
            value, source = safe, "safety-default"
        if axis == "workspace":
            resolved_workspace = value
        else:
            resolved_strategy = value
        sources[axis] = source
        reasons.append("%s: %s (%s)" % (axis, value, source))

    desired_effort = (effort or "auto").strip().lower()
    if desired_effort in ("", "auto"):
        desired_effort = ("low" if resolved_quality == "quick" else
                          {"simple": "low", "standard": "medium", "hard": "high"}[complexity])
        sources["effort"] = "task-policy"
    else:
        sources["effort"] = "user-or-config"
    from .providers import resolve_reasoning_effort
    resolved_effort, effort_note = resolve_reasoning_effort(
        provider, resolved_model, desired_effort)
    reasons.append("effort: %s (%s%s)" % (
        resolved_effort, sources["effort"], "; " + effort_note if effort_note else ""))
    from .providers import resolve_speed_tier
    resolved_speed, caps = resolve_speed_tier(provider, resolved_model, speed)
    sources["speed"] = "user" if resolved_speed == "fast" or "speed" in explicit else "default"
    reasons.append("speed: %s (%s%s)" % (
        resolved_speed, sources["speed"],
        "; " + caps["fast_note"] if resolved_speed == "fast" else ""))
    if failures:
        reasons.append("complexity: escalated after %d recent failure signal%s" %
                       (failures, "" if failures == 1 else "s"))
    else:
        reasons.append("complexity: %s from task scope and risk" % complexity)

    # Resolve each fallback against its own capability contract now, not in the
    # error path.  In particular, passing the literal string ``auto`` to an SDK
    # that expects low/medium/high would turn a healthy fallback into a 400.
    resolved_fallbacks = []
    for item in fallbacks:
        candidate = dict(item)
        try:
            candidate_effort, _ = resolve_reasoning_effort(
                candidate["provider"], candidate["model"], desired_effort)
            candidate_speed, _ = resolve_speed_tier(
                candidate["provider"], candidate["model"], speed)
        except (KeyError, ValueError):
            # An explicitly requested Fast tier is a real service/billing
            # contract; a provider without it is not a transparent equivalent.
            continue
        candidate["effort"] = candidate_effort
        candidate["speed"] = candidate_speed
        resolved_fallbacks.append(candidate)
    fallbacks = tuple(resolved_fallbacks)
    if fallbacks:
        reasons.append("fallback: " + " -> ".join(
            "%s/%s" % (item["provider"], item["model"]) for item in fallbacks))

    decision = RunDecision(
        provider=provider, model=resolved_model, effort=resolved_effort,
        speed=resolved_speed, billing_multiplier=(
            caps["fast_billing_multiplier"] if resolved_speed == "fast" else 1.0),
        intent=resolved_intent, quality=resolved_quality,
        verification=resolved_verification, workspace=resolved_workspace,
        strategy=resolved_strategy, route_kind=kind, complexity=complexity,
        explicit=tuple(sorted(explicit)), sources=sources, reasons=tuple(reasons),
        transport=transport, executor=executor, fallbacks=fallbacks,
        routing_claims=(selection.claims if selection else ()),
        routing_context=(selection.routing_context if selection else
                         (routing_context.to_dict() if hasattr(routing_context, "to_dict") else {})),
        automatic=bool(automatic_provider and not configured_model))
    if automatic_provider and resolved_store is not None:
        try:
            decision_id = resolved_store.record_decision(
                decision.to_dict(), task=text, purpose=purpose, now=now)
            decision = replace(decision, decision_id=decision_id)
        except Exception:
            # Routing must still work in a read-only home.  Session receipts keep
            # the same explanation even when the health ledger cannot be updated.
            pass
    return decision

_PREFIX = re.compile(r"^\s*/(mission|delegate|code|chat)\s+(.*)", re.I | re.S)
_JSON = re.compile(r"\{.*\}", re.S)

_SYS = (
    "You are collie's front-door router. Classify the user's message into ONE task kind so it "
    "routes to the right executor. Decide on two axes:\n"
    "  AXIS 1 (know vs do): does the message ask you to PRODUCE INFORMATION (answer / explain / "
    "find something out), or to TAKE ACTION that changes something?\n"
    "  AXIS 2 (only if it is an action): change a REVERSIBLE artifact in the current code workspace, "
    "or take a CONSEQUENTIAL real-world action that may be irreversible and/or wait for events?\n\n"
    "Kinds:\n"
    "- \"chat\": produce information — answer, explain, compare, discuss, or research/find something "
    "out on the web. No workspace change, no real-world action. (Research is chat.)\n"
    "- \"code\": create, modify, or debug code or files in the current workspace. Reversible.\n"
    "- \"mission\": a durable, multi-step real-world errand that may take IRREVERSIBLE actions "
    "(send, publish, buy, apply, book, pay) or wait for external events (a reply, availability). "
    "e.g. 'sell my car', 'email X and follow up', 'watch this listing and tell me when it drops'.\n\n"
    "Rules:\n"
    "- Bias toward the cheaper, reversible kind when unsure: chat over code, code over mission. "
    "Only choose \"mission\" when it clearly asks you to act in the world over time.\n"
    "- confidence (0..1) = how sure you are; a genuinely ambiguous message gets LOW confidence.\n"
    "- goal = a short normalized imperative (essential for mission; echo the ask for others).\n\n"
    "Examples:\n"
    "  'why is this test flaky?' -> {\"kind\":\"chat\",\"confidence\":0.9}\n"
    "  'add a --json flag to the CLI' -> {\"kind\":\"code\",\"confidence\":0.9}\n"
    "  'sell my 2018 Corolla on marketplace, local only' -> {\"kind\":\"mission\",\"confidence\":0.95}\n"
    "  'find me a cheap flight to Tokyo next month' -> {\"kind\":\"chat\",\"confidence\":0.6} (finding out = chat)\n\n"
    "Reply with STRICT JSON only and nothing else:\n"
    '{"kind": "chat|code|mission", "goal": "<short imperative>", '
    '"confidence": 0.0, "reason": "<one short clause>"}')


class ModelUnavailable(RuntimeError):
    """The model could not be reached — every route needs it, so the caller must
    surface this, NOT route somewhere as if it worked."""


def prefix_override(text: str):
    """Explicit user override: '/mission …' '/code …' '/chat …' ('/delegate' == mission).
    Returns (kind, stripped_text) or None. Handled BEFORE any model call (zero latency)."""
    from .missioncmd import parse as parse_mission
    mission = parse_mission(text or "")
    if mission is not None:
        # Body is retained for backward compatibility; classify() replaces it
        # with structured command/goal fields below.
        body = re.sub(r"^\s*/(?:mission|delegate)\b", "", text or "",
                      count=1, flags=re.I).strip()
        return "mission", body
    m = _PREFIX.match(text or "")
    if not m:
        return None
    word = m.group(1).lower()
    kind = "mission" if word in ("mission", "delegate") else word
    return kind, m.group(2).strip()


def _parse(txt: str):
    m = _JSON.search(txt or "")
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _backoff(attempt: int) -> float:
    # short, front-door-friendly: a user is waiting on their message.
    return min(0.5 * (2 ** attempt), 4.0)


def _decide(comp, text: str) -> dict:
    """Turn a successful completion into the routing decision (parse + threshold)."""
    plan = _parse(getattr(comp, "text", "") or "")
    if not plan or plan.get("kind") not in KINDS:
        # the model IS up but its label is unusable -> the cheapest working path.
        # NOT a heuristic classifier: it only fires when the model already answered.
        return {"kind": "chat", "goal": text, "confidence": 0.0,
                "reason": "classification unparsed", "source": "fallback", "abstained": False}
    kind = plan["kind"]
    try:
        conf = float(plan.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    conf = max(0.0, min(1.0, conf))
    goal = (plan.get("goal") or text).strip()
    reason = (plan.get("reason") or "")[:200]
    # Mission is EXPLICIT-ONLY. A model label can never start durable work.
    if kind == "mission":
        return {"kind": "chat", "goal": text, "confidence": conf, "reason": reason,
                "source": "model", "abstained": False}
    return {"kind": kind, "goal": goal, "confidence": conf, "reason": reason,
            "source": "model", "abstained": False}


def classify(text: str, provider, ctx: dict = None, retries: int = 3, _sleep=None) -> dict:
    """Classify `text` -> a routing decision dict:
        {kind, goal, confidence, reason, source, abstained[, suggested]}
      source: 'override' (explicit prefix) | 'model' | 'fallback' (model up, label unusable)

    Retries TRANSIENT model errors (HTTP 529 overloaded / 429 / timeouts — the same
    class collie's loop retries, via providers.classify_error) with a short backoff,
    since the front door must ride out an overload rather than fail the user's first
    message. Raises ModelUnavailable only on a TERMINAL error (auth / bad request /
    no provider) or after the transient retries are exhausted (persistent overload
    == effectively down) — never a silent heuristic fallback.
    """
    text = (text or "").strip()
    ov = prefix_override(text)
    if ov:
        kind, body = ov
        out = {"kind": kind, "goal": body or text, "confidence": 1.0,
               "reason": "explicit prefix", "source": "override", "abstained": False}
        if kind == "mission":
            from .missioncmd import parse
            command = parse(text)
            if command:
                out.update({"mission_command": command.action,
                            "mission_id": command.mission_id,
                            "autonomous": command.autonomous,
                            "goal": command.goal})
                if command.error:
                    out["command_error"] = command.error
        return out

    if provider is None:
        raise ModelUnavailable("no model provider configured")
    from .providers import classify_error       # lazy: keep router import light
    sleep = _sleep or time.sleep
    last = "model unavailable"
    for attempt in range(max(1, retries + 1)):
        try:
            comp = provider.complete(_SYS, [{"role": "user", "content": text}], [])
        except Exception as e:                    # a RAISING provider = hard failure, don't spin
            raise ModelUnavailable(f"{type(e).__name__}: {e}")
        if getattr(comp, "stop_reason", "") != "error":
            return _decide(comp, text)
        # error-as-data: is it transient (retry) or terminal (give up now)?
        detail = (getattr(comp, "error_detail", "") or getattr(comp, "text", "") or "model error")
        status = getattr(comp, "error_status", 0)
        last = detail[:200]
        if classify_error(detail, status) == "retryable" and attempt < retries:
            sleep(_backoff(attempt))
            continue
        raise ModelUnavailable(last)              # terminal, or transient after retries exhausted
    raise ModelUnavailable(last)
