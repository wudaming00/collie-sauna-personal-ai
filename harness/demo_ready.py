"""Deterministic interview-demo preparation for Collie × Sauna.

The normal profile is the wrong place to rehearse: real missions, test calendar rows, chat history
and personal tasks all compete with the story.  This module creates a new state directory for every
rehearsal, starts a separate loopback server, and points all three native surfaces (ambient desktop,
global capsule, app window) at it.  Nothing in the person's normal state is copied, hidden or
deleted.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import signal
import time
import urllib.request


DEFAULT_PORT = 8878
_MANIFEST = os.path.expanduser("~/.collie/interview-demo.json")


def _new_state_dir() -> str:
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return os.path.expanduser("~/.collie/demos/interview-%s-%s" % (stamp, os.getpid()))


def _load_manifest() -> dict:
    try:
        with open(_MANIFEST, "r", encoding="utf-8-sig") as fh:
            row = json.load(fh)
        return row if isinstance(row, dict) else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _save_manifest(row: dict) -> None:
    os.makedirs(os.path.dirname(_MANIFEST), exist_ok=True)
    tmp = _MANIFEST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(row, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, _MANIFEST)


def _activate(state_dir: str) -> dict:
    """Select the isolated profile and return the previous environment for restoration."""
    old = {k: os.environ.get(k) for k in ("COLLIE_STATE_DIR", "COLLIE_SAUNA_DIR")}
    state_dir = os.path.abspath(os.path.expanduser(state_dir))
    os.makedirs(state_dir, exist_ok=True)
    os.environ["COLLIE_STATE_DIR"] = state_dir
    os.environ["COLLIE_SAUNA_DIR"] = os.path.join(state_dir, "sauna")
    return old


def _restore_env(old: dict) -> None:
    for key, value in old.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _wait_for(predicate, wanted: bool, timeout: float = 8.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if bool(predicate()) is wanted:
            return True
        time.sleep(0.1)
    return bool(predicate()) is wanted


def _http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2.0) as response:
            return int(response.status) == 200 and bool(response.read(32))
    except Exception:
        return False


def _stop_server(manifest: dict) -> bool:
    """Stop only the isolated server process recorded by this demo launcher."""
    try:
        pid = int(manifest.get("server_pid") or 0)
        port = int(manifest.get("port") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return False
    if port > 0:
        _wait_for(lambda: _http_ok("http://127.0.0.1:%d/api/ver" % port), False, 4.0)
    return True


def scenario_checks(state, sauna=None) -> list[dict]:
    """Return small, human-readable readiness receipts over the actual demo database."""
    expected_tasks = {"demo_tsk_%d" % n for n in range(1, 8)}
    tasks = state.tasks(include_done=True)
    events = state.events(since=0, until=int(time.time()) + 40 * 86400, limit=300)
    notes = state.notes(limit=300)
    foreign = ([t["title"] for t in tasks if not str(t.get("id") or "").startswith("demo_")] +
               [e["title"] for e in events if not str(e.get("id") or "").startswith("demo_")] +
               [n["title"] for n in notes if not str(n.get("id") or "").startswith("demo_")])
    titles = [str(x.get("title") or "") for x in tasks + events + notes]
    status = sauna.status() if sauna is not None else {"connected": False}
    return [
        {"level": "pass" if expected_tasks.issubset({t["id"] for t in tasks}) else "fail",
         "label": "seven-step interview scenario is seeded"},
        {"level": "pass" if state.get_meta("focus_task") == "demo_tsk_4" else "fail",
         "label": "desktop focus starts on Build Collie prototype"},
        {"level": "pass" if any(e.get("id") == "demo_evt_interview" for e in events) else "fail",
         "label": "Sauna interview is on the calendar"},
        {"level": "pass" if len(notes) >= 3 else "fail",
         "label": "product thesis and interview memory are available"},
        {"level": "pass" if not foreign else "fail",
         "label": "profile contains demo data only", "detail": ", ".join(foreign[:3])},
        {"level": "pass" if not any("[Test]" in title for title in titles) else "fail",
         "label": "no test labels can leak into the interview"},
        {"level": "pass" if not status.get("connected") else "fail",
         "label": "Sauna starts disconnected for the reveal"},
    ]


def provider_check() -> dict:
    from . import settings
    from .catalog import probe_auth

    configured = str(settings.get("PROVIDER", "auto") or "auto")
    candidates = ([configured] if configured not in ("auto", "mock") else
                  ["codex-oauth", "claude-agent-sdk", "claude-cli", "anthropic-oauth", "ollama"])
    ready = []
    for provider in candidates:
        try:
            if probe_auth(provider) == "ok":
                ready.append(provider)
        except Exception:
            pass
    if ready:
        return {"level": "pass", "label": "a live brain is available", "detail": ready[0]}
    return {"level": "warn", "label": "no live brain detected; the offline story still works",
            "detail": "configure a provider before the optional live run"}


def _print_report(checks: list[dict], *, url: str = "") -> int:
    icons = {"pass": "✓", "warn": "!", "fail": "×"}
    print("\nCollie × Sauna interview demo")
    for row in checks:
        detail = " · " + row["detail"] if row.get("detail") else ""
        print("  %s %s%s" % (icons.get(row["level"], "·"), row["label"], detail))
    failed = sum(1 for row in checks if row["level"] == "fail")
    warned = sum(1 for row in checks if row["level"] == "warn")
    if url:
        print("  open %s" % url)
    print("\n%s" % ("READY" if not failed else "NOT READY") +
          " · %d passed · %d warning(s) · %d failed" %
          (sum(1 for row in checks if row["level"] == "pass"), warned, failed))
    return 1 if failed else 0


def prepare(*, state_dir: str = "", port: int = DEFAULT_PORT, launch: bool = True,
            desktop: bool = True) -> int:
    """Create a clean scenario and optionally replace the native surfaces with it."""
    from . import demo_seed
    from .executive import default_executive
    from .sauna import default_client

    prior_manifest = _load_manifest()
    state_dir = os.path.abspath(os.path.expanduser(state_dir or _new_state_dir()))
    previous_env = _activate(state_dir)
    executive = default_executive()
    state = executive.state
    sauna = default_client(state)
    # A rehearsal can connect the prototype.  Reset that reveal without writing a synthetic
    # "disconnected" activity into the otherwise pristine timeline.
    state.set_meta("sauna_connected", "0")
    state.set_meta("sauna_token_ref", "")
    demo_seed.seed(state, executive, sauna, connect_sauna=False)

    from . import wallpaper as wp
    restore = ((prior_manifest.get("restore") or {}) if prior_manifest.get("active") else
               {"wallpaper": wp.engine_running() and wp.panel_running(),
                "command": wp.command_running(), "app": wp.app_running()})
    manifest = {
        "active": bool(launch), "state_dir": state_dir, "prepared_at": int(time.time()),
        "port": 0, "desktop": bool(desktop),
        "restore": restore,
    }
    checks = scenario_checks(state, sauna) + [provider_check()]
    if not launch:
        # A read-only preparation must not orphan an already running demo by
        # replacing the only manifest that knows how to close and restore it.
        if not prior_manifest.get("active"):
            _save_manifest(manifest)
        _restore_env(previous_env)
        return _print_report(checks)

    # Replace any prior rehearsal as one unit. The stored process id belongs only
    # to the isolated server; the ordinary 8787 service is never touched.
    wp.stop_app()
    wp.stop_command()
    if desktop:
        wp.stop()
    _wait_for(wp.app_running, False)
    _wait_for(wp.command_running, False)
    if desktop:
        _wait_for(lambda: wp.engine_running() or wp.panel_running(), False)
    if prior_manifest.get("active"):
        _stop_server(prior_manifest)

    # A fresh port guarantees that an everyday server cannot silently serve the demo window.
    actual_port = wp.free_port(int(port or DEFAULT_PORT))
    manifest["port"] = actual_port
    manifest["server_pid"] = wp.start_server_windowless(actual_port)
    server_ready = _wait_for(lambda: wp.server_up(actual_port), True, 20.0)
    checks.append({"level": "pass" if server_ready else "fail",
                   "label": "isolated demo server is responding", "detail": str(actual_port)})
    desktop_ready = True
    app_ready = False
    if server_ready:
        if desktop:
            desktop_ready = wp.launch_engine(actual_port)
        app_ready = wp.run_app(actual_port, "/?page=today&demo=1") == 0
        time.sleep(0.7)
    checks.extend([
        {"level": "pass" if (not desktop or (desktop_ready and wp.engine_running() and wp.panel_running())) else "fail",
         "label": "ambient desktop is attached to the demo"},
        {"level": "pass" if app_ready and wp.app_running() else "fail",
         "label": "native app opened directly on Today"},
        {"level": "pass" if wp.command_running() else "warn",
         "label": "global command capsule is listening"},
    ])
    manifest["url"] = "http://127.0.0.1:%d/?page=today&demo=1" % actual_port
    _save_manifest(manifest)
    return _print_report(checks, url=manifest["url"])


def check(*, state_dir: str = "") -> int:
    from .executive import default_executive
    from .sauna import default_client
    from . import wallpaper as wp

    manifest = _load_manifest()
    selected = os.path.abspath(os.path.expanduser(state_dir or manifest.get("state_dir") or ""))
    if not selected or not os.path.exists(selected):
        return _print_report([{"level": "fail", "label": "no prepared demo found",
                               "detail": "run collie demo prepare"}])
    _activate(selected)
    executive = default_executive()
    sauna = default_client(executive.state)
    checks = scenario_checks(executive.state, sauna) + [provider_check()]
    port = int(manifest.get("port") or 0)
    if manifest.get("active"):
        base = "http://127.0.0.1:%d" % port
        checks.extend([
            {"level": "pass" if _http_ok(base + "/ambient") else "fail",
             "label": "ambient page passes the HTTP smoke check"},
            {"level": "pass" if _http_ok(base + "/?page=today&demo=1") else "fail",
             "label": "Today page passes the HTTP smoke check"},
            {"level": "pass" if wp.app_running() else "fail", "label": "native app is open"},
            {"level": "pass" if (not manifest.get("desktop") or
                                   (wp.engine_running() and wp.panel_running())) else "fail",
             "label": "both halves of the ambient desktop are running"},
            {"level": "pass" if wp.command_running() else "warn",
             "label": "global command capsule is listening"},
        ])
    return _print_report(checks, url=str(manifest.get("url") or ""))


def reset() -> int:
    """Close demo surfaces, remove only seeded rows, and restore surfaces that were open before."""
    from . import demo_seed
    from .executive import default_executive
    from . import wallpaper as wp

    manifest = _load_manifest()
    state_dir = str(manifest.get("state_dir") or "")
    if not state_dir:
        print("collie demo: no prepared demo to reset")
        return 0
    old = _activate(state_dir)
    demo_seed.reset(default_executive().state)
    if manifest.get("active"):
        wp.stop_app()
        wp.stop_command()
        if manifest.get("desktop"):
            wp.stop()
        _wait_for(wp.app_running, False)
        _wait_for(wp.command_running, False)
        if manifest.get("desktop"):
            _wait_for(lambda: wp.engine_running() or wp.panel_running(), False)
        _stop_server(manifest)
    _restore_env(old)

    restore = manifest.get("restore") or {}
    if manifest.get("active") and restore.get("wallpaper"):
        wp.run(8787)
    if manifest.get("active") and restore.get("command"):
        wp.run_command(8787)
    if manifest.get("active") and restore.get("app"):
        wp.run_app(8787)
    manifest["active"] = False
    manifest["reset_at"] = int(time.time())
    _save_manifest(manifest)
    print("collie demo · reset · normal state was never modified")
    return 0
