"""Device context — what the person is doing *right now* on this computer.

The global capsule already opens instantly; what it lacked was understanding.  This module is the
"Collie understands what you're doing now" half of the thesis: the foreground application and
window, the text the person has selected, (opt-in) the clipboard, the browser tab title, and the
project the window belongs to.  It is read once per capsule open / run start, never continuously
recorded: there is no screen history, no keylogging, no background observer.

Privacy rules
-------------
* Each channel is gated by a setting (``CONTEXT_ACTIVE_WINDOW``, ``CONTEXT_SELECTION``,
  ``CONTEXT_CLIPBOARD`` — off by default — ``CONTEXT_BROWSER_TAB``).  The caller passes the
  resolved flags; this module never reads settings itself so tests stay hermetic.
* Captured text is bounded (selection 4000 chars, clipboard 2000) and is shown to the person in
  the capsule before it reaches a model.
* Nothing here is persisted; a snapshot is a value.

Platform notes
--------------
* Windows: ``ctypes`` for the foreground window/process (no dependencies), PowerShell + UI
  Automation ``TextPattern`` for the selection of *that* window (so it still works after the
  capsule itself took focus).  The hidden-window flags from ``plat.no_window_kwargs`` are used so
  nothing flashes.
* macOS: ``osascript`` (System Events) for the frontmost process / window, Accessibility
  ``AXSelectedText`` for the selection.  Requires the Accessibility permission the desktop app
  already asks for.
* Linux: ``xdotool`` / ``xclip`` when present, otherwise only cwd-derived context.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time

__all__ = ["snapshot", "foreground", "selection", "clipboard", "infer_project", "chips"]

SELECTION_MAX = 4000
CLIPBOARD_MAX = 2000
_APP_NAMES = {
    "chrome.exe": "Chrome", "msedge.exe": "Edge", "firefox.exe": "Firefox", "brave.exe": "Brave",
    "code.exe": "VS Code", "cursor.exe": "Cursor", "windowsterminal.exe": "Terminal", "wt.exe": "Terminal",
    "cmd.exe": "Command Prompt", "powershell.exe": "PowerShell", "pwsh.exe": "PowerShell",
    "explorer.exe": "Explorer", "slack.exe": "Slack", "discord.exe": "Discord", "notepad.exe": "Notepad",
    "winword.exe": "Word", "excel.exe": "Excel", "powerpnt.exe": "PowerPoint", "outlook.exe": "Outlook",
    "teams.exe": "Teams", "ms-teams.exe": "Teams", "zoom.exe": "Zoom", "obsidian.exe": "Obsidian",
    "notion.exe": "Notion", "figma.exe": "Figma", "collie-wallpaper.exe": "Collie", "spotify.exe": "Spotify",
    "acrobat.exe": "Acrobat", "devenv.exe": "Visual Studio", "idea64.exe": "IntelliJ", "pycharm64.exe": "PyCharm",
}
_BROWSERS = {"Chrome", "Edge", "Firefox", "Brave", "Safari", "Arc"}
_SELF_TITLES = ("Collie · Dispatch", "Collie — ", "Ask Collie")


# ------------------------------------------------------------------------------ public API
def snapshot(*, active_window: bool = True, selection_text: bool = True, clipboard_text: bool = False,
             browser_tab: bool = True, cwd: str = "", state=None, wait: float = 1.2) -> dict:
    """One bounded read of the device context.  Fast parts are synchronous; the selection read (a
    subprocess) is given ``wait`` seconds and otherwise reported as ``pending`` so a caller can ask
    again without blocking the capsule."""
    out = {"at": int(time.time()), "platform": sys.platform, "foreground": None, "selection": None,
           "clipboard": None, "browser": None, "project": None, "cwd": cwd or ""}
    fg = None
    if active_window:
        try:
            fg = foreground()
        except Exception as exc:
            fg = {"error": str(exc)[:120]}
        out["foreground"] = fg
    if selection_text and fg and fg.get("hwnd") is not None or (selection_text and sys.platform == "darwin"):
        out["selection"] = _selection_with_timeout(fg, wait)
    elif selection_text:
        out["selection"] = {"state": "unavailable", "text": ""}
    if clipboard_text:
        try:
            out["clipboard"] = clipboard()
        except Exception as exc:
            out["clipboard"] = {"error": str(exc)[:120], "text": ""}
    if browser_tab and fg and fg.get("app") in _BROWSERS and fg.get("title"):
        out["browser"] = {"title": _strip_browser_suffix(fg["title"]), "app": fg["app"]}
    try:
        out["project"] = infer_project(fg, cwd=cwd, state=state)
    except Exception:
        out["project"] = None
    out["self"] = bool(fg and (fg.get("app") == "Collie" or any(t in (fg.get("title") or "") for t in _SELF_TITLES)))
    return out


def chips(snap: dict) -> list[dict]:
    """Short labels for the capsule: ``Chrome · Sauna``, ``Selected text · 38 words``, ``Project · Collie``."""
    out = []
    fg = (snap or {}).get("foreground") or {}
    if fg.get("app") and not (snap or {}).get("self"):
        label = fg["app"]
        title = _strip_browser_suffix(fg.get("title") or "")
        if title and title.lower() != label.lower():
            label += " · " + _clip(title, 34)
        out.append({"kind": "app", "label": label})
    sel = (snap or {}).get("selection") or {}
    if sel.get("text"):
        words = len(sel["text"].split())
        out.append({"kind": "selection", "label": "Selected text · %d word%s" % (words, "" if words == 1 else "s")})
    elif sel.get("state") == "pending":
        out.append({"kind": "selection", "label": "Selected text · checking…"})
    clip = (snap or {}).get("clipboard") or {}
    if clip.get("text"):
        out.append({"kind": "clipboard", "label": "Clipboard · %d chars" % len(clip["text"])})
    pr = (snap or {}).get("project") or {}
    if pr.get("name"):
        out.append({"kind": "project", "label": "Project · " + pr["name"]})
    return out


# ------------------------------------------------------------------------------ foreground
def foreground() -> dict | None:
    if sys.platform.startswith("win"):
        return _win_foreground()
    if sys.platform == "darwin":
        return _mac_foreground()
    return _linux_foreground()


def _win_foreground() -> dict | None:
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = ""
    handle = kernel32.OpenProcess(0x1000, False, pid.value)   # PROCESS_QUERY_LIMITED_INFORMATION
    if handle:
        try:
            size = wintypes.DWORD(2048)
            pbuf = ctypes.create_unicode_buffer(2048)
            if kernel32.QueryFullProcessImageNameW(handle, 0, pbuf, ctypes.byref(size)):
                exe = os.path.basename(pbuf.value)
        finally:
            kernel32.CloseHandle(handle)
    app = _APP_NAMES.get(exe.lower(), exe[:-4] if exe.lower().endswith(".exe") else exe) or "Unknown"
    return {"hwnd": int(hwnd), "pid": int(pid.value), "exe": exe, "app": app, "title": buf.value}


def _mac_foreground() -> dict | None:
    script = ('tell application "System Events"\n'
              ' set p to first application process whose frontmost is true\n'
              ' set n to name of p\n set u to unix id of p\n set t to ""\n'
              ' try\n  set t to name of front window of p\n end try\n'
              ' return n & linefeed & u & linefeed & t\nend tell')
    res = _run(["osascript", "-e", script], timeout=1.5)
    if not res:
        return None
    parts = res.split("\n")
    app = parts[0].strip() if parts else ""
    pid = int(parts[1]) if len(parts) > 1 and parts[1].strip().isdigit() else 0
    title = parts[2].strip() if len(parts) > 2 else ""
    return {"hwnd": None, "pid": pid, "exe": app, "app": app, "title": title}


def _linux_foreground() -> dict | None:
    name = _run(["xdotool", "getactivewindow", "getwindowname"], timeout=1.0)
    if name is None:
        return None
    pid = _run(["xdotool", "getactivewindow", "getwindowpid"], timeout=1.0) or "0"
    app = ""
    try:
        with open("/proc/%s/comm" % pid.strip()) as f:
            app = f.read().strip()
    except Exception:
        pass
    return {"hwnd": None, "pid": int(pid.strip() or 0), "exe": app, "app": app or "Unknown", "title": name.strip()}


# ------------------------------------------------------------------------------ selection
_SEL_CACHE: dict = {}          # key -> (started_at, result dict, Event)
_SEL_CACHE_TTL = 12.0          # a second call within this window reuses the in-flight / finished read
_SEL_LOCK = threading.Lock()


def _selection_with_timeout(fg: dict | None, wait: float) -> dict:
    """Start (or reuse) the selection read for this window and wait at most ``wait`` seconds.

    The read is a subprocess (PowerShell UIA / osascript) that can take longer than a capsule
    should block, so the first call may answer ``pending`` and a call a moment later returns the
    finished text — the same window keeps the same in-flight read."""
    key = ((fg or {}).get("hwnd"), (fg or {}).get("pid"), (fg or {}).get("title"))
    now = time.time()
    with _SEL_LOCK:
        entry = _SEL_CACHE.get(key)
        if entry and now - entry[0] <= _SEL_CACHE_TTL:
            started, result, done = entry
        else:
            result = {"state": "pending", "text": ""}
            done = threading.Event()

            def _work(res=result, ev=done, fgw=fg):
                try:
                    text = selection(fgw)
                    res["text"] = text or ""
                    res["state"] = "ok" if text else "none"
                except Exception as exc:
                    res["state"] = "error"
                    res["error"] = str(exc)[:120]
                finally:
                    ev.set()

            threading.Thread(target=_work, daemon=True).start()
            _SEL_CACHE[key] = (now, result, done)
            for k in [k for k, v in _SEL_CACHE.items() if now - v[0] > _SEL_CACHE_TTL * 4]:
                _SEL_CACHE.pop(k, None)
    done.wait(max(0.0, float(wait)))
    return dict(result)


def selection(fg: dict | None) -> str:
    """The selected text in the given foreground window (best effort, bounded)."""
    if sys.platform.startswith("win"):
        if not fg or not fg.get("hwnd"):
            return ""
        return _win_selection(int(fg["hwnd"]))
    if sys.platform == "darwin":
        return _mac_selection()
    return _run(["xclip", "-o", "-selection", "primary"], timeout=1.0) or ""


_PS_SELECTION = r"""
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$h = [IntPtr]::new(%d)
function SelOf($el) {
  if ($el -eq $null) { return $null }
  $p = $null
  if ($el.TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern, [ref]$p)) {
    $ranges = $p.GetSelection()
    if ($ranges -ne $null -and $ranges.Length -gt 0) {
      $t = $ranges[0].GetText(%d)
      if ($t -ne $null -and $t.Length -gt 0) { return $t }
    }
  }
  return $null
}
$text = $null
try {
  $root = [System.Windows.Automation.AutomationElement]::FromHandle($h)
  $focused = [System.Windows.Automation.AutomationElement]::FocusedElement
  if ($focused -ne $null) {
    $fpid = $focused.Current.ProcessId
    if ($fpid -eq $root.Current.ProcessId) { $text = SelOf $focused }
  }
  if ($text -eq $null) {
    $cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::IsTextPatternAvailableProperty, $true)
    $els = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)
    $n = 0
    foreach ($e in $els) { $n++; if ($n -gt 60) { break }; $text = SelOf $e; if ($text -ne $null) { break } }
  }
} catch { }
if ($text -ne $null) { [Console]::Out.Write($text) }
"""


def _win_selection(hwnd: int) -> str:
    script = _PS_SELECTION % (hwnd, SELECTION_MAX)
    out = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
               timeout=2.5)
    return (out or "").strip()[:SELECTION_MAX]


def _mac_selection() -> str:
    script = ('tell application "System Events"\n'
              ' set p to first application process whose frontmost is true\n'
              ' try\n  set e to value of attribute "AXFocusedUIElement" of p\n'
              '  set t to value of attribute "AXSelectedText" of e\n  return t\n end try\nend tell\nreturn ""')
    return (_run(["osascript", "-e", script], timeout=1.5) or "").strip()[:SELECTION_MAX]


# ------------------------------------------------------------------------------ clipboard
def clipboard() -> dict:
    text = ""
    if sys.platform.startswith("win"):
        text = _win_clipboard()
    elif sys.platform == "darwin":
        text = _run(["pbpaste"], timeout=1.0) or ""
    else:
        text = _run(["xclip", "-o", "-selection", "clipboard"], timeout=1.0) or ""
    text = text[:CLIPBOARD_MAX]
    return {"text": text, "state": "ok" if text else "none"}


def _win_clipboard() -> str:
    import ctypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    CF_UNICODETEXT = 13
    if not user32.OpenClipboard(0):
        return ""
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return ""
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        kernel32.GlobalLock.restype = ctypes.c_void_p
        ptr = kernel32.GlobalLock(ctypes.c_void_p(handle))
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(ctypes.c_void_p(handle))
    finally:
        user32.CloseClipboard()


# ------------------------------------------------------------------------------ project
_TITLE_PATTERNS = (
    # "file - folder - Visual Studio Code" / "folder - Visual Studio Code" (folders often carry hyphens)
    re.compile(r"^(?:.* - )?(?P<name>[^\\/]+?) - Visual Studio Code$"),
    re.compile(r"^(?:.* - )?(?P<name>[^\\/]+?) - Cursor$"),
    re.compile(r"^(?P<name>[^\\/]+?) (?:– |- )(?:[^-]+ - )?(?:IntelliJ IDEA|PyCharm|WebStorm|GoLand).*$"),
)


def infer_project(fg: dict | None, *, cwd: str = "", state=None) -> dict | None:
    """Which project the person is in: the state's project list wins (it carries meaning), then the
    editor title, then the working directory's git root."""
    title = (fg or {}).get("title") or ""
    if state is not None and title:
        try:
            p = state.find_project(title)
            if p:
                return {"name": p["name"], "path": p.get("path") or "", "source": "window", "id": p["id"]}
        except Exception:
            pass
    for pat in _TITLE_PATTERNS:
        m = pat.match(title)
        if m:
            name = m.group("name").strip()
            hit = None
            if state is not None:
                try:
                    hit = state.find_project(name)
                except Exception:
                    hit = None
            return {"name": hit["name"] if hit else name, "path": (hit or {}).get("path", ""), "source": "window",
                    "id": (hit or {}).get("id", "")}
    if cwd:
        root = _git_root(cwd) or cwd
        base = os.path.basename(root.rstrip("\\/"))
        hit = None
        if state is not None:
            try:
                hit = state.find_project(base) or state.find_project(root)
            except Exception:
                hit = None
        if hit:
            return {"name": hit["name"], "path": hit.get("path") or root, "source": "cwd", "id": hit["id"]}
        if base:
            return {"name": base, "path": root, "source": "cwd", "id": ""}
    return None


def _git_root(path: str) -> str | None:
    cur = os.path.abspath(path)
    for _ in range(12):
        if os.path.isdir(os.path.join(cur, ".git")) or os.path.isfile(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent
    return None


# ------------------------------------------------------------------------------ helpers
def _strip_browser_suffix(title: str) -> str:
    for suffix in (" - Google Chrome", " - Microsoft Edge", " — Mozilla Firefox", " - Mozilla Firefox", " - Brave",
                   " - Personal - Microsoft​ Edge"):
        if title.endswith(suffix):
            return title[: -len(suffix)]
    return title


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _run(argv: list[str], *, timeout: float) -> str | None:
    kwargs = {}
    try:
        from . import plat
        kwargs = plat.no_window_kwargs()
    except Exception:
        kwargs = {}
    try:
        res = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=timeout, stdin=subprocess.DEVNULL, **kwargs)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if res.returncode != 0 and not res.stdout:
        return None
    return res.stdout
