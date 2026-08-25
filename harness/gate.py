"""The gate — allow / deny / ask for one proposed tool call.

The gate only DECIDES. The loop routes a `needs_user` decision to whatever surface is
attached (TUI prompt, web card, ACP's native permission request, a phone) and records
the answer. That split is what lets attended and unattended runs share one code path.

WHY THE DEFAULT MODE IS `project`, NOT `interactive`
----------------------------------------------------
Agents that live in a scratch directory and are handed folders one at a time can afford
to ask before every write and every command. collie cannot: you run `collie -p "fix the
bug"` inside your repo, and **that is the consent**. Asking again is noise, and an agent
that interrupts every `pytest` is not usable for the work collie exists to do.

So the boundary is drawn somewhere else. In `project` mode:

    reading                       — always fine
    writing / running INSIDE cwd  — covered by the consent you gave by launching here
    writing OUTSIDE cwd           — ask
    anything reaching OFF-machine — ask, every time, until a rule says otherwise

That last line is the one that matters. collie drives the user's real logged-in browser
and their real desktop; `browser_click` can send, post, buy, or delete under their
cookies. Nothing else in the tool set has that reach, and until now nothing gated it.

WHAT PATH SCOPING IS AND IS NOT
-------------------------------
In `project` mode ordinary local build/test shell commands run without interruption. Obvious
network clients, destructive commands and paths beyond the granted roots now require approval,
and Bash receives a credential-minimised environment. That is a useful fail-closed policy, but it
is not an OS sandbox: a repository-owned compiler/plugin/script can still implement behaviour a
shell-string inspection cannot see. Surfaces must not describe this as hostile-code containment.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from .risk import (
    NO_STANDING_RULE,
    RiskClass,
    RiskOverrides,
    classify,
    is_consequential,
    target_for,
)

# Shell metacharacters that turn one allowlisted command into several. An allowlist entry
# runs WITHOUT asking, so prefix matching alone is unsafe: an entry for `git status` would
# auto-run `git status && rm -rf ~`. Any of these disqualifies the command from the
# allowlist and sends it to the human instead.
_SHELL_OPERATORS = (";", "&", "|", ">", "<", "`", "$(", "(", "\n", "\r")

# Cross-platform process sandboxes are not uniformly available to Collie's zero-dependency core.
# Until a native sandbox is present, project mode automatically runs local build/test commands but
# routes obvious network, remote-control and destructive shell capabilities to the human.  This is
# deliberately conservative and complements (rather than replaces) BashTool's minimal environment.
_SHELL_EXTERNAL = re.compile(
    r"(?:\bhttps?://|\b(?:curl|wget|ftp|sftp|scp|ssh|telnet|nc|ncat)\b|"
    r"\b(?:invoke-webrequest|invoke-restmethod|iwr|irm)\b|"
    r"\bgit\s+(?:push|pull|fetch|clone|ls-remote|submodule\s+update)\b|"
    r"\b(?:npm|pnpm|yarn|pip|pip3|poetry|cargo|go)\s+(?:install|add|get)\b|"
    r"\b(?:aws|gcloud|az|kubectl|helm|terraform)\b|"
    r"\bdocker\s+(?:push|pull|login)\b)", re.I)
_SHELL_DESTRUCTIVE = re.compile(
    r"(?:^|[;&|]\s*)(?:rm|rmdir|del|erase|remove-item|clear-content|format|shutdown|reboot)\b",
    re.I)


def _project_shell_needs_approval(command: str) -> str:
    """Return a policy reason for shell capabilities project consent does not cover."""
    if not command or not isinstance(command, str):
        return "empty or malformed command"
    if _SHELL_EXTERNAL.search(command):
        return "command may reach another machine or remote service"
    if _SHELL_DESTRUCTIVE.search(command):
        return "destructive shell command needs explicit approval"
    # Catch common inline-code escapes without trying to pretend this is a complete shell parser.
    lowered = command.lower()
    if (re.search(r"\b(?:python|python3|node|ruby|perl)\b[^\n]*(?:-c|-e)\b", lowered)
            and re.search(r"(?:socket|urllib|requests|http\.client|fetch\s*\(|net\.|child_process)",
                          lowered)):
        return "inline program may bypass the external-action gate"
    return ""


def _has_shell_operators(command: str) -> bool:
    return any(op in command for op in _SHELL_OPERATORS)


class Mode(str, Enum):
    PLAN = "plan"                # read-only: explore and propose, change nothing
    REVIEW = "review"            # read-only findings tied to existing artifacts
    TEST = "test"                # read + allowlisted verification commands; never write
    PROJECT = "project"          # default — see the module docstring
    INTERACTIVE = "interactive"  # ask before every consequential call
    AUTO = "auto"                # allow everything (CI, benchmarks, sandboxes)


READ_ONLY_MODES = frozenset({Mode.PLAN, Mode.REVIEW})

# External tools whose target the USER can authorise simply by naming it in their own message
# (tool name -> how the target is matched). A direct instruction is product authority for the
# ordinary action it names (docs/voice-telephony.md); asking "approve phone_call?" after "call
# Kobe at 650-944-9576" is a second question about the same decision. Matching is by the
# user's own text, never by what the model claims.
USER_DIRECTED_BY_NAME = {"phone_call": "phone"}


class Outcome(str, Enum):
    """Deliberately the four values of ACP's PermissionOptionKind, so the editor
    adapter is a pass-through and Zed/JetBrains/neovim render their native prompt."""
    ALLOW_ONCE = "allow_once"
    ALLOW_ALWAYS = "allow_always"      # mints a (tool, target) rule for this run
    REJECT_ONCE = "reject_once"
    REJECT_ALWAYS = "reject_always"    # stop asking for this tool; deny for this run


ALLOWING = frozenset({Outcome.ALLOW_ONCE, Outcome.ALLOW_ALWAYS})


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    needs_user: bool = False
    rule: str = ""          # set when a standing rule allowed it, so audit can cite it
    risk: str = ""
    target: Optional[str] = None
    # The rule an "always" answer would create, or "" when this call cannot carry one.
    # Surfaces read it to decide whether to OFFER "always" at all — an "always" button
    # that quietly degrades to allow-once is a lie told in the user's own interface.
    rule_offer: str = ""
    # Set by the loop, not the gate: the model's tool-call id. It is the idempotency key
    # for a parked approval, so a reconnecting surface finds the same question rather
    # than asking a second time.
    call_id: str = ""


@dataclass
class Gate:
    cwd: Path
    mode: Mode = Mode.PROJECT
    roots: list = field(default_factory=list)          # extra writable dirs
    allowed_commands: list = field(default_factory=list)
    # (tool, target) pairs approved for the rest of this run, and tools the user
    # rejected with "never ask again".
    session_rules: set = field(default_factory=set)
    session_denied: set = field(default_factory=set)
    risk_overrides: Optional[RiskOverrides] = None
    origin_lookup: Optional[Callable[[], str]] = None
    # The user's OWN words in this conversation (every user-role turn, including mid-run
    # steering), as one string. Set by the loop, never by a tool. It lets the gate recognise a
    # USER-DIRECTED external action — the user typed the very target the tool is about to act
    # on — and not ask again for what was just asked for. Read live, never cached, so a number
    # typed while the run is in flight counts and a target the model invented never does.
    user_text_lookup: Optional[Callable[[], str]] = None

    def __post_init__(self) -> None:
        self.cwd = Path(self.cwd).expanduser().resolve()

    # -- user-directed targets --------------------------------------------
    def user_named_target(self, tool_name: str, target: Optional[str]) -> bool:
        """True when the user themself named `target` for this tool in their own message.

        Only tools in USER_DIRECTED_BY_NAME qualify, and only by a comparison the model cannot
        game: for the phone line, the digits the user typed (any separators) must be the
        number being dialled. "Call Kobe 650-944-9576" authorises +16509449576 and nothing
        else; a number that appears only in the model's own reasoning still asks.
        """
        if not target or tool_name not in USER_DIRECTED_BY_NAME or self.user_text_lookup is None:
            return False
        try:
            text = str(self.user_text_lookup() or "")
        except Exception:
            return False
        if not text:
            return False
        if USER_DIRECTED_BY_NAME[tool_name] == "phone":
            want = re.sub(r"\D", "", str(target))
            if len(want) < 7:
                return False
            for run in re.findall(r"\+?\d[\d\s().\-]{5,}\d", text):
                got = re.sub(r"\D", "", run)
                if len(got) >= 7 and (want == got or want.endswith(got) or got.endswith(want)):
                    return True
        return False

    # -- the decision -------------------------------------------------------
    def evaluate(self, tool_name: str, args: dict, tool: Any = None) -> Decision:
        args = args or {}
        risk = classify(tool_name, tool, self.risk_overrides)
        d = lambda ok, why, **kw: Decision(ok, why, risk=risk.value, **kw)   # noqa: E731

        if not is_consequential(risk):
            return d(True, "read")

        if self.mode in READ_ONLY_MODES:
            return d(False, "%s mode is read-only" % self.mode.value)

        if self.mode is Mode.TEST:
            if risk is RiskClass.EXEC:
                command = str(args.get("command") or args.get("cmd") or "")
                if self._command_allowed(command):
                    return d(True, "test mode: detected verification command")
                return d(False, "test mode only runs the proposed verification command")
            # Reads returned above. Everything else is a write or an external
            # side effect, neither of which Test is authorized to perform.
            return d(False, "test mode is read-only except for verification")

        # The local desktop hand is part of the product surface, like typing in Collie's own
        # composer. Its master Settings switch is enforced inside native.py; duplicating that as a
        # per-click/per-type approval made ordinary commands stall behind a second conversation.
        # Read-only modes above still fence every consequential action, while browser/account/cloud
        # tools continue through the external-action policy below.
        if tool_name in {"desktop_click", "desktop_type", "desktop_launch",
                          "desktop_focus", "desktop_menu"}:
            return d(True, "trusted local desktop control")

        if tool_name in self.session_denied:
            return d(False, "denied for this run")

        # Path scoping applies in every mode that is not read-only, including auto:
        # a mis-resolved path is an accident, and an accident does not care about mode.
        if risk is RiskClass.WRITE_LOCAL:
            path = args.get("path")
            if path is not None and not self._under_root(str(path)):
                if self.mode is Mode.AUTO:
                    return d(False, "path is outside the writable roots: %s" % path)
                return d(False, "writes outside %s need approval" % self.cwd,
                         needs_user=True, target=str(path))

        if self.mode is Mode.AUTO:
            return d(True, "auto mode")

        if risk is RiskClass.EXEC:
            command = str(args.get("command") or args.get("cmd") or "")
            if self._command_allowed(command):
                return d(True, "command on allowlist")
            # `project` mode covers ordinary local coding commands, not an implicit network or
            # destructive-shell grant. Those capabilities remain available after a real approval.
            if self.mode is Mode.PROJECT:
                if tool_name == "bash":
                    policy = _project_shell_needs_approval(command)
                    policy = policy or self._shell_path_escape(command)
                    if policy:
                        return d(False, policy, needs_user=True, target=command[:240])
                return d(True, "project mode: commands run in %s" % self.cwd)
            return d(False, "running commands needs approval", needs_user=True)

        if risk is RiskClass.WRITE_LOCAL:
            if self.mode is Mode.PROJECT:
                return d(True, "project mode: writes inside %s" % self.cwd)
            return d(False, "writing files needs approval", needs_user=True)

        # -- external -------------------------------------------------------
        target = target_for(tool_name, args, self.origin_lookup)
        if target and (tool_name, target) in self.session_rules:
            rule = "%s → %s" % (tool_name, target)
            return d(True, "allowed by rule: " + rule, rule=rule, target=target)
        if self.user_named_target(tool_name, target):
            rule = "%s → %s (named by the user)" % (tool_name, target)
            return d(True, "user-directed: the user named this target in their own message",
                     rule=rule, target=target)
        return d(False, "acts outside this machine", needs_user=True, target=target,
                 rule_offer=self.standing_rule_offer(tool_name, target) or "")

    # -- outcomes -----------------------------------------------------------
    def apply_outcome(self, outcome: "Outcome", tool_name: str, target: Optional[str]) -> None:
        """Record what the human chose, so the rest of the run honours it."""
        if outcome is Outcome.ALLOW_ALWAYS:
            # A rule needs something concrete to be pinned to. Without a target
            # "always" would mean "always, anywhere" — which is what we refuse to
            # let anyone express. No target, no rule: it degrades to allow-once.
            if target and tool_name not in NO_STANDING_RULE:
                self.session_rules.add((tool_name, target))
        elif outcome is Outcome.REJECT_ALWAYS:
            self.session_denied.add(tool_name)

    def standing_rule_offer(self, tool_name: str, target: Optional[str]) -> Optional[str]:
        """The rule an "always" answer would create, or None when the call cannot
        carry one (so the surface hides the option instead of offering a lie)."""
        if not target or tool_name in NO_STANDING_RULE:
            return None
        return "%s → %s" % (tool_name, target)

    # -- helpers ------------------------------------------------------------
    def _writable_roots(self) -> list:
        out = [self.cwd]
        for r in self.roots or []:
            try:
                out.append(Path(r).expanduser().resolve())
            except (OSError, ValueError):
                continue
        return out

    def _under_root(self, path: str) -> bool:
        try:
            p = Path(path).expanduser()
            cand = p.resolve() if p.is_absolute() else (self.cwd / p).resolve()
        except (OSError, ValueError):
            return False
        for root in self._writable_roots():
            try:
                cand.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def _shell_path_escape(self, command: str) -> str:
        """Conservatively identify shell arguments that resolve beyond the granted roots."""
        if re.search(r"(?:\$\{?(?:HOME|USERPROFILE)\}?|%USERPROFILE%|~[/\\])", command, re.I):
            return "command references a user directory outside the workspace"
        try:
            argv = shlex.split(command, posix=os.name != "nt")
        except ValueError:
            return "command quoting could not be safely inspected"
        command_pos = True
        for raw in argv:
            if raw in (";", "&&", "||", "|"):
                command_pos = True
                continue
            token = raw.strip("'\";,()")
            if command_pos:                 # an absolute executable path is not file authority
                command_pos = False
                continue
            if token in ("/dev/null", "NUL", "nul") or token.startswith("-"):
                continue
            if "=" in token and token.split("=", 1)[0].startswith("-"):
                token = token.split("=", 1)[1]
            token = token.lstrip("><")
            if not token or "://" in token:
                continue
            p = Path(token).expanduser()
            if not p.is_absolute() and not token.startswith((".." + os.sep, "../", "..\\")):
                continue
            try:
                candidate = p.resolve() if p.is_absolute() else (self.cwd / p).resolve()
            except (OSError, ValueError):
                return "command path could not be safely resolved"
            allowed = False
            for root in self._writable_roots():
                try:
                    candidate.relative_to(root)
                    allowed = True
                    break
                except ValueError:
                    continue
            if not allowed:
                return "shell path is outside the granted workspace: %s" % token[:160]
        return ""

    def _command_allowed(self, command: str) -> bool:
        """Two stages, and both are load-bearing. Reject anything carrying shell
        operators outright, then require an entry's tokens to be an exact argv PREFIX
        of the command's — so `git status` matches `git status -s`, but never
        `git statusfoo` and never a bare `git`."""
        if not command or _has_shell_operators(command):
            return False
        try:
            argv = shlex.split(command)
        except ValueError:
            return False        # unbalanced quotes: not something to auto-run
        if not argv:
            return False
        for allowed in self.allowed_commands or []:
            try:
                prefix = shlex.split(str(allowed))
            except ValueError:
                continue
            if prefix and argv[:len(prefix)] == prefix:
                return True
        return False


def mode_from_env(default: Mode = Mode.PROJECT) -> Mode:
    """COLLIE_MODE=plan|review|test|project|interactive|auto. An unrecognised value falls back to
    the default rather than failing the run — but never silently to something laxer."""
    raw = (os.environ.get("COLLIE_MODE") or "").strip().lower()
    try:
        return Mode(raw) if raw else default
    except ValueError:
        return default
