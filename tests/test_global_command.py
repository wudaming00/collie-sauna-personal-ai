"""Contracts for the one-computer / one-Collie native command surface."""

import os
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_native_host_owns_a_real_global_hotkey_and_distinct_lifecycle():
    src = (ROOT / "harness" / "wallpaper" / "Program.cs").read_text(encoding="utf-8")
    assert "RegisterHotKey" in src and "UnregisterHotKey" in src
    assert "WM_HOTKEY" in src and "MOD_NOREPEAT" in src
    assert '"ctrl+shift+space"' in src
    assert 'args[i] == "--command"' in src
    assert '"collie-wallpaper-command"' in src
    assert '"collie-wallpaper-quit-command"' in src
    assert '"webview2-command"' in src
    assert 'Mutex.OpenExisting("collie-wallpaper-command")' in src


def test_native_and_page_command_message_contract_is_exact():
    native = (ROOT / "harness" / "wallpaper" / "Program.cs").read_text(encoding="utf-8")
    page = (ROOT / "harness" / "webui" / "index.html").read_text(encoding="utf-8")
    assert "collie-command" in native and "collie-command-state" in native
    assert "collie-command" in page and "collie-command-state" in page
    assert "PostWebMessageAsJson" in native
    assert 'Environment.GetEnvironmentVariable("COLLIE_VOICE_INPUT")' in native
    assert '(_voiceInputEnabled ? "true" : "false")' in native
    assert '"voice_enabled\\\":"' in native


def test_native_hotkey_uses_explicit_open_close_and_second_press_hides_command_host():
    src = (ROOT / "harness" / "wallpaper" / "Program.cs").read_text(encoding="utf-8")
    toggle = src.split("void ToggleCommand()", 1)[1].split("void PostPendingCommand()", 1)[0]
    post = src.split("void PostPendingCommand()", 1)[1].split("CollieWallpaper()", 1)[0]

    # The native window is authoritative: a second press closes immediately even if Web Speech is
    # unavailable. The first press stays hidden until the page ACK proves that content is painted.
    close_guard = (
        "if (_commandMode && (_commandVisibleRequested || "
        "(_commandBootstrapShownComplete && Visible)))"
    )
    assert close_guard in toggle
    close_branch = toggle.split(
        close_guard, 1
    )[1].split("if (!_commandMode && _commandPageOpen)", 1)[0]
    assert 'QueueCommandAction("close");' in close_branch
    assert "PostPendingCommand();" in close_branch
    assert "Hide();" in close_branch
    assert close_branch.index("PostPendingCommand();") < close_branch.index("Hide();")

    open_branch = toggle.split("if (_commandMode)", 1)[1]
    assert '_commandVisibleRequested = true;' in open_branch
    assert "if (!Visible) Show();" not in open_branch.split("else", 1)[0]
    assert 'QueueCommandAction("open");' in open_branch
    assert '\\"action\\":\\"" + action' in post
    assert '\\"host\\":\\"" + (_commandMode ? "command" : "window")' in post
    assert '\\"action\\":\\"toggle' not in post


def test_command_bootstrap_shown_cannot_erase_a_first_hotkey_request():
    src = (ROOT / "harness" / "wallpaper" / "Program.cs").read_text(encoding="utf-8")
    shown = src.split("Shown += delegate", 1)[1].split("Icon = AppIcon();", 1)[0]

    # The transparent bootstrap form may already report Visible between HandleCreated and Shown.
    # Intent alone must not expose it: only a rendered-page ACK may keep the surface visible.
    assert "bool _commandBootstrapShownComplete;" in src
    assert "(_commandBootstrapShownComplete && Visible)" in src
    assert "if (!_commandPageOpen) { Hide(); Opacity = 1; }" in shown
    assert "_commandVisibleRequested = false" not in shown
    assert "_commandBootstrapShownComplete = true;" in shown
    assert shown.index("if (!_commandPageOpen) { Hide(); Opacity = 1; }") < shown.index(
        "_commandBootstrapShownComplete = true;"
    )


def test_command_native_surface_is_compact_transparent_and_round_without_black_frame():
    src = (ROOT / "harness" / "wallpaper" / "Program.cs").read_text(encoding="utf-8")
    constructor = src.split("CollieWallpaper()", 1)[1].split("async void InitWeb()", 1)[0]
    positioning = src.split("void PositionCommandWindow()", 1)[1].split(
        "void ApplyCommandWindowChrome()", 1
    )[0]
    chrome = src.split("void ApplyCommandWindowChrome()", 1)[1].split(
        "protected override void OnSizeChanged", 1
    )[0]
    ready = src.split("void OnWebReady", 1)[1].split(
        "// Everything below is WALLPAPER-only", 1
    )[0]

    # The host and the host-mode HTML share one 660x176 DIP outline; no larger native rectangle can
    # show through around the card. DPI comes from the target monitor, not a hard-coded physical size.
    assert "GetDpiForWindow(Handle)" in src
    assert "area.Width * 96.0 / dpi" in positioning
    assert "Math.Min(660, Math.Max(320, availableWidthDip))" in positioning
    assert "widthDip < 480 ? 212 : (widthDip < 600 ? 192 : 176)" in src
    assert "ScaleCommandDip(cwDip, dpi)" in positioning
    assert "Math.Min(720" not in positioning and "Math.Min(260" not in positioning

    # Transparency is selected before controller creation and reinforced after initialization. The
    # process environment prevents WebView2's pre-controller opaque flash, but is command-only.
    assert 'if (_commandMode)\n            Environment.SetEnvironmentVariable(' in src
    assert '"WEBVIEW2_DEFAULT_BACKGROUND_COLOR", "00000000"' in src
    assert "BackColor = _commandMode ? CommandFallbackColor : Color.Black;" in constructor
    assert "_web.DefaultBackgroundColor = _commandMode ? Color.Transparent : Color.Black;" in constructor
    assert "_web.DefaultBackgroundColor = _commandMode ? Color.Transparent : Color.Black;" in ready

    # DWM owns the polished Windows 11 outline. A DPI-scaled 18px native region is used only when that
    # API is unsupported, since a custom region would otherwise disable DWM antialiasing/shadow.
    assert "DWMWA_WINDOW_CORNER_PREFERENCE" in chrome and "DWMWCP_ROUND" in chrome
    assert "DWMWA_BORDER_COLOR" in chrome and "0xFFFFFFFE" in chrome
    assert "if (cornerResult == 0)" in chrome
    assert "Region = null;" in chrome
    assert "GetDpiForWindow(Handle)" in chrome
    assert "18.0 * dpi / 96.0" in chrome
    assert "CreateRoundRectRgn(0, 0, ClientSize.Width + 1, ClientSize.Height + 1" in chrome
    assert "Region = next;" in chrome and "previous.Dispose();" in chrome
    assert "ApplyCommandWindowChrome();" in src.split(
        "protected override void OnSizeChanged", 1
    )[1].split("void PresentCommandWindow", 1)[0]
    dpi_changed = src.split("protected override void OnDpiChanged", 1)[1].split(
        "void PresentCommandWindow", 1
    )[0]
    assert "base.OnDpiChanged(e);" in dpi_changed
    assert "PositionCommandWindow(Screen.FromHandle(Handle), true);" in dpi_changed

    # Transparent pixels blend against the active Collie surface in either theme, never a fixed black
    # rectangle. The theme message updates the host substrate while the WebView remains transparent.
    assert "CommandFallbackColor = Color.FromArgb(255, 255, 255)" in src
    assert "CommandFallbackDarkColor = Color.FromArgb(21, 25, 35)" in src
    assert "ApplyCommandSubstrate(dark);" in ready


def test_command_layout_protocol_is_exact_request_scoped_and_never_resurrects_a_window():
    src = (ROOT / "harness" / "wallpaper" / "Program.cs").read_text(encoding="utf-8")
    handler = src.split('JsonStringValue(raw, "type") == "collie-command-layout"', 1)[1].split(
        'raw.IndexOf("collie-command-prepared-state"', 1
    )[0]
    apply = src.split("void ApplyCommandLayout(long requestId, string phase)", 1)[1].split(
        "static int ScaleCommandDip", 1
    )[0]

    assert 'JsonLong(raw, "request_id", -1)' in handler
    assert 'JsonStringValue(raw, "phase")' in handler
    assert "requestId != _commandRequestId" in handler
    assert "!_commandMode || !_commandPageOpen" in handler
    assert "!Visible || _shutdownRequested" in handler
    assert 'phase != "compact" && phase != "conversation"' in handler
    assert "ApplyCommandLayout(requestId, phase);" in handler

    # Repeat every trust boundary in the callee in case a future caller bypasses message dispatch.
    assert "requestId != _commandRequestId" in apply
    assert "!_commandPageOpen || !Visible" in apply
    assert "_shutdownRequested || IsDisposed || !IsHandleCreated" in apply
    for forbidden in ("Show();", "Activate();", "FocusCommandWindow", "SetForegroundWindow", "BringToFront"):
        assert forbidden not in apply


def test_command_conversation_layout_grows_upward_with_dpi_and_work_area_bounds():
    src = (ROOT / "harness" / "wallpaper" / "Program.cs").read_text(encoding="utf-8")
    apply = src.split("void ApplyCommandLayout(long requestId, string phase)", 1)[1].split(
        "static int ScaleCommandDip", 1
    )[0]
    height = src.split("int CommandHeightDip(int widthDip, int workAreaHeightDip)", 1)[1].split(
        "void PositionCommandWindow()", 1
    )[0]
    ack = src.split("void PostCommandLayoutApplied", 1)[1].split(
        "void ApplyCommandLayout", 1
    )[0]

    assert "Math.Min(360, conversationLimit)" in height
    assert "workAreaHeightDip * 0.72" in height
    assert "Math.Max(compact" in height
    assert "Math.Min(360, conversationLimit)" in apply
    assert "workAreaHeightDip * 0.72" in apply
    assert "Math.Max(compactDip" in apply
    assert apply.index("int bottom =") < apply.index("ClientSize =") < apply.index("Location =")
    assert "Location = new Point(left, bottom - Height);" in apply
    assert "ClientSize.Width, targetHeight" in apply

    assert "collie-command-layout-applied" in ack
    assert "requestId != _commandRequestId" in ack
    assert "height_dip" in ack


def test_command_layout_phase_resets_on_replacement_close_navigation_and_cleanup():
    src = (ROOT / "harness" / "wallpaper" / "Program.cs").read_text(encoding="utf-8")
    queue = src.split("void QueueCommandAction(string action)", 1)[1].split(
        "void PostPendingCommand()", 1
    )[0]
    receipt = src.split('raw.IndexOf("collie-command-state"', 1)[1].split(
        'raw.IndexOf("\\\"theme\\\""', 1
    )[0]
    close_receipt = receipt.split("else if (_commandMode && !open)", 1)[1]
    navigation = src.split("NavigationStarting +=", 1)[1].split(
        "_web.CoreWebView2.Navigate(url);", 1
    )[0]
    cleanup = src.split("void Cleanup()", 1)[1]
    dpi = src.split("protected override void OnDpiChanged", 1)[1].split(
        "void PresentCommandWindow", 1
    )[0]

    assert queue.index('_commandLayoutPhase = "compact";') < queue.index("_commandRequestId++;")
    assert '_commandLayoutPhase = "compact";' in close_receipt
    assert '_commandLayoutPhase = "compact";' in navigation
    assert '_commandLayoutPhase = "compact";' in cleanup
    assert cleanup.index("_shutdownRequested = true;") < cleanup.index(
        '_commandLayoutPhase = "compact";'
    )
    assert cleanup.index("_commandRequestId++;") < cleanup.index(
        '_commandLayoutPhase = "compact";'
    )
    assert '_commandPageOpen = false;' in cleanup
    # DPI changes recompute the current phase; they must not silently collapse conversation.
    assert '_commandLayoutPhase = "compact";' not in dpi
    assert "PositionCommandWindow(Screen.FromHandle(Handle), true);" in dpi


def test_command_request_ids_reject_stale_close_receipts_and_deferred_hides():
    native = (ROOT / "harness" / "wallpaper" / "Program.cs").read_text(encoding="utf-8")
    page = (ROOT / "harness" / "webui" / "index.html").read_text(encoding="utf-8")
    queue = native.split("void QueueCommandAction(string action)", 1)[1].split(
        "void PostPendingCommand()", 1
    )[0]
    post = native.split("void PostPendingCommand()", 1)[1].split("CollieWallpaper()", 1)[0]
    receipt = native.split('raw.IndexOf("collie-command-state"', 1)[1].split(
        'raw.IndexOf("\\\"theme\\\""', 1
    )[0]

    assert "_commandRequestId++;" in queue
    assert "if (_commandRequestId <= 0) _commandRequestId = 1;" in queue
    assert "_pendingCommand = true;" in queue
    assert '\\"request_id\\\":" + _commandRequestId.ToString()' in post
    assert '+ ",\\"voice\\":"' in post
    assert '+ "\\",\\"voice\\":"' not in post
    assert "!_pageBridgeReady" in post
    assert "_pendingCommand = false;" not in post
    assert 'JsonLong(raw, "request_id", -1)' in receipt
    assert "if (requestId != _commandRequestId) return;" in receipt
    assert "_pendingCommand = false;" in receipt
    assert "long closingRequest = requestId;" in receipt
    assert "if (closingRequest == _commandRequestId && !_commandPageOpen" in receipt
    assert "&& !_commandVisibleRequested) Hide();" in receipt

    # The page must echo the exact native request id; otherwise the native stale-ACK fence is inert.
    assert "capsuleNativeRequestId = 0" in page
    assert "request_id:capsuleNativeRequestId" in page
    assert "capsuleNativeRequestId = incomingRequestId" in page


def test_command_bridge_ready_gates_show_and_retries_until_matching_ack():
    native = (ROOT / "harness" / "wallpaper" / "Program.cs").read_text(encoding="utf-8")
    page = (ROOT / "harness" / "webui" / "index.html").read_text(encoding="utf-8")
    receipt = native.split('raw.IndexOf("collie-command-state"', 1)[1].split(
        'raw.IndexOf("\\\"theme\\\""', 1
    )[0]
    refresh = native.split("void RefreshCommandHotKeyOwnership()", 1)[1].split(
        "static string OriginOf", 1
    )[0]
    navigation = native.split("NavigationStarting +=", 1)[1].split(
        "_web.CoreWebView2.Navigate(url);", 1
    )[0]
    reveal = native.split("void ScheduleCommandReveal(long openingRequest)", 1)[1].split(
        "void ToggleCommand()", 1
    )[0]
    lease = native.split("void StartCommandFocusLease(long requestId)", 1)[1].split(
        "void ScheduleCommandReveal(long openingRequest)", 1
    )[0]
    authorize = native.split("void TryAuthorizeCommandPresentation(long requestId)", 1)[1].split(
        "void FailCommandPresentation(long requestId, string reason)", 1
    )[0]

    assert "collie-command-ready" in page and "collie-command-ready" in native
    assert 'protocol:2' in page
    assert "_pageBridgeReady = true;" in native
    assert "if (_pendingCommand && _pageBridgeReady) PostPendingCommand();" in refresh
    assert "NavigationStarting +=" in native
    assert "_pageReady = false;" in navigation
    assert "_pageBridgeReady = false;" in navigation
    assert "(_commandBootstrapShownComplete && Visible)" in navigation
    assert "PresentCommandWindow();" in receipt
    assert "openingRequest != _commandRequestId" in receipt
    assert '\"bridge_ready\\\":"' in native
    assert '\"pending_command\\\":"' in native

    # Equal-id redelivery is acknowledged without replaying open/voice side effects.
    assert "incomingRequestId === capsuleNativeHandledRequestId" in page
    assert "postCapsuleNativeState(!capsuleLayer.hidden);" in page
    assert "collie-command-presented" in page and "collie-command-presented" in native
    assert "_pendingPresentation = true;" in authorize
    assert "PostPendingPresentation();" in native
    assert "collie-command-presented-state" in native
    assert "capsuleNativePresentedRequestId" in page
    assert '"surface_ready\\\":"' in native
    assert '"voice_available\\\":"' in native
    assert '"voice_started\\\":"' in native
    assert '"voice_started_once\\\":"' in native
    assert "Opacity = 0;" in native
    assert "ScheduleCommandReveal(openingRequest);" in receipt
    assert "reveal.Interval = 320;" in reveal
    assert "_commandSurfaceReady = true;" in native
    assert "Opacity = 1;" in reveal
    assert '\\"voice_started\\":true' in native
    assert "if (_commandVoiceStarted) _commandVoiceStartedOnce = true;" in native


def test_rapid_command_transitions_cancel_stale_reveal_and_focus_the_webview_once_ready():
    native = (ROOT / "harness" / "wallpaper" / "Program.cs").read_text(encoding="utf-8")
    queue = native.split("void QueueCommandAction(string action)", 1)[1].split(
        "void PostPendingCommand()", 1
    )[0]
    reveal = native.split("void ScheduleCommandReveal(long openingRequest)", 1)[1].split(
        "void ToggleCommand()", 1
    )[0]
    focus = native.split("bool FocusCommandWindow(long requestId)", 1)[1].split(
        "void StartCommandFocusLease(long requestId)", 1
    )[0]
    lease = native.split("void StartCommandFocusLease(long requestId)", 1)[1].split(
        "void ScheduleCommandReveal(long openingRequest)", 1
    )[0]
    cancel_lease = native.split("void CancelCommandFocusLease()", 1)[1].split(
        "void ReleaseCommandForegroundLock()", 1
    )[0]
    release_lock = native.split("void ReleaseCommandForegroundLock()", 1)[1].split(
        "void TryLockCommandForeground(long requestId)", 1
    )[0]
    acquire_lock = native.split("void TryLockCommandForeground(long requestId)", 1)[1].split(
        "void TryAuthorizeCommandPresentation(long requestId)", 1
    )[0]
    authorize = native.split("void TryAuthorizeCommandPresentation(long requestId)", 1)[1].split(
        "void FailCommandPresentation(long requestId, string reason)", 1
    )[0]
    fail_presentation = native.split(
        "void FailCommandPresentation(long requestId, string reason)", 1
    )[1].split("bool FocusCommandWindow(long requestId)", 1)[0]
    post_presentation = native.split("void PostPendingPresentation()", 1)[1].split(
        "CollieWallpaper()", 1
    )[0]
    receipt = native.split('raw.IndexOf("collie-command-state"', 1)[1].split(
        'raw.IndexOf("\\\"theme\\\""', 1
    )[0]
    presented = native.split('raw.IndexOf("collie-command-presented-state"', 1)[1].split(
        'raw.IndexOf("collie-command-state"', 1
    )[0]
    navigation = native.split("NavigationStarting +=", 1)[1].split(
        "_web.CoreWebView2.Navigate(url);", 1
    )[0]
    cleanup = native.split("void Cleanup()", 1)[1]

    # Each hotkey action invalidates the prior 320ms warm-up before advancing its request id. Thus
    # open-close-open has exactly one timer capable of revealing/focusing the final request.
    assert "Timer _commandRevealTimer;" in native
    assert queue.index("CancelCommandReveal();") < queue.index("_commandRequestId++;")
    assert "CancelCommandReveal();" in reveal
    assert "Object.ReferenceEquals(_commandRevealTimer, reveal)" in reveal
    assert "openingRequest != _commandRequestId" in reveal
    assert "_shutdownRequested || IsDisposed || !IsHandleCreated" in reveal
    assert "CancelCommandReveal();" in navigation
    assert "CancelCommandReveal();" in cleanup
    assert "CancelCommandReveal();" in receipt.split("else if (_commandMode && !open)", 1)[1]
    assert "Timer _commandFocusLeaseTimer;" in native
    assert queue.index("CancelCommandFocusLease();") < queue.index("_commandRequestId++;")
    assert "CancelCommandFocusLease();" in navigation
    assert "CancelCommandFocusLease();" in cleanup
    assert "_commandPresentationAuthorizedRequestId = -1;" in cleanup
    assert "CancelCommandFocusLease();" in receipt.split("else if (_commandMode && !open)", 1)[1]

    # The HWND becomes visible before a request-fenced foreground transaction moves keyboard focus
    # to WebView2. A bounded first lease tick reasserts native focus before surface_ready/presented
    # releases callers to type or allows the page to focus its textarea/start voice.
    assert "FocusCommandWindow(openingRequest);" in reveal
    assert reveal.index("Opacity = 1;") < reveal.index("FocusCommandWindow(openingRequest);")
    assert reveal.index("FocusCommandWindow(openingRequest);") < reveal.index(
        "StartCommandFocusLease(openingRequest);"
    )
    assert "lease.Interval = 80;" in lease
    assert "_commandFocusLeaseTicksRemaining = 13;" in lease
    assert "Object.ReferenceEquals(_commandFocusLeaseTimer, lease)" in lease
    assert "requestId != _commandRequestId || !_commandPageOpen" in lease
    assert "const uint LSFW_LOCK = 1, LSFW_UNLOCK = 2;" in native
    assert "LockSetForegroundWindow(uint lockCode)" in native
    assert "bool _commandForegroundLocked;" in native
    assert "!_commandMode || _commandForegroundLocked" in acquire_lock
    assert "requestId != _commandRequestId || !_commandPageOpen" in acquire_lock
    assert "GetForegroundWindow() != Handle" in acquire_lock
    assert "_commandForegroundLocked = LockSetForegroundWindow(LSFW_LOCK);" in acquire_lock
    assert "ReleaseCommandForegroundLock();" in cancel_lease
    assert "if (!_commandForegroundLocked)" in release_lock
    assert "_commandForegroundLockRequestId = -1;" in release_lock
    assert release_lock.index("_commandForegroundLocked = false;") < release_lock.index(
        "LockSetForegroundWindow(LSFW_UNLOCK)"
    )
    assert lease.index("lease.Tick += delegate") < lease.rindex(
        "TryLockCommandForeground(requestId);"
    )
    assert lease.rindex("TryLockCommandForeground(requestId);") < lease.index(
        "try { lease.Start(); }"
    )
    assert lease.index("FocusCommandWindow(requestId);") < lease.rindex(
        "TryLockCommandForeground(requestId);"
    )
    assert "command focus lease tick exception" in lease
    assert "command focus lease start exception" in lease
    assert lease.count("CancelCommandFocusLease();") >= 2
    assert "StopCommandFocusLeaseTimer();" in lease
    assert lease.count("FailCommandPresentation(requestId") >= 3
    assert lease.index("FocusCommandWindow(requestId);") < lease.index(
        "TryLockCommandForeground(requestId);"
    )
    assert lease.index("TryLockCommandForeground(requestId);") < lease.index(
        "TryAuthorizeCommandPresentation(requestId);"
    )
    assert "if (!HasVerifiedCommandKeyboardFocus()) return;" in authorize
    assert authorize.index("if (!HasVerifiedCommandKeyboardFocus()) return;") < authorize.index(
        "_pendingPresentation = true;"
    )
    assert authorize.index("_pendingPresentation = true;") < authorize.index(
        "PostPendingPresentation();"
    )
    assert "CommandPresentationAuthorizationIsLive(_commandRequestId)" in post_presentation
    assert "requestId != _commandPresentationAuthorizedRequestId" in presented
    assert "if (firstPresentationAck" in presented
    assert "&& (!domFocused || !CommandPresentationAuthorizationIsLive(requestId))" in presented
    assert "_commandSurfaceReady = true;" in presented
    assert presented.index("_commandSurfaceReady = true;") < presented.index(
        "_pendingPresentation = false;"
    )
    assert "FailCommandPresentation(requestId" in lease
    assert "keyboard-focus-timeout" in lease
    assert fail_presentation.index("CancelCommandFocusLease();") < fail_presentation.index("Hide();")
    assert "_commandPresentationAuthorizedRequestId = -1;" in fail_presentation
    assert "_commandSurfaceReady = false;" in fail_presentation

    # SetForegroundWindow is allowed to fail across process input queues. Join only the current and
    # actual foreground UI threads, focus Collie's WebView, and unconditionally detach in finally.
    assert "GetForegroundWindow();" in focus
    assert "GetWindowThreadProcessId(previousForeground, out foregroundPid)" in focus
    assert "AttachThreadInput(currentThread, foregroundThread, true)" in focus
    assert "requestId != _commandRequestId || !_commandPageOpen" in focus
    assert "BringWindowToTop(Handle);" in focus
    assert "SetForegroundWindow(Handle);" in focus
    assert "SetActiveWindow(Handle);" in focus
    assert "ActiveControl = _web;" in focus
    assert "_web.Focus();" in focus
    finally_block = focus.split("finally", 1)[1]
    assert "if (attached)" in finally_block
    assert "AttachThreadInput(currentThread, foregroundThread, false)" in finally_block
    assert "_attached =" not in focus  # wallpaper's persistent bridge state is a separate lifecycle


def test_page_focus_requests_are_exact_id_open_only_native_reassertions():
    native = (ROOT / "harness" / "wallpaper" / "Program.cs").read_text(encoding="utf-8")
    handler = native.split(
        'raw.IndexOf("collie-command-focus-request", StringComparison.Ordinal)', 1
    )[1].split('raw.IndexOf("collie-command-presented-state"', 1)[0]

    assert 'JsonLong(raw, "request_id", -1)' in handler
    assert "requestId != _commandRequestId" in handler
    assert "!_commandMode || !_commandPageOpen" in handler
    assert "!Visible || _shutdownRequested" in handler
    assert "surface_ready cannot be published until this" in handler
    assert "FocusCommandWindow(requestId);" in handler
    assert "StartCommandFocusLease" not in handler  # a click requests one reassert, not a new lease


def test_presentation_requires_verified_focus_while_lsfw_remains_best_effort_only():
    native = (ROOT / "harness" / "wallpaper" / "Program.cs").read_text(encoding="utf-8")
    lock = native.split("void TryLockCommandForeground(long requestId)", 1)[1].split(
        "void TryAuthorizeCommandPresentation(long requestId)", 1
    )[0]
    authorize = native.split("void TryAuthorizeCommandPresentation(long requestId)", 1)[1].split(
        "void FailCommandPresentation(long requestId, string reason)", 1
    )[0]
    fail = native.split("void FailCommandPresentation(long requestId, string reason)", 1)[1].split(
        "bool FocusCommandWindow(long requestId)", 1
    )[0]
    lease = native.split("void StartCommandFocusLease(long requestId)", 1)[1].split(
        "void ScheduleCommandReveal(long openingRequest)", 1
    )[0]
    post = native.split("void PostPendingPresentation()", 1)[1].split("CollieWallpaper()", 1)[0]
    presented = native.split('raw.IndexOf("collie-command-presented-state"', 1)[1].split(
        'raw.IndexOf("collie-command-state"', 1
    )[0]
    queue = native.split("void QueueCommandAction(string action)", 1)[1].split(
        "void PostPendingCommand()", 1
    )[0]
    receipt = native.split('raw.IndexOf("collie-command-state"', 1)[1].split(
        'raw.IndexOf("\\\"theme\\\""', 1
    )[0]
    close_receipt = receipt.split("else if (_commandMode && !open)", 1)[1]
    navigation = native.split("NavigationStarting +=", 1)[1].split(
        "_web.CoreWebView2.Navigate(url);", 1
    )[0]

    # LSFW failure alone is no longer fatal, and LSFW success alone is not proof. Authorization waits
    # for two stable keyboard-route ticks after the best-effort lock attempt.
    assert "_commandForegroundLocked = LockSetForegroundWindow(LSFW_LOCK);" in lock
    assert "else Log(\"command foreground lock failed" in lock
    tick = lease.split("lease.Tick += delegate", 1)[1].split("};", 1)[0]
    assert tick.index("FocusCommandWindow(requestId);") < tick.index(
        "TryLockCommandForeground(requestId);"
    ) < tick.index("TryAuthorizeCommandPresentation(requestId);")
    assert "LSFW is only a best-effort stabilizer" in authorize
    assert "if (!HasVerifiedCommandKeyboardFocus()) return;" in authorize
    assert authorize.index("if (!HasVerifiedCommandKeyboardFocus()) return;") < authorize.index(
        "_commandPresentationAuthorizedRequestId = requestId;"
    ) < authorize.index("_pendingPresentation = true;") < authorize.index(
        "PostPendingPresentation();"
    )
    assert native.count("_pendingPresentation = true;") == 1
    assert "CommandPresentationAuthorizationIsLive(_commandRequestId)" in post
    assert "!_commandPageOpen || !Visible || _shutdownRequested" in post

    # Even a forged/delayed page ACK cannot publish ready without exact authorization, an open/visible
    # page, a focused DOM input, and a freshly verified native keyboard route.
    assert "requestId != _commandPresentationAuthorizedRequestId" in presented
    assert "!_commandMode || !_commandPrepared || !_commandPageOpen" in presented
    assert "if (firstPresentationAck" in presented
    assert "&& (!domFocused || !CommandPresentationAuthorizationIsLive(requestId))" in presented
    assert presented.index("if (firstPresentationAck") < presented.index(
        "_commandSurfaceReady = true;"
    )

    # Close, replacement, and navigation invalidate authorization. If all bounded attempts fail (or
    # the renderer never ACKs), native unlocks before hiding and keeps surface_ready honestly false.
    assert "_commandPresentationAuthorizedRequestId = -1;" in queue
    assert "_commandPresentationAuthorizedRequestId = -1;" in close_receipt
    assert "_commandPresentationAuthorizedRequestId = -1;" in navigation
    assert "keyboard-focus-timeout" in lease
    assert fail.index("CancelCommandFocusLease();") < fail.index("Hide();")
    assert "_pendingPresentation = false;" in fail
    assert "_commandPresentationAuthorizedRequestId = -1;" in fail
    assert "_commandSurfaceReady = false;" in fail
    assert "_commandVoiceStartedOnce = false;" in fail


def test_prepare_phase_focuses_dom_before_lock_without_publishing_ready_or_voice():
    native = (ROOT / "harness" / "wallpaper" / "Program.cs").read_text(encoding="utf-8")
    begin = native.split("void BeginCommandPreparation(long requestId)", 1)[1].split(
        "void TryAuthorizeCommandPresentation(long requestId)", 1
    )[0]
    lease = native.split("void StartCommandFocusLease(long requestId)", 1)[1].split(
        "void ScheduleCommandReveal(long openingRequest)", 1
    )[0]
    reveal = native.split("void ScheduleCommandReveal(long openingRequest)", 1)[1].split(
        "void ToggleCommand()", 1
    )[0]
    post_prepare = native.split("void PostPendingPreparation()", 1)[1].split(
        "void PostPendingPresentation()", 1
    )[0]
    authorize = native.split("void TryAuthorizeCommandPresentation(long requestId)", 1)[1].split(
        "void FailCommandPresentation(long requestId, string reason)", 1
    )[0]
    prepared = native.split('raw.IndexOf("collie-command-prepared-state"', 1)[1].split(
        'raw.IndexOf("collie-command-focus-request"', 1
    )[0]
    presented = native.split('raw.IndexOf("collie-command-presented-state"', 1)[1].split(
        'raw.IndexOf("collie-command-state"', 1
    )[0]
    queue = native.split("void QueueCommandAction(string action)", 1)[1].split(
        "void PostPendingCommand()", 1
    )[0]
    receipt = native.split('raw.IndexOf("collie-command-state"', 1)[1].split(
        'raw.IndexOf("\\\"theme\\\""', 1
    )[0]
    close_receipt = receipt.split("else if (_commandMode && !open)", 1)[1]
    navigation = native.split("NavigationStarting +=", 1)[1].split(
        "_web.CoreWebView2.Navigate(url);", 1
    )[0]

    assert "bool _pendingPreparation;" in native
    assert "bool _commandPrepared;" in native
    assert "long _commandPreparationRequestId = -1;" in native
    assert '"pending_preparation\\\":"' in native
    assert '"prepared\\\":"' in native
    assert "requestId != _commandRequestId || !_commandPageOpen" in begin
    assert "_commandPreparationRequestId = requestId;" in begin
    assert "_commandPrepared = false;" in begin
    assert "_pendingPreparation = true;" in begin
    assert "PostPendingPreparation();" in begin
    assert reveal.index("FocusCommandWindow(openingRequest);") < reveal.index(
        "BeginCommandPreparation(openingRequest);"
    ) < reveal.index("StartCommandFocusLease(openingRequest);")

    assert "collie-command-prepare" in post_prepare
    assert "_commandPreparationRequestId != _commandRequestId" in post_prepare
    assert "!_commandPageOpen || !Visible || _shutdownRequested" in post_prepare
    tick = lease.split("lease.Tick += delegate", 1)[1].split("};", 1)[0]
    not_prepared = tick.split("if (!_commandPrepared)", 1)[1].split("else", 1)[0]
    after_prepared = tick.split("else", 1)[1]
    assert "PostPendingPreparation();" in not_prepared
    assert "TryLockCommandForeground" not in not_prepared
    assert after_prepared.index("TryLockCommandForeground(requestId);") < after_prepared.index(
        "TryAuthorizeCommandPresentation(requestId);"
    )
    assert "!_commandMode || !_commandPrepared" in authorize

    # Prepared-state itself is only an exact-id preflight receipt. It cannot report ready, update
    # voice, or authorize final presentation; two failed locks still do nothing and the third success
    # reaches the separately locked authorization gate in the lease above.
    assert "requestId != _commandRequestId || requestId != _commandPreparationRequestId" in prepared
    assert "!_commandMode || !_commandPageOpen || !Visible || _shutdownRequested" in prepared
    assert "_commandPrepared = true;" in prepared
    assert "_pendingPreparation = false;" in prepared
    assert "_commandSurfaceReady" not in prepared
    assert "_commandVoiceStarted" not in prepared
    assert "_pendingPresentation = true" not in prepared
    assert "_commandPrepared" in presented

    # Replacement, exact close, and navigation all invalidate delayed preparation receipts.
    for block in (queue, close_receipt, navigation):
        assert "_pendingPreparation = false;" in block
        assert "_commandPrepared = false;" in block
        assert "_commandPreparationRequestId = -1;" in block
    assert "preparation-timeout" in lease


def test_presentation_timeout_posts_a_new_exact_close_and_next_hotkey_is_clean_open():
    native = (ROOT / "harness" / "wallpaper" / "Program.cs").read_text(encoding="utf-8")
    fail = native.split("void FailCommandPresentation(long requestId, string reason)", 1)[1].split(
        "bool FocusCommandWindow(long requestId)", 1
    )[0]
    queue = native.split("void QueueCommandAction(string action)", 1)[1].split(
        "void PostPendingCommand()", 1
    )[0]
    toggle = native.split("void ToggleCommand()", 1)[1].split(
        "void QueueCommandAction(string action)", 1
    )[0]

    assert fail.index("CancelCommandFocusLease();") < fail.index("_commandPageOpen = false;")
    assert fail.index("_commandPageOpen = false;") < fail.index('QueueCommandAction("close");')
    assert fail.index('QueueCommandAction("close");') < fail.index("PostPendingCommand();")
    assert fail.index("PostPendingCommand();") < fail.index("Hide();")
    assert "long failedRequest = requestId;" in fail
    assert "close_request_id=" in fail
    assert queue.index("_commandRequestId++;") < queue.index("_pendingCommand = true;")
    assert "_commandSurfaceReady = false;" in queue
    # Hidden + page_open=false bypasses the close-toggle guard, so the next physical chord creates a
    # new open request rather than trying to close an already-hidden stale DOM.
    assert 'QueueCommandAction("open");' in toggle


def test_hotkey_foreground_lock_is_attempted_early_and_preserved_for_the_exact_reveal():
    native = (ROOT / "harness" / "wallpaper" / "Program.cs").read_text(encoding="utf-8")
    receipt = native.split('raw.IndexOf("collie-command-state"', 1)[1].split(
        'raw.IndexOf("\\\"theme\\\""', 1
    )[0]
    open_receipt = receipt.split("if (_commandMode && open)", 1)[1].split(
        "else if (_commandMode && !open)", 1
    )[0]
    reveal = native.split("void ScheduleCommandReveal(long openingRequest)", 1)[1].split(
        "void ToggleCommand()", 1
    )[0]
    lease = native.split("void StartCommandFocusLease(long requestId)", 1)[1].split(
        "void ScheduleCommandReveal(long openingRequest)", 1
    )[0]
    lock = native.split("void TryLockCommandForeground(long requestId)", 1)[1].split(
        "void BeginCommandPreparation(long requestId)", 1
    )[0]
    cancel = native.split("void CancelCommandFocusLease()", 1)[1].split(
        "void ReleaseCommandForegroundLock()", 1
    )[0]
    release = native.split("void ReleaseCommandForegroundLock()", 1)[1].split(
        "void TryLockCommandForeground(long requestId)", 1
    )[0]
    queue = native.split("void QueueCommandAction(string action)", 1)[1].split(
        "void PostPendingCommand()", 1
    )[0]

    # Use the original WM_HOTKEY foreground grant immediately after Show, rather than first trying
    # after the compositor's 320ms warm-up has already consumed that Windows permission window.
    assert open_receipt.index("PresentCommandWindow();") < open_receipt.index(
        "FocusCommandWindow(openingRequest);"
    ) < open_receipt.index("TryLockCommandForeground(openingRequest);") < open_receipt.index(
        "ScheduleCommandReveal(openingRequest);"
    )
    assert "long _commandForegroundLockRequestId = -1;" in native
    assert "_commandForegroundLockRequestId = requestId;" in lock

    # No prepare/final-present message crosses the bridge before the 320ms reveal. The early phase is
    # native HWND/WebView focus + LSFW only, so it cannot start voice or claim surface_ready.
    assert "BeginCommandPreparation(openingRequest);" not in open_receipt
    assert "PostPendingPreparation" not in open_receipt
    assert "PostPendingPresentation" not in open_receipt
    assert "_pendingPresentation = true" not in open_receipt
    assert "reveal.Interval = 320;" in reveal
    assert reveal.index("Opacity = 1;") < reveal.index("BeginCommandPreparation(openingRequest);")

    # Lease startup drops old timer callbacks but preserves only a lock owned by this exact live open.
    assert "bool preserveEarlyLock = _commandForegroundLocked" in lease
    assert "_commandForegroundLockRequestId == requestId" in lease
    assert "requestId == _commandRequestId" in lease
    assert lease.index("StopCommandFocusLeaseTimer();") < lease.index(
        "if (!preserveEarlyLock) ReleaseCommandForegroundLock();"
    )
    assert "ReleaseCommandForegroundLock();\n            return;" in lease

    # If the early call failed, prepared lease ticks still retry LSFW. Any close before reveal cancels
    # both the reveal and the lease/lock before advancing the request id; unlock clears owner first.
    assert "if (_commandPrepared) TryLockCommandForeground(requestId);" in lease
    assert queue.index("CancelCommandReveal();") < queue.index("CancelCommandFocusLease();")
    assert queue.index("CancelCommandFocusLease();") < queue.index("_commandRequestId++;")
    assert "StopCommandFocusLeaseTimer();" in cancel
    assert "ReleaseCommandForegroundLock();" in cancel
    assert release.index("_commandForegroundLockRequestId = -1;") < release.index(
        "LockSetForegroundWindow(LSFW_UNLOCK)"
    )
    assert "reveal-failed" in reveal and "reveal-start-exception" in reveal


def test_verified_webview_keyboard_focus_is_a_bounded_supported_lsfw_fallback():
    native = (ROOT / "harness" / "wallpaper" / "Program.cs").read_text(encoding="utf-8")
    verify = native.split("bool CommandOwnsKeyboardFocus()", 1)[1].split(
        "bool HasVerifiedCommandKeyboardFocus()", 1
    )[0]
    verified = native.split("bool HasVerifiedCommandKeyboardFocus()", 1)[1].split(
        "void BeginCommandPreparation", 1
    )[0]
    authorize = native.split("void TryAuthorizeCommandPresentation(long requestId)", 1)[1].split(
        "bool CommandPresentationAuthorizationIsLive(long requestId)", 1
    )[0]
    live = native.split("bool CommandPresentationAuthorizationIsLive(long requestId)", 1)[1].split(
        "void FailCommandPresentation", 1
    )[0]
    lease = native.split("void StartCommandFocusLease(long requestId)", 1)[1].split(
        "void ScheduleCommandReveal(long openingRequest)", 1
    )[0]
    post = native.split("void PostPendingPresentation()", 1)[1].split("CollieWallpaper()", 1)[0]
    presented = native.split('raw.IndexOf("collie-command-presented-state"', 1)[1].split(
        'raw.IndexOf("collie-command-state"', 1
    )[0]
    queue = native.split("void QueueCommandAction(string action)", 1)[1].split(
        "void PostPendingCommand()", 1
    )[0]
    receipt = native.split('raw.IndexOf("collie-command-state"', 1)[1].split(
        'raw.IndexOf("\\\"theme\\\""', 1
    )[0]
    close_receipt = receipt.split("else if (_commandMode && !open)", 1)[1]
    navigation = native.split("NavigationStarting +=", 1)[1].split(
        "_web.CoreWebView2.Navigate(url);", 1
    )[0]
    cleanup = native.split("void Cleanup()", 1)[1]

    # Foreground HWND alone is insufficient. Query that foreground thread's true focus HWND and only
    # accept the command form or a descendant (the WebView2 Chrome child); null/unrelated focus fails.
    assert "struct GUITHREADINFO" in native
    assert "GetGUIThreadInfo(uint threadId, ref GUITHREADINFO info)" in native
    assert "IsChild(IntPtr parent, IntPtr child)" in native
    assert "IntPtr foregroundBefore = GetForegroundWindow();" in verify
    assert "if (foregroundBefore != Handle) return false;" in verify
    assert "GetWindowThreadProcessId(foregroundBefore, out pid)" in verify
    assert "info.cbSize = (uint)Marshal.SizeOf(typeof(GUITHREADINFO));" in verify
    assert "!GetGUIThreadInfo(foregroundThread, ref info) || info.hwndFocus == IntPtr.Zero" in verify
    assert "IntPtr foregroundAfter = GetForegroundWindow();" in verify
    assert "if (foregroundAfter != Handle) return false;" in verify
    assert "IntPtr webHandle = _web.Handle;" in verify
    assert "info.hwndFocus == webHandle || IsChild(webHandle, info.hwndFocus)" in verify
    assert "!_commandPrepared" in verify and "!_commandPageOpen" in verify

    # With LSFW failing forever, tick 1 records one valid child focus and cannot authorize; tick 2
    # reaches the threshold and the fresh verification inside authorization permits final presented.
    tick = lease.split("lease.Tick += delegate", 1)[1].split("};", 1)[0]
    assert tick.index("routeAtTickEntry = CommandOwnsKeyboardFocus();") < tick.index(
        "FocusCommandWindow(requestId);"
    ) < tick.index(
        "TryLockCommandForeground(requestId);"
    ) < tick.index("bool routeAfterFocus = CommandOwnsKeyboardFocus();") < tick.index(
        "TryAuthorizeCommandPresentation(requestId);"
    )
    assert "if (!routeAtTickEntry) _commandVerifiedFocusTicks = 0;" in tick
    assert "if (!routeAtTickEntry) _commandVerifiedFocusTicks = 1;" in tick
    assert "else if (_commandVerifiedFocusTicks < 2) _commandVerifiedFocusTicks++;" in tick
    assert "else _commandVerifiedFocusTicks = 0;" in tick
    assert "if (_commandVerifiedFocusTicks < 2) return false;" in verified
    assert "if (CommandOwnsKeyboardFocus()) return true;" in verified
    assert "_commandVerifiedFocusTicks = 0;" in verified
    assert "if (!HasVerifiedCommandKeyboardFocus()) return;" in authorize
    assert 'string authorizationMode = "verified-focus";' in authorize
    assert "_commandPresentationAuthorizationMode = authorizationMode;" in authorize
    assert "CommandPresentationAuthorizationIsLive(_commandRequestId)" in post

    # First surface-ready ACK rechecks the exact verified-focus route; lock success is never accepted
    # as proof. Later voice ACKs remain asynchronous-safe after surface_ready is established.
    assert 'if (_commandPresentationAuthorizationMode == "verified-focus")' in live
    assert "return HasVerifiedCommandKeyboardFocus();" in live
    assert "if (firstPresentationAck" in presented
    assert "&& (!domFocused || !CommandPresentationAuthorizationIsLive(requestId))" in presented
    assert '\\"dom_focused\\":true' in presented
    assert "if (firstPresentationAck) CancelCommandFocusLease();" in presented

    # Reassertion stays bounded. Only neither-lock-nor-verified-focus takes the fail-close branch;
    # a proven child-focus surface stays visible/manual-input capable without falsely setting ready.
    assert "else if (HasVerifiedCommandKeyboardFocus())" in lease
    fail_branch = lease.split(
        "else if (HasVerifiedCommandKeyboardFocus())", 1
    )[1]
    assert fail_branch.index("CancelCommandFocusLease();") < fail_branch.index(
        "else FailCommandPresentation(requestId"
    )
    assert '"keyboard-focus-timeout"' in fail_branch

    # Any replacement, close, navigation, or cleanup clears both the consecutive proof and its auth
    # mode, so a stale Chrome child HWND can never authorize a new request.
    for block in (queue, close_receipt, navigation, cleanup):
        assert "_commandVerifiedFocusTicks = 0;" in block
        assert '_commandPresentationAuthorizationMode = "";' in block
        assert "_commandPresentationAuthorizedRequestId = -1;" in block


def test_native_hotkey_owner_timer_hands_off_and_reclaims_the_chord():
    src = (ROOT / "harness" / "wallpaper" / "Program.cs").read_text(encoding="utf-8")
    created = src.split("protected override void OnHandleCreated", 1)[1].split(
        "static bool TryParseCommandHotKey", 1
    )[0]
    refresh = src.split("void RefreshCommandHotKeyOwnership()", 1)[1].split(
        "static string OriginOf", 1
    )[0]
    cleanup = src.split("void Cleanup()", 1)[1]

    assert "_hotKeyOwnershipTimer = new Timer();" in created
    assert "_hotKeyOwnershipTimer.Tick += delegate { RefreshCommandHotKeyOwnership(); };" in created
    assert "_hotKeyOwnershipTimer.Start();" in created

    # A dedicated host retries a transient registration failure on each cycle.
    command_mode = refresh.split("if (_commandMode)", 1)[1].split(
        "// The ordinary app is a live fallback", 1
    )[0]
    assert "if (!_hotKeyRegistered) RegisterCommandHotKey();" in command_mode

    # The full app yields as soon as the command mutex appears, then becomes the live fallback again
    # if that owner exits instead of leaving the shortcut dead for the rest of the app process.
    command_exists = refresh.split("if (CommandHostExists())", 1)[1].split(
        "if (!_hotKeyRegistered) RegisterCommandHotKey();", 1
    )[0]
    assert "UnregisterHotKey(Handle, COMMAND_HOTKEY_ID)" in command_exists
    assert "_hotKeyRegistered = false;" in command_exists
    assert "if (!_hotKeyRegistered) RegisterCommandHotKey();" in refresh

    assert "_hotKeyOwnershipTimer.Stop(); _hotKeyOwnershipTimer.Dispose();" in cleanup


def test_command_alt_f4_hides_but_named_quit_is_the_only_close_path():
    src = (ROOT / "harness" / "wallpaper" / "Program.cs").read_text(encoding="utf-8")
    closing = src.split("FormClosing += delegate", 1)[1].split("FormClosed += delegate", 1)[0]
    quit_channel = src.split("string quitName =", 1)[1].split("catch { }", 1)[0]

    assert "_commandMode && !_shutdownRequested" in closing
    assert "CloseReason." not in closing  # every external close hides; only named quit may exit
    assert "e.Cancel = true;" in closing
    assert 'QueueCommandAction("close");' in closing
    assert "PostPendingCommand();" in closing
    assert "Hide();" in closing
    assert "Close();" not in closing

    assert '"collie-wallpaper-quit-command"' in quit_channel
    assert "_quit.WaitOne()" in quit_channel
    assert "_shutdownRequested = true; Close();" in quit_channel
    assert src.count("_shutdownRequested = true; Close();") == 1


def test_run_command_uses_capsule_url_and_separate_native_mode(monkeypatch):
    from harness import wallpaper as wp
    from harness import settings

    launched = []
    monkeypatch.setattr(wp.plat, "is_windows", lambda: True)
    monkeypatch.setattr(wp, "webview2_present", lambda: True)
    monkeypatch.setattr(wp, "command_running", lambda: False)
    monkeypatch.setattr(wp, "server_up", lambda port: True)
    monkeypatch.setattr(wp, "build_engine", lambda: r"C:\\Collie\\collie-wallpaper.exe")
    monkeypatch.setattr(settings, "get", lambda key, default=None: "win+shift+space"
                        if key == "GLOBAL_HOTKEY" else default)
    monkeypatch.setattr(wp.subprocess, "Popen", lambda args, **kw: launched.append((args, kw)))
    monkeypatch.setattr(wp, "_wait_command_status",
                        lambda proc=None, timeout=8.0: {"state": "registered",
                                                       "chord": "Windows+Shift+Space",
                                                       "error": 0, "pid": 42,
                                                       "voice_enabled": True})

    assert wp.run_command(8899) == 0
    args, kwargs = launched[0]
    assert args[-1] == "--command"
    assert kwargs["env"]["COLLIE_WALLPAPER_URL"] == "http://127.0.0.1:8899/?capsule=1"
    assert kwargs["env"]["COLLIE_GLOBAL_HOTKEY"] == "win+shift+space"
    assert kwargs["env"]["COLLIE_VOICE_INPUT"] == "true"
    assert kwargs.get("creationflags") == wp.CREATE_NO_WINDOW


def test_spawn_command_host_waits_for_full_window_handoff(monkeypatch):
    from harness import wallpaper as wp

    class Proc:
        def poll(self):
            return None

    registered = {
        "state": "registered", "chord": "Ctrl+Shift+Space", "error": 0,
        "pid": 43, "voice_enabled": True,
    }
    sleeps = []
    monkeypatch.setattr(wp, "_clear_command_status", lambda: None)
    monkeypatch.setattr(wp, "_command_env", lambda port: {"PORT": str(port)})
    monkeypatch.setattr(wp.subprocess, "Popen", lambda *args, **kwargs: Proc())
    monkeypatch.setattr(wp, "_wait_command_status", lambda proc=None, timeout=8.0: {
        "state": "unavailable", "chord": "Ctrl+Shift+Space", "error": 1409,
        "pid": 43, "voice_enabled": True,
    })
    monkeypatch.setattr(wp, "command_status", lambda: dict(registered))
    monkeypatch.setattr(wp.time, "time", lambda: 0.0)
    monkeypatch.setattr(wp.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert wp._spawn_command_host(r"C:\\Collie\\collie-wallpaper.exe", 8899) == registered
    assert sleeps == [0.2]


def test_command_env_keeps_shortcut_registered_when_voice_is_off(monkeypatch):
    from harness import wallpaper as wp
    from harness import settings

    values = {"GLOBAL_HOTKEY": "ctrl+shift+space", "VOICE_INPUT": "off"}
    monkeypatch.setattr(settings, "get", lambda key, default=None: values.get(key, default))
    env = wp._command_env(8899)
    assert env["COLLIE_GLOBAL_HOTKEY"] == "ctrl+shift+space"
    assert env["COLLIE_VOICE_INPUT"] == "false"


def test_command_autostart_is_hidden_and_does_not_enable_wallpaper(tmp_path, monkeypatch):
    from harness import wallpaper as wp

    appdata = tmp_path / "AppData" / "Roaming"
    home = tmp_path / ".collie"
    home.mkdir()
    pyw = tmp_path / "pythonw.exe"
    pyw.write_bytes(b"")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(wp, "_collie_home", lambda: str(home))
    monkeypatch.setattr(wp, "pythonw", lambda: str(pyw))
    monkeypatch.setattr(wp.plat, "is_windows", lambda: True)
    monkeypatch.setattr(wp, "command_running", lambda: True)
    monkeypatch.setattr(wp, "run_command", lambda: 0)

    assert wp.install_command() == 0
    boot = (home / "command-boot.pyw").read_text(encoding="utf-8")
    vbs = Path(wp._command_startup_vbs()).read_text(encoding="utf-8")
    assert "'command', '--boot'" in boot
    assert "wallpaper" not in boot.lower()
    assert "WScript.Shell" in vbs and ", 0, False" in vbs


def test_normal_app_launch_gives_command_host_first_hotkey_ownership(monkeypatch):
    from harness import wallpaper as wp

    launched = []
    checks = {"n": 0}
    monkeypatch.setattr(wp.plat, "is_windows", lambda: True)
    monkeypatch.setattr(wp, "webview2_present", lambda: True)
    monkeypatch.setattr(wp, "server_up", lambda port: True)
    monkeypatch.setattr(wp, "build_engine", lambda: r"C:\\Collie\\collie-wallpaper.exe")
    monkeypatch.setattr(wp, "_command_env", lambda port: {
        "COLLIE_WALLPAPER_URL": "http://127.0.0.1:%d/?capsule=1" % port,
        "COLLIE_GLOBAL_HOTKEY": "ctrl+shift+space",
        "COLLIE_COMMAND_STATUS": r"C:\\Collie\\command-host.json",
    })
    def running():
        checks["n"] += 1
        return checks["n"] > 1
    monkeypatch.setattr(wp, "command_running", running)
    monkeypatch.setattr(wp.subprocess, "Popen", lambda args, **kw: launched.append((args, kw)))
    monkeypatch.setattr(wp, "_wait_command_status",
                        lambda proc=None, timeout=8.0: {"state": "registered",
                                                       "chord": "Ctrl+Shift+Space",
                                                       "error": 0, "pid": 42,
                                                       "voice_enabled": True})

    assert wp.run_app(8898, "/?page=today&demo=1") == 0
    assert [args[-1] for args, _ in launched] == ["--command", "--window"]
    assert launched[-1][1]["env"]["COLLIE_GLOBAL_HOTKEY"] == "ctrl+shift+space"
    assert launched[-1][1]["env"]["COLLIE_WALLPAPER_URL"] == \
        "http://127.0.0.1:8898/?page=today&demo=1"


def test_native_host_attests_registration_and_scopes_microphone_to_command_origin():
    src = (ROOT / "harness" / "wallpaper" / "Program.cs").read_text(encoding="utf-8")
    assert "COLLIE_COMMAND_STATUS" in src and '"registered"' in src
    assert "Marshal.GetLastWin32Error()" in src
    assert "_voiceInputEnabled && TrustedCommandMicrophoneOrigin(e3.Uri)" in src
    assert "_trustedCommandOrigin" in src
    assert "IsLoopbackHttpUrl(url)" in src
    assert "CoreWebView2PermissionState.Deny" in src
    assert '"voice_enabled\\\":"' in src
    assert "if (_commandMode)" in src
    assert "modifiers != 0" in src


def test_command_status_rejects_a_stale_process_record(tmp_path, monkeypatch):
    import json
    from harness import wallpaper as wp

    monkeypatch.setattr(wp, "_collie_home", lambda: str(tmp_path))
    path = tmp_path / "command-host.json"
    path.write_text(json.dumps({
        "state": "registered", "chord": "Ctrl+Shift+Space",
        "error": 0, "pid": 2_147_483_647, "voice_enabled": True,
    }), encoding="utf-8")
    assert wp.command_status() == {}
    assert wp._pid_alive(2_147_483_647) is False
    path.write_text(json.dumps({
        "state": "registered", "chord": "Ctrl+Shift+Space",
        "error": 0, "pid": os.getpid(), "voice_enabled": False,
    }), encoding="utf-8")
    assert wp._pid_alive(os.getpid()) is True
    assert wp.command_status() == {
        "state": "registered", "chord": "Ctrl+Shift+Space", "error": 0,
        "pid": os.getpid(), "voice_enabled": False,
    }


def test_command_status_rejects_pre_voice_policy_receipts(tmp_path, monkeypatch):
    import json
    from harness import wallpaper as wp

    monkeypatch.setattr(wp, "_collie_home", lambda: str(tmp_path))
    (tmp_path / "command-host.json").write_text(json.dumps({
        "state": "registered", "chord": "Ctrl+Shift+Space",
        "error": 0, "pid": os.getpid(),
    }), encoding="utf-8")
    assert wp.command_status() == {}


def test_run_command_fails_closed_when_windows_rejects_the_chord(monkeypatch, capsys):
    from harness import wallpaper as wp
    from harness import settings

    monkeypatch.setattr(wp.plat, "is_windows", lambda: True)
    monkeypatch.setattr(wp, "webview2_present", lambda: True)
    monkeypatch.setattr(wp, "command_running", lambda: False)
    monkeypatch.setattr(wp, "server_up", lambda port: True)
    monkeypatch.setattr(wp, "build_engine", lambda: r"C:\\Collie\\collie-wallpaper.exe")
    monkeypatch.setattr(settings, "get", lambda key, default=None: "alt+space"
                        if key == "GLOBAL_HOTKEY" else default)
    monkeypatch.setattr(wp, "_spawn_command_host",
                        lambda exe, port: {"state": "unavailable", "chord": "Alt+Space",
                                           "error": 1409, "pid": 42})
    monkeypatch.setattr(wp, "stop_command", lambda: 0)

    assert wp.run_command(8899) == 1
    err = capsys.readouterr().err
    assert "Alt+Space is already owned by another app" in err


def test_off_is_not_reported_as_listening(monkeypatch, capsys):
    from harness import wallpaper as wp
    from harness import settings

    monkeypatch.setattr(wp.plat, "is_windows", lambda: True)
    monkeypatch.setattr(wp, "webview2_present", lambda: True)
    monkeypatch.setattr(wp, "command_running", lambda: False)
    monkeypatch.setattr(settings, "get", lambda key, default=None: "off"
                        if key == "GLOBAL_HOTKEY" else default)
    assert wp.run_command(8899) == 0
    out = capsys.readouterr().out
    assert "shortcut is Off" in out and "ready" not in out and "listening" not in out


def test_existing_host_is_replaced_when_its_voice_receipt_disagrees(monkeypatch):
    from harness import wallpaper as wp
    from harness import settings

    calls = []
    values = {"GLOBAL_HOTKEY": "ctrl+shift+space", "VOICE_INPUT": "off"}
    monkeypatch.setattr(wp.plat, "is_windows", lambda: True)
    monkeypatch.setattr(wp, "webview2_present", lambda: True)
    monkeypatch.setattr(settings, "get", lambda key, default=None: values.get(key, default))
    monkeypatch.setattr(wp, "command_running", lambda: True)
    monkeypatch.setattr(wp, "_wait_command_status", lambda timeout=2.0: {
        "state": "registered", "chord": "Ctrl+Shift+Space", "error": 0,
        "pid": 42, "voice_enabled": True,
    })
    monkeypatch.setattr(wp, "stop_command", lambda: calls.append("stop") or 0)
    monkeypatch.setattr(wp, "server_up", lambda port: True)
    monkeypatch.setattr(wp, "build_engine", lambda: r"C:\\Collie\\collie-wallpaper.exe")
    monkeypatch.setattr(wp, "_spawn_command_host", lambda exe, port: calls.append("start") or {
        "state": "registered", "chord": "Ctrl+Shift+Space", "error": 0,
        "pid": 43, "voice_enabled": False,
    })

    assert wp.run_command(8899) == 0
    assert calls == ["stop", "start"]


def test_cli_exposes_install_stop_and_uninstall(monkeypatch):
    from harness import cli, plat, wallpaper

    seen = []
    monkeypatch.setattr(plat, "is_windows", lambda: True)
    monkeypatch.setattr(wallpaper, "install_command", lambda: seen.append("install") or 0)
    monkeypatch.setattr(wallpaper, "stop_command", lambda: seen.append("stop") or 0)
    monkeypatch.setattr(wallpaper, "uninstall_command", lambda: seen.append("uninstall") or 0)
    assert cli.main(["command", "--install"]) == 0
    assert cli.main(["command", "--stop"]) == 0
    assert cli.main(["command", "--uninstall"]) == 0
    assert seen == ["install", "stop", "uninstall"]


def test_windows_installer_enables_command_surface_by_default():
    src = (ROOT / "installer" / "collie.iss").read_text(encoding="utf-8")
    assert 'Parameters: "-m harness.cli command --install"' in src
    assert 'Name: "{userstartup}\\collie-command.vbs"' in src
    assert 'Name: "{%USERPROFILE}\\.collie\\command-boot.pyw"' in src
    assert 'Name: "{userprofile}\\.collie\\command-boot.pyw"' not in src


def test_shortcut_is_a_user_setting_and_live_save_restarts_only_command_host():
    from harness import settings

    row = next(item for item in settings.SCHEMA if item["key"] == "GLOBAL_HOTKEY")
    assert row["default"] == "ctrl+shift+space"
    assert {item["value"] for item in row["options"]} >= {"ctrl+shift+space", "off"}
    web = (ROOT / "harness" / "webapp.py").read_text(encoding="utf-8")
    block = web.split('command_rollback = {}', 1)[1].split(
        'if path == "/api/work-identities":', 1)[0]
    assert '"GLOBAL_HOTKEY" in body' in block and '"VOICE_INPUT" in body' in block
    assert '"MOUSE_SHORTCUT" in body' in block
    assert '"VOICE_LANGUAGE" in body' in block
    assert "stop_command()" in block and "run_command()" in block
    assert "wp.stop()" not in block and "wp.uninstall()" not in block


def test_mouse_side_button_uses_the_exact_native_command_toggle():
    from harness import settings
    from harness import wallpaper as wp

    row = next(item for item in settings.SCHEMA if item["key"] == "MOUSE_SHORTCUT")
    assert row["default"] == "off"
    assert {item["value"] for item in row["options"]} >= {
        "off", "xbutton1", "xbutton2", "middle",
    }

    native = (ROOT / "harness" / "wallpaper" / "Program.cs").read_text(encoding="utf-8")
    mouse = native.split("static IntPtr MouseProc", 1)[1].split("static IntPtr KeyProc", 1)[0]
    assert "WM_XBUTTONDOWN" in native and "WM_XBUTTONUP" in native
    assert "WM_MBUTTONDOWN" in native and "COLLIE_MOUSE_SHORTCUT" in native
    assert "target.ToggleCommand();" in mouse
    assert "LLMHF_INJECTED" in mouse
    assert "return new IntPtr(1);" in mouse  # no simultaneous browser Back/Forward action

    values = {"GLOBAL_HOTKEY": "ctrl+shift+space", "MOUSE_SHORTCUT": "xbutton1",
              "VOICE_INPUT": "on"}
    original_get = settings.get
    try:
        settings.get = lambda key, default=None: values.get(key, default)
        env = wp._command_env(8899)
    finally:
        settings.get = original_get
    assert env["COLLIE_MOUSE_SHORTCUT"] == "xbutton1"


def test_voice_input_is_an_independent_default_on_desktop_setting():
    from harness import settings

    shortcut = next(item for item in settings.SCHEMA if item["key"] == "GLOBAL_HOTKEY")
    voice = next(item for item in settings.SCHEMA if item["key"] == "VOICE_INPUT")
    voice_language = next(item for item in settings.SCHEMA if item["key"] == "VOICE_LANGUAGE")
    assert shortcut["default"] == "ctrl+shift+space"
    assert voice["type"] == "bool" and voice["default"] == "on"
    assert "may use cloud recognition" in voice["hint"]
    assert "keeping the global shortcut" in voice["hint"]
    assert voice_language["type"] == "select" and voice_language["default"] == "auto"
    assert {item["value"] for item in voice_language["options"]} >= {"auto", "zh-CN", "en-US"}
    assert "can differ" in voice_language["hint"]


def test_saving_voice_off_restarts_host_and_returns_false_voice_receipt(monkeypatch, tmp_path):
    from harness import plat, settings, wallpaper, webapp

    state = {"GLOBAL_HOTKEY": "ctrl+shift+space", "VOICE_INPUT": "on"}
    calls = []
    monkeypatch.setattr(settings, "get", lambda key, default=None: state.get(key, default))
    monkeypatch.setattr(settings, "update", lambda values: state.update(values) or dict(state))
    monkeypatch.setattr(settings, "apply", lambda: None)
    monkeypatch.setattr(settings, "all_values", lambda: dict(state))
    monkeypatch.setattr(plat, "is_windows", lambda: True)
    monkeypatch.setattr(wallpaper, "_startup_vbs", lambda: str(tmp_path / "wallpaper.vbs"))
    monkeypatch.setattr(wallpaper, "_command_startup_vbs", lambda: str(tmp_path / "command.vbs"))
    monkeypatch.setattr(wallpaper, "command_running", lambda: True)
    monkeypatch.setattr(wallpaper, "stop_command", lambda: calls.append("stop") or 0)
    monkeypatch.setattr(wallpaper, "run_command", lambda: calls.append("start") or 0)
    monkeypatch.setattr(wallpaper, "command_status", lambda: {
        "state": "registered", "chord": "Ctrl+Shift+Space", "error": 0,
        "pid": 42, "voice_enabled": state["VOICE_INPUT"] == "on",
    })

    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = json.dumps({"VOICE_INPUT": "off"}).encode("utf-8")
        url = "http://127.0.0.1:%d/api/settings?token=%s" % (
            server.server_address[1], webapp.TOKEN)
        request = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read())
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)

    assert calls == ["stop", "start"]
    assert payload["ok"] is True
    assert payload["values"]["VOICE_INPUT"] == "off"
    assert payload["command_host"]["voice_enabled"] is False
