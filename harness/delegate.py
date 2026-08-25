"""delegate — hand a noisy sub-investigation to a single-depth child agent that has its OWN
clean context, returning only the child's final summary. A token-discipline mechanism (Hermes'
framing), NOT Claude-Code-style fan-out: the parent's context window never sees the child's
dozens of read/grep/tool messages — only the distilled answer comes back.

Capped at ONE level: a delegated agent runs with COLLIE_SUBAGENT=1, and both default_registry
(no delegate tool registered under that flag) AND this tool's own guard refuse further nesting,
so total tree cost can't blow up — the documented Hermes footgun where independent child budgets
make the tree exceed the parent cap.
"""
import os

from .tools import Tool


class DelegateTool(Tool):
    name, tier = "delegate", "always"
    description = (
        "Delegate a focused, read-heavy sub-task to a fresh child agent with its own CLEAN "
        "context — e.g. 'find every call site of parse_config and summarize the signatures', or "
        "'investigate why test_x fails and report the root cause'. You get back ONLY the child's "
        "final summary; its exploration never enters your context, saving tokens. Best for noisy "
        "investigations whose intermediate tool output you don't need. Args: task (required), "
        "optional max_turns (default 12).")
    schema = {"type": "object", "properties": {
        "task": {"type": "string"}, "max_turns": {"type": "integer"}},
        "required": ["task"]}

    def run(self, args, ctx):
        task = (args.get("task") or "").strip()
        if not task:
            return "ERROR: empty task"
        if os.environ.get("COLLIE_SUBAGENT") == "1":
            return ("ERROR: delegation is single-depth — a delegated agent cannot itself "
                    "delegate (prevents runaway sub-sub-agent cost). Do this part directly.")
        from .cli import (build_turn_routing_context, configure_brain_decision,
                          make_harness)
        from . import settings
        from .router import resolve_run_decision
        parent = dict(getattr(ctx, "route_decision", None) or {})
        parent_sources = dict(parent.get("sources") or {})
        # A concrete parent route is user/config authority and therefore inherited
        # exactly.  An Auto parent grants this focused subtask a fresh delegated
        # decision; its health/quality needs can differ from the foreground turn.
        if parent and parent_sources.get("provider") == "configured":
            provider = str(parent.get("provider") or "")
            configured_model = str(parent.get("model") or "") or None
        else:
            provider = "auto" if parent.get("automatic") else settings.get("PROVIDER", "auto")
            configured_model = (None if provider == "auto" else
                                (str(parent.get("model") or "") or
                                 settings.get("MODEL", "") or None))
        routing_context = build_turn_routing_context(
            memory=ctx.memory, project=ctx.project, purpose="delegate",
            device_id=str(getattr(ctx, "device_id", "") or ""),
            shared_budget=getattr(ctx, "shared_budget", None),
            # DelegateTool is a read-only context-isolation primitive, not a
            # Mission workspace authority.  It therefore never advertises an
            # external coding executor even when a CLI happens to be installed.
            allowed_executors=())
        decision = resolve_run_decision(
            task, provider=provider, model=configured_model,
            effort=str(parent.get("effort") or settings.get("REASONING_EFFORT", "auto") or "auto"),
            speed=str(parent.get("speed") or "standard"), route_kind="chat",
            intent="review", explicit_axes=("intent",), purpose="delegate",
            trusted_profile=routing_context.trusted_profile,
            routing_context=routing_context)
        try:
            max_turns = max(1, min(30, int(args.get("max_turns", 12))))
        except (TypeError, ValueError):
            max_turns = 12
        prev = os.environ.get("COLLIE_SUBAGENT")
        # set the flag BEFORE make_harness so the child's registry is built WITHOUT the delegate
        # tool (single-depth); otherwise the child advertises a tool it can only be refused on.
        os.environ["COLLIE_SUBAGENT"] = "1"
        h = None
        try:
            # The delegate tool is an investigation primitive, not a second
            # unbounded coding agent.  It receives a read-only Gate and a positive
            # tool subset even if the parent is allowed to write.
            from .gate import Gate, Mode
            child_gate = Gate(ctx.cwd, mode=Mode.REVIEW)
            h = make_harness(
                ctx.cwd, provider=decision.provider, model=decision.model,
                effort=decision.effort, speed=decision.speed,
                project=ctx.project, code_search=True, gate=child_gate,
                route_decision=decision,
                device_id=str(getattr(ctx, "device_id", "") or ""))
            configure_brain_decision(h, decision)
            h.registry.retain({
                "read_file", "grep", "glob", "memory_search", "code_search", "plan",
            })
            h.force_edit = False
            h.self_verify = False
            h.shared_budget = getattr(ctx, "shared_budget", None)
            h.max_turns = max_turns
            res = h.run("delegate", task, consolidate=False)
            return (res.answer or res.error or "(child agent produced no answer)")[:6000]
        except Exception as e:                       # make_harness or run failure — never leak/escape
            return "ERROR(delegate): %s" % e
        finally:
            if prev is None:
                os.environ.pop("COLLIE_SUBAGENT", None)
            else:
                os.environ["COLLIE_SUBAGENT"] = prev
            if h is not None:                        # only close what we actually opened
                try:
                    h.memory.close()
                    h.recorder.close()
                except Exception:
                    pass


def register_delegate(registry):
    registry.register(DelegateTool())
    return True
