"""collie desktop wallpaper — the behind-icons live desktop (Windows / WebView2), made portable.

Nothing is hardcoded, so `collie wallpaper` works from any install location (source checkout OR a
pip/pipx install) on any machine:
  - python    : pythonw next to sys.executable (windowless — no console flash)
  - engine    : collie-wallpaper.exe, BUILT ON DEMAND from the shipped C# source via the in-box
                .NET Framework csc (no .NET SDK needed), cached next to the source
  - server    : `collie web` on a FREE port, handed to the engine via COLLIE_WALLPAPER_URL (so it
                never collides with a busy 8787)
  - autostart : `collie wallpaper --install` writes a per-machine, hidden Startup-folder launcher
                (a generated .pyw with the resolved package path + a .vbs that runs it hidden) —
                so it survives being moved and needs no console window

Windows only (it pins a WebView2 window under Progman). On macOS/Linux it degrades to a borderless
full-screen browser window (see cli._desktop_window).
"""
PLATFORM = "windows"   # declared, not implied: tests/test_platform_purity.py reads this

import os
import json
import socket
import subprocess
import sys
import time
import urllib.request

from . import plat

QUIT_EVENT = "collie-wallpaper-quit"
COMMAND_QUIT_EVENT = "collie-wallpaper-quit-command"
PANEL_QUIT_EVENT = "collie-wallpaper-quit-panel"
APP_QUIT_EVENT = "collie-wallpaper-quit-window"


# ── path resolution (all dynamic) ────────────────────────────────────────────
def src_dir() -> str:
    """The shipped wallpaper/ dir (Program.cs + WebView2 DLLs). Repo: <root>/wallpaper; wheel:
    harness/wallpaper (package-data)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "wallpaper"), os.path.join(os.path.dirname(here), "wallpaper")):
        if os.path.exists(os.path.join(cand, "Program.cs")):
            return cand
    return os.path.join(os.path.dirname(here), "wallpaper")


def exe_path() -> str:
    return os.path.join(src_dir(), "collie-wallpaper.exe")


def pythonw() -> str:
    """The windowless interpreter next to the running one (pipx/uv/pythoncore all keep pythonw.exe
    beside python.exe). Falls back to sys.executable where there is none."""
    cand = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return cand if os.path.exists(cand) else sys.executable


def _pkg_parent() -> str:
    """The directory that must be on sys.path for `import harness` — the repo root (source) or
    site-packages (installed)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _collie_home() -> str:
    d = os.path.abspath(os.path.expanduser(
        os.environ.get("COLLIE_STATE_DIR") or os.path.join("~", ".collie")))
    os.makedirs(d, exist_ok=True)
    return d


def _startup_vbs() -> str:
    return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                        "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
                        "collie-wallpaper.vbs")


def _command_startup_vbs() -> str:
    return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                        "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
                        "collie-command.vbs")


# ── server + port ────────────────────────────────────────────────────────────
def free_port(preferred: int = 8787) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def server_up(port: int) -> bool:
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/api/ver" % port, timeout=0.8).read()
        return True
    except Exception:
        return False


def start_server_windowless(port: int) -> int:
    """Spawn ``collie web`` as a windowless process that outlives the launcher.

    Demo/App launchers are short-lived and may themselves run inside a kill-on-close Windows Job.
    ``CREATE_NO_WINDOW`` only hid the console; it did not detach the child, which left a healthy
    WebView displaying stale "still working" state after the launcher exited. Prefer a breakaway
    process and fall back cleanly on hosts whose parent Job forbids breakaway.
    """
    log = os.path.join(_collie_home(), "wallpaper-web.log")
    code = ("import sys,os;"
            "sys.path.insert(0, r'%s');"
            "sys.stdin=open(os.devnull,'r');"
            "f=open(r'%s','a',encoding='utf-8');sys.stdout=sys.stderr=f;"
            "from harness.webapp import main;"
            "sys.exit(main(['--port','%d','--no-open']))" % (_pkg_parent(), log, port))
    command = [pythonw(), "-c", code]
    if not plat.is_windows():
        return int(subprocess.Popen(command, start_new_session=True, close_fds=True).pid)
    detached = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_PROCESS_GROUP | NO_WINDOW
    breakaway = 0x01000000                            # CREATE_BREAKAWAY_FROM_JOB
    try:
        return int(subprocess.Popen(command, creationflags=detached | breakaway,
                                    close_fds=True).pid)
    except OSError:
        # Some enterprise Jobs disallow breakaway. Detaching still gives the correct lifetime on
        # ordinary desktop launches and is strictly better than the historical hidden-only child.
        return int(subprocess.Popen(command, creationflags=detached, close_fds=True).pid)


CREATE_NO_WINDOW = 0x08000000


def _quiet() -> dict:
    """Kwargs that keep a console child from flashing a window on screen.

    capture_output does NOT do this: it redirects the handles, and Windows still creates a console
    for a console-subsystem program. csc.exe and tasklist.exe are both console programs, and both
    used to blink a black box over whatever the user was looking at, every single launch.
    """
    return {"creationflags": CREATE_NO_WINDOW} if plat.is_windows() else {}


def _try_remove(path: str) -> bool:
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def _sweep_builds(d: str, keep: str) -> None:
    """Delete build leftovers that are no longer in use. A running image cannot be deleted, so
    whatever is still live simply survives to be swept by a later launch — that is the intended
    outcome, not a failure, and nothing here raises."""
    try:
        names = os.listdir(d)
    except OSError:
        return
    for n in names:
        if n == keep:
            continue
        if n.startswith("cw-build-") and n.endswith(".exe"):
            _try_remove(os.path.join(d, n))
        elif n.startswith("collie-wallpaper.exe.old-"):
            _try_remove(os.path.join(d, n))


def _install_build(tmp: str, exe: str) -> "str | None":
    """Put a freshly compiled exe at the canonical path, even when the old one is running.

    Windows refuses to overwrite or delete a running image — which is why the plain os.replace()
    this used to do failed on every launch that mattered: the logon autostart keeps an engine live,
    so the canonical exe could never be refreshed, the freshness check failed again next time, and
    csc ran (and flashed) on EVERY launch, leaving another orphan build behind each time.

    Renaming a running image, however, IS allowed. Move the old one aside, then the swap lands.
    """
    try:
        os.replace(tmp, exe)
        return exe
    except OSError:
        pass
    aside = exe + ".old-%s" % os.urandom(3).hex()
    try:
        os.replace(exe, aside)
    except OSError:
        return tmp if os.path.exists(tmp) else None      # nothing else to try; the fresh build runs
    try:
        os.replace(tmp, exe)
    except OSError:
        try:
            os.replace(aside, exe)                        # put it back rather than leave none
        except OSError:
            pass
        return tmp if os.path.exists(tmp) else None
    _try_remove(aside)                                    # still running -> swept on a later launch
    return exe


# ── engine: build-on-demand + WebView2 check ─────────────────────────────────
def webview2_present() -> bool:
    if not plat.is_windows():
        return False
    import winreg
    key = r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, key) as k:
                v, _ = winreg.QueryValueEx(k, "pv")
                if v and v != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def build_engine(force: bool = False) -> "str | None":
    """Build collie-wallpaper.exe from the shipped C# source using the in-box .NET Framework csc
    (present on every Windows — NO .NET SDK needed). Cached: reused unless the C# source is newer."""
    exe = exe_path()
    if os.path.exists(exe) and not force:
        # Reuse the cached exe ONLY if it's at least as new as the source. An UPDATE ships a newer
        # Program.cs; without this check the stale exe from the previous version was reused forever and
        # C# fixes (mouse-hook, load-retry, …) never actually reached the running wallpaper.
        try:
            if os.path.getmtime(exe) >= os.path.getmtime(os.path.join(src_dir(), "Program.cs")):
                _sweep_builds(src_dir(), os.path.basename(exe))
                return exe
        except OSError:
            return exe
    if not plat.is_windows():
        return None
    csc = os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                       r"Microsoft.NET\Framework64\v4.0.30319\csc.exe")
    if not os.path.exists(csc):
        return None
    d = src_dir()
    # Build to a UNIQUE temp name, then atomically swap it into place. Two builds can fire almost at
    # once (the logon autostart AND the app-window shortcut, both after an update) — csc'ing into the
    # same collie-wallpaper.exe races (sharing violation / truncated exe). And a FAILED compile must
    # never replace a working exe, so we check csc's return code before the swap.
    out = "cw-build-%d-%s.exe" % (os.getpid(), os.urandom(3).hex())
    tmp = os.path.join(d, out)
    cmd = [csc, "/nologo", "/target:winexe", "/platform:x64", "/out:" + out,
           "/reference:System.Windows.Forms.dll", "/reference:System.Drawing.dll",
           "/reference:Microsoft.Web.WebView2.Core.dll",
           "/reference:Microsoft.Web.WebView2.WinForms.dll", "Program.cs"]
    try:
        r = subprocess.run(cmd, cwd=d, capture_output=True, text=True, timeout=120, **_quiet())
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return exe if os.path.exists(exe) else None
    if r.returncode != 0 or not os.path.exists(tmp):
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return exe if os.path.exists(exe) else None   # keep the working exe; don't hand back garbage
    got = _install_build(tmp, exe)                      # swaps even when the old one is running
    _sweep_builds(d, os.path.basename(got or exe))
    return got


def launch_engine(port: int, split: bool = True) -> bool:
    """Bring up the desktop. Two processes by default, one for each job:

    ``--ground`` is the wallpaper behind the icons and takes NO input — no mouse hook, so nothing
    synthesises window messages into Chromium and nothing argues with Explorer over the cursor.
    ``--panel`` is an ordinary top-level window ON the desktop holding every control, clipped by
    the host to the widget rectangles the page reports.

    ``split=False`` runs the old all-in-one wallpaper instead: one window, behind the icons, with
    the input hook. Kept as the way back if a machine cannot run the pair.
    """
    exe = build_engine()
    if not exe:
        return False
    base = "http://127.0.0.1:%d/ambient" % port
    if not split:
        env = dict(os.environ, COLLIE_WALLPAPER_URL=base)
        try:
            subprocess.Popen([exe], cwd=src_dir(), env=env, **_quiet())
            return True
        except Exception:
            return False
    started = 0
    for args, suffix in ((["--ground"], "?ground=1"), (["--panel"], "?panel=1")):
        env = dict(os.environ, COLLIE_WALLPAPER_URL=base + suffix)
        try:
            subprocess.Popen([exe] + args, cwd=src_dir(), env=env, **_quiet())
            started += 1
        except Exception:
            pass
    # The ground alone is a desktop you cannot use, so report failure unless both came up.
    return started == 2


def panel_running() -> bool:
    """Whether the widget window owns its dedicated single-instance mutex."""
    if not plat.is_windows():
        return False
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenMutexW(0x00100000, False, "collie-wallpaper-panel")
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
    except Exception:
        pass
    return False


def engine_running() -> bool:
    """Is the BACKGROUND wallpaper engine running? The app window is the same exe in --window mode,
    so the old tasklist-by-image-name check saw an open `collie app` window and skipped the wallpaper
    forever ("already running", desktop bare). Ask for the bg instance's own mutex instead."""
    if not plat.is_windows():
        return False
    try:
        import ctypes
        SYNCHRONIZE = 0x00100000
        h = ctypes.windll.kernel32.OpenMutexW(SYNCHRONIZE, False, "collie-wallpaper-bg")
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    except Exception:
        return False


def command_running() -> bool:
    """Whether the hidden global-command host owns its dedicated single-instance mutex."""
    if not plat.is_windows():
        return False
    try:
        import ctypes
        SYNCHRONIZE = 0x00100000
        h = ctypes.windll.kernel32.OpenMutexW(SYNCHRONIZE, False, "collie-wallpaper-command")
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    except Exception:
        return False


def app_running() -> bool:
    """Whether the ordinary native Collie window is open.

    The interview-demo launcher needs to replace that window with one bound to an isolated state
    directory.  Checking the per-mode mutex keeps this precise: the command capsule and desktop
    engine use the same executable, so a process-name check cannot tell them apart.
    """
    if not plat.is_windows():
        return False
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenMutexW(0x00100000, False, "collie-wallpaper-window")
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
    except Exception:
        pass
    return False


def _command_status_path() -> str:
    return os.path.join(_collie_home(), "command-host.json")


def command_status() -> dict:
    """Return the native host's attested registration result, never mutex-derived readiness."""
    try:
        with open(_command_status_path(), "r", encoding="utf-8-sig") as f:
            row = json.load(f)
        if not isinstance(row, dict):
            return {}
        state = str(row.get("state") or "")
        if state not in ("registered", "disabled", "invalid", "unavailable"):
            return {}
        row["state"] = state
        row["chord"] = str(row.get("chord") or "")
        row["error"] = int(row.get("error") or 0)
        row["pid"] = int(row.get("pid") or 0)
        # The voice policy is part of the native host's attestation, not a UI guess.  Reject an
        # old receipt that predates this field so run_command() replaces that host instead of
        # claiming voice is off while the old binary still auto-grants microphone access.
        if "voice_enabled" not in row:
            return {}
        voice = row.get("voice_enabled")
        if isinstance(voice, bool):
            row["voice_enabled"] = voice
        elif str(voice).strip().lower() in ("on", "1", "true", "yes"):
            row["voice_enabled"] = True
        elif str(voice).strip().lower() in ("off", "0", "false", "no"):
            row["voice_enabled"] = False
        else:
            return {}
        if "mouse_shortcut" in row:
            row["mouse_shortcut"] = str(row.get("mouse_shortcut") or "off").strip().lower()
        if "mouse_shortcut_state" in row:
            row["mouse_shortcut_state"] = str(row.get("mouse_shortcut_state") or "")
        if "mouse_error" in row:
            row["mouse_error"] = int(row.get("mouse_error") or 0)
        if row["pid"] <= 0 or not _pid_alive(row["pid"]):
            return {}
        return row
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _pid_alive(pid: int) -> bool:
    if plat.is_windows():
        try:
            import ctypes
            from ctypes import wintypes
            SYNCHRONIZE = 0x00100000
            WAIT_TIMEOUT = 0x00000102
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
            if not handle:
                return False
            try:
                return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
            finally:
                kernel32.CloseHandle(handle)
        except (OSError, ValueError, TypeError, AttributeError):
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        # A process we cannot signal still exists; status is non-authority metadata.
        return True
    except (OSError, ValueError, TypeError):
        return False


def _clear_command_status() -> None:
    try:
        os.remove(_command_status_path())
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _wait_command_status(proc=None, timeout: float = 8.0) -> dict:
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        row = command_status()
        if row:
            return row
        if proc is not None:
            try:
                if proc.poll() is not None:
                    break
            except (AttributeError, OSError):
                pass
        time.sleep(0.05)
    return {}


def _hotkey_off(value: str) -> bool:
    return str(value or "").strip().lower() in ("off", "none", "disabled")


def _voice_input_on(value) -> bool:
    """Normalize the user-facing on/off setting before crossing the native-process boundary."""
    if value is None or str(value).strip() == "":
        return True
    normalized = str(value).strip().lower()
    if normalized in ("on", "1", "true", "yes"):
        return True
    # A malformed privacy switch fails closed. Settings itself only emits on/off, but explicit
    # environments and older launchers may supply other strings.
    return False


def _command_failure(row: dict) -> str:
    state = row.get("state") or "no handshake"
    chord = row.get("chord") or "configured shortcut"
    error = row.get("error") or 0
    if state == "unavailable":
        detail = " (Windows error %s)" % error if error else ""
        return "%s is already owned by another app%s" % (chord, detail)
    if state == "invalid":
        return "%s is not a supported shortcut" % chord
    if state == "launch-error" and row.get("detail"):
        return "native command host could not start: %s" % row.get("detail")
    return "native command host did not report shortcut registration"


def _spawn_command_host(exe: str, port: int) -> dict:
    _clear_command_status()
    env = _command_env(port)
    try:
        proc = subprocess.Popen([exe, "--command"], cwd=src_dir(), env=env, **_quiet())
    except Exception as exc:
        return {"state": "launch-error", "detail": str(exc)}
    row = _wait_command_status(proc)
    if not row:
        return {"state": "handshake-timeout"}
    # A running full app may temporarily own the chord as the crash fallback. Its native ownership
    # timer releases that registration when this command-host mutex appears; give the hidden host a
    # bounded chance to retry and publish the later registered receipt instead of killing it on the
    # first transient ERROR_HOTKEY_ALREADY_REGISTERED result.
    if row.get("state") == "unavailable":
        deadline = time.time() + 6.0
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.2)
            newer = command_status()
            if newer.get("state") == "registered":
                return newer
            if newer and newer.get("state") not in ("unavailable",):
                row = newer
                break
    return row


def _command_env(port: int) -> dict:
    # CLI entrypoints call settings.apply(), but this helper is also used from the running web
    # server after a live Settings save and from embedding callers. Resolve it here so the native
    # process always receives the effective chord, including the explicit `off` choice.
    try:
        from . import settings
        hotkey = settings.get("GLOBAL_HOTKEY", "ctrl+shift+space") or "ctrl+shift+space"
        mouse_shortcut = settings.get("MOUSE_SHORTCUT", "off") or "off"
        voice_enabled = _voice_input_on(settings.get("VOICE_INPUT", "on"))
    except Exception:
        hotkey = os.environ.get("COLLIE_GLOBAL_HOTKEY") or "ctrl+shift+space"
        mouse_shortcut = os.environ.get("COLLIE_MOUSE_SHORTCUT") or "off"
        voice_enabled = _voice_input_on(os.environ.get("COLLIE_VOICE_INPUT", "on"))
    return dict(os.environ,
                COLLIE_WALLPAPER_URL="http://127.0.0.1:%d/?capsule=1" % port,
                COLLIE_GLOBAL_HOTKEY=str(hotkey),
                COLLIE_MOUSE_SHORTCUT=str(mouse_shortcut),
                COLLIE_VOICE_INPUT="true" if voice_enabled else "false",
                COLLIE_COMMAND_STATUS=_command_status_path())


# ── the operations ───────────────────────────────────────────────────────────
def run(port_pref: int = 8787, boot: bool = False) -> int:
    """Bring the wallpaper up: ensure the server (free port) → build+attach the engine. On `boot`
    (windowless autostart entry) the server is run IN THIS process so pythonw stays alive hosting it;
    interactively it is spawned as a child so the shell returns."""
    if not plat.is_windows():
        print("collie wallpaper: the behind-icons engine is Windows-only. On this OS use `collie "
              "web` (browser) or the borderless-window fallback.", file=sys.stderr)
        return 2
    if not webview2_present():
        print("collie wallpaper: WebView2 runtime not found. install it:\n"
              "  winget install Microsoft.EdgeWebView2Runtime", file=sys.stderr)
        return 3
    # REUSE a collie server already serving the preferred port — only pick a different free port
    # when nothing of ours is there (otherwise a second `collie wallpaper` spawns a duplicate server).
    port = port_pref if server_up(port_pref) else free_port(port_pref)
    if not server_up(port):
        start_server_windowless(port)
    for _ in range(90):                                    # wait up to ~45s
        if server_up(port):
            break
        time.sleep(0.5)
    if not server_up(port):
        print("collie wallpaper: server did not come up on port %d — see %s"
              % (port, os.path.join(_collie_home(), "wallpaper-web.log")), file=sys.stderr)
        return 1
    if not engine_running() or not panel_running():
        if engine_running() or panel_running():
            stop()                       # never leave one half of the desktop running alone
            time.sleep(1.0)
        ok = launch_engine(port)
        print("collie wallpaper · http://127.0.0.1:%d/ambient · %s"
              % (port, "ground + panel launched" if ok else "engine failed to build/launch"), flush=True)
        if not ok:
            return 1
    else:
        print("collie wallpaper · already running · http://127.0.0.1:%d/ambient" % port)
    return 0


def run_app(port_pref: int = 8787, url_path: str = "/") -> int:
    """Open collie as a normal desktop APP WINDOW — the same WebView2 host in --window mode, showing
    the full GUI, with the server started windowless behind it. This is what the installer's desktop
    shortcut launches: a real program with a taskbar entry and icon, instead of a browser tab showing
    127.0.0.1:8787 that gets lost among the user's other tabs."""
    if not plat.is_windows():
        print("collie app: the native window is Windows-only — use `collie web` here.", file=sys.stderr)
        return 2
    if not webview2_present():
        print("collie app: WebView2 runtime not found. install it:\n"
              "  winget install Microsoft.EdgeWebView2Runtime", file=sys.stderr)
        return 3
    port = port_pref if server_up(port_pref) else free_port(port_pref)
    if not server_up(port):
        start_server_windowless(port)
    for _ in range(90):
        if server_up(port):
            break
        time.sleep(0.5)
    if not server_up(port):
        print("collie app: server did not come up on port %d — see %s"
              % (port, os.path.join(_collie_home(), "wallpaper-web.log")), file=sys.stderr)
        return 1
    exe = build_engine()
    if not exe:
        print("collie app: could not build the window host", file=sys.stderr)
        return 1
    # Give the dedicated capsule process first ownership of the global chord. The full app remains
    # a fallback for someone who starts the EXE directly, but normal `collie app` must summon the
    # small command surface rather than cover the current application with the whole Workbench.
    if not command_running():
        configured = _command_env(port).get("COLLIE_GLOBAL_HOTKEY", "ctrl+shift+space")
        if not _hotkey_off(configured):
            status = _spawn_command_host(exe, port)
            if status.get("state") != "registered":
                print("collie app: command shortcut unavailable — %s" % _command_failure(status),
                      file=sys.stderr)
    # The full window is the fallback hotkey owner when no command host exists, so it must receive
    # the same resolved setting too (especially `off`) rather than inheriting an arbitrary parent env.
    # Internal callers may open a first-party destination such as Today.  Keep the native host
    # loopback-only even if a future CLI argument accidentally reaches this helper.
    url_path = str(url_path or "/")
    if not url_path.startswith("/") or url_path.startswith("//"):
        url_path = "/"
    env = _command_env(port)
    env["COLLIE_WALLPAPER_URL"] = "http://127.0.0.1:%d%s" % (port, url_path)
    try:
        subprocess.Popen([exe, "--window"], cwd=src_dir(), env=env, **_quiet())
    except Exception as e:
        print("collie app: %s" % e, file=sys.stderr)
        return 1
    print("collie app · http://127.0.0.1:%d%s · window opened" % (port, url_path))
    return 0


def run_command(port_pref: int = 8787, boot: bool = False) -> int:
    """Keep Collie's typed/voice outcome capsule available behind a global shortcut.

    The host is a separate, hidden WebView2 process and identity from the full app and wallpaper.
    Ctrl+Shift+Space (or ``COLLIE_GLOBAL_HOTKEY``) shows it on the active display. With
    ``COLLIE_VOICE_INPUT`` enabled the page starts listening; with voice off it focuses the typed
    input and the shortcut remains registered.
    """
    if not plat.is_windows():
        print("collie command: the global capsule is currently Windows-only; use `collie app`.",
              file=sys.stderr)
        return 2
    if not webview2_present():
        print("collie command: WebView2 runtime not found. install it:\n"
              "  winget install Microsoft.EdgeWebView2Runtime", file=sys.stderr)
        return 3
    command_env = _command_env(port_pref)
    configured = command_env.get("COLLIE_GLOBAL_HOTKEY", "ctrl+shift+space")
    wanted_voice = _voice_input_on(command_env.get("COLLIE_VOICE_INPUT", "true"))
    wanted_mouse = str(command_env.get("COLLIE_MOUSE_SHORTCUT", "off") or "off").strip().lower()
    if _hotkey_off(configured):
        if command_running():
            stop_command()
        print("collie command · shortcut is Off · text command remains available in the app")
        return 0
    if command_running():
        row = _wait_command_status(timeout=2.0)
        if (row.get("state") == "registered" and row.get("voice_enabled") is wanted_voice
                and row.get("mouse_shortcut", "off") == wanted_mouse):
            print("collie command · already listening · %s" % row.get("chord"))
            return 0
        # Upgrade an old/no-handshake host, or replace a host whose attested voice policy differs
        # from Settings. Its mutex alone is not evidence that the shortcut or mic boundary is true.
        stop_command()
    port = port_pref if server_up(port_pref) else free_port(port_pref)
    if not server_up(port):
        start_server_windowless(port)
    for _ in range(90):
        if server_up(port):
            break
        time.sleep(0.5)
    if not server_up(port):
        print("collie command: server did not come up on port %d — see %s"
              % (port, os.path.join(_collie_home(), "wallpaper-web.log")), file=sys.stderr)
        return 1
    exe = build_engine()
    if not exe:
        print("collie command: could not build the native command host", file=sys.stderr)
        return 1
    row = _spawn_command_host(exe, port)
    if (row.get("state") == "registered" and row.get("voice_enabled") is wanted_voice
            and row.get("mouse_shortcut", "off") == wanted_mouse
            and (wanted_mouse == "off" or row.get("mouse_shortcut_state") == "registered")):
        print("collie command · %s · ready" % row.get("chord"))
        return 0
    # Do not leave a mutex-owning but unusable host around: it would also prevent the full app's
    # fallback registration and make later retries falsely look healthy.
    try:
        stop_command()
    except Exception:
        pass
    print("collie command: shortcut is not ready — %s" % _command_failure(row), file=sys.stderr)
    return 1


def install() -> int:
    """Register a per-machine, hidden logon autostart. Generates a .pyw launcher with THIS machine's
    resolved package path + a .vbs that runs it windowless — no hardcoded repo/python paths."""
    if not plat.is_windows():
        print("collie wallpaper --install is Windows-only.", file=sys.stderr)
        return 2
    boot_pyw = os.path.join(_collie_home(), "wallpaper-boot.pyw")
    log = os.path.join(_collie_home(), "wallpaper-boot.log")
    with open(boot_pyw, "w", encoding="utf-8") as f:
        # repr() the paths, not r'%s' — a username with an apostrophe (C:\Users\O'Brien) closes a
        # raw string early and the generated boot script dies with a SyntaxError, so the wallpaper
        # never starts. repr() emits a correctly-escaped string literal for any path.
        f.write(
            "# auto-generated by `collie wallpaper --install` — launches the wallpaper at logon.\n"
            "import sys, os\n"
            "sys.path.insert(0, %s)\n"
            "sys.stdin = open(os.devnull, 'r')\n"
            "f = open(%s, 'a', encoding='utf-8'); sys.stdout = sys.stderr = f\n"
            "from harness.cli import main\n"
            "sys.argv = ['collie', 'wallpaper', '--boot']\n"
            "sys.exit(main())\n" % (repr(_pkg_parent()), repr(log)))
    vbs = _startup_vbs()
    os.makedirs(os.path.dirname(vbs), exist_ok=True)
    with open(vbs, "w", encoding="utf-8") as f:
        # Chr(34) is a literal double-quote — safer than VBScript's ""-doubling for quoting the two
        # paths (which often contain spaces, e.g. "C:\Users\First Last"). 0 = hidden, False = no wait.
        f.write("' collie desktop wallpaper - hidden logon autostart (auto-generated).\n"
                "q = Chr(34)\n"
                'CreateObject("WScript.Shell").Run q & "%s" & q & " " & q & "%s" & q, 0, False\n'
                % (pythonw(), boot_pyw))
    # Also START it now, not only at the next logon — someone who just ticked "enable the wallpaper"
    # (in Setup or via this command) expects to SEE it immediately, not after a reboot. Spawn the very
    # same windowless launcher the .vbs fires at logon, detached so it outlives this process.
    started = False
    try:
        flags = 0x00000008 | 0x08000000   # DETACHED_PROCESS | CREATE_NO_WINDOW
        subprocess.Popen([pythonw(), boot_pyw], creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        started = True
    except Exception:
        pass
    print("collie wallpaper: autostart installed%s\n"
          "  launcher: %s\n  startup : %s\n  disable : collie wallpaper --uninstall"
          % (" + started now" if started else " (starts at next logon)", boot_pyw, vbs))
    return 0


def install_command() -> int:
    """Start the low-footprint command capsule at logon, without enabling the wallpaper."""
    if not plat.is_windows():
        print("collie command --install is Windows-only.", file=sys.stderr)
        return 2
    boot_pyw = os.path.join(_collie_home(), "command-boot.pyw")
    log = os.path.join(_collie_home(), "command-boot.log")
    with open(boot_pyw, "w", encoding="utf-8") as f:
        f.write(
            "# auto-generated by `collie command --install` — keeps the global capsule ready.\n"
            "import sys, os\n"
            "sys.path.insert(0, %s)\n"
            "sys.stdin = open(os.devnull, 'r')\n"
            "f = open(%s, 'a', encoding='utf-8'); sys.stdout = sys.stderr = f\n"
            "from harness.cli import main\n"
            "sys.argv = ['collie', 'command', '--boot']\n"
            "sys.exit(main())\n" % (repr(_pkg_parent()), repr(log)))
    vbs = _command_startup_vbs()
    os.makedirs(os.path.dirname(vbs), exist_ok=True)
    with open(vbs, "w", encoding="utf-8") as f:
        f.write("' Collie global command capsule - hidden logon autostart (auto-generated).\n"
                "q = Chr(34)\n"
                'CreateObject("WScript.Shell").Run q & "%s" & q & " " & q & "%s" & q, 0, False\n'
                % (pythonw(), boot_pyw))
    try:
        from . import settings
        configured = settings.get("GLOBAL_HOTKEY", "ctrl+shift+space") or "ctrl+shift+space"
    except Exception:
        configured = os.environ.get("COLLIE_GLOBAL_HOTKEY") or "ctrl+shift+space"
    rc = run_command()
    state = "disabled by setting" if _hotkey_off(configured) else (
        "registered now" if rc == 0 else "installed, but registration failed")
    print("collie command: global shortcut autostart installed · %s\n"
          "  shortcut : %s\n  startup  : %s\n  disable  : collie command --uninstall"
          % (state, configured, vbs))
    return rc


def uninstall() -> int:
    vbs = _startup_vbs()
    boot_pyw = os.path.join(_collie_home(), "wallpaper-boot.pyw")
    removed = []
    for p in (vbs, boot_pyw):
        try:
            if os.path.exists(p):
                os.remove(p)
                removed.append(p)
        except OSError:
            pass
    # Symmetry with install() (which STARTS the engine now): turning the wallpaper off must also STOP
    # the running one, not just delete the autostart — otherwise it stays on screen until next logoff,
    # contradicting the "keep your normal wallpaper" promise. Graceful signal, never -Force.
    try:
        stop()
    except Exception:
        pass
    print("collie wallpaper: autostart removed" if removed else "collie wallpaper: autostart was not installed")
    return 0


def uninstall_command() -> int:
    removed = []
    for p in (_command_startup_vbs(), os.path.join(_collie_home(), "command-boot.pyw")):
        try:
            if os.path.exists(p):
                os.remove(p)
                removed.append(p)
        except OSError:
            pass
    try:
        stop_command()
    except Exception:
        pass
    print("collie command: global shortcut removed" if removed
          else "collie command: global shortcut was not installed")
    return 0


def _signal_quit(event_name: str) -> None:
    if not plat.is_windows():
        return
    try:
        import ctypes
        EVENT_MODIFY_STATE = 0x0002
        h = ctypes.windll.kernel32.OpenEventW(EVENT_MODIFY_STATE, False, event_name)
        if h:
            ctypes.windll.kernel32.SetEvent(h)
            ctypes.windll.kernel32.CloseHandle(h)
            time.sleep(2)
    except Exception:
        pass


def stop() -> int:
    """Signal the engine's named-event clean shutdown (never -Force — that orphans WebView2 COM),
    then best-effort reap."""
    _signal_quit(QUIT_EVENT)
    _signal_quit(PANEL_QUIT_EVENT)        # the desktop is two processes now; stop means both
    plat.rmtree  # noqa: keep import used
    print("collie wallpaper: stop signalled")
    return 0


def stop_command() -> int:
    """Gracefully stop only the hidden command host, leaving app and wallpaper untouched."""
    _signal_quit(COMMAND_QUIT_EVENT)
    _clear_command_status()
    print("collie command: stop signalled")
    return 0


def stop_app() -> int:
    """Gracefully close only the ordinary native app window."""
    _signal_quit(APP_QUIT_EVENT)
    print("collie app: stop signalled")
    return 0
