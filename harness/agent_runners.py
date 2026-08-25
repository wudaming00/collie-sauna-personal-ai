"""Durable adapters for running external coding-agent harnesses.

This module is deliberately separate from :mod:`harness.adapters`: benchmark
adapters are one-shot measurement shims, while an ``AgentRunner`` is a Mission
primitive with resumable state, cancellation, and conservative recovery rules.

The first implementation wraps Codex's documented non-interactive JSONL
interface.  Prompts are sent on stdin (never exposed in the process argv), the
workspace is bounded by Codex's ``workspace-write`` sandbox, and an interrupted
turn that may have changed files is *not* silently replayed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Mapping, Protocol, Sequence

from . import plat
from .redact import redact, redact_obj
from .verification import workspace_snapshot


_THREAD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_TOKEN_SECRET = re.compile(
    r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+|\b(?:sk|sess)-[A-Za-z0-9_-]{12,}\b"
)
_USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
_CODEX_STATE_VERSION = 2
_CODEX_PROTOCOL_VERSION = "codex-exec-jsonl-v1"
_CODEX_REQUIRED_FLAGS = ("--ignore-user-config", "--ignore-rules")
_PROCESS_OUTPUT_LIMIT = 4_000_000
_STATE_STORAGE_LIMIT = 4_500_000
_WORKER_ENV_ALLOWLIST = {
    "APPDATA", "COMSPEC", "HOMEDRIVE", "HOMEPATH", "LANG", "LOCALAPPDATA",
    "LOGNAME", "PATH", "PATHEXT", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "PROGRAMW6432", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "USER", "WINDIR",
}


def codex_worker_credential_state() -> dict[str, Any]:
    """Describe only the credential a hardened Codex worker can actually see.

    The worker gets no ambient API key or endpoint override and loads one explicit
    Codex login store.  A login store containing ``OPENAI_API_KEY`` is rejected:
    removing the environment variable is not enough when the CLI could still
    select paid API billing from its own credential file.
    """
    try:
        from .codex_oauth import _auth_path, _token_and_account
        path = os.path.realpath(os.path.abspath(os.path.expanduser(_auth_path())))
    except Exception as exc:
        return {"admitted": False, "reason": "Codex login-store path is unavailable",
                "fingerprint": hashlib.sha256(
                    ("codex-unavailable:" + type(exc).__name__).encode()).hexdigest(),
                "mtime_ns": 0}
    material = ["codex-worker-v1", path]
    try:
        st = os.stat(path)
        if st.st_size <= 0 or st.st_size > 2_000_000:
            raise ValueError("Codex login store has an invalid size")
        with open(path, "rb") as fh:
            raw = fh.read()
        doc = json.loads(raw.decode("utf-8"))
        if not isinstance(doc, dict):
            raise ValueError("Codex login store is not an object")
        material.extend([str(st.st_size), str(st.st_mtime_ns),
                         hashlib.sha256(raw).hexdigest()])
        api_key = doc.get("OPENAI_API_KEY")
        if isinstance(api_key, str) and api_key.strip():
            reason = "Codex login store contains a paid API key"
            admitted = False
            account = ""
        else:
            access, account, _claims = _token_and_account(doc)
            admitted = bool(access and account)
            reason = ("ChatGPT subscription login is present and paid API credentials "
                      "are absent" if admitted else
                      "Codex login store has no account-scoped ChatGPT token")
        account_fingerprint = (hashlib.sha256(str(account).encode(
            "utf-8", "replace")).hexdigest() if account else "")
        return {
            "admitted": admitted, "reason": reason,
            "auth_kind": "chatgpt-subscription" if admitted else "unavailable",
            "api_key_absent": not bool(isinstance(api_key, str) and api_key.strip()),
            "endpoint_overrides_ignored": True,
            "account_fingerprint": account_fingerprint,
            "credential_path": path,
            "fingerprint": hashlib.sha256("\0".join(material).encode()).hexdigest(),
            "mtime_ns": int(st.st_mtime_ns),
        }
    except Exception as exc:
        material.append(type(exc).__name__)
        return {
            "admitted": False,
            "reason": "Codex login store is unavailable or malformed",
            "auth_kind": "unavailable", "api_key_absent": False,
            "endpoint_overrides_ignored": True, "account_fingerprint": "",
            "credential_path": path,
            "fingerprint": hashlib.sha256("\0".join(material).encode()).hexdigest(),
            "mtime_ns": 0,
        }


def codex_worker_environment(private_home: str) -> tuple[dict[str, str], dict[str, Any]]:
    """Build a minimal non-secret environment and its billing/auth receipt."""
    home = os.path.realpath(os.path.abspath(os.path.expanduser(private_home)))
    created = not os.path.isdir(home)
    os.makedirs(home, exist_ok=True)
    if created:
        try:
            os.chmod(home, 0o700)
        except OSError:
            pass
    evidence = codex_worker_credential_state()
    env = {name: value for name, value in os.environ.items()
           if name.upper() in _WORKER_ENV_ALLOWLIST or name.upper().startswith("LC_")}
    env["HOME"] = home
    env["USERPROFILE"] = home
    credential_path = str(evidence.get("credential_path") or "")
    if credential_path:
        env["CODEX_HOME"] = os.path.dirname(credential_path)
    # No OPENAI_API_KEY, CODEX_BASE_URL, proxy, provider, Slack, audit, hook, or
    # COLLIE_* variable crosses this boundary.  The CLI flags below separately
    # prevent ambient user config/rules from reintroducing executable behavior.
    return env, evidence


@dataclass(frozen=True)
class AgentBackendStatus:
    """Read-only admission snapshot for optional external agent transports.

    Availability is intentionally separate from Brain selection.  A runnable
    binary is not enough authority to launch it: Mission/TaskTree must still own
    workspace, budget, cancellation, and recovery before an external runner is
    dispatched.
    """

    name: str
    available: bool
    provider: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "available": self.available,
                "provider": self.provider, "reason": self.reason}


def probe_agent_backends(*, which: Callable[[str], str | None] = shutil.which,
                         auth_probe: Callable[[str], str] | None = None
                         ) -> tuple[AgentBackendStatus, ...]:
    """Cheap, no-inference snapshot used by delegation planners.

    ``native`` means Collie's provider/tool loop and is always structurally
    available; its selected provider is admitted separately by the brain router.
    Codex/Claude entries report only whether their safe adapter prerequisites
    exist.  They do not imply permission to bypass the Mission container.
    """
    strict_codex_auth = auth_probe is None
    if auth_probe is None:
        from .catalog import probe_auth
        auth_probe = probe_auth

    def status(name: str, executable: str, provider: str) -> AgentBackendStatus:
        binary = which(executable)
        auth = str(auth_probe(provider) or "unknown")
        available = bool(binary and auth == "ok")
        if not binary:
            reason = "%s executable is not installed" % executable
        elif auth != "ok":
            reason = "%s auth is %s" % (provider, auth)
        else:
            reason = "adapter prerequisites are available; Mission authority is still required"
        return AgentBackendStatus(name, available, provider, reason)

    codex = status("codex", "codex", "codex-oauth")
    if codex.available and strict_codex_auth:
        evidence = codex_worker_credential_state()
        if not evidence.get("admitted"):
            codex = AgentBackendStatus(
                "codex", False, "codex-oauth", str(evidence.get("reason") or
                "the hardened worker cannot prove subscription-only credentials"))
    return (
        AgentBackendStatus(
            "native", True, "auto",
            "Collie's provider/tool loop; provider auth is checked per decision"),
        codex,
        AgentBackendStatus(
            "claude-code", False, "claude-agent-sdk",
            "Claude is available only as a Collie model transport; no bounded "
            "resumable Claude Code workspace runner is implemented"),
    )


# The target agent must not get even one instruction byte until the parent has
# installed its process-tree owner and published the process to cancel_current.
# A tiny trusted Python gate is used instead of launching Codex directly: the
# gate blocks in ``stdin.read()``; only after ownership/registration succeeds do
# we send the target argv and private prompt.  The target inherits the gate's
# POSIX process group or Windows Job Object.
_START_GATE_SCRIPT = r"""
import json
import subprocess
import sys

try:
    request = json.loads(sys.stdin.read())
    argv = request.get("argv")
    prompt = request.get("stdin_text")
    if (not isinstance(argv, list) or not argv or
            not all(isinstance(item, str) and item for item in argv) or
            not isinstance(prompt, str)):
        raise ValueError("invalid gated process request")
    kw = ({"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
          if sys.platform == "win32" else {})
    child = subprocess.Popen(
        argv, stdin=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace", **kw)
    child.communicate(input=prompt)
    raise SystemExit(child.returncode if child.returncode is not None else 125)
except SystemExit:
    raise
except BaseException as exc:
    sys.stderr.write("gated agent launch failed: %s: %s\n" %
                     (type(exc).__name__, exc))
    raise SystemExit(125)
"""


@dataclass(frozen=True)
class RunnerEvent:
    """One canonical, cursor-addressable event emitted by an agent runner."""

    cursor: int
    type: str
    payload: dict[str, Any]
    at: float

    def to_dict(self) -> dict[str, Any]:
        return {"cursor": self.cursor, "type": self.type,
                "payload": self.payload, "at": self.at}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RunnerEvent":
        return cls(cursor=max(0, int(value.get("cursor", 0))),
                   type=str(value.get("type") or "unknown"),
                   payload=dict(value.get("payload") or {}),
                   at=float(value.get("at") or 0.0))


@dataclass(frozen=True)
class RunnerSnapshot:
    """Serializable state required to inspect or resume an external harness.

    ``events`` is a bounded tail, while ``cursor`` remains monotonic even after
    old events are compacted.  ``usage`` is cumulative across all turns in the
    thread.  The terminal fields describe the most recent invocation.
    """

    runner: str
    workspace: str
    thread_id: str = ""
    cursor: int = 0
    events: tuple[RunnerEvent, ...] = ()
    usage: dict[str, int] = field(default_factory=dict)
    settled: bool = False
    exit_code: int | None = None
    error: str = ""
    recovery_required: bool = False
    mutated: bool = False
    mutation_check_complete: bool = False
    workspace_digest: str = ""
    final_output: str = ""
    timed_out: bool = False
    cancelled: bool = False
    invocation: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "runner": self.runner,
            "workspace": self.workspace,
            "thread_id": self.thread_id,
            "cursor": self.cursor,
            "events": [event.to_dict() for event in self.events],
            "usage": dict(self.usage),
            "settled": self.settled,
            "exit_code": self.exit_code,
            "error": self.error,
            "recovery_required": self.recovery_required,
            "mutated": self.mutated,
            "mutation_check_complete": self.mutation_check_complete,
            "workspace_digest": self.workspace_digest,
            "final_output": self.final_output,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "invocation": self.invocation,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RunnerSnapshot":
        usage = {}
        for key, amount in dict(value.get("usage") or {}).items():
            if isinstance(amount, (int, float)) and amount >= 0:
                usage[str(key)] = int(amount)
        exit_code = value.get("exit_code")
        return cls(
            runner=str(value.get("runner") or ""),
            workspace=os.path.realpath(os.path.abspath(str(value.get("workspace") or "."))),
            thread_id=str(value.get("thread_id") or ""),
            cursor=max(0, int(value.get("cursor") or 0)),
            events=tuple(RunnerEvent.from_dict(item)
                         for item in (value.get("events") or []) if isinstance(item, dict)),
            usage=usage,
            settled=bool(value.get("settled")),
            exit_code=int(exit_code) if isinstance(exit_code, (int, float)) else None,
            error=str(value.get("error") or ""),
            recovery_required=bool(value.get("recovery_required")),
            mutated=bool(value.get("mutated")),
            mutation_check_complete=bool(value.get("mutation_check_complete")),
            workspace_digest=str(value.get("workspace_digest") or ""),
            final_output=str(value.get("final_output") or ""),
            timed_out=bool(value.get("timed_out")),
            cancelled=bool(value.get("cancelled")),
            invocation=max(0, int(value.get("invocation") or 0)),
            started_at=float(value.get("started_at") or 0.0),
            finished_at=float(value.get("finished_at") or 0.0),
        )


@dataclass(frozen=True)
class ProcessOutcome:
    """Result returned by an injectable process transport."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    cancelled: bool = False


class ProcessRunner(Protocol):
    def run(self, argv: Sequence[str], *, cwd: str, stdin_text: str,
            timeout_s: float, on_process: Callable[[Any], bool | None],
            environment: Mapping[str, str] | None = None, job_name: str = "",
            process_marker: str = "", on_settled=None) -> ProcessOutcome:
        ...


class AgentRunner(Protocol):
    def start(self, prompt: str, workspace: str, *, timeout_s: float | None = None
              ) -> RunnerSnapshot:
        ...

    def resume(self, snapshot: RunnerSnapshot, prompt: str, *,
               timeout_s: float | None = None) -> RunnerSnapshot:
        ...

    def cancel_current(self) -> bool:
        ...


class RecoveryRequiredError(RuntimeError):
    """Raised when replaying a possibly half-applied mutation would be unsafe."""


def _process_wait(proc: Any, timeout_s: float) -> bool:
    """Confirm the direct process exited; deliberately reject an unknown state."""
    wait = getattr(proc, "wait", None)
    if not callable(wait):
        # Injectable process doubles have no OS process behind them.  Production
        # Popen objects always expose wait(), so this compatibility path cannot
        # weaken the real process-tree guarantee.
        return True
    try:
        wait(timeout=max(0.0, float(timeout_s)))
        return True
    except Exception:
        return False


def _terminate_posix_group(proc: Any, timeout_s: float) -> bool:
    """Kill and then prove extinction of the group captured before launch."""
    pgid = int(getattr(proc, "_collie_process_group", 0) or 0)
    if pgid <= 1:
        # Custom process transports used by embedders/tests may not establish a
        # group.  The production transport always records one on POSIX.
        plat.kill_tree(proc)
        return _process_wait(proc, timeout_s)
    try:
        os.killpg(pgid, getattr(signal, "SIGKILL", 9))
    except ProcessLookupError:
        setattr(proc, "_collie_tree_extinct", True)
        return True
    except OSError:
        return False
    _process_wait(proc, min(1.0, timeout_s))
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            setattr(proc, "_collie_tree_extinct", True)
            return True
        except PermissionError:
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(.01)


def _terminate_owned_process(proc: Any, timeout_s: float = 5.0) -> bool:
    """Terminate an owned agent process tree and return only after extinction.

    ``kill`` delivery is not completion evidence.  Windows asks the Job Object
    for ``ActiveProcesses == 0``; POSIX polls the dedicated process group until
    ``killpg(..., 0)`` reports ESRCH.  The per-process lock serializes a user
    cancellation with transport timeout/finally cleanup.
    """
    lock = getattr(proc, "_collie_tree_lock", None)
    if lock is None:
        lock = threading.RLock()
        setattr(proc, "_collie_tree_lock", lock)
    with lock:
        if bool(getattr(proc, "_collie_tree_extinct", False)):
            return True
        owner = getattr(proc, "_collie_kill_job", None)
        if owner is not None:
            confirmed = False
            try:
                terminate_and_wait = getattr(owner, "terminate_and_wait", None)
                if callable(terminate_and_wait):
                    confirmed = bool(terminate_and_wait(timeout_s=timeout_s))
                else:
                    # Compatibility for injected owners.  A production Windows
                    # Job exposes terminate_and_wait and never takes this path.
                    delivered = bool(owner.terminate())
                    wait_extinct = getattr(owner, "wait_extinct", None)
                    confirmed = bool(wait_extinct(timeout_s=timeout_s)) \
                        if callable(wait_extinct) else bool(
                            delivered and _process_wait(proc, timeout_s))
            except Exception:
                confirmed = False
            setattr(proc, "_collie_tree_extinct", confirmed)
            return confirmed
        if not plat.is_windows():
            return _terminate_posix_group(proc, timeout_s)
        # Production refuses to run without a Job.  This fallback exists only
        # for injected ProcessRunner implementations and still confirms the
        # direct process rather than reporting success immediately after kill.
        plat.kill_tree(proc)
        confirmed = _process_wait(proc, timeout_s)
        setattr(proc, "_collie_tree_extinct", confirmed)
        return confirmed


class SubprocessRunner:
    """Killable, shell-free subprocess transport used in production."""

    def run(self, argv: Sequence[str], *, cwd: str, stdin_text: str,
            timeout_s: float, on_process: Callable[[Any], bool | None],
            environment: Mapping[str, str] | None = None, job_name: str = "",
            process_marker: str = "", on_settled=None) -> ProcessOutcome:
        target_argv = [str(item) for item in argv]
        if not target_argv or not all(target_argv):
            raise ValueError("agent argv must contain non-empty strings")
        group_kwargs = plat.new_group_kwargs()
        gate_argv = [sys.executable, "-I", "-c", _START_GATE_SCRIPT]
        if process_marker:
            gate_argv.append(str(process_marker))
        proc = subprocess.Popen(
            gate_argv,
            cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            env=(dict(environment) if environment is not None else None),
            **group_kwargs, **plat.no_window_kwargs())
        proc._collie_tree_lock = threading.RLock()
        if not plat.is_windows() and group_kwargs.get("start_new_session"):
            # Capture the group while the trusted gate leader is alive.  Once it
            # exits, getpgid(proc.pid) cannot rediscover background descendants.
            proc._collie_process_group = int(proc.pid)
        owner = None
        try:
            owner = plat.attach_kill_on_close_job(proc, name=job_name or None)
            if owner is not None:
                proc._collie_kill_job = owner
        except Exception:
            # The trusted gate has not received a target request, so killing its
            # direct process is sufficient even if Job assignment itself failed.
            plat.kill_tree(proc)
            _process_wait(proc, 5.0)
            raise RuntimeError("could not establish Codex process-tree ownership")
        outcome = None
        raised = None
        try:
            # Registration is the start latch.  Returning False means a cancel
            # arrived during launch; never send the request, so Codex never starts.
            if on_process(proc) is False:
                if not _terminate_owned_process(proc):
                    raise RuntimeError(
                        "Codex start was cancelled but process-tree extinction "
                        "could not be confirmed")
                outcome = ProcessOutcome(exit_code=getattr(proc, "returncode", None),
                                         cancelled=True)
            else:
                request = json.dumps(
                    {"argv": target_argv, "stdin_text": stdin_text},
                    ensure_ascii=True, separators=(",", ":"))
                try:
                    stdout, stderr = proc.communicate(input=request, timeout=timeout_s)
                    stdout = stdout or ""
                    stderr = stderr or ""
                    if len(stdout) > _PROCESS_OUTPUT_LIMIT:
                        stdout = ("codex-output-limit-exceeded\n" +
                                  stdout[-_PROCESS_OUTPUT_LIMIT:])
                    stderr = stderr[-64_000:]
                    outcome = ProcessOutcome(
                        stdout=stdout, stderr=stderr,
                        exit_code=proc.returncode)
                except subprocess.TimeoutExpired as exc:
                    confirmed = _terminate_owned_process(proc)
                    stdout = _text(exc.stdout)
                    stderr = _text(exc.stderr)
                    try:
                        tail_out, tail_err = proc.communicate(timeout=5)
                        # A second communicate() returns the complete buffered
                        # stream on supported Python platforms, not necessarily
                        # just a suffix.  Do not duplicate JSONL events/usage.
                        stdout = tail_out or stdout
                        stderr = tail_err or stderr
                    except Exception:
                        pass
                    if not confirmed:
                        raise RuntimeError(
                            "Codex timed out and process-tree extinction could "
                            "not be confirmed")
                    outcome = ProcessOutcome(
                        stdout=stdout, stderr=stderr,
                        exit_code=proc.returncode, timed_out=True)
        except BaseException as exc:
            raised = exc
        finally:
            # A successful CLI can still leave a background writer.  Reap the
            # complete owned tree before its JSONL result becomes settled state.
            confirmed = _terminate_owned_process(proc)
            if callable(on_settled):
                try:
                    on_settled(proc, bool(confirmed))
                except Exception as exc:
                    if raised is None:
                        raised = RuntimeError(
                            "could not retire durable Codex process ownership: %s: %s" %
                            (type(exc).__name__, exc))
            try:
                if owner is not None:
                    owner.close()
            finally:
                if not confirmed and raised is None:
                    raised = RuntimeError(
                        "Codex process-tree extinction could not be confirmed")
        if raised is not None:
            raise raised
        assert outcome is not None
        return outcome


class CodexExecRunner:
    """Resumable Codex CLI runner backed by ``codex exec --json``.

    The runner owns one active child at a time.  A caller persists the returned
    :class:`RunnerSnapshot` in Mission state and passes it back to ``resume``.
    """

    key = "codex-exec"

    def __init__(self, *, executable: str = "codex", model: str = "",
                 process_runner: ProcessRunner | None = None,
                 snapshotter: Callable[[str], dict[str, Any]] = workspace_snapshot,
                 default_timeout_s: float = 900.0, max_events: int = 2_000,
                 max_event_chars: int = 128_000,
                 environment: Mapping[str, str] | None = None,
                 job_name: str = "", process_marker: str = "",
                 on_process_owned=None, on_process_settled=None):
        if default_timeout_s <= 0:
            raise ValueError("default_timeout_s must be positive")
        self.executable = executable
        self.model = model
        self.process_runner = process_runner or SubprocessRunner()
        self.snapshotter = snapshotter
        self.default_timeout_s = float(default_timeout_s)
        self.max_events = max(1, int(max_events))
        self.max_event_chars = max(1_024, int(max_event_chars))
        self.environment = dict(environment) if environment is not None else None
        self.job_name = str(job_name or "")
        self.process_marker = str(process_marker or "")
        self.on_process_owned = on_process_owned
        self.on_process_settled = on_process_settled
        self._cli_identity_cache: dict[str, Any] | None = None
        self._redaction_vault: dict[str, str] = {}
        self._run_lock = threading.Lock()
        self._active_lock = threading.Lock()
        self._active_condition = threading.Condition(self._active_lock)
        self._active_process: Any = None
        self._starting = False
        self._cancel_requested = False

    def start(self, prompt: str, workspace: str, *, timeout_s: float | None = None
              ) -> RunnerSnapshot:
        root = _workspace(workspace)
        argv = [self._executable(), "exec", *_CODEX_REQUIRED_FLAGS,
                "--json", "--sandbox", "workspace-write", "--cd", root]
        if self.model:
            argv += ["--model", self.model]
        argv.append("-")
        return self._invoke(None, argv, prompt, root, timeout_s)

    def resume(self, snapshot: RunnerSnapshot, prompt: str, *,
               timeout_s: float | None = None) -> RunnerSnapshot:
        if snapshot.runner != self.key:
            raise ValueError("snapshot belongs to %s, not %s" % (snapshot.runner, self.key))
        if snapshot.recovery_required:
            raise RecoveryRequiredError(
                "the previous Codex turn may have left a partial workspace mutation; "
                "inspect or roll back the workspace before resuming")
        root = _workspace(snapshot.workspace)
        if root != snapshot.workspace:
            raise ValueError("snapshot workspace is not canonical")
        if not snapshot.thread_id or not _THREAD_ID.fullmatch(snapshot.thread_id):
            raise ValueError("snapshot has no safe Codex thread id")
        # `resume` does not expose the top-level --sandbox flag.  An explicit
        # config override keeps the resumed turn at the same workspace-write
        # boundary even if the user's global default later changes.
        argv = [self._executable(), "exec", "resume", *_CODEX_REQUIRED_FLAGS,
                "--json", "-c",
                'sandbox_mode="workspace-write"']
        if self.model:
            argv += ["--model", self.model]
        argv += [snapshot.thread_id, "-"]
        return self._invoke(snapshot, argv, prompt, root, timeout_s)

    def cancel_current(self) -> bool:
        deadline = time.monotonic() + 5.0
        with self._active_condition:
            if self._active_process is None and not self._starting:
                return False
            self._cancel_requested = True
            # Cancellation may win after the invocation lock but before Popen or
            # registration.  Wait for the trusted gate to become owned; register
            # will see _cancel_requested and refuse to release the real target.
            while self._active_process is None and self._starting:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._active_condition.wait(remaining)
            proc = self._active_process
            if proc is None:
                # The process transport failed before creating a child.  The
                # requested invocation is gone and no target can start later.
                return True
        return _terminate_owned_process(
            proc, timeout_s=max(0.0, deadline - time.monotonic()))

    def _executable(self) -> str:
        # Resolve npm/installer shims now, but permit an explicit absolute path.
        # Fake process runners intentionally do not need a real CLI on PATH.
        if not isinstance(self.process_runner, SubprocessRunner):
            return self.executable
        resolved = shutil.which(self.executable)
        if not resolved:
            raise FileNotFoundError("Codex CLI is not installed or not on PATH")
        self._probe_cli(resolved)
        return resolved

    def state_identity(self) -> dict[str, Any]:
        """Pin the executable and JSONL contract used by resumable state."""
        if not isinstance(self.process_runner, SubprocessRunner):
            return {"protocol": _CODEX_PROTOCOL_VERSION,
                    "runner_type": type(self.process_runner).__name__,
                    "executable": str(self.executable)}
        resolved = shutil.which(self.executable)
        if not resolved:
            raise FileNotFoundError("Codex CLI is not installed or not on PATH")
        return dict(self._probe_cli(resolved))

    def _probe_cli(self, resolved: str) -> dict[str, Any]:
        if self._cli_identity_cache is not None:
            return self._cli_identity_cache
        environment = self.environment
        cleanup_home = ""
        if environment is None:
            cleanup_home = tempfile.mkdtemp(prefix="collie-codex-probe-")
            environment, _evidence = codex_worker_environment(cleanup_home)
        try:
            help_run = subprocess.run(
                [resolved, "exec", "--help"], stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", timeout=10, check=False,
                env=dict(environment), **plat.no_window_kwargs())
            help_text = str(help_run.stdout or "")
            if help_run.returncode != 0 or any(
                    flag not in help_text for flag in _CODEX_REQUIRED_FLAGS):
                raise RuntimeError(
                    "installed Codex CLI cannot prove --ignore-user-config and "
                    "--ignore-rules isolation")
            resume_run = subprocess.run(
                [resolved, "exec", "resume", "--help"], stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", timeout=10, check=False,
                env=dict(environment), **plat.no_window_kwargs())
            resume_help = str(resume_run.stdout or "")
            if resume_run.returncode != 0 or any(
                    flag not in resume_help for flag in _CODEX_REQUIRED_FLAGS):
                raise RuntimeError(
                    "installed Codex CLI resume path cannot prove config/rules isolation")
            version_run = subprocess.run(
                [resolved, "--version"], stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", timeout=10, check=False,
                env=dict(environment), **plat.no_window_kwargs())
            version = " ".join(str(version_run.stdout or "").split())[:200]
            if version_run.returncode != 0 or not version:
                raise RuntimeError("installed Codex CLI version is unavailable")
            stat = os.stat(resolved)
            self._cli_identity_cache = {
                "protocol": _CODEX_PROTOCOL_VERSION,
                "path": os.path.realpath(resolved), "version": version,
                "size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns),
                "isolation_flags": list(_CODEX_REQUIRED_FLAGS),
            }
            return self._cli_identity_cache
        finally:
            if cleanup_home:
                shutil.rmtree(cleanup_home, ignore_errors=True)

    def _invoke(self, prior: RunnerSnapshot | None, argv: list[str], prompt: str,
                workspace: str, timeout_s: float | None) -> RunnerSnapshot:
        prompt = _prompt(prompt)
        timeout = self.default_timeout_s if timeout_s is None else float(timeout_s)
        if timeout <= 0:
            raise ValueError("timeout_s must be positive")
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("this Codex runner already has an active turn")

        with self._active_condition:
            self._cancel_requested = False
            self._starting = True
            self._active_process = None
            self._active_condition.notify_all()
        started_at = time.time()
        before = _snapshot(self.snapshotter, workspace)
        outcome: ProcessOutcome | None = None
        raised: Exception | None = None
        process_started = False
        invocation_home = ""
        invocation_environment = self.environment
        if isinstance(self.process_runner, SubprocessRunner) and invocation_environment is None:
            invocation_home = tempfile.mkdtemp(prefix="collie-codex-worker-")
            invocation_environment, evidence = codex_worker_environment(invocation_home)
            if not evidence.get("admitted"):
                shutil.rmtree(invocation_home, ignore_errors=True)
                self._run_lock.release()
                with self._active_condition:
                    self._starting = False
                    self._active_condition.notify_all()
                raise RuntimeError(str(evidence.get("reason") or
                                       "Codex subscription credential is unavailable"))

        def register(proc: Any) -> bool:
            nonlocal process_started
            process_started = True
            if getattr(proc, "_collie_tree_lock", None) is None:
                proc._collie_tree_lock = threading.RLock()
            if callable(self.on_process_owned):
                try:
                    if self.on_process_owned(proc) is False:
                        with self._active_condition:
                            self._cancel_requested = True
                        return False
                except Exception:
                    with self._active_condition:
                        self._cancel_requested = True
                    return False
            with self._active_condition:
                self._active_process = proc
                self._starting = False
                allowed = not self._cancel_requested
                self._active_condition.notify_all()
                return allowed

        try:
            try:
                run_kwargs = dict(
                    cwd=workspace, stdin_text=prompt,
                    timeout_s=timeout, on_process=register)
                if isinstance(self.process_runner, SubprocessRunner):
                    run_kwargs.update({
                        "environment": invocation_environment,
                        "job_name": self.job_name,
                        "process_marker": self.process_marker,
                        "on_settled": self.on_process_settled,
                    })
                outcome = self.process_runner.run(tuple(argv), **run_kwargs)
                if not isinstance(outcome, ProcessOutcome):
                    raise TypeError("process runner must return ProcessOutcome")
            except Exception as exc:  # represented in state; Mission decides retry policy
                raised = exc
            with self._active_condition:
                cancelled = self._cancel_requested
                self._active_process = None
                self._starting = False
                self._active_condition.notify_all()
        finally:
            # A ProcessRunner exception before/after registration must not strand
            # cancellation waiters in the launch state.
            with self._active_condition:
                cancelled = self._cancel_requested
                self._active_process = None
                self._starting = False
                self._active_condition.notify_all()
            self._run_lock.release()
            if invocation_home:
                shutil.rmtree(invocation_home, ignore_errors=True)

        finished_at = time.time()
        after = _snapshot(self.snapshotter, workspace)
        prior_events = tuple(prior.events) if prior else ()
        prior_cursor = prior.cursor if prior else 0
        prior_usage = dict(prior.usage) if prior else {}
        thread_id = prior.thread_id if prior else ""
        invocation = (prior.invocation if prior else 0) + 1

        if raised is not None:
            mutated, complete = _mutation(before, after)
            recovery = mutated or (process_started and not complete)
            return RunnerSnapshot(
                runner=self.key, workspace=workspace, thread_id=thread_id,
                cursor=prior_cursor, events=prior_events, usage=prior_usage,
                settled=False, exit_code=None,
                error=_clean_error("%s: %s" % (type(raised).__name__, raised)),
                recovery_required=recovery, mutated=mutated,
                mutation_check_complete=complete,
                workspace_digest=str(after.get("tree_digest") or ""),
                final_output=prior.final_output if prior else "", timed_out=False,
                cancelled=cancelled, invocation=invocation,
                started_at=started_at, finished_at=finished_at)

        assert outcome is not None
        cancelled = bool(cancelled or outcome.cancelled)
        parsed, invalid_json = self._events(outcome.stdout, prior_cursor)
        all_events = (prior_events + tuple(parsed))[-self.max_events:]
        cursor = prior_cursor + len(parsed)
        new_thread_ids = [str(event.payload.get("thread_id") or "")
                          for event in parsed if event.type == "thread.started"]
        protocol_error = invalid_json
        for candidate in new_thread_ids:
            if not candidate or not _THREAD_ID.fullmatch(candidate):
                protocol_error = True
                continue
            if thread_id and thread_id != candidate:
                protocol_error = True
            else:
                thread_id = candidate
        if not thread_id:
            protocol_error = True

        terminal = ""
        terminal_error = ""
        terminal_events = 0
        for event in parsed:
            if event.type == "turn.completed":
                terminal_events += 1
                terminal = "completed"
            elif event.type == "turn.failed":
                terminal_events += 1
                terminal = "failed"
                terminal_error = _event_error(event.payload)
        if terminal_events != 1:
            protocol_error = True

        usage, terminal_usage_valid = _merge_usage(prior_usage, parsed)
        if terminal == "completed" and not terminal_usage_valid:
            protocol_error = True
        final_output = (_final_output(parsed) or
                        (prior.final_output if prior else ""))[:32_000]
        exit_code = outcome.exit_code
        timed_out = bool(outcome.timed_out)
        settled = (exit_code == 0 and terminal == "completed" and not timed_out
                   and not cancelled and not protocol_error and bool(thread_id))
        error = ""
        if timed_out:
            error = "Codex turn exceeded its %.1fs wall timeout" % timeout
        elif cancelled:
            error = "Codex turn was cancelled"
        elif exit_code not in (0, None) and terminal != "failed":
            # A failed process may be interrupted before it emits a terminal
            # JSONL event.  Keep the snapshot unsettled/protocol-invalid, but
            # preserve the (redacted) transport diagnosis for cooldown and
            # recovery decisions instead of replacing it with a generic error.
            error = (_clean_error(outcome.stderr) or
                     "Codex exited with status %s" % exit_code)
        elif protocol_error:
            error = "Codex emitted invalid or inconsistent JSONL state"
        elif terminal == "failed":
            error = terminal_error or "Codex reported turn.failed"
        elif exit_code is None:
            error = "Codex process exit status unavailable"
        elif exit_code not in (0, None):
            error = _clean_error(outcome.stderr) or "Codex exited with status %s" % exit_code
        elif terminal != "completed":
            error = _clean_error(outcome.stderr) or "Codex exited without turn.completed"

        mutated, complete = _mutation(before, after)
        abnormal = not settled
        recovery = abnormal and (mutated or (process_started and not complete))
        return RunnerSnapshot(
            runner=self.key, workspace=workspace, thread_id=thread_id,
            cursor=cursor, events=all_events, usage=usage, settled=settled,
            exit_code=exit_code, error=_clean_error(error),
            recovery_required=recovery, mutated=mutated,
            mutation_check_complete=complete,
            workspace_digest=str(after.get("tree_digest") or ""),
            final_output=final_output, timed_out=timed_out, cancelled=cancelled,
            invocation=invocation, started_at=started_at, finished_at=finished_at)

    def _events(self, stdout: str, prior_cursor: int) -> tuple[list[RunnerEvent], bool]:
        events = []
        invalid = len(stdout or "") > _PROCESS_OUTPUT_LIMIT
        if invalid:
            stdout = (stdout or "")[-_PROCESS_OUTPUT_LIMIT:]
        for raw in (stdout or "").splitlines():
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("event is not an object")
            except (ValueError, json.JSONDecodeError):
                invalid = True
                value = {"type": "protocol.invalid_json",
                         "preview": raw[: min(self.max_event_chars, 4_096)]}
            value = redact_obj(value, self._redaction_vault)
            payload = _bounded_payload(value, self.max_event_chars)
            event_type = str(value.get("type") or "unknown")
            events.append(RunnerEvent(cursor=prior_cursor + len(events) + 1,
                                      type=event_type, payload=payload, at=time.time()))
        return events, invalid


class MissionCodexCodeRunner:
    """Mission-aware adapter around :class:`CodexExecRunner`.

    TaskTree and Mission still own workspace authority and the resource lock.
    This adapter adds physical-request reservation, per-Mission cancellation,
    durable runner state, usage deltas, host verification, and recovery fences.
    """

    key = "codex-exec"

    def __init__(self, *, state_dir: str, runner_factory=None,
                 snapshotter: Callable[[str], dict[str, Any]] = workspace_snapshot,
                 credential_probe=None):
        self.state_dir = os.path.realpath(os.path.abspath(os.path.expanduser(state_dir)))
        created = not os.path.isdir(self.state_dir)
        os.makedirs(self.state_dir, exist_ok=True)
        if created:
            try:
                os.chmod(self.state_dir, 0o700)
            except OSError:
                pass
        self.runner_factory = runner_factory or (
            lambda model, **kwargs: CodexExecRunner(
                model=model, snapshotter=snapshotter, **kwargs))
        self.credential_probe = credential_probe or codex_worker_credential_state
        self.snapshotter = snapshotter
        self.worker_dir = os.path.join(self.state_dir, "owners")
        self.home_dir = os.path.join(self.state_dir, "homes")
        os.makedirs(self.worker_dir, exist_ok=True)
        os.makedirs(self.home_dir, exist_ok=True)
        for private in (self.worker_dir, self.home_dir):
            try:
                os.chmod(private, 0o700)
            except OSError:
                pass
        self._lock = threading.RLock()
        self._active: dict[str, dict[str, Any]] = {}

    def _path(self, mission_id: str) -> str:
        name = hashlib.sha256(str(mission_id or "").encode(
            "utf-8", "replace")).hexdigest()[:40] + ".json"
        return os.path.join(self.state_dir, name)

    def _load(self, mission_id: str) -> tuple[str, dict]:
        try:
            with open(self._path(mission_id), encoding="utf-8") as fh:
                value = json.load(fh)
            if not isinstance(value, dict):
                return "invalid", {}
            if (int(value.get("version") or 0) != _CODEX_STATE_VERSION or
                    str(value.get("mission_id") or "") != str(mission_id or "") or
                    str(value.get("protocol") or "") != _CODEX_PROTOCOL_VERSION or
                    not isinstance(value.get("snapshot"), dict) or
                    not isinstance(value.get("runner_identity"), dict)):
                return "invalid", {}
            return "ok", value
        except FileNotFoundError:
            return "missing", {}
        except (OSError, ValueError, TypeError):
            return "invalid", {}

    def _save(self, mission_id: str, value: Mapping[str, Any], *, limit: int = 0) -> bool:
        path = self._path(mission_id)
        temp = path + "." + secrets.token_hex(8) + ".tmp"
        try:
            safe = redact_obj(dict(value), {})
            effective_limit = min(
                _STATE_STORAGE_LIMIT, int(limit)) if int(limit or 0) > 0 else \
                _STATE_STORAGE_LIMIT
            snapshot = safe.get("snapshot") if isinstance(safe, dict) else None
            events = snapshot.get("events") if isinstance(snapshot, dict) else None
            encoded = json.dumps(
                safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            while len(encoded.encode("utf-8")) > effective_limit and events:
                del events[:max(1, len(events) // 4)]
                encoded = json.dumps(
                    safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > effective_limit:
                return False
            with open(temp, "x", encoding="utf-8") as fh:
                try:
                    os.chmod(temp, 0o600)
                except OSError:
                    pass
                fh.write(encoded)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            return True
        except OSError:
            try:
                os.unlink(temp)
            except OSError:
                pass
            return False

    def _owner_path(self, mission_id: str) -> str:
        return os.path.join(self.worker_dir, hashlib.sha256(str(
            mission_id or "").encode("utf-8", "replace")).hexdigest()[:40] + ".json")

    def _cancel_path(self, mission_id: str) -> str:
        return self._owner_path(mission_id) + ".cancel"

    def _job_name(self, mission_id: str, token: str) -> str:
        digest = hashlib.sha256((self.worker_dir + "\0" + str(mission_id) +
                                 "\0" + token).encode()).hexdigest()[:32]
        return "Local\\CollieMissionCodex-" + digest

    @staticmethod
    def _process_identity(pid: int) -> str:
        if plat.is_windows():
            return ""
        try:
            path = "/proc/%d/stat" % int(pid)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    raw = fh.read()
                tail = raw[raw.rfind(")") + 2:].split()
                start = tail[19]
                boot = ""
                try:
                    with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as fh:
                        boot = fh.read().strip()
                except OSError:
                    pass
                return "linux:%s:%s" % (boot, start)
            started = subprocess.run(
                ["ps", "-p", str(int(pid)), "-o", "lstart="],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=3, check=False).stdout.strip()
            return "ps:" + started if started else ""
        except Exception:
            return ""

    @classmethod
    def _process_matches(cls, pid: int, marker: str, identity: str) -> bool:
        if plat.is_windows():
            return True
        try:
            if not identity or cls._process_identity(pid) != identity:
                return False
            path = "/proc/%d/cmdline" % int(pid)
            if os.path.exists(path):
                with open(path, "rb") as fh:
                    command = fh.read().decode("utf-8", "replace")
            else:
                command = subprocess.run(
                    ["ps", "-p", str(int(pid)), "-o", "command="],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, timeout=3, check=False).stdout
            return bool(marker and marker in command and _START_GATE_SCRIPT[:24] not in marker)
        except Exception:
            return False

    @staticmethod
    def _remove_owner(path: str, token: str) -> bool:
        try:
            with open(path, encoding="utf-8") as fh:
                current = json.load(fh)
            if not isinstance(current, dict) or not secrets.compare_digest(
                    str(current.get("token") or ""), str(token or "")):
                return False
            os.remove(path)
            return True
        except FileNotFoundError:
            return True
        except (OSError, ValueError, TypeError):
            return False

    def _write_cancel_fence(self, mission_id: str) -> bool:
        path = self._cancel_path(mission_id)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as fh:
                fh.write("cancelled\n")
                fh.flush()
                os.fsync(fh.fileno())
            return True
        except FileExistsError:
            return True
        except OSError:
            return False

    def _owner_callbacks(self, mission_id: str, phase: str,
                         active: dict[str, Any]) -> tuple[str, str, Any, Any]:
        token = secrets.token_hex(16)
        marker = "collie-codex-owner:%s:%s" % (mission_id, token)
        job_name = self._job_name(mission_id, token)
        path = self._owner_path(mission_id)

        def owned(proc):
            identity = self._process_identity(int(proc.pid))
            if not plat.is_windows() and not identity:
                return False
            row = {
                "version": 1, "mission_id": mission_id, "phase": phase,
                "pid": int(proc.pid), "pgid": int(getattr(
                    proc, "_collie_process_group", 0) or 0),
                "process_identity": identity, "marker": marker,
                "job_name": job_name, "token": token, "created_at": time.time(),
            }
            temp = path + "." + secrets.token_hex(8) + ".tmp"
            try:
                with open(temp, "x", encoding="utf-8") as fh:
                    try:
                        os.chmod(temp, 0o600)
                    except OSError:
                        pass
                    json.dump(row, fh, sort_keys=True, separators=(",", ":"))
                    fh.flush(); os.fsync(fh.fileno())
                os.replace(temp, path)
                with self._lock:
                    active["process"] = proc
                return not os.path.exists(self._cancel_path(mission_id))
            except OSError:
                try:
                    os.unlink(temp)
                except OSError:
                    pass
                return False

        def settled(_proc, confirmed):
            with self._lock:
                if active.get("process") is _proc:
                    active["process"] = None
            if confirmed and not self._remove_owner(path, token):
                raise RuntimeError("durable process-owner receipt could not be retired")
        return marker, job_name, owned, settled

    def _has_owner(self, mission_id: str) -> bool:
        try:
            with open(self._owner_path(mission_id), encoding="utf-8") as fh:
                return isinstance(json.load(fh), dict)
        except FileNotFoundError:
            return False
        except (OSError, ValueError, TypeError):
            return True

    def cancel_persisted(self, mission_id: str) -> bool:
        path = self._owner_path(mission_id)
        try:
            with open(path, encoding="utf-8") as fh:
                row = json.load(fh)
            if (not isinstance(row, dict) or int(row.get("version") or 0) != 1 or
                    str(row.get("mission_id") or "") != str(mission_id)):
                return False
        except FileNotFoundError:
            return True
        except (OSError, ValueError, TypeError):
            return False
        token = str(row.get("token") or "")
        if plat.is_windows():
            confirmed = plat.terminate_named_job_and_wait(str(row.get("job_name") or ""))
            return bool(confirmed and self._remove_owner(path, token))
        pid = int(row.get("pid") or 0)
        pgid = int(row.get("pgid") or 0)
        marker = str(row.get("marker") or "")
        identity = str(row.get("process_identity") or "")
        if pid <= 1 or pgid != pid or not self._process_matches(pid, marker, identity):
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return self._remove_owner(path, token)
            except (OSError, ValueError):
                pass
            return False
        try:
            os.killpg(pgid, getattr(signal, "SIGKILL", 9))
        except ProcessLookupError:
            return self._remove_owner(path, token)
        except OSError:
            return False
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return self._remove_owner(path, token)
            except OSError:
                return False
            time.sleep(.01)
        return False

    def cancel_current(self, mission_id: str = "", include_persisted: bool = True) -> bool:
        key = str(mission_id or "")
        if include_persisted and key and not self._write_cancel_fence(key):
            return False
        with self._lock:
            entries = ([self._active[key]] if key and key in self._active else
                       list(self._active.values()) if not key else [])
        local_ok = True
        for active in entries:
            runner = active.get("runner")
            proc = active.get("process")
            if runner is not None:
                try:
                    local_ok = bool(runner.cancel_current()) or local_ok
                except Exception:
                    local_ok = False
            if proc is not None:
                local_ok = bool(_terminate_owned_process(proc)) and local_ok
        persisted_ok = self.cancel_persisted(key) if include_persisted and key else True
        return bool(local_ok and persisted_ok and (not key or not self._has_owner(key)))

    cancel_pending = cancel_current

    def cancel_for(self, mission_id: str):
        return lambda: self.cancel_current(mission_id)

    @staticmethod
    def _usage_delta(before: Mapping, after: Mapping) -> dict[str, int]:
        return {key: max(0, int(after.get(key, 0) or 0) -
                              int(before.get(key, 0) or 0))
                for key in _USAGE_KEYS}

    @staticmethod
    def _refusal(message: str, *, session_id: str = "",
                 recovery_required: bool = False, post_digest: str = "") -> dict:
        return {
            "answer": message, "error": message, "verified": False,
            "continue_needed": False, "needs_human": bool(recovery_required),
            "recovery_required": bool(recovery_required),
            "session_id": session_id, "post_tree_digest": post_digest,
            "model_calls": 0, "_model_calls_reserved": False,
            "_usage": {"input_tokens": 0, "output_tokens": 0,
                       "cache_tokens": 0, "cost_usd": 0.0},
        }

    @staticmethod
    def _runner_identity(runner) -> dict[str, Any]:
        identity = getattr(runner, "state_identity", None)
        value = identity() if callable(identity) else {
            "protocol": _CODEX_PROTOCOL_VERSION,
            "runner_type": type(runner).__name__,
        }
        if not isinstance(value, dict) or value.get("protocol") != _CODEX_PROTOCOL_VERSION:
            raise RuntimeError("Codex runner protocol identity is unavailable")
        return dict(value)

    def _make_runner(self, model: str, *, environment: Mapping[str, str],
                     marker: str, job_name: str, owned, settled):
        kwargs = {
            "environment": environment, "process_marker": marker,
            "job_name": job_name, "on_process_owned": owned,
            "on_process_settled": settled,
        }
        try:
            return self.runner_factory(model, **kwargs)
        except TypeError:
            # Deterministic injectable test/embedding runners predate the
            # production ownership hooks.  They never cross an OS boundary.
            return self.runner_factory(model)

    def _run_verification(self, command: str, workspace: str, timeout: int,
                          environment: Mapping[str, str], mission_id: str,
                          active: dict[str, Any], agent_digest: str,
                          baseline: str, patch_attributed: bool) -> tuple[dict, dict]:
        command = str(command or "").strip()
        if not command:
            return ({"verified": False,
                     "detail": "no host verification command configured",
                     "evidence": None}, _snapshot(self.snapshotter, workspace))
        before = _snapshot(self.snapshotter, workspace)
        args, use_shell = plat.shell_argv(command)
        if use_shell:
            args = ([os.environ.get("COMSPEC") or "cmd.exe", "/d", "/s", "/c", command]
                    if plat.is_windows() else ["/bin/sh", "-c", command])
        marker, job_name, owned, settled = self._owner_callbacks(
            mission_id, "verification", active)
        transport = SubprocessRunner()

        def register(proc):
            if owned(proc) is False:
                return False
            with self._lock:
                active["process"] = proc
            return not os.path.exists(self._cancel_path(mission_id))

        try:
            outcome = transport.run(
                tuple(args), cwd=workspace, stdin_text="",
                timeout_s=max(1, min(3600, int(timeout or 300))),
                on_process=register, environment=environment,
                job_name=job_name, process_marker=marker, on_settled=settled)
            output = redact(((outcome.stdout or "") + ("\n" + outcome.stderr
                if outcome.stderr else ""))[-4000:], {})
        except Exception as exc:
            outcome = ProcessOutcome(exit_code=None)
            output = _clean_error("check failed to run: %s: %s" %
                                  (type(exc).__name__, exc))
        after = _snapshot(self.snapshotter, workspace)
        comparable = bool(before.get("tree_digest") and after.get("tree_digest") and
                          before.get("snapshot_complete") and
                          after.get("snapshot_complete"))
        unchanged = bool(comparable and before.get("tree_digest") ==
                         after.get("tree_digest"))
        command_passed = bool(outcome.exit_code == 0 and not outcome.timed_out and
                              not outcome.cancelled)
        evidence = {
            "command": command, "exit_code": outcome.exit_code,
            "command_passed": command_passed,
            "passed": bool(command_passed and unchanged),
            "output": output, "cwd": workspace,
            "tree_digest": str(before.get("tree_digest") or ""),
            "post_tree_digest": str(after.get("tree_digest") or ""),
            "snapshot_complete": bool(before.get("snapshot_complete")),
            "post_snapshot_complete": bool(after.get("snapshot_complete")),
            "ran_after_last_edit": bool(command_passed and unchanged),
            "freshness": ("fresh" if command_passed and unchanged else
                          "changed_during_check" if command_passed and comparable else
                          "cancelled" if outcome.cancelled else "failed"),
            "source": "mission_code_profile",
            "working_tree_changed_during_check": (not unchanged) if comparable else None,
            "agent_post_tree_digest": agent_digest,
            "agent_boundary_matches": bool(
                agent_digest and before.get("tree_digest") == agent_digest),
        }
        differs = bool(baseline and agent_digest and baseline != agent_digest)
        current_patch = bool(patch_attributed and differs)
        evidence["agent_differs_from_baseline"] = differs
        evidence["patch_attributed"] = current_patch
        verified = bool(evidence["passed"] and evidence["agent_boundary_matches"] and
                        current_patch)
        detail = ("configured host check passed against the current Mission patch"
                  if verified else
                  "check passed but the Mission produced no attributed patch"
                  if evidence["passed"] and evidence["agent_boundary_matches"] else
                  "workspace changed between the agent boundary and host verification"
                  if evidence["passed"] else
                  "configured check failed (exit %s)" % outcome.exit_code)
        return {"verified": verified, "detail": detail,
                "evidence": evidence}, after

    def __call__(self, goal: str, *, workspace=None, mission_id=None,
                 execution_profile=None, verify_command="", max_wall_seconds=0,
                 session_id="", baseline_tree_digest="", expected_tree_digest="",
                 max_model_calls=None, mission_store_path="", mission_run_token="",
                 verify_timeout_seconds=None, max_model_tokens=None,
                 max_model_cost_usd=None, max_session_storage_bytes=None, **_unused):
        mission_id = str(mission_id or "")
        profile = dict(execution_profile or {})
        if profile.get("runner") != self.key:
            return self._refusal("Mission runner profile is not codex-exec")
        if any(value not in (None, "") for value in (
                max_model_calls, max_model_tokens, max_model_cost_usd)):
            return self._refusal(
                "codex-exec is unavailable for this Mission because the Codex CLI "
                "does not expose verifiable per-request model-call, token, and cost "
                "limits; Collie's native code runner is required for a hard budget")
        workspace = _workspace(str(workspace or ""))
        current = _snapshot(self.snapshotter, workspace)
        current_digest = str(current.get("tree_digest") or "")
        sid = str(session_id or "") or ("mission-codex-" + hashlib.sha256(
            (mission_id or workspace).encode("utf-8", "replace")).hexdigest()[:24])
        baseline = str(baseline_tree_digest or current_digest)
        expected = str(expected_tree_digest or baseline)
        if not current.get("snapshot_complete") or not current_digest:
            return self._refusal(
                "Mission could not establish a complete workspace boundary",
                session_id=sid, recovery_required=True, post_digest=current_digest)
        if expected and current_digest != expected:
            return self._refusal(
                "workspace changed outside the last completed Codex slice",
                session_id=sid, recovery_required=True, post_digest=current_digest)
        if os.path.exists(self._cancel_path(mission_id)):
            return self._refusal(
                "Mission Codex execution was cancelled and cannot be replayed",
                session_id=sid, recovery_required=True, post_digest=current_digest)
        try:
            storage_limit = max(0, int(max_session_storage_bytes or 0))
        except (TypeError, ValueError):
            storage_limit = 0
        try:
            existing_storage = os.path.getsize(self._path(mission_id))
        except OSError:
            existing_storage = 0
        if storage_limit and existing_storage >= storage_limit:
            refused = self._refusal(
                "durable Codex runner storage budget is exhausted",
                session_id=sid, post_digest=current_digest)
            refused["_external_storage_bytes"] = existing_storage
            return refused

        load_status, persisted = self._load(mission_id)
        if load_status == "invalid":
            return self._refusal(
                "durable Codex runner state is corrupt, from another Mission, or "
                "uses an unsupported schema/protocol",
                session_id=sid, recovery_required=True, post_digest=current_digest)

        credential = dict(self.credential_probe() or {})
        if not credential.get("admitted") or not profile.get("subscription_only"):
            return self._refusal(
                str(credential.get("reason") or
                    "codex-exec requires proven subscription-only credentials"),
                session_id=sid, post_digest=current_digest)
        private_home = os.path.join(self.home_dir, hashlib.sha256(
            mission_id.encode("utf-8", "replace")).hexdigest()[:40])
        environment, live_credential = codex_worker_environment(private_home)
        # An injected credential probe can make tests deterministic, but the
        # production helper must agree with the environment's real login store.
        if self.credential_probe is codex_worker_credential_state:
            credential = live_credential
        if not credential.get("admitted"):
            return self._refusal(str(credential.get("reason") or
                "Codex subscription credential is unavailable"),
                session_id=sid, post_digest=current_digest)

        active = {"runner": None, "process": None}
        marker, job_name, owned, settled = self._owner_callbacks(
            mission_id, "codex", active)
        runner = self._make_runner(
            str(profile.get("model") or ""), environment=environment,
            marker=marker, job_name=job_name, owned=owned, settled=settled)
        try:
            runner_identity = self._runner_identity(runner)
        except Exception as exc:
            return self._refusal(
                "Codex runner isolation/version admission failed: %s" %
                _clean_error(exc), session_id=sid, post_digest=current_digest)
        prior = None
        raw_snapshot = persisted.get("snapshot")
        if isinstance(raw_snapshot, dict):
            try:
                prior = RunnerSnapshot.from_dict(raw_snapshot)
            except Exception:
                return self._refusal(
                    "durable Codex runner snapshot is invalid",
                    session_id=sid, recovery_required=True, post_digest=current_digest)
            if (persisted.get("runner_identity") != runner_identity or
                    prior.runner != self.key or prior.workspace != workspace or
                    prior.recovery_required or
                    persisted.get("adapter_recovery_required") or
                    str(persisted.get("expected_tree_digest") or "") not in
                    ("", current_digest)):
                return self._refusal(
                    "durable Codex runner state requires recovery before resume",
                    session_id=sid, recovery_required=True, post_digest=current_digest)
            if not prior.thread_id:
                if prior.mutated:
                    return self._refusal(
                        "Codex stopped after a mutation without resumable state",
                        session_id=sid, recovery_required=True,
                        post_digest=current_digest)
                # No target thread and no mutation is safe to retry from a new
                # invocation; do not feed an invalid empty id to `exec resume`.
                prior = None
                persisted = {}
        if not mission_store_path or not mission_run_token:
            return self._refusal(
                "Mission Codex model-request accounting authority is missing",
                session_id=sid, post_digest=current_digest)

        from .mission import MissionStore
        request_store = MissionStore(str(mission_store_path))
        request_id = "req_" + secrets.token_hex(16)
        reserved = False
        snapshot = None
        before_usage = dict(prior.usage) if prior else {}
        verification = {"verified": False, "detail": "runner did not settle",
                        "evidence": None}
        post = current
        saved = False
        recovery = False
        with self._lock:
            if mission_id in self._active:
                request_store.close()
                return self._refusal(
                    "Mission already has an active Codex runner",
                    session_id=sid, post_digest=current_digest)
            active["runner"] = runner
            self._active[mission_id] = active
        try:
            reserved = request_store.reserve_model_request(
                mission_id, str(mission_run_token), request_id,
                provider=str(profile.get("provider") or "codex-oauth"),
                model=str(profile.get("model") or ""), purpose="codex_exec")
            if not reserved:
                return self._refusal(
                    "Mission Codex model-request reservation was denied",
                    session_id=sid, post_digest=current_digest)
            prompt = (str(goal or "").strip() +
                      "\n\nWork only inside the assigned Mission workspace. "
                      "Do not claim verification; Collie runs the configured host check.")
            timeout = max(1.0, float(max_wall_seconds or 900))
            snapshot = (runner.resume(prior, prompt, timeout_s=timeout)
                        if prior is not None else
                        runner.start(prompt, workspace, timeout_s=timeout))
            usage = self._usage_delta(before_usage, snapshot.usage)
            state = {
                "version": _CODEX_STATE_VERSION, "protocol": _CODEX_PROTOCOL_VERSION,
                "mission_id": mission_id, "runner_identity": runner_identity,
                "snapshot": snapshot.to_dict(),
                "baseline_tree_digest": baseline,
                "expected_tree_digest": snapshot.workspace_digest,
                # Attribution belongs to this slice. A historical mutation must
                # not make a verifier-only change or clean revert look green.
                "patch_attributed": bool(
                    snapshot.mutated and snapshot.workspace_digest and
                    snapshot.workspace_digest != baseline),
            }
            saved = self._save(mission_id, state, limit=storage_limit)
            recovery = bool(snapshot.recovery_required or not saved)
            post = _snapshot(self.snapshotter, workspace)
            if snapshot.settled and not recovery:
                verification, post = self._run_verification(
                    verify_command, workspace, verify_timeout_seconds or 300,
                    environment, mission_id, active, snapshot.workspace_digest,
                    baseline, bool(state["patch_attributed"]))
                evidence = verification.get("evidence") or {}
                if evidence and evidence.get(
                        "working_tree_changed_during_check") is not False:
                    # A verifier that wrote bytes (or whose boundary is unknown)
                    # cannot donate those bytes to the worker's provenance.
                    state["patch_attributed"] = False
            state["expected_tree_digest"] = str(post.get("tree_digest") or "")
            if (not state["expected_tree_digest"] or
                    not post.get("snapshot_complete") or
                    os.path.exists(self._cancel_path(mission_id))):
                recovery = True
            state["adapter_recovery_required"] = bool(recovery)
            if not recovery and not self._save(
                    mission_id, state, limit=storage_limit):
                recovery = True
        finally:
            if reserved:
                try:
                    request_store.complete_model_request(
                        request_id, "completed" if snapshot is not None and
                        snapshot.settled else "error")
                except Exception:
                    pass
            request_store.close()
            with self._lock:
                self._active.pop(mission_id, None)

        assert snapshot is not None
        usage = self._usage_delta(before_usage, snapshot.usage)
        verified = bool(verification.get("verified"))
        from .costs import cost_usd
        equivalent = cost_usd(
            str(profile.get("model") or ""), usage["input_tokens"],
            usage["output_tokens"], usage["cached_input_tokens"], 0)
        marginal = 0.0  # admitted only with account-scoped subscription evidence
        error = _clean_error(snapshot.error or "")
        if not saved:
            error = "Codex slice completed but its durable snapshot could not be persisted"
        billing_evidence = {key: credential.get(key) for key in (
            "auth_kind", "api_key_absent", "endpoint_overrides_ignored",
            "account_fingerprint", "fingerprint", "mtime_ns")}
        result = {
            "answer": snapshot.final_output or error,
            "error": error, "verified": verified,
            "continue_needed": False, "needs_human": recovery,
            "recovery_required": recovery,
            "session_id": sid, "turns": 1, "model_calls": 1,
            "_model_calls_reserved": True,
            "worker_invocations": 1,
            "physical_model_calls_observed": False,
            "model_calls_are_lower_bound": True,
            "_usage": {"input_tokens": usage["input_tokens"],
                       "output_tokens": usage["output_tokens"],
                       "cache_tokens": usage["cached_input_tokens"],
                       "cost_usd": marginal},
            "equivalent_cost_usd": equivalent,
            "billing_evidence": billing_evidence,
            "brain_transport": str(profile.get("provider") or "codex-oauth"),
            "worker_executor": self.key,
            "baseline_tree_digest": baseline,
            "expected_tree_digest": expected,
            "agent_post_tree_digest": snapshot.workspace_digest,
            "post_tree_digest": str(post.get("tree_digest") or ""),
            "verification": verification,
            "transient": bool(snapshot.timed_out and not recovery),
            "retry_after_seconds": 60 if snapshot.timed_out and not recovery else 0,
            "slice_mutated": bool(snapshot.mutated),
            "verifier_mutated": bool((verification.get("evidence") or {}).get(
                "working_tree_changed_during_check") is True),
            "patch_attributed": bool(state["patch_attributed"]),
            "_external_storage_bytes": (os.path.getsize(self._path(mission_id))
                                        if os.path.exists(self._path(mission_id)) else 0),
        }
        return redact_obj(result, {})


def _workspace(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("workspace must be a non-empty path")
    root = os.path.realpath(os.path.abspath(value))
    if not os.path.isdir(root):
        raise ValueError("workspace does not exist or is not a directory: %s" % root)
    return root


def _prompt(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("prompt must be non-empty")
    if "\x00" in value:
        raise ValueError("prompt contains a NUL byte")
    return value


def _snapshot(snapshotter: Callable[[str], dict[str, Any]], workspace: str) -> dict[str, Any]:
    try:
        value = snapshotter(workspace)
        return dict(value or {})
    except Exception:
        return {"tree_digest": "", "snapshot_complete": False}


def _mutation(before: dict[str, Any], after: dict[str, Any]) -> tuple[bool, bool]:
    left = str(before.get("tree_digest") or "")
    right = str(after.get("tree_digest") or "")
    complete = bool(left and right and before.get("snapshot_complete")
                    and after.get("snapshot_complete"))
    return bool(left and right and left != right), complete


def _merge_usage(prior: dict[str, int], events: Sequence[RunnerEvent]
                 ) -> tuple[dict[str, int], bool]:
    usage = {str(key): max(0, int(value)) for key, value in prior.items()
             if isinstance(value, (int, float))}
    terminal_count = 0
    valid_count = 0
    for event in events:
        if event.type != "turn.completed":
            continue
        terminal_count += 1
        raw = event.payload.get("usage")
        if not isinstance(raw, dict):
            continue
        required = ("input_tokens", "output_tokens")
        if any(isinstance(raw.get(key), bool) or
               not isinstance(raw.get(key), int) or raw.get(key) < 0
               for key in required):
            continue
        optional_valid = True
        for key in _USAGE_KEYS:
            value = raw.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                optional_valid = False
                break
        if not optional_valid:
            continue
        valid_count += 1
        for key in _USAGE_KEYS:
            usage[key] = usage.get(key, 0) + int(raw.get(key, 0))
    return usage, bool(terminal_count == 1 and valid_count == 1)


def _final_output(events: Sequence[RunnerEvent]) -> str:
    answer = ""
    for event in events:
        payload = event.payload
        if event.type in ("agent_message", "message", "assistant"):
            answer = str(payload.get("text") or payload.get("message") or answer)
        if event.type in ("item.completed", "item.updated"):
            item = payload.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "agent_message":
                answer = str(item.get("text") or answer)
    return answer


def _event_error(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("detail") or "")
    return str(error or payload.get("message") or "")


def _bounded_payload(value: dict[str, Any], limit: int) -> dict[str, Any]:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) <= limit:
            return json.loads(encoded)
        return {"type": str(value.get("type") or "unknown"), "truncated": True,
                "preview": encoded[:limit]}
    except (TypeError, ValueError):
        return {"type": str(value.get("type") or "unknown"), "truncated": True,
                "preview": repr(value)[:limit]}


def _clean_error(value: Any, limit: int = 4_000) -> str:
    text = str(value or "").strip()
    text = _TOKEN_SECRET.sub(lambda match: (match.group(1) or "") + "[redacted]", text)
    return redact(text, {})[:limit]


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")


__all__ = [
    "AgentBackendStatus", "AgentRunner", "CodexExecRunner", "MissionCodexCodeRunner",
    "ProcessOutcome",
    "ProcessRunner", "RecoveryRequiredError", "RunnerEvent", "RunnerSnapshot",
    "SubprocessRunner", "codex_worker_credential_state",
    "codex_worker_environment", "probe_agent_backends",
]
