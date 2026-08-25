// collie-wallpaper M4 — WebView2 galaxy pinned behind the icons + INPUT FORWARDING.
// Behind-icons windows get zero OS input, so we synthesize it (exactly like Wallpaper Engine / Lively):
// install low-level mouse + keyboard hooks; when the desktop shell is the foreground surface, forward
// the events by PostMessage to the WebView2 Chromium child (Chrome_WidgetWin_1). Now the on-page chat
// is clickable and typable even though it lives on the wallpaper layer.

using System;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using Timer = System.Windows.Forms.Timer;   // disambiguate from System.Threading.Timer
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

class CollieWallpaper : Form
{
    const uint WS_CHILD = 0x40000000, WS_CLIPSIBLINGS = 0x04000000, WS_CLIPCHILDREN = 0x02000000;
    const long WS_POPUP = 0x80000000L, WS_CAPTION = 0x00C00000L, WS_THICKFRAME = 0x00040000L, WS_BORDER = 0x00800000L;
    const int GWL_STYLE = -16, GWL_EXSTYLE = -20;
    const long WS_EX_NOACTIVATE = 0x08000000L, WS_EX_TOOLWINDOW = 0x00000080L;
    const uint SWP_NOACTIVATE = 0x10, SWP_SHOWWINDOW = 0x40, SWP_NOMOVE = 0x2, SWP_NOSIZE = 0x1, SWP_NOZORDER = 0x4;
    const int WM_MOUSEACTIVATE = 0x0021, WM_NCHITTEST = 0x0084, WM_WINDOWPOSCHANGING = 0x0046;
    const int HTTRANSPARENT = -1, MA_NOACTIVATE = 3;
    const int DWMWA_WINDOW_CORNER_PREFERENCE = 33, DWMWA_BORDER_COLOR = 34;
    const int DWMWCP_ROUND = 2;
    const int WM_HOTKEY = 0x0312, COMMAND_HOTKEY_ID = 0xC011;
    const uint LSFW_LOCK = 1, LSFW_UNLOCK = 2;
    const uint MOD_ALT = 0x0001, MOD_CONTROL = 0x0002, MOD_SHIFT = 0x0004,
               MOD_WIN = 0x0008, MOD_NOREPEAT = 0x4000;
    [StructLayout(LayoutKind.Sequential)] struct WINDOWPOS { public IntPtr hwnd, hwndInsertAfter; public int x, y, cx, cy; public uint flags; }
    const int WH_MOUSE_LL = 14, WH_KEYBOARD_LL = 13;
    const int WM_MOUSEMOVE = 0x0200, WM_LBUTTONDOWN = 0x0201, WM_LBUTTONUP = 0x0202,
              WM_RBUTTONDOWN = 0x0204, WM_RBUTTONUP = 0x0205,
              WM_MBUTTONDOWN = 0x0207, WM_MBUTTONUP = 0x0208, WM_MOUSEWHEEL = 0x020A,
              WM_XBUTTONDOWN = 0x020B, WM_XBUTTONUP = 0x020C,
              WM_KEYDOWN = 0x0100, WM_KEYUP = 0x0101, WM_CHAR = 0x0102, WM_SYSKEYDOWN = 0x0104, WM_SYSKEYUP = 0x0105;
    const int MK_LBUTTON = 0x0001, MK_RBUTTON = 0x0002;
    const uint LLMHF_INJECTED = 0x00000001;

    [StructLayout(LayoutKind.Sequential)] struct POINT { public int x, y; }
    [StructLayout(LayoutKind.Sequential)] struct RECT { public int left, top, right, bottom; }
    [StructLayout(LayoutKind.Sequential)] struct GUITHREADINFO
    {
        public uint cbSize, flags;
        public IntPtr hwndActive, hwndFocus, hwndCapture, hwndMenuOwner, hwndMoveSize, hwndCaret;
        public RECT rcCaret;
    }
    [StructLayout(LayoutKind.Sequential)] struct MSLLHOOKSTRUCT { public POINT pt; public uint mouseData; public uint flags; public uint time; public IntPtr dwExtraInfo; }
    [StructLayout(LayoutKind.Sequential)] struct KBDLLHOOKSTRUCT { public uint vkCode; public uint scanCode; public uint flags; public uint time; public IntPtr dwExtraInfo; }

    delegate IntPtr HookProc(int nCode, IntPtr wParam, IntPtr lParam);
    delegate bool EnumProc(IntPtr h, IntPtr l);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern IntPtr FindWindowW(string cls, string name);
    [DllImport("user32.dll")] static extern IntPtr GetWindow(IntPtr h, uint cmd);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern IntPtr FindWindowExW(IntPtr parent, IntPtr after, string cls, string name);
    [DllImport("user32.dll")] static extern IntPtr GetWindowLongPtrW(IntPtr h, int idx);
    [DllImport("user32.dll")] static extern IntPtr SetWindowLongPtrW(IntPtr h, int idx, IntPtr val);
    [DllImport("user32.dll", SetLastError = true)] static extern IntPtr SetParent(IntPtr child, IntPtr parent);
    [DllImport("user32.dll")] static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int w, int hh, uint flags);
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr ctx);
    [DllImport("user32.dll")] static extern int GetSystemMetrics(int i);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern bool GetCursorPos(out POINT p);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetClassNameW(IntPtr h, StringBuilder s, int m);
    [DllImport("user32.dll")] static extern bool ScreenToClient(IntPtr h, ref POINT p);
    [DllImport("user32.dll")] static extern bool PostMessageW(IntPtr h, uint msg, IntPtr w, IntPtr l);
    [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr h, EnumProc cb, IntPtr p);
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll", SetLastError = true)] static extern IntPtr SetWindowsHookExW(int id, HookProc proc, IntPtr hMod, uint thread);
    [DllImport("user32.dll")] static extern bool UnhookWindowsHookEx(IntPtr h);
    [DllImport("user32.dll")] static extern IntPtr CallNextHookEx(IntPtr h, int code, IntPtr w, IntPtr l);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern IntPtr GetModuleHandleW(string name);
    [DllImport("user32.dll")] static extern int ToUnicode(uint vk, uint scan, byte[] state, [Out] StringBuilder buf, int bufLen, uint flags);
    [DllImport("user32.dll")] static extern bool GetKeyboardState(byte[] state);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll", SetLastError = true)] static extern bool GetGUIThreadInfo(uint threadId, ref GUITHREADINFO info);
    [DllImport("user32.dll")] static extern bool IsChild(IntPtr parent, IntPtr child);
    [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
    [DllImport("kernel32.dll")] static extern uint GetCurrentProcessId();
    [DllImport("user32.dll", SetLastError = true)] static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool attach);
    [DllImport("user32.dll")] static extern IntPtr SetFocus(IntPtr h);
    [DllImport("user32.dll")] static extern IntPtr SetActiveWindow(IntPtr h);
    [DllImport("user32.dll")] static extern bool BringWindowToTop(IntPtr h);
    [DllImport("user32.dll")] static extern IntPtr SendMessageW(IntPtr h, uint msg, IntPtr w, IntPtr l);
    [DllImport("user32.dll")] static extern IntPtr SendMessageTimeoutW(IntPtr h, uint msg, IntPtr w, IntPtr l, uint flags, uint timeout, out IntPtr res);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc cb, IntPtr l);
    [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
    [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr VirtualAllocEx(IntPtr proc, IntPtr addr, IntPtr size, uint type, uint protect);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool WriteProcessMemory(IntPtr proc, IntPtr addr, byte[] buf, IntPtr size, out IntPtr wrote);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool ReadProcessMemory(IntPtr proc, IntPtr addr, byte[] buf, IntPtr size, out IntPtr read);
    [DllImport("user32.dll")] static extern bool ClientToScreen(IntPtr h, ref POINT p);
    [DllImport("dwmapi.dll")] static extern int DwmSetWindowAttribute(IntPtr h, int attr, ref int val, int size);
    [DllImport("gdi32.dll")] static extern IntPtr CreateRoundRectRgn(int left, int top, int right, int bottom, int width, int height);
    [DllImport("gdi32.dll")] static extern bool DeleteObject(IntPtr hObject);
    [DllImport("user32.dll")] static extern uint GetDpiForWindow(IntPtr hwnd);
    [DllImport("user32.dll", SetLastError = true)] static extern bool RegisterHotKey(IntPtr h, int id, uint mods, uint vk);
    [DllImport("user32.dll", SetLastError = true)] static extern bool UnregisterHotKey(IntPtr h, int id);
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll", SetLastError = true)] static extern bool LockSetForegroundWindow(uint lockCode);

    // The title bar is drawn by DWM, not by us and not by the page — so a dark UI inside a window
    // whose caption stays white is not a CSS problem, it is a window that was never told. 20 is
    // DWMWA_USE_IMMERSIVE_DARK_MODE on Windows 10 20H1 and later; before that the same flag lived at
    // 19. Try the current one, and fall back only if it is rejected.
    static bool _titleBarDark;
    // Match the command page's final --surface values exactly.  The rounded CSS corners expose the
    // native substrate by a fraction of a pixel during antialiasing; even a nearby warm grey/green
    // reads as a hairline around an otherwise borderless card at 125-200% display scaling.
    static readonly Color CommandFallbackColor = Color.FromArgb(255, 255, 255);
    static readonly Color CommandFallbackDarkColor = Color.FromArgb(21, 25, 35);
    void ApplyTitleBarTheme(bool dark)
    {
        if (!_windowMode || !IsHandleCreated) return;
        _titleBarDark = dark;
        int on = dark ? 1 : 0;
        if (DwmSetWindowAttribute(Handle, 20, ref on, sizeof(int)) != 0)
            DwmSetWindowAttribute(Handle, 19, ref on, sizeof(int));
        // DWM repaints the caption on the next frame; nudge it so the change is not deferred until
        // the user happens to move or focus the window.
        SetWindowPos(Handle, IntPtr.Zero, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0020);
    }

    static string _log = Path.Combine(Path.GetTempPath(), "collie-wallpaper.log");
    static void Log(string s) { try { File.AppendAllText(_log, DateTime.Now.ToString("HH:mm:ss") + " " + s + "\r\n"); } catch { } }

    WebView2 _web;
    static EventWaitHandle _quit;           // signalled by another process to request a CLEAN shutdown
    static IntPtr _progman, _input;         // Chromium child to post to
    static bool _pinned;                    // once true, WndProc forces our z-order below the icons
    static IntPtr _icons, _iconProc, _iconMem;   // desktop icon ListView + explorer handle + remote LVHITTESTINFO
    static IntPtr _mouseHook, _keyHook;
    static HookProc _mouseProc, _keyProc;   // keep delegates alive
    static EnumProc _enumProc;
    static int _buttons;
    static int _lastMove;                   // throttle mouse-move forwarding (the LL hook fires 100s/sec)
    static IntPtr _enumFound; static int _enumArea;

    // The Collie mark for the window title bar + taskbar. Load the shipped multi-resolution
    // collie.ico (16/48/128) directly — ExtractAssociatedIcon only returns one size and often
    // renders as a generic icon at the taskbar/alt-tab sizes.
    static Icon AppIcon()
    {
        try
        {
            var ico = Path.Combine(Path.GetDirectoryName(Application.ExecutablePath), "collie.ico");
            if (File.Exists(ico)) return new Icon(ico);
        }
        catch { }
        try { return Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { return null; }
    }

    // ONE binary, THREE modes. Default = the behind-the-icons wallpaper. `--window` = an ordinary app
    // window (title bar, taskbar entry, icon) hosting the same page — what the installer's desktop
    // shortcut launches, so a non-technical user gets a real program instead of a browser tab that
    // shows 127.0.0.1:8787 in the address bar and gets lost among their other tabs. `--command` is
    // the always-available, borderless voice capsule summoned by the global shortcut.
    static bool _windowMode;
    static bool _commandMode;
    // The two halves of the split desktop. --ground is the wallpaper with no input at all;
    // --panel is the widget window that sits on the desktop and owns every control. Neither is
    // set when the old all-in-one wallpaper runs, which is still the fallback path.
    static bool _panelMode;
    static bool _groundMode;
    static bool _voiceInputEnabled = true;
    static string _mouseShortcut = "off";
    static string _mouseShortcutState = "disabled";
    static int _mouseShortcutError;
    static bool _mouseShortcutHeld;
    static CollieWallpaper _activeInstance;
    static Mutex _instanceMutex;   // held for the life of the process — keeps duplicate launches out
    bool _hotKeyRegistered;
    bool _hotKeyRetryAllowed = true;
    Timer _hotKeyOwnershipTimer;
    Timer _commandRevealTimer;
    Timer _commandFocusLeaseTimer;
    int _commandFocusLeaseTicksRemaining;
    bool _commandForegroundLocked;
    long _commandForegroundLockRequestId = -1;
    int _commandVerifiedFocusTicks;
    bool _pageReady;
    bool _pageBridgeReady;
    bool _pendingCommand;
    bool _pendingPreparation;
    bool _commandPrepared;
    long _commandPreparationRequestId = -1;
    bool _pendingPresentation;
    long _commandPresentationAuthorizedRequestId = -1;
    string _commandPresentationAuthorizationMode = "";
    string _pendingCommandAction = "open";
    long _commandRequestId;
    bool _commandPageOpen;
    bool _commandSurfaceReady;
    bool _pageVoiceAvailable;
    bool _commandVoiceStarted;
    bool _commandVoiceStartedOnce;
    bool _commandVisibleRequested;
    bool _commandBootstrapShownComplete;
    string _commandLayoutPhase = "compact";
    bool _shutdownRequested;
    string _hotKeySpec = "";
    string _commandStatusPath = "";
    string _lastCommandStatusState = "starting";
    int _lastCommandStatusError;
    string _trustedCommandOrigin = "";

    [STAThread]
    static void Main(string[] args)
    {
        for (int i = 0; args != null && i < args.Length; i++)
        {
            if (args[i] == "--window" || args[i] == "-w") _windowMode = true;
            if (args[i] == "--command") { _commandMode = true; _windowMode = true; }
            if (args[i] == "--panel") _panelMode = true;
            if (args[i] == "--ground") _groundMode = true;
        }
        _voiceInputEnabled = VoiceInputIsOn(Environment.GetEnvironmentVariable("COLLIE_VOICE_INPUT"));
        _mouseShortcut = NormalizeMouseShortcut(Environment.GetEnvironmentVariable("COLLIE_MOUSE_SHORTCUT"));
        // SINGLE-INSTANCE, per mode. The logon autostart + a `collie wallpaper` invocation could each
        // fire the engine, and two instances then fought over the ONE shared WebView2 profile lock —
        // the loser died with exit -1 and the desktop was left BLANK ("the wallpaper won't come back").
        // A named mutex makes every duplicate exit cleanly (0) before it ever touches the profile.
        bool fresh;
        string mutexName = _commandMode ? "collie-wallpaper-command"
                         : (_panelMode ? "collie-wallpaper-panel"
                         : (_windowMode ? "collie-wallpaper-window" : "collie-wallpaper-bg"));
        try { _instanceMutex = new Mutex(true, mutexName, out fresh); }
        catch { fresh = true; }
        string mode = _commandMode ? "command" : (_panelMode ? "panel"
                    : (_windowMode ? "window" : (_groundMode ? "ground" : "wallpaper")));
        if (!fresh) { Log("another " + mode + " instance is already running — exiting"); return; }
        try { File.Delete(_log); } catch { }
        Log("start M4 mode=" + mode);
        SetProcessDpiAwarenessContext((IntPtr)(-4));
        // WebView2 otherwise paints its opaque default before the controller property is applied.
        // The command process owns a dedicated profile/process, so this process-local transparent
        // bootstrap cannot affect the ordinary app window or the live wallpaper.
        if (_commandMode)
            Environment.SetEnvironmentVariable("WEBVIEW2_DEFAULT_BACKGROUND_COLOR", "00000000",
                                               EnvironmentVariableTarget.Process);
        Application.EnableVisualStyles();
        Application.Run(new CollieWallpaper());
    }

    // Force WS_EX_NOACTIVATE (+ TOOLWINDOW) at handle creation. WinForms manages window styles and
    // overwrites a post-hoc SetWindowLongPtr(GWL_EXSTYLE), so it MUST be set here to stick. Without it
    // the wallpaper could become the foreground window and break desktop icon double-click.
    // What turns a full-screen window into a set of desktop widgets. The page reports the CSS
    // rectangles its widgets occupy as "collie-panel-regions <scale> x,y,w,h x,y,w,h ..." and the
    // window is clipped to their union: outside them there is no window, so those pixels are the
    // desktop — icons visible, icons clickable, Explorer's cursor. A plain string rather than JSON
    // because this fires on every layout change and a splitter is cheaper than a parser.
    string _lastRegions = "", _lastHitRegions = "", _lastFocusRegion = "";
    Region _panelHitRegions = new Region(new Rectangle(0, 0, 0, 0));
    Rectangle _panelFocusRegion = Rectangle.Empty;
    Timer _panelFocusRestoreTimer;
    void ApplyPanelRegions(string spec)
    {
        spec = spec.Replace("\\\"", " ").Replace("\"", " ").Trim();
        if (spec == _lastRegions) return;
        _lastRegions = spec;
        string[] parts = spec.Split(new char[] { ' ' }, StringSplitOptions.RemoveEmptyEntries);
        double scale = 1.0;
        if (parts.Length > 1) double.TryParse(parts[1], System.Globalization.NumberStyles.Float,
                                              System.Globalization.CultureInfo.InvariantCulture, out scale);
        if (scale <= 0.1 || scale > 8) scale = 1.0;
        Region rgn = new Region(new Rectangle(0, 0, 0, 0));
        int kept = 0;
        for (int i = 2; i < parts.Length; i++)
        {
            string[] f = parts[i].Split(',');
            if (f.Length != 4) continue;
            double x, y, w2, h2;
            if (!double.TryParse(f[0], System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out x)) continue;
            if (!double.TryParse(f[1], System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out y)) continue;
            if (!double.TryParse(f[2], System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out w2)) continue;
            if (!double.TryParse(f[3], System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out h2)) continue;
            if (w2 < 1 || h2 < 1) continue;
            rgn.Union(new Rectangle((int)Math.Floor(x * scale), (int)Math.Floor(y * scale),
                                    (int)Math.Ceiling(w2 * scale), (int)Math.Ceiling(h2 * scale)));
            kept++;
        }
        try { Region = rgn; } catch { }
        Log("panel regions: " + kept + " @" + scale.ToString(System.Globalization.CultureInfo.InvariantCulture));
    }

    // Painting and hit-testing are intentionally two different shapes. The visual region carries
    // generous shadow padding so the clipped WebView joins the ground without a seam. Treating that
    // same padded area as input made quiet labels (for example "Today's to-dos") activate a
    // full-screen top-level window. The page reports the real controls separately; outside their
    // union this form is transparent to mouse hit-testing even though it still paints there.
    void ApplyPanelHitRegions(string spec)
    {
        spec = spec.Replace("\\\"", " ").Replace("\"", " ").Trim();
        if (spec == _lastHitRegions) return;
        _lastHitRegions = spec;
        string[] parts = spec.Split(new char[] { ' ' }, StringSplitOptions.RemoveEmptyEntries);
        double scale = 1.0;
        if (parts.Length > 1) double.TryParse(parts[1], System.Globalization.NumberStyles.Float,
                                              System.Globalization.CultureInfo.InvariantCulture, out scale);
        if (scale <= 0.1 || scale > 8) scale = 1.0;
        Region next = new Region(new Rectangle(0, 0, 0, 0));
        int kept = 0;
        for (int i = 2; i < parts.Length; i++)
        {
            string[] f = parts[i].Split(',');
            if (f.Length != 4) continue;
            double x, y, w2, h2;
            if (!double.TryParse(f[0], System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out x)) continue;
            if (!double.TryParse(f[1], System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out y)) continue;
            if (!double.TryParse(f[2], System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out w2)) continue;
            if (!double.TryParse(f[3], System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out h2)) continue;
            if (w2 < 1 || h2 < 1) continue;
            next.Union(new Rectangle((int)Math.Floor(x * scale), (int)Math.Floor(y * scale),
                                     (int)Math.Ceiling(w2 * scale), (int)Math.Ceiling(h2 * scale)));
            kept++;
        }
        Region old = _panelHitRegions;
        _panelHitRegions = next;
        if (old != null) old.Dispose();
        Log("panel hit regions: " + kept + " @" + scale.ToString(System.Globalization.CultureInfo.InvariantCulture));
    }

    // Only a text-entry control is allowed to activate the panel. Buttons still receive their mouse
    // message (MA_NOACTIVATE does not eat it), but the desktop never jumps in front of the app the
    // person is using merely because they expanded a task.
    void ApplyPanelFocusRegion(string spec)
    {
        spec = spec.Replace("\\\"", " ").Replace("\"", " ").Trim();
        if (spec == _lastFocusRegion) return;
        _lastFocusRegion = spec;
        _panelFocusRegion = Rectangle.Empty;
        string[] parts = spec.Split(new char[] { ' ' }, StringSplitOptions.RemoveEmptyEntries);
        double scale = 1.0;
        if (parts.Length > 1) double.TryParse(parts[1], System.Globalization.NumberStyles.Float,
                                              System.Globalization.CultureInfo.InvariantCulture, out scale);
        if (scale <= 0.1 || scale > 8 || parts.Length < 3) return;
        string[] f = parts[2].Split(',');
        if (f.Length != 4) return;
        double x, y, w2, h2;
        if (!double.TryParse(f[0], System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out x)
            || !double.TryParse(f[1], System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out y)
            || !double.TryParse(f[2], System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out w2)
            || !double.TryParse(f[3], System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out h2)
            || w2 < 1 || h2 < 1) return;
        _panelFocusRegion = new Rectangle((int)Math.Floor(x * scale), (int)Math.Floor(y * scale),
                                          (int)Math.Ceiling(w2 * scale), (int)Math.Ceiling(h2 * scale));
    }

    bool CursorInside(Region region)
    {
        POINT p;
        if (region == null || !GetCursorPos(out p) || !ScreenToClient(Handle, ref p)) return false;
        return region.IsVisible(p.x, p.y);
    }

    bool CursorInside(Rectangle rectangle)
    {
        POINT p;
        if (rectangle.IsEmpty || !GetCursorPos(out p) || !ScreenToClient(Handle, ref p)) return false;
        return rectangle.Contains(p.x, p.y);
    }

    void FocusPanelComposer()
    {
        if (!_panelMode || _shutdownRequested || !IsHandleCreated || _web == null) return;
        try
        {
            // The panel is normally WS_EX_NOACTIVATE so a task/header click can never lift its
            // full-screen WebView over another app. Text entry is the one explicit exception: the
            // page asks here from the composer's pointerdown, we grant a short activation lease,
            // focus the DOM target, then restore the non-activating style before the next click.
            long ex = GetWindowLongPtrW(Handle, GWL_EXSTYLE).ToInt64();
            SetWindowLongPtrW(Handle, GWL_EXSTYLE, (IntPtr)(ex & ~WS_EX_NOACTIVATE));
            SetForegroundWindow(Handle);
            SetActiveWindow(Handle);
            _web.Focus();
            if (_web.CoreWebView2 != null)
                _web.CoreWebView2.ExecuteScriptAsync("(function(){var e=document.getElementById('input');if(e)e.focus();})()") ;
            if (_panelFocusRestoreTimer != null)
            {
                _panelFocusRestoreTimer.Stop();
                _panelFocusRestoreTimer.Dispose();
            }
            _panelFocusRestoreTimer = new Timer();
            _panelFocusRestoreTimer.Interval = 650;
            _panelFocusRestoreTimer.Tick += delegate
            {
                _panelFocusRestoreTimer.Stop();
                _panelFocusRestoreTimer.Dispose();
                _panelFocusRestoreTimer = null;
                if (!IsHandleCreated || _shutdownRequested) return;
                long current = GetWindowLongPtrW(Handle, GWL_EXSTYLE).ToInt64();
                SetWindowLongPtrW(Handle, GWL_EXSTYLE,
                                  (IntPtr)(current | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW));
            };
            _panelFocusRestoreTimer.Start();
        }
        catch (Exception ex) { Log("panel composer focus: " + ex.Message); }
    }

    // The window the panel should sit directly beneath: the one immediately above the desktop.
    // Returns zero when the panel is already there, or when the desktop cannot be found — in both
    // cases the right move is to leave the z-order alone rather than guess.
    IntPtr DesktopNeighbour()
    {
        if (_progman == IntPtr.Zero) _progman = FindWindowW("Progman", null);
        if (_progman == IntPtr.Zero) return IntPtr.Zero;
        IntPtr above = GetWindow(_progman, 3 /*GW_HWNDPREV*/);
        if (above == IntPtr.Zero || above == Handle) return IntPtr.Zero;   // already in place
        return above;
    }

    // A fullscreen app can reorder the z-order without our window ever moving, so there is no
    // position change to correct and the panel silently ends up under the desktop. Check on a
    // timer as well; cheap, and it is the difference between "widgets are gone" and self-healing.
    void StartPanelWatchdog()
    {
        var t = new Timer();
        t.Interval = 3000;
        t.Tick += delegate
        {
            if (!_panelMode || !IsHandleCreated || _shutdownRequested) return;
            try
            {
                if (!Visible) Show();
                IntPtr after = DesktopNeighbour();
                if (after != IntPtr.Zero)
                    SetWindowPos(Handle, after, 0, 0, 0, 0,
                                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
            }
            catch { }
        };
        t.Start();
    }

    // Showing a WinForms form activates it. For the panel that would mean the desktop stealing the
    // caret out of whatever the person was typing in, every time it starts or reloads. It still
    // takes focus when they CLICK it — which is the point of giving the controls a real window —
    // it just never grabs it uninvited.
    protected override bool ShowWithoutActivation { get { return _panelMode; } }

    protected override CreateParams CreateParams
    {
        get
        {
            CreateParams cp = base.CreateParams;
            // window mode wants a NORMAL, activatable, alt-tabbable window — the NOACTIVATE +
            // TOOLWINDOW styles below exist only to keep the WALLPAPER from stealing focus.
            // The panel is furniture: no taskbar/alt-tab and, critically, it never becomes the
            // foreground window from an ordinary widget click. The composer requests a short,
            // explicit activation lease through FocusPanelComposer when the person presses it.
            if (_panelMode) cp.ExStyle |= 0x08000000 | 0x00000080;
            else if (!_windowMode) cp.ExStyle |= 0x08000000 | 0x00000080;
            return cp;
        }
    }

    // The CORRECT, event-driven way to stay behind the icons: intercept every z-order change and force
    // our window to insert directly below SHELLDLL_DefView. It can never come on top of the icons — not
    // even for a single frame — so clicking the galaxy no longer makes the icons flash away.
    protected override void WndProc(ref Message m)
    {
        if (m.Msg == WM_HOTKEY && m.WParam.ToInt32() == COMMAND_HOTKEY_ID)
        {
            ToggleCommand();
            return;
        }
        if (_panelMode && m.Msg == WM_NCHITTEST && !CursorInside(_panelHitRegions))
        {
            m.Result = new IntPtr(HTTRANSPARENT);
            return;
        }
        if (_panelMode && m.Msg == WM_MOUSEACTIVATE && !CursorInside(_panelFocusRegion))
        {
            m.Result = new IntPtr(MA_NOACTIVATE);
            return;
        }
        // A desktop widget belongs directly above the desktop: below every application, above the
        // icons. Not HWND_BOTTOM — that is the bottom of the ENTIRE z-order, and Progman lives
        // down there too, so "bottom" is a coin toss with the desktop that the panel eventually
        // loses (a fullscreen app reorders things and the desktop ends up covering it). The
        // position we want has an exact handle: whatever sits immediately above Progman.
        if (m.Msg == WM_WINDOWPOSCHANGING && _panelMode)
        {
            IntPtr after = DesktopNeighbour();
            if (after != IntPtr.Zero)
            {
                WINDOWPOS pp = (WINDOWPOS)Marshal.PtrToStructure(m.LParam, typeof(WINDOWPOS));
                pp.hwndInsertAfter = after;
                pp.flags &= ~SWP_NOZORDER;
                Marshal.StructureToPtr(pp, m.LParam, false);
            }
        }
        if (m.Msg == WM_WINDOWPOSCHANGING && _pinned && _progman != IntPtr.Zero)
        {
            WINDOWPOS wp = (WINDOWPOS)Marshal.PtrToStructure(m.LParam, typeof(WINDOWPOS));
            IntPtr dv = FindWindowExW(_progman, IntPtr.Zero, "SHELLDLL_DefView", null);
            if (dv != IntPtr.Zero) { wp.hwndInsertAfter = dv; wp.flags &= ~SWP_NOZORDER; Marshal.StructureToPtr(wp, m.LParam, false); }
        }
        base.WndProc(ref m);
    }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        _activeInstance = this;
        if (_commandMode) PositionCommandWindow();
        // Start dark rather than starting light and correcting: the client area is black from the
        // first frame, so a light caption would flash before the page finishes loading and reports
        // its real theme. The page's own message (below) is what settles it either way.
        ApplyTitleBarTheme(true);
        if (_commandMode) InstallCommandMouseShortcut();
        RegisterCommandHotKey();
        if (_windowMode)
        {
            _hotKeyOwnershipTimer = new Timer();
            _hotKeyOwnershipTimer.Interval = _commandMode ? 1000 : 250;
            _hotKeyOwnershipTimer.Tick += delegate { RefreshCommandHotKeyOwnership(); };
            _hotKeyOwnershipTimer.Start();
        }
    }

    static bool TryParseCommandHotKey(string value, out uint mods, out uint vk)
    {
        mods = MOD_NOREPEAT; vk = 0;
        string spec = string.IsNullOrWhiteSpace(value) ? "ctrl+shift+space" : value.Trim().ToLowerInvariant();
        if (spec == "off" || spec == "none" || spec == "disabled") return false;
        string[] pieces = spec.Split(new char[] { '+', '-' }, StringSplitOptions.RemoveEmptyEntries);
        for (int i = 0; i < pieces.Length; i++)
        {
            string p = pieces[i].Trim();
            if (p == "ctrl" || p == "control") mods |= MOD_CONTROL;
            else if (p == "shift") mods |= MOD_SHIFT;
            else if (p == "alt" || p == "option") mods |= MOD_ALT;
            else if (p == "win" || p == "windows" || p == "meta" || p == "cmd") mods |= MOD_WIN;
            else if (p == "space") vk = 0x20;
            else if (p == "enter" || p == "return") vk = 0x0D;
            else if (p.Length == 1 && p[0] >= 'a' && p[0] <= 'z') vk = (uint)char.ToUpperInvariant(p[0]);
            else if (p.Length >= 2 && p[0] == 'f')
            {
                int n; if (!int.TryParse(p.Substring(1), out n) || n < 1 || n > 12) return false;
                vk = (uint)(0x70 + n - 1);
            }
            else return false;
        }
        uint modifiers = mods & (MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_WIN);
        return vk != 0 && modifiers != 0;
    }

    static bool HotKeyIsOff(string value)
    {
        string spec = string.IsNullOrWhiteSpace(value) ? "" : value.Trim().ToLowerInvariant();
        return spec == "off" || spec == "none" || spec == "disabled";
    }

    static bool VoiceInputIsOn(string value)
    {
        if (string.IsNullOrWhiteSpace(value)) return true;
        string spec = value.Trim().ToLowerInvariant();
        if (spec == "on" || spec == "1" || spec == "true" || spec == "yes") return true;
        // Settings writes on/off. Unknown explicit values fail closed because this switch controls
        // whether the dedicated host may request and automatically receive microphone access.
        return false;
    }

    static string NormalizeMouseShortcut(string value)
    {
        string spec = string.IsNullOrWhiteSpace(value) ? "off" : value.Trim().ToLowerInvariant();
        if (spec == "xbutton1" || spec == "back" || spec == "back-side") return "xbutton1";
        if (spec == "xbutton2" || spec == "forward" || spec == "forward-side") return "xbutton2";
        if (spec == "middle" || spec == "mbutton") return "middle";
        return "off";
    }

    static string FormatCommandHotKey(uint mods, uint vk)
    {
        var parts = new System.Collections.Generic.List<string>();
        if ((mods & MOD_CONTROL) != 0) parts.Add("Ctrl");
        if ((mods & MOD_ALT) != 0) parts.Add("Alt");
        if ((mods & MOD_SHIFT) != 0) parts.Add("Shift");
        if ((mods & MOD_WIN) != 0) parts.Add("Windows");
        if (vk == 0x20) parts.Add("Space");
        else if (vk == 0x0D) parts.Add("Enter");
        else if (vk >= 0x70 && vk <= 0x7B) parts.Add("F" + (vk - 0x70 + 1).ToString());
        else if (vk >= 'A' && vk <= 'Z') parts.Add(((char)vk).ToString());
        return string.Join("+", parts.ToArray());
    }

    static string JsonString(string value)
    {
        if (value == null) return "";
        return value.Replace("\\", "\\\\").Replace("\"", "\\\"")
                    .Replace("\r", "\\r").Replace("\n", "\\n");
    }

    static long JsonLong(string raw, string key, long fallback)
    {
        if (string.IsNullOrEmpty(raw) || string.IsNullOrEmpty(key)) return fallback;
        string marker = "\"" + key + "\"";
        int at = raw.IndexOf(marker, StringComparison.Ordinal);
        if (at < 0) return fallback;
        at = raw.IndexOf(':', at + marker.Length);
        if (at < 0) return fallback;
        at++;
        while (at < raw.Length && char.IsWhiteSpace(raw[at])) at++;
        int end = at;
        while (end < raw.Length && raw[end] >= '0' && raw[end] <= '9') end++;
        if (end == at) return fallback;
        long value;
        return long.TryParse(raw.Substring(at, end - at), out value) ? value : fallback;
    }

    static string JsonStringValue(string raw, string key)
    {
        if (string.IsNullOrEmpty(raw) || string.IsNullOrEmpty(key)) return "";
        string marker = "\"" + key + "\"";
        int at = raw.IndexOf(marker, StringComparison.Ordinal);
        if (at < 0) return "";
        at = raw.IndexOf(':', at + marker.Length);
        if (at < 0) return "";
        at++;
        while (at < raw.Length && char.IsWhiteSpace(raw[at])) at++;
        if (at >= raw.Length || raw[at] != '"') return "";
        int start = ++at;
        // Layout phases are deliberately a tiny ASCII enum. Reject escaped/unterminated values
        // rather than growing this purpose-built receipt parser into a general JSON parser.
        while (at < raw.Length && raw[at] != '"' && raw[at] != '\\') at++;
        if (at >= raw.Length || raw[at] != '"') return "";
        return raw.Substring(start, at - start);
    }

    void PublishCommandStatus(string state, string chord, int error)
    {
        if (!_commandMode) return;
        _lastCommandStatusState = state;
        _lastCommandStatusError = error;
        if (string.IsNullOrEmpty(_commandStatusPath))
            _commandStatusPath = Environment.GetEnvironmentVariable("COLLIE_COMMAND_STATUS") ?? "";
        if (string.IsNullOrEmpty(_commandStatusPath)) return;
        try
        {
            string parent = Path.GetDirectoryName(_commandStatusPath);
            if (!string.IsNullOrEmpty(parent)) Directory.CreateDirectory(parent);
            string body = "{\"state\":\"" + JsonString(state) + "\",\"chord\":\""
                        + JsonString(chord) + "\",\"error\":" + error.ToString()
                        + ",\"pid\":" + GetCurrentProcessId().ToString()
                        + ",\"voice_enabled\":" + (_voiceInputEnabled ? "true" : "false")
                        + ",\"mouse_shortcut\":\"" + JsonString(_mouseShortcut) + "\""
                        + ",\"mouse_shortcut_state\":\"" + JsonString(_mouseShortcutState) + "\""
                        + ",\"mouse_error\":" + _mouseShortcutError.ToString()
                        + ",\"page_ready\":" + (_pageReady ? "true" : "false")
                        + ",\"bridge_ready\":" + (_pageBridgeReady ? "true" : "false")
                        + ",\"page_open\":" + (_commandPageOpen ? "true" : "false")
                        + ",\"surface_ready\":" + (_commandSurfaceReady ? "true" : "false")
                        + ",\"voice_available\":" + (_pageVoiceAvailable ? "true" : "false")
                        + ",\"voice_started\":" + (_commandVoiceStarted ? "true" : "false")
                        + ",\"voice_started_once\":" + (_commandVoiceStartedOnce ? "true" : "false")
                        + ",\"pending_command\":" + (_pendingCommand ? "true" : "false")
                        + ",\"pending_preparation\":" + (_pendingPreparation ? "true" : "false")
                        + ",\"prepared\":" + (_commandPrepared ? "true" : "false")
                        + ",\"pending_presentation\":" + (_pendingPresentation ? "true" : "false")
                        + ",\"presentation_mode\":\"" + JsonString(_commandPresentationAuthorizationMode) + "\""
                        + ",\"layout_phase\":\"" + JsonString(_commandLayoutPhase) + "\""
                        + ",\"request_id\":" + _commandRequestId.ToString()
                        + ",\"updated_utc\":\"" + DateTime.UtcNow.ToString("o") + "\"}";
            string tmp = _commandStatusPath + "." + GetCurrentProcessId().ToString() + ".tmp";
            File.WriteAllText(tmp, body, Encoding.UTF8);
            if (File.Exists(_commandStatusPath)) File.Delete(_commandStatusPath);
            File.Move(tmp, _commandStatusPath);
        }
        catch (Exception ex) { Log("command status write failed: " + ex.Message); }
    }

    void RepublishCommandStatus()
    {
        if (_commandMode)
            PublishCommandStatus(_lastCommandStatusState, _hotKeySpec, _lastCommandStatusError);
    }

    void RegisterCommandHotKey()
    {
        // Wallpaper mode has intentionally global mouse forwarding but no application shortcut.
        // The command host normally owns the chord; the full window registers it only when the host
        // is not running (RegisterHotKey fails safely if the command host already has it).
        if (!_windowMode || !IsHandleCreated || _hotKeyRegistered) return;
        if (!_commandMode)
        {
            // The full app is only the fallback owner. If the dedicated hidden host exists, leave
            // the chord to it even during its WebView startup; otherwise a launch race can make the
            // app steal Ctrl+Shift+Space before the capsule reaches OnHandleCreated.
            if (CommandHostExists()) { Log("global command hotkey owned by command host"); return; }
        }
        uint mods, vk;
        string spec = Environment.GetEnvironmentVariable("COLLIE_GLOBAL_HOTKEY");
        _hotKeySpec = string.IsNullOrWhiteSpace(spec) ? "Ctrl+Shift+Space" : spec.Trim();
        if (!TryParseCommandHotKey(spec, out mods, out vk))
        {
            _hotKeyRetryAllowed = false;
            bool off = HotKeyIsOff(spec);
            Log("global command hotkey " + (off ? "disabled" : "invalid") + ": " + (spec ?? ""));
            PublishCommandStatus(off ? "disabled" : "invalid", off ? "" : _hotKeySpec, 0);
            return;
        }
        _hotKeyRetryAllowed = true;
        _hotKeySpec = FormatCommandHotKey(mods, vk);
        _hotKeyRegistered = RegisterHotKey(Handle, COMMAND_HOTKEY_ID, mods, vk);
        int error = _hotKeyRegistered ? 0 : Marshal.GetLastWin32Error();
        Log(_hotKeyRegistered ? "global command hotkey registered: " + _hotKeySpec
                              : "global command hotkey unavailable error=" + error.ToString());
        PublishCommandStatus(_hotKeyRegistered ? "registered" : "unavailable", _hotKeySpec, error);
    }

    void InstallCommandMouseShortcut()
    {
        if (!_commandMode || _mouseHook != IntPtr.Zero) return;
        if (_mouseShortcut == "off")
        {
            _mouseShortcutState = "disabled";
            _mouseShortcutError = 0;
            return;
        }
        _mouseProc = new HookProc(MouseProc);              // static field keeps the delegate alive
        _mouseHook = SetWindowsHookExW(WH_MOUSE_LL, _mouseProc, GetModuleHandleW(null), 0);
        _mouseShortcutError = _mouseHook == IntPtr.Zero ? Marshal.GetLastWin32Error() : 0;
        _mouseShortcutState = _mouseHook == IntPtr.Zero ? "unavailable" : "registered";
        Log("command mouse shortcut " + _mouseShortcutState + ": " + _mouseShortcut
            + (_mouseShortcutError == 0 ? "" : " error=" + _mouseShortcutError.ToString()));
    }

    static bool CommandHostExists()
    {
        try
        {
            using (Mutex command = Mutex.OpenExisting("collie-wallpaper-command")) return true;
        }
        catch (WaitHandleCannotBeOpenedException) { return false; }
        catch (UnauthorizedAccessException) { return true; }
    }

    void RefreshCommandHotKeyOwnership()
    {
        if (!_windowMode || !IsHandleCreated || !_hotKeyRetryAllowed) return;
        // WebView delivery is acknowledged, not fire-and-forget. A transport hiccup or a renderer
        // reload leaves the request pending; the existing ownership timer retries the exact same id.
        if (_pendingCommand && _pageBridgeReady) PostPendingCommand();
        if (_pendingPreparation && _pageBridgeReady) PostPendingPreparation();
        if (_pendingPresentation && _pageBridgeReady) PostPendingPresentation();
        if (_commandMode)
        {
            if (!_hotKeyRegistered) RegisterCommandHotKey();
            return;
        }
        // The ordinary app is a live fallback, not a permanent competitor. It releases the chord
        // as soon as a dedicated capsule process appears, and reclaims it if that process exits.
        if (CommandHostExists())
        {
            if (_hotKeyRegistered)
            {
                try { UnregisterHotKey(Handle, COMMAND_HOTKEY_ID); } catch { }
                _hotKeyRegistered = false;
                Log("global command hotkey handed to command host");
            }
            return;
        }
        if (!_hotKeyRegistered) RegisterCommandHotKey();
    }

    static string OriginOf(string raw)
    {
        Uri uri;
        if (!Uri.TryCreate(raw, UriKind.Absolute, out uri)) return "";
        if (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps) return "";
        return uri.GetLeftPart(UriPartial.Authority).TrimEnd('/').ToLowerInvariant();
    }

    static bool IsLoopbackHttpUrl(string raw)
    {
        Uri uri;
        if (!Uri.TryCreate(raw, UriKind.Absolute, out uri)) return false;
        if (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps) return false;
        string host = (uri.Host ?? "").Trim().ToLowerInvariant();
        return host == "localhost" || host == "127.0.0.1" || host == "::1";
    }

    bool TrustedCommandMicrophoneOrigin(string requestUri)
    {
        if (!_commandMode || string.IsNullOrEmpty(_trustedCommandOrigin)) return false;
        string requestOrigin = OriginOf(requestUri);
        string topOrigin = "";
        try { topOrigin = OriginOf(_web.CoreWebView2.Source); } catch { }
        return requestOrigin == _trustedCommandOrigin && topOrigin == _trustedCommandOrigin;
    }

    static int CompactCommandHeightDip(int widthDip, int workAreaHeightDip)
    {
        int target = widthDip < 480 ? 212 : (widthDip < 600 ? 192 : 176);
        int available = Math.Max(1, workAreaHeightDip - 32);
        return Math.Max(140, Math.Min(target, available));
    }

    int CommandHeightDip(int widthDip, int workAreaHeightDip)
    {
        int compact = CompactCommandHeightDip(widthDip, workAreaHeightDip);
        if (_commandLayoutPhase != "conversation") return compact;
        // Conversation may expand to 360 DIPs, but never consumes more than 72% of the monitor's
        // work area and never becomes shorter than the compact layout on a constrained display.
        int conversationLimit = Math.Max(compact, (int)Math.Floor(workAreaHeightDip * 0.72));
        return Math.Min(360, conversationLimit);
    }

    void PositionCommandWindow()
    {
        if (!_commandMode) return;
        PositionCommandWindow(Screen.FromPoint(Cursor.Position), false);
    }

    void PositionCommandWindow(Screen screen, bool preserveBottom)
    {
        if (!_commandMode || screen == null) return;
        Rectangle area = screen.WorkingArea;
        // WorkingArea and ClientSize are physical pixels in this Per-Monitor-V2 process, while the
        // product geometry is specified in DIPs/CSS px. Move onto the cursor's monitor first so
        // GetDpiForWindow observes that monitor, then scale the requested phase exactly.
        if (IsHandleCreated && !preserveBottom) Location = new Point(area.Left, area.Top);
        uint dpi = CommandWindowDpi();
        int availableWidthDip = Math.Max(1, (int)Math.Floor(area.Width * 96.0 / dpi) - 32);
        int cwDip = Math.Min(660, Math.Max(320, availableWidthDip));
        int cw = ScaleCommandDip(cwDip, dpi);
        int workAreaHeightDip = Math.Max(1, (int)Math.Floor(area.Height * 96.0 / dpi));
        int chDip = CommandHeightDip(cwDip, workAreaHeightDip);
        int ch = ScaleCommandDip(chDip, dpi);
        int bottom = preserveBottom ? Bounds.Bottom
            : area.Bottom - Math.Max(24, area.Height / 18);
        bottom = Math.Min(area.Bottom, Math.Max(area.Top + ch, bottom));
        ClientSize = new Size(cw, ch);
        Location = new Point(area.Left + (area.Width - cw) / 2, bottom - Height);
        ApplyCommandWindowChrome();
    }

    void PostCommandLayoutApplied(long requestId, string phase, uint dpi)
    {
        if (!_commandMode || requestId != _commandRequestId || !_commandPageOpen || !Visible
            || _shutdownRequested || _web == null || _web.CoreWebView2 == null) return;
        try
        {
            int heightDip = Math.Max(1, (int)Math.Round(ClientSize.Height * 96.0 / dpi));
            string message = "{\"type\":\"collie-command-layout-applied\",\"request_id\":"
                           + requestId.ToString() + ",\"phase\":\"" + JsonString(phase)
                           + "\",\"height_dip\":" + heightDip.ToString() + "}";
            _web.CoreWebView2.PostWebMessageAsJson(message);
        }
        catch (Exception ex) { Log("command layout ACK failed: " + ex.Message); }
    }

    void ApplyCommandLayout(long requestId, string phase)
    {
        // A layout receipt may only reshape the already-presented exact request. It cannot show,
        // activate, focus, or resurrect a hidden/replaced capsule.
        if (!_commandMode || requestId != _commandRequestId || !_commandPageOpen || !Visible
            || _shutdownRequested || IsDisposed || !IsHandleCreated) return;
        if (phase != "compact" && phase != "conversation") return;

        Screen screen = Screen.FromHandle(Handle);
        Rectangle area = screen.WorkingArea;
        uint dpi = CommandWindowDpi();
        int widthDip = Math.Max(1, (int)Math.Round(ClientSize.Width * 96.0 / dpi));
        int workAreaHeightDip = Math.Max(1, (int)Math.Floor(area.Height * 96.0 / dpi));
        int compactDip = CompactCommandHeightDip(widthDip, workAreaHeightDip);
        int targetDip = compactDip;
        if (phase == "conversation")
        {
            int conversationLimit = Math.Max(compactDip,
                (int)Math.Floor(workAreaHeightDip * 0.72));
            targetDip = Math.Min(360, conversationLimit);
        }
        int targetHeight = ScaleCommandDip(targetDip, dpi);
        int bottom = Math.Min(area.Bottom, Math.Max(area.Top + targetHeight, Bounds.Bottom));
        int left = Bounds.Left;

        _commandLayoutPhase = phase;
        ClientSize = new Size(ClientSize.Width, targetHeight);
        // ClientSize may change the outer Bounds synchronously. Use the resulting borderless Height
        // and the saved bottom edge so conversation grows upward without a visible downward jump.
        Location = new Point(left, bottom - Height);
        ApplyCommandWindowChrome();
        RepublishCommandStatus();
        PostCommandLayoutApplied(requestId, phase, dpi);
    }

    static int ScaleCommandDip(int dip, uint dpi)
    {
        return Math.Max(1, (int)Math.Round(dip * dpi / 96.0));
    }

    uint CommandWindowDpi()
    {
        if (IsHandleCreated)
        {
            try
            {
                uint dpi = GetDpiForWindow(Handle);
                if (dpi != 0) return dpi;
            }
            catch { }
        }
        try
        {
            using (Graphics graphics = CreateGraphics())
                return Math.Max(96, (uint)Math.Round(graphics.DpiX));
        }
        catch { return 96; }
    }

    void ApplyCommandSubstrate(bool dark)
    {
        if (!_commandMode) return;
        BackColor = dark ? CommandFallbackDarkColor : CommandFallbackColor;
    }

    void ApplyCommandWindowChrome()
    {
        if (!_commandMode || !IsHandleCreated || ClientSize.Width <= 0 || ClientSize.Height <= 0) return;

        // Ask Windows 11 for its antialiased rounded treatment and explicitly suppress the system
        // border. Older Windows versions reject these attributes; the native region below is the
        // deterministic fallback and also prevents a compositor/backend regression from exposing a
        // rectangular host around the CSS card.
        int corner = DWMWCP_ROUND;
        int cornerResult = DwmSetWindowAttribute(Handle, DWMWA_WINDOW_CORNER_PREFERENCE,
                                                 ref corner, sizeof(int));
        int noBorder = unchecked((int)0xFFFFFFFE); // DWMWA_COLOR_NONE
        DwmSetWindowAttribute(Handle, DWMWA_BORDER_COLOR, ref noBorder, sizeof(int));

        // On Windows 11 the DWM path provides the best antialiasing and system shadow. Supplying a
        // custom region would opt the window out of that treatment, so clear any Win10 fallback left
        // behind by a recreated handle. Unsupported systems return a failing HRESULT and continue to
        // the rounded-region path below.
        if (cornerResult == 0)
        {
            Region previous = Region;
            Region = null;
            if (previous != null) previous.Dispose();
            return;
        }

        uint dpi = 96;
        try { dpi = GetDpiForWindow(Handle); } catch { dpi = 96; }
        if (dpi == 0) dpi = 96;
        int radius = Math.Max(12, (int)Math.Round(18.0 * dpi / 96.0));
        IntPtr rounded = CreateRoundRectRgn(0, 0, ClientSize.Width + 1, ClientSize.Height + 1,
                                            radius * 2, radius * 2);
        if (rounded == IntPtr.Zero) return;
        try
        {
            Region next = Region.FromHrgn(rounded);
            Region previous = Region;
            Region = next;
            if (previous != null) previous.Dispose();
        }
        finally { DeleteObject(rounded); }
    }

    protected override void OnSizeChanged(EventArgs e)
    {
        base.OnSizeChanged(e);
        ApplyCommandWindowChrome();
    }

    protected override void OnDpiChanged(DpiChangedEventArgs e)
    {
        base.OnDpiChanged(e);
        if (!_commandMode || !IsHandleCreated || IsDisposed) return;
        // Let WinForms accept WM_DPICHANGED's suggested bounds, then recompute the CURRENT compact or
        // conversation phase on that monitor. BeginInvoke avoids resizing inside the native message.
        try { BeginInvoke((MethodInvoker)delegate {
            if (_shutdownRequested || IsDisposed || !IsHandleCreated) return;
            PositionCommandWindow(Screen.FromHandle(Handle), true);
        }); } catch { }
    }

    void PresentCommandWindow()
    {
        if (!_commandMode) return;
        PositionCommandWindow();
        // WebView2 can need a cold compositor frame after a hidden window is shown. Keep that frame
        // transparent; the request-scoped reveal timer below makes the already-rendered capsule
        // visible, then starts speech. This removes the last 100-200ms black flash on first use.
        Opacity = 0;
        if (!Visible) Show();
        WindowState = FormWindowState.Normal;
        TopMost = true;
        SetForegroundWindow(Handle);
        Activate();
        BringToFront();
    }

    void CancelCommandReveal()
    {
        // There may only be one compositor warm-up in flight. A fast open -> close -> open sequence
        // used to leave an older local Timer alive; its delayed Tick could then focus/reveal a newer
        // request (or touch the Form while a clean shutdown was disposing it). Detach the field first
        // so even an already-queued Tick can identify itself as stale and become a no-op.
        Timer reveal = _commandRevealTimer;
        _commandRevealTimer = null;
        if (reveal == null) return;
        try { reveal.Stop(); reveal.Dispose(); } catch { }
    }

    void StopCommandFocusLeaseTimer()
    {
        Timer lease = _commandFocusLeaseTimer;
        _commandFocusLeaseTimer = null;
        _commandFocusLeaseTicksRemaining = 0;
        if (lease != null)
        {
            try { lease.Stop(); lease.Dispose(); } catch { }
        }
    }

    void CancelCommandFocusLease()
    {
        StopCommandFocusLeaseTimer();
        ReleaseCommandForegroundLock();
    }

    void ReleaseCommandForegroundLock()
    {
        if (!_commandForegroundLocked)
        {
            _commandForegroundLockRequestId = -1;
            return;
        }
        _commandForegroundLocked = false;
        _commandForegroundLockRequestId = -1;
        try
        {
            if (!LockSetForegroundWindow(LSFW_UNLOCK))
                Log("command foreground unlock failed error=" + Marshal.GetLastWin32Error().ToString());
            else Log("command foreground unlocked");
        }
        catch (Exception ex) { Log("command foreground unlock exception: " + ex.Message); }
    }

    void TryLockCommandForeground(long requestId)
    {
        if (!_commandMode || _commandForegroundLocked || _shutdownRequested || IsDisposed || !IsHandleCreated
            || !Visible || requestId != _commandRequestId || !_commandPageOpen
            || GetForegroundWindow() != Handle) return;
        try
        {
            _commandForegroundLocked = LockSetForegroundWindow(LSFW_LOCK);
            if (_commandForegroundLocked)
            {
                _commandForegroundLockRequestId = requestId;
                Log("command foreground locked request_id=" + requestId.ToString());
            }
            else Log("command foreground lock failed error=" + Marshal.GetLastWin32Error().ToString());
        }
        catch (Exception ex) { Log("command foreground lock exception: " + ex.Message); }
    }

    bool CommandOwnsKeyboardFocus()
    {
        // Foreground ownership alone is not keyboard ownership: a full-screen app can alternate the
        // top-level HWND while Chromium's textarea never receives the caret. Query the explicit
        // foreground GUI thread, then verify foreground did not change across that native snapshot.
        if (!_commandMode || !_commandPrepared || _shutdownRequested || IsDisposed || !IsHandleCreated
            || !Visible || !_commandPageOpen || _web == null || _web.IsDisposed
            || !_web.IsHandleCreated) return false;
        try
        {
            IntPtr foregroundBefore = GetForegroundWindow();
            if (foregroundBefore != Handle) return false;
            uint pid;
            uint foregroundThread = GetWindowThreadProcessId(foregroundBefore, out pid);
            if (foregroundThread == 0) return false;
            GUITHREADINFO info = new GUITHREADINFO();
            info.cbSize = (uint)Marshal.SizeOf(typeof(GUITHREADINFO));
            if (!GetGUIThreadInfo(foregroundThread, ref info) || info.hwndFocus == IntPtr.Zero)
                return false;
            IntPtr foregroundAfter = GetForegroundWindow();
            if (foregroundAfter != Handle) return false;
            IntPtr webHandle = _web.Handle;
            return info.hwndFocus == webHandle || IsChild(webHandle, info.hwndFocus);
        }
        catch (Exception ex)
        {
            Log("command keyboard focus verification exception: " + ex.Message);
            return false;
        }
    }

    bool HasVerifiedCommandKeyboardFocus()
    {
        if (_commandVerifiedFocusTicks < 2) return false;
        if (CommandOwnsKeyboardFocus()) return true;
        // A failed fresh check invalidates the earlier consecutive proof. A duplicate page ACK or
        // status-timer retry must earn two stable lease samples again, not reuse a stale counter.
        _commandVerifiedFocusTicks = 0;
        return false;
    }

    void BeginCommandPreparation(long requestId)
    {
        if (!_commandMode || _shutdownRequested || IsDisposed || !IsHandleCreated || !Visible
            || requestId != _commandRequestId || !_commandPageOpen) return;
        _commandPreparationRequestId = requestId;
        _commandPrepared = false;
        _commandVerifiedFocusTicks = 0;
        _pendingPreparation = true;
        RepublishCommandStatus();
        PostPendingPreparation();
    }

    void TryAuthorizeCommandPresentation(long requestId)
    {
        // Posting `presented` focuses the textarea and may start SpeechRecognition immediately in
        // Chromium. It is therefore the irreversible presentation boundary, not just a paint ACK.
        // Never cross it merely because SetForegroundWindow appeared to work: hostile/full-screen
        // applications can take foreground back before LSFW succeeds (ERROR_ACCESS_DENIED is legal).
        if (!_commandMode || !_commandPrepared || _shutdownRequested || IsDisposed
            || !IsHandleCreated || !Visible || requestId != _commandRequestId || !_commandPageOpen
            || _commandSurfaceReady) return;

        // LSFW is only a best-effort stabilizer. Windows may revoke its effect without notification,
        // so even a successful lock never substitutes for the fresh native keyboard route proof.
        if (!HasVerifiedCommandKeyboardFocus()) return;
        string authorizationMode = "verified-focus";

        if (_commandPresentationAuthorizedRequestId != requestId)
        {
            _commandPresentationAuthorizedRequestId = requestId;
            _commandPresentationAuthorizationMode = authorizationMode;
            _pendingPresentation = true;
            // A route first verified on the final normal retry still needs a small, bounded window
            // for the renderer's exact-id ACK. This cannot become an unbounded focus reassertion.
            if (_commandFocusLeaseTicksRemaining < 3) _commandFocusLeaseTicksRemaining = 3;
            RepublishCommandStatus();
            Log("command presentation authorized request_id=" + requestId.ToString()
                + " mode=" + authorizationMode);
        }
        if (_pendingPresentation) PostPendingPresentation();
    }

    bool CommandPresentationAuthorizationIsLive(long requestId)
    {
        if (requestId != _commandRequestId || requestId != _commandPresentationAuthorizedRequestId
            || !_commandMode || !_commandPrepared || _shutdownRequested || IsDisposed
            || !IsHandleCreated || !Visible || !_commandPageOpen) return false;
        if (_commandPresentationAuthorizationMode == "verified-focus")
            return HasVerifiedCommandKeyboardFocus();
        return false;
    }

    void FailCommandPresentation(long requestId, string reason)
    {
        // An unpresented page must never masquerade as surface_ready. Close the exact failed open with
        // a NEW request id so both native status and the hidden DOM return to a truthful closed state.
        // Cancel first so LSFW_UNLOCK is guaranteed even if close publication or Hide throws below.
        CancelCommandFocusLease();
        if (!_commandMode || _shutdownRequested || IsDisposed || requestId != _commandRequestId
            || !_commandPageOpen || _commandSurfaceReady) return;
        _commandVisibleRequested = false;
        _commandPageOpen = false;
        _pendingPreparation = false;
        _commandPrepared = false;
        _commandPreparationRequestId = -1;
        _pendingPresentation = false;
        _commandPresentationAuthorizedRequestId = -1;
        _commandPresentationAuthorizationMode = "";
        _commandVerifiedFocusTicks = 0;
        _commandSurfaceReady = false;
        _commandVoiceStarted = false;
        _commandVoiceStartedOnce = false;
        long failedRequest = requestId;
        QueueCommandAction("close");
        PostPendingCommand();
        try { if (Visible) Hide(); }
        catch (Exception ex) { Log("command unready hide failed: " + ex.Message); }
        Log("command presentation unavailable request_id=" + failedRequest.ToString()
            + " close_request_id=" + _commandRequestId.ToString() + " reason=" + reason);
        RepublishCommandStatus();
    }

    bool FocusCommandWindow(long requestId)
    {
        if (!_commandMode || _shutdownRequested || IsDisposed || !IsHandleCreated || !Visible
            || requestId != _commandRequestId || !_commandPageOpen) return false;

        // Windows can legally reject SetForegroundWindow when the previous foreground app owns a
        // different input queue. That made the first click activate Collie and only the second click
        // reach its textarea. Briefly join the two queues, perform the activation/focus transaction,
        // and always detach before returning. This local flag deliberately does not touch `_attached`,
        // which belongs exclusively to the behind-the-icons wallpaper input bridge.
        uint currentThread = GetCurrentThreadId();
        uint foregroundThread = 0, foregroundPid = 0;
        bool attached = false;
        IntPtr previousForeground = IntPtr.Zero;
        try
        {
            previousForeground = GetForegroundWindow();
            if (previousForeground != IntPtr.Zero && previousForeground != Handle)
            {
                foregroundThread = GetWindowThreadProcessId(previousForeground, out foregroundPid);
                if (foregroundThread != 0 && foregroundThread != currentThread)
                {
                    attached = AttachThreadInput(currentThread, foregroundThread, true);
                    if (!attached)
                        Log("command focus attach failed error=" + Marshal.GetLastWin32Error().ToString());
                }
            }

            // Recheck intent after the native boundary. No stale open may steal focus from a newer
            // close/open request even if Windows delivered another message around presentation.
            if (_shutdownRequested || IsDisposed || !IsHandleCreated || !Visible
                || requestId != _commandRequestId || !_commandPageOpen) return false;

            BringWindowToTop(Handle);
            SetForegroundWindow(Handle);
            SetActiveWindow(Handle);
            Activate();
            BringToFront();
            if (_web != null && !_web.IsDisposed)
            {
                ActiveControl = _web;
                _web.Focus();
            }
            // WebView2 focus can cross into its Chromium child. Reassert the top-level foreground
            // owner while the input queues are still joined, then detach immediately below.
            SetForegroundWindow(Handle);
            bool focused = GetForegroundWindow() == Handle;
            Log("command focus " + (focused ? "acquired" : "not-acquired")
                + " request_id=" + requestId.ToString()
                + " previous=" + previousForeground.ToInt64().ToString()
                + " attached=" + (attached ? "true" : "false"));
            return focused;
        }
        catch (Exception ex)
        {
            Log("command focus failed: " + ex.Message);
            return false;
        }
        finally
        {
            if (attached)
            {
                try
                {
                    if (!AttachThreadInput(currentThread, foregroundThread, false))
                        Log("command focus detach failed error=" + Marshal.GetLastWin32Error().ToString());
                }
                catch (Exception ex) { Log("command focus detach exception: " + ex.Message); }
            }
        }
    }

    void StartCommandFocusLease(long requestId)
    {
        // PresentCommandWindow attempts LSFW_LOCK while the WM_HOTKEY foreground grant is still
        // fresh. Starting the post-reveal timer must stop an old timer without releasing that exact
        // current-request lock; every stale/different-request lock is still unconditionally unlocked.
        bool preserveEarlyLock = _commandForegroundLocked
            && _commandForegroundLockRequestId == requestId && requestId == _commandRequestId
            && !_shutdownRequested && !IsDisposed && IsHandleCreated && Visible && _commandPageOpen;
        StopCommandFocusLeaseTimer();
        if (!preserveEarlyLock) ReleaseCommandForegroundLock();
        if (_shutdownRequested || IsDisposed || !IsHandleCreated || !Visible
            || requestId != _commandRequestId || !_commandPageOpen)
        {
            ReleaseCommandForegroundLock();
            return;
        }

        // Some full-screen applications reclaim foreground for a few compositor/input turns after
        // Collie is summoned. Reassert only during a bounded ~1s presentation lease; after that the
        // user is free to switch apps without Collie pulling itself back. One field-owned timer plus
        // both object-identity and request-id fences make old open/close/open callbacks harmless.
        var lease = new Timer(); lease.Interval = 80;
        _commandFocusLeaseTimer = lease;
        _commandFocusLeaseTicksRemaining = 13; // final attempt at ~1040ms
        lease.Tick += delegate
        {
            try
            {
                if (!Object.ReferenceEquals(_commandFocusLeaseTimer, lease))
                {
                    try { lease.Stop(); lease.Dispose(); } catch { }
                    return;
                }
                if (_shutdownRequested || IsDisposed || !IsHandleCreated || !Visible
                    || requestId != _commandRequestId || !_commandPageOpen
                    || _commandFocusLeaseTicksRemaining <= 0)
                {
                    CancelCommandFocusLease();
                    return;
                }
                _commandFocusLeaseTicksRemaining--;
                bool routeAtTickEntry = false;
                if (_commandPrepared)
                {
                    // This passive sample happens before any reassertion in this tick. If another app
                    // reclaimed focus between ticks, stability is broken and the counter must restart.
                    routeAtTickEntry = CommandOwnsKeyboardFocus();
                    if (!routeAtTickEntry) _commandVerifiedFocusTicks = 0;
                }
                FocusCommandWindow(requestId);
                if (!_commandPrepared)
                {
                    // Prepare focuses Chromium's textarea without starting voice or reporting ready.
                    // The ACK is request-fenced; retrying the same id is page-side idempotent.
                    PostPendingPreparation();
                }
                else
                {
                    TryLockCommandForeground(requestId);
                    // LSFW is an optimization, not a supported guarantee. A failed passive entry
                    // sample resets stability; this tick's post-Focus sample can then count only as
                    // sample one. Sample two must survive until the next tick before reassertion.
                    bool routeAfterFocus = CommandOwnsKeyboardFocus();
                    if (routeAfterFocus)
                    {
                        if (!routeAtTickEntry) _commandVerifiedFocusTicks = 1;
                        else if (_commandVerifiedFocusTicks < 2) _commandVerifiedFocusTicks++;
                    }
                    else _commandVerifiedFocusTicks = 0;
                    TryAuthorizeCommandPresentation(requestId);
                }
                if (_commandFocusLeaseTicksRemaining <= 0)
                {
                    if (_commandSurfaceReady) CancelCommandFocusLease();
                    else if (HasVerifiedCommandKeyboardFocus())
                    {
                        // Reassertion is bounded. Preserve a usable visible/manual-input surface
                        // without claiming ready, allowing a slightly-late exact ACK to land only
                        // while the same child-focus route remains freshly verified.
                        Log("command focus lease ended awaiting presentation ACK request_id="
                            + requestId.ToString());
                        CancelCommandFocusLease();
                        RepublishCommandStatus();
                    }
                    else FailCommandPresentation(requestId, !_commandPrepared
                        ? "preparation-timeout" : "keyboard-focus-timeout");
                }
            }
            catch (Exception ex)
            {
                Log("command focus lease tick exception: " + ex.Message);
                FailCommandPresentation(requestId, "lease-tick-exception");
            }
        };
        // Acquire only after every potentially-throwing timer construction/subscription step. If
        // Start itself fails, the catch below cancels the lease and releases the successful lock.
        if (_commandPrepared) TryLockCommandForeground(requestId);
        try { lease.Start(); }
        catch (Exception ex)
        {
            Log("command focus lease start exception: " + ex.Message);
            FailCommandPresentation(requestId, "lease-start-exception");
        }
    }

    void ScheduleCommandReveal(long openingRequest)
    {
        CancelCommandReveal();
        var reveal = new Timer(); reveal.Interval = 320;
        _commandRevealTimer = reveal;
        reveal.Tick += delegate
        {
            // A disposed WinForms timer can already have a WM_TIMER queued. Never let that callback
            // mutate the state belonging to the replacement request.
            if (!Object.ReferenceEquals(_commandRevealTimer, reveal))
            {
                try { reveal.Stop(); reveal.Dispose(); } catch { }
                return;
            }
            CancelCommandReveal();
            if (_shutdownRequested || IsDisposed || !IsHandleCreated
                || openingRequest != _commandRequestId || !_commandPageOpen || !Visible) return;
            try
            {
                Opacity = 1;
                try { _web.Invalidate(); _web.Refresh(); } catch { }
            }
            catch (Exception ex)
            {
                Log("command reveal failed: " + ex.Message);
                FailCommandPresentation(openingRequest, "reveal-failed");
                return;
            }
            // The DOM deliberately deferred textarea focus until this exact presentation. First make
            // the native HWND/WebView the real foreground target; the page presentation below then
            // places the caret inside Chromium without an activation click.
            FocusCommandWindow(openingRequest);
            BeginCommandPreparation(openingRequest);
            StartCommandFocusLease(openingRequest);
        };
        try { reveal.Start(); }
        catch (Exception ex)
        {
            Log("command reveal start exception: " + ex.Message);
            CancelCommandReveal();
            FailCommandPresentation(openingRequest, "reveal-start-exception");
        }
    }

    void ToggleCommand()
    {
        if (!_windowMode || _shutdownRequested || IsDisposed) return;
        // The shortcut is a real toggle.  The old implementation always Show()ed and left the page
        // to guess whether a second press meant "send" or "focus again"; when speech recognition was
        // unavailable there was consequently no path back to a hidden window.  Keep the OS/window
        // state authoritative and send an explicit action to the page.
        if (_commandMode && (_commandVisibleRequested || (_commandBootstrapShownComplete && Visible)))
        {
            _commandVisibleRequested = false;
            _commandPageOpen = false;
            _commandSurfaceReady = false;
            QueueCommandAction("close");
            PostPendingCommand();
            Hide();
            return;
        }
        if (!_commandMode && _commandPageOpen)
        {
            QueueCommandAction("close");
            PostPendingCommand();
            return;
        }
        if (_commandMode)
        {
            _commandLayoutPhase = "compact";
            _commandVisibleRequested = true;
            PositionCommandWindow();
            // Keep the native surface hidden until the page confirms that the capsule is painted.
            // Showing first turns any bridge/parser failure into the opaque black rectangle the user
            // reported. The matching open ACK below is the only command-mode presentation point.
        }
        else
        {
            if (!Visible) Show();
            if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Normal;
        }
        if (!_commandMode)
        {
            SetForegroundWindow(Handle);
            Activate();
            BringToFront();
        }
        QueueCommandAction("open");
        PostPendingCommand();
    }

    void QueueCommandAction(string action)
    {
        // Every action supersedes the preceding reveal. In particular, the second hotkey press must
        // cancel an unpainted first open synchronously; a third press starts one fresh timer/id.
        CancelCommandReveal();
        CancelCommandFocusLease();
        if (_commandMode) _commandLayoutPhase = "compact";
        _commandRequestId++;
        if (_commandRequestId <= 0) _commandRequestId = 1;
        _pendingCommandAction = action == "close" ? "close" : "open";
        _pendingCommand = true;
        _pendingPreparation = false;
        _commandPrepared = false;
        _commandPreparationRequestId = -1;
        _pendingPresentation = false;
        _commandPresentationAuthorizedRequestId = -1;
        _commandPresentationAuthorizationMode = "";
        _commandVerifiedFocusTicks = 0;
        _commandSurfaceReady = false;
        _commandVoiceStarted = false;
        _commandVoiceStartedOnce = false;
    }

    void PostPendingCommand()
    {
        if (!_pendingCommand || !_pageReady || !_pageBridgeReady
            || _web == null || _web.CoreWebView2 == null) return;
        try
        {
            string action = string.IsNullOrEmpty(_pendingCommandAction) ? "open" : _pendingCommandAction;
            string message = "{\"type\":\"collie-command\",\"action\":\"" + action
                           + "\",\"host\":\"" + (_commandMode ? "command" : "window")
                           + "\",\"request_id\":" + _commandRequestId.ToString()
                           + ",\"voice\":" + (_voiceInputEnabled ? "true" : "false") + "}";
            _web.CoreWebView2.PostWebMessageAsJson(message);
            Log("command posted action=" + action + " request_id=" + _commandRequestId.ToString());
        }
        catch (Exception ex) { Log("command post failed: " + ex.Message); }
    }

    void PostPendingPreparation()
    {
        if (!_commandMode || !_pendingPreparation || _commandPrepared
            || _commandPreparationRequestId != _commandRequestId
            || !_commandPageOpen || !Visible || _shutdownRequested || !_pageReady || !_pageBridgeReady
            || _web == null || _web.CoreWebView2 == null) return;
        try
        {
            string message = "{\"type\":\"collie-command-prepare\",\"request_id\":"
                           + _commandRequestId.ToString() + "}";
            _web.CoreWebView2.PostWebMessageAsJson(message);
        }
        catch (Exception ex) { Log("command preparation post failed: " + ex.Message); }
    }

    void PostPendingPresentation()
    {
        if (!_commandMode || !_pendingPresentation
            || !_commandPrepared || !_commandPageOpen || !Visible || _shutdownRequested
            || !_pageReady || !_pageBridgeReady
            || _web == null || _web.CoreWebView2 == null
            || !CommandPresentationAuthorizationIsLive(_commandRequestId)) return;
        try
        {
            string message = "{\"type\":\"collie-command-presented\",\"request_id\":"
                           + _commandRequestId.ToString() + "}";
            _web.CoreWebView2.PostWebMessageAsJson(message);
        }
        catch (Exception ex) { Log("command presentation post failed: " + ex.Message); }
    }

    CollieWallpaper()
    {
        int w = GetSystemMetrics(0), h = GetSystemMetrics(1);
        if (_commandMode)
        {
            Text = "Collie command";
            FormBorderStyle = FormBorderStyle.None;
            ShowInTaskbar = false;
            TopMost = true;
            StartPosition = FormStartPosition.Manual;
            // Placeholder bounds are never presented: OnHandleCreated recomputes these 660x176 DIPs
            // for the cursor monitor before the transparent bootstrap Show.
            ClientSize = new Size(660, 176);
            Location = Screen.FromPoint(Cursor.Position).WorkingArea.Location;
            // Application.Run(Form) must show once so Load creates WebView2 and the message pump owns
            // a real HWND. Opacity zero + no taskbar prevents a login-time flash; Shown immediately
            // hides it until Ctrl+Shift+Space asks for it.
            Opacity = 0;
            Shown += delegate
            {
                // Intent alone is not enough to expose the native surface: until the page has
                // acknowledged a rendered capsule it would still be an opaque black rectangle.
                // An unusually fast ACK may beat Shown; preserve only that proven-open surface.
                if (!_commandPageOpen) { Hide(); Opacity = 1; }
                _commandBootstrapShownComplete = true;
            };
            Icon = AppIcon();
        }
        else if (_windowMode)
        {
            Text = "Collie";
            FormBorderStyle = FormBorderStyle.Sizable;
            ShowInTaskbar = true;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(Math.Min(1180, (int)(w * 0.8)), Math.Min(820, (int)(h * 0.85)));
            MinimumSize = new Size(720, 520);
            Icon = AppIcon();   // the Collie mark in the title bar + taskbar
        }
        else if (_panelMode)
        {
            // Same geometry as the ground, so the page lays out identically in both and the
            // background lines up pixel for pixel. Starts with an EMPTY region: until the page
            // reports where its widgets are, this window covers nothing and takes no clicks.
            Text = "Collie desktop";
            FormBorderStyle = FormBorderStyle.None;
            ShowInTaskbar = false;
            StartPosition = FormStartPosition.Manual;
            Bounds = new Rectangle(0, 0, w, h);
            Region = new Region(new Rectangle(0, 0, 0, 0));
            Icon = AppIcon();
        }
        else
        {
            FormBorderStyle = FormBorderStyle.None;
            ShowInTaskbar = false;
            StartPosition = FormStartPosition.Manual;
            Bounds = new Rectangle(0, 0, w, h);
        }
        // In command mode the page intentionally has a transparent root around its rounded surface.
        // Never resolve those pixels to black: a light neutral is the safe no-flash fallback, while
        // the WebView controller itself is fully transparent and the native rounded region clips the
        // four corners. The other two modes retain their established black loading surface.
        BackColor = _commandMode ? CommandFallbackColor : Color.Black;
        _web = new WebView2();
        _web.DefaultBackgroundColor = _commandMode ? Color.Transparent : Color.Black;
        _web.Dock = DockStyle.Fill;
        _web.CoreWebView2InitializationCompleted += OnWebReady;
        Controls.Add(_web);
        Load += delegate { InitWeb(); };
        FormClosing += delegate (object sender, FormClosingEventArgs e)
        {
            // Alt+F4 / the shell close gesture dismisses the ambient surface; it must not kill the
            // process that owns the global hotkey until the named clean-shutdown event explicitly
            // asks us to exit (uninstall/reconfigure).  Otherwise one frustrated close leaves the
            // shortcut dead until the next Windows logon.
            if (_commandMode && !_shutdownRequested)
            {
                e.Cancel = true;
                _commandVisibleRequested = false;
                _commandPageOpen = false;
                QueueCommandAction("close");
                PostPendingCommand();
                Hide();
            }
        };
        FormClosed += delegate { Cleanup(); };
        // Also tear the hook + input attachment down on ANY process exit / unhandled crash, not only a
        // clean FormClosed — a half-installed hook or a dangling AttachThreadInput must never outlive us.
        AppDomain.CurrentDomain.ProcessExit += delegate { Cleanup(); };
        AppDomain.CurrentDomain.UnhandledException += delegate { Cleanup(); };
        // Clean-shutdown channel: another process Sets this named event -> we Close() gracefully, which
        // disposes WebView2 (browser process exits cleanly) instead of being -Force killed (which orphans
        // COM/GPU processes -> DCOM 10010 storm -> the Hyper-V/WSL network cascade).
        try
        {
            // Per-mode name: the quit event is AutoReset, so one Set wakes ONE waiter — with a shared
            // name, "stop the wallpaper" could just as easily close the app WINDOW (same exe, both
            // listening). The wallpaper keeps the historic name so existing stop paths still work.
            string quitName = _commandMode ? "collie-wallpaper-quit-command"
                            : (_panelMode ? "collie-wallpaper-quit-panel"
                            : (_windowMode ? "collie-wallpaper-quit-window" : "collie-wallpaper-quit"));
            _quit = new EventWaitHandle(false, EventResetMode.AutoReset, quitName);
            var qt = new Thread(delegate () { _quit.WaitOne(); try { BeginInvoke((MethodInvoker)delegate { _shutdownRequested = true; Close(); }); } catch { } });
            qt.IsBackground = true; qt.Start();
        }
        catch { }
    }

    async void InitWeb()
    {
        try
        {
            // Per-mode profile dir: the wallpaper and the app-window are DIFFERENT processes that may run
            // at the same time; one shared profile means whichever starts second can't lock it and comes
            // up blank. Separate dirs let both live.
            string profile = _commandMode ? "webview2-command" : (_windowMode ? "webview2-win" : "webview2");
            string udf = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                                      "collie", profile);
            var opts = new CoreWebView2EnvironmentOptions("--autoplay-policy=no-user-gesture-required");
            var env = await CoreWebView2Environment.CreateAsync(null, udf, opts);
            _env = env;   // child windows (the star map, the meadow) initialise from this same profile
            await _web.EnsureCoreWebView2Async(env);
        }
        catch (Exception ex) { Log("InitWeb EXCEPTION: " + ex.Message); }
    }

    void OnWebReady(object sender, CoreWebView2InitializationCompletedEventArgs e)
    {
        if (!e.IsSuccess) { Log("webview init FAILED: " + (e.InitializationException == null ? "?" : e.InitializationException.Message)); return; }
        try
        {
            _web.CoreWebView2.Settings.AreDefaultContextMenusEnabled = false;
            _web.CoreWebView2.Settings.IsStatusBarEnabled = false;
            _web.CoreWebView2.Settings.IsZoomControlEnabled = false;
            _web.DefaultBackgroundColor = _commandMode ? Color.Transparent : Color.Black;
            // The page owns the theme (a saved choice, else the system's) and can flip it at any
            // time from the toggle in its header. It posts {type:"theme",dark:bool}; the caption is
            // ours to repaint, so this is the only way the two can agree.
            _web.CoreWebView2.WebMessageReceived += delegate (object sT, CoreWebView2WebMessageReceivedEventArgs eT)
            {
                // WebMessageAsJson, NOT TryGetWebMessageAsString: postMessage is called with an
                // OBJECT, and the string accessor throws for anything that is not a bare string —
                // so every theme report was dropped and the caption never followed the page.
                string raw = null;
                try { raw = eT.WebMessageAsJson; } catch { }
                if (string.IsNullOrEmpty(raw)) { try { raw = eT.TryGetWebMessageAsString(); } catch { return; } }
                if (string.IsNullOrEmpty(raw)) return;
                int hitAt = raw.IndexOf("collie-panel-hit-regions", StringComparison.Ordinal);
                if (hitAt >= 0) { if (_panelMode) ApplyPanelHitRegions(raw.Substring(hitAt)); return; }
                int focusAt = raw.IndexOf("collie-panel-focus-region", StringComparison.Ordinal);
                if (focusAt >= 0) { if (_panelMode) ApplyPanelFocusRegion(raw.Substring(focusAt)); return; }
                if (raw.IndexOf("collie-panel-focus-request", StringComparison.Ordinal) >= 0)
                { if (_panelMode) FocusPanelComposer(); return; }
                int rgnAt = raw.IndexOf("collie-panel-regions", StringComparison.Ordinal);
                if (rgnAt >= 0) { if (_panelMode) ApplyPanelRegions(raw.Substring(rgnAt)); return; }
                if (raw.IndexOf("collie-command-ready", StringComparison.Ordinal) >= 0)
                {
                    _pageBridgeReady = true;
                    _pageVoiceAvailable = raw.IndexOf("\"voice_available\":true", StringComparison.Ordinal) >= 0
                                          || raw.IndexOf("\"voice_available\": true", StringComparison.Ordinal) >= 0;
                    Log("command page bridge ready");
                    RepublishCommandStatus();
                    PostPendingCommand();
                    return;
                }
                if (JsonStringValue(raw, "type") == "collie-command-layout")
                {
                    long requestId = JsonLong(raw, "request_id", -1);
                    string phase = JsonStringValue(raw, "phase");
                    if (requestId != _commandRequestId || !_commandMode || !_commandPageOpen
                        || !Visible || _shutdownRequested) return;
                    if (phase != "compact" && phase != "conversation") return;
                    ApplyCommandLayout(requestId, phase);
                    return;
                }
                if (raw.IndexOf("collie-command-prepared-state", StringComparison.Ordinal) >= 0)
                {
                    long requestId = JsonLong(raw, "request_id", -1);
                    // Preparation is a focus-only preflight. It never publishes surface_ready or
                    // voice state; it merely permits subsequent lease ticks to attempt LSFW_LOCK.
                    if (requestId != _commandRequestId || requestId != _commandPreparationRequestId
                        || !_commandMode || !_commandPageOpen || !Visible || _shutdownRequested) return;
                    _commandPrepared = true;
                    _commandVerifiedFocusTicks = 0;
                    _pendingPreparation = false;
                    Log("command preparation acknowledged request_id=" + requestId.ToString());
                    RepublishCommandStatus();
                    return;
                }
                if (raw.IndexOf("collie-command-focus-request", StringComparison.Ordinal) >= 0)
                {
                    long requestId = JsonLong(raw, "request_id", -1);
                    // Pointer/focus interaction in the page can arrive after the short presentation
                    // lease has expired. Honor only the exact currently-open request, once; never let
                    // an old renderer message resurrect or focus a closed/replaced capsule.
                    if (requestId != _commandRequestId || !_commandMode || !_commandPageOpen
                        || !Visible || _shutdownRequested) return;
                    // Keep this synchronous with WebMessage ordering: the page sends its focus request
                    // before its presented-state ACK, so surface_ready cannot be published until this
                    // native reassertion has completed.
                    FocusCommandWindow(requestId);
                    return;
                }
                if (raw.IndexOf("collie-command-presented-state", StringComparison.Ordinal) >= 0)
                {
                    long requestId = JsonLong(raw, "request_id", -1);
                    if (requestId != _commandRequestId
                        || requestId != _commandPresentationAuthorizedRequestId
                        || !_commandMode || !_commandPrepared || !_commandPageOpen
                        || !Visible || _shutdownRequested) return;
                    // The page posts this only after its exact-id presented handler has focused the
                    // textarea and attempted voice. The FIRST ACK is accepted only while its exact
                    // verified-focus authorization is still live and freshly confirms that the
                    // foreground thread focuses this WebView or one of its Chromium descendants.
                    // Later exact-id ACKs may update asynchronous voice state after the lease ends.
                    // Keeping the authorization id until the next action is intentional: Web Speech's
                    // onstart is asynchronous and is what makes voice_started_once become true.
                    bool firstPresentationAck = !_commandSurfaceReady;
                    bool domFocused = raw.IndexOf("\"dom_focused\":true", StringComparison.Ordinal) >= 0
                                      || raw.IndexOf("\"dom_focused\": true", StringComparison.Ordinal) >= 0;
                    if (firstPresentationAck
                        && (!domFocused || !CommandPresentationAuthorizationIsLive(requestId)))
                    {
                        Log("command presentation ACK ignored without live focus request_id="
                            + requestId.ToString() + " mode=" + _commandPresentationAuthorizationMode
                            + " dom_focused=" + (domFocused ? "true" : "false"));
                        return;
                    }
                    _commandSurfaceReady = true;
                    _commandVoiceStarted = raw.IndexOf("\"voice_started\":true", StringComparison.Ordinal) >= 0
                                           || raw.IndexOf("\"voice_started\": true", StringComparison.Ordinal) >= 0;
                    if (_commandVoiceStarted) _commandVoiceStartedOnce = true;
                    _pendingPresentation = false;
                    // A fresh native+DOM route now proves the first-click contract. Stop bounded
                    // reassertion and release LSFW immediately; it is no longer an authorization gate.
                    if (firstPresentationAck) CancelCommandFocusLease();
                    Log("command presentation acknowledged request_id=" + requestId.ToString());
                    RepublishCommandStatus();
                    return;
                }
                if (raw.IndexOf("collie-command-state", StringComparison.Ordinal) >= 0)
                {
                    long requestId = JsonLong(raw, "request_id", -1);
                    // WebView delivery and BeginInvoke are asynchronous. A stale close receipt must
                    // never hide a newer open request when the user presses the shortcut quickly.
                    if (requestId != _commandRequestId) return;
                    bool open = raw.IndexOf("\"open\":true", StringComparison.Ordinal) >= 0
                                || raw.IndexOf("\"open\": true", StringComparison.Ordinal) >= 0;
                    _pendingCommand = false;
                    _commandPageOpen = open;
                    if (_commandMode && open)
                    {
                        long openingRequest = requestId;
                        try
                        {
                            BeginInvoke((MethodInvoker)delegate
                            {
                                if (openingRequest != _commandRequestId || !_commandPageOpen
                                    || !_commandVisibleRequested) return;
                                PresentCommandWindow();
                                // WM_HOTKEY grants this process a short SetForegroundWindow window.
                                // Use it immediately, before the 320ms compositor warm-up. A successful
                                // exact-request LSFW lock is preserved when the post-reveal lease starts.
                                FocusCommandWindow(openingRequest);
                                TryLockCommandForeground(openingRequest);
                                _commandVisibleRequested = false;
                                RepublishCommandStatus();
                                // Keep the cold WebView frame transparent long enough for a compositor
                                // commit. Reveal first, then ask the browser speech service to start.
                                // The exact-id presentation message is retried until acknowledged.
                                ScheduleCommandReveal(openingRequest);
                            });
                        }
                        catch { }
                    }
                    else if (_commandMode && !open)
                    {
                        CancelCommandReveal();
                        CancelCommandFocusLease();
                        _commandVisibleRequested = false;
                        _pendingPreparation = false;
                        _commandPrepared = false;
                        _commandPreparationRequestId = -1;
                        _pendingPresentation = false;
                        _commandPresentationAuthorizedRequestId = -1;
                        _commandPresentationAuthorizationMode = "";
                        _commandVerifiedFocusTicks = 0;
                        _commandSurfaceReady = false;
                        _commandVoiceStarted = false;
                        _commandLayoutPhase = "compact";
                        long closingRequest = requestId;
                        try
                        {
                            BeginInvoke((MethodInvoker)delegate
                            {
                                if (closingRequest == _commandRequestId && !_commandPageOpen
                                    && !_commandVisibleRequested) Hide();
                            });
                        }
                        catch { }
                    }
                    Log("command acknowledged open=" + (open ? "true" : "false")
                        + " request_id=" + requestId.ToString());
                    RepublishCommandStatus();
                    return;
                }
                if (raw.IndexOf("\"theme\"", StringComparison.Ordinal) >= 0)
                {
                    bool dark = raw.IndexOf("\"dark\":true", StringComparison.Ordinal) >= 0
                                || raw.IndexOf("\"dark\": true", StringComparison.Ordinal) >= 0;
                    ApplyCommandSubstrate(dark);
                    if (dark == _titleBarDark) return;
                    try { BeginInvoke((MethodInvoker)delegate { ApplyTitleBarTheme(dark); }); } catch { }
                }
            };
            // Resolve the exact first-party origin before installing the microphone policy.  A WebView
            // can navigate, and blanket permission would otherwise follow it to an unrelated site.
            string url = Environment.GetEnvironmentVariable("COLLIE_WALLPAPER_URL");
            if (string.IsNullOrEmpty(url))
                url = _commandMode ? "http://127.0.0.1:8787/?capsule=1"
                    : (_windowMode ? "http://127.0.0.1:8787/" : "http://127.0.0.1:8787/wallpaper");
            _trustedCommandOrigin = IsLoopbackHttpUrl(url) ? OriginOf(url) : "";

            // Only a voice-enabled dedicated capsule may receive an automatic microphone grant, and
            // only while its top-level page and requesting frame are on the configured Collie loopback
            // origin. Voice off is a host-enforced deny, not merely a hidden microphone button.
            // The normal app keeps WebView2's ordinary prompt; the unpromptable wallpaper is denied.
            _web.CoreWebView2.PermissionRequested += delegate (object s3, CoreWebView2PermissionRequestedEventArgs e3)
            {
                if (e3.PermissionKind != CoreWebView2PermissionKind.Microphone) return;
                if (_commandMode)
                    e3.State = _voiceInputEnabled && TrustedCommandMicrophoneOrigin(e3.Uri)
                        ? CoreWebView2PermissionState.Allow : CoreWebView2PermissionState.Deny;
                else if (!_windowMode)
                    e3.State = CoreWebView2PermissionState.Deny;
            };
            // URL is passed by `collie wallpaper` via COLLIE_WALLPAPER_URL (the port is picked at
            // runtime, not hardcoded, so it never collides with a busy 8787). Fallback for a manual run.
            // Window mode shows the full GUI, command mode shows only the outcome capsule, and
            // wallpaper mode shows the desktop page.
            // Keep target=_blank links (the star map, the meadow) INSIDE the app. Unhandled they
            // escape to a bare popup / the system browser, which is exactly what makes a native shell
            // feel like a browser wrapper. Each opens its own titled Collie window instead.
            _web.CoreWebView2.NewWindowRequested += delegate (object s2, CoreWebView2NewWindowRequestedEventArgs e2)
            {
                e2.Handled = true;
                if (_windowMode) OpenChildWindow(e2.Uri);
                else _web.CoreWebView2.Navigate(e2.Uri);   // wallpaper has no window manager: navigate in place
            };
            // SELF-HEAL the startup race: right after login the engine can load before the local
            // server binds its port, and WebView2 would then sit on a blank error page FOREVER — the
            // "wallpaper is running but the desktop is blank" bug. Retry every ~2s until it loads.
            _web.CoreWebView2.NavigationCompleted += delegate (object sN, CoreWebView2NavigationCompletedEventArgs eN)
            {
                if (eN.IsSuccess)
                {
                    _pageReady = true;
                    // Navigation completion alone does not prove the page installed its bridge
                    // listener. The page's collie-command-ready message is the delivery fence.
                    RepublishCommandStatus();
                    PostPendingCommand();
                    return;
                }
                _pageReady = false;
                _pageBridgeReady = false;
                RepublishCommandStatus();
                var rt = new Timer(); rt.Interval = 2000;
                rt.Tick += delegate { rt.Stop(); rt.Dispose(); try { _web.CoreWebView2.Navigate(url); } catch { } };
                rt.Start();
            };
            _web.CoreWebView2.NavigationStarting += delegate
            {
                // During the one-time transparent bootstrap WinForms may report Visible before the
                // user has ever summoned Collie. Treat visibility as intent only after Shown has
                // completed; otherwise initial navigation would manufacture an unsolicited open.
                bool reopen = _commandMode && (_commandPageOpen || _commandVisibleRequested
                    || (_commandBootstrapShownComplete && Visible));
                CancelCommandReveal();
                CancelCommandFocusLease();
                _pageReady = false;
                _pageBridgeReady = false;
                _pageVoiceAvailable = false;
                _commandPageOpen = false;
                _pendingPreparation = false;
                _commandPrepared = false;
                _commandPreparationRequestId = -1;
                _pendingPresentation = false;
                _commandPresentationAuthorizedRequestId = -1;
                _commandPresentationAuthorizationMode = "";
                _commandVerifiedFocusTicks = 0;
                _commandSurfaceReady = false;
                _commandVoiceStarted = false;
                _commandLayoutPhase = "compact";
                if (_commandMode)
                {
                    Hide();
                    if (reopen)
                    {
                        _commandVisibleRequested = true;
                        QueueCommandAction("open");
                    }
                }
                RepublishCommandStatus();
            };
            _web.CoreWebView2.Navigate(url);
        }
        catch (Exception ex) { Log("navigate EXCEPTION: " + ex.Message); }
        // Everything below is WALLPAPER-only: pinning under the desktop icons and forwarding desktop
        // mouse/keyboard into the page. A normal window is activatable and WebView2 gets input natively.
        if (_windowMode || _panelMode)
        {
            Log(_panelMode ? "panel mode: ordinary window, no pin, no hooks"
                           : "window mode: skipping pin + input hooks");
            if (_panelMode) StartPanelWatchdog();
            return;
        }
        Pin();
        // Ground mode is display only. No mouse hook means no synthesised WM_MOUSEMOVE into
        // Chromium, which is what made the cursor strobe and the surface repaint ~70 times a
        // second — and it retires the global WH_MOUSE_LL that once stalled left-click everywhere.
        if (_groundMode) { Log("ground mode: display only, no input hooks"); return; }

        // resolve the Chromium child + install input hooks a moment after the page starts
        var t = new Timer();
        t.Interval = 1500;
        t.Tick += delegate
        {
            IntPtr input = FindInput();
            if (input != IntPtr.Zero) { _input = input; }
            if (_input != IntPtr.Zero && _mouseHook == IntPtr.Zero)
            {
                InstallHooks(); Log("input=" + _input + " hooks installed");
                // watchdog: keep the window pinned BELOW the icons (so it can never cover them), and
                // re-resolve the Chromium input HWND if it goes stale (e.g. after a page reload).
                var wd = new Timer(); wd.Interval = 2000;
                wd.Tick += delegate { RepinZ(); if (_input == IntPtr.Zero) { IntPtr ni = FindInput(); if (ni != IntPtr.Zero) _input = ni; } };
                wd.Start();
                if (Environment.GetEnvironmentVariable("COLLIE_SELFTEST") == "1")
                {
                    var st = new Timer(); st.Interval = 2500;
                    st.Tick += delegate { st.Stop(); SelfTest(); };
                    st.Start();
                }
            }
            if (_input != IntPtr.Zero) t.Stop();
        };
        t.Start();
    }

    static bool _attached;
    static void EnsureFocus()
    {
        if (_input == IntPtr.Zero) return;
        uint ipid; uint it = GetWindowThreadProcessId(_input, out ipid);
        uint mt = GetCurrentThreadId();
        if (!_attached && it != mt) { AttachThreadInput(mt, it, true); _attached = true; }
        SetFocus(_input);
    }

    void SelfTest()
    {
        uint ipid; uint it = GetWindowThreadProcessId(_input, out ipid);
        Log("selftest input=" + _input + " inputPid=" + ipid + " ourPid=" + GetCurrentProcessId() + " inputThread=" + it + " ourThread=" + GetCurrentThreadId());
        EnsureFocus();
        int x = 2450, y = 1355; IntPtr lp = (IntPtr)((y << 16) | x);
        PostMessageW(_input, (uint)WM_MOUSEMOVE, IntPtr.Zero, lp);
        PostMessageW(_input, (uint)WM_LBUTTONDOWN, (IntPtr)MK_LBUTTON, lp);
        PostMessageW(_input, (uint)WM_LBUTTONUP, IntPtr.Zero, lp);
        foreach (char c in "hello collie") PostMessageW(_input, (uint)WM_CHAR, (IntPtr)c, IntPtr.Zero);
        Log("selftest posted click+text");
    }

    // A second ordinary Collie window — used for target=_blank links (star map, meadow) so they stay
    // in the app instead of escaping to the browser.
    static CoreWebView2Environment _env;   // set once by InitWeb; child windows share its profile
    static void OpenChildWindow(string url)
    {
        try
        {
            Form f = new Form();
            f.Text = "Collie";
            f.StartPosition = FormStartPosition.CenterScreen;
            f.ClientSize = new Size(1100, 780);
            f.BackColor = Color.Black;
            f.Icon = AppIcon();
            WebView2 w = new WebView2();
            w.Dock = DockStyle.Fill;
            w.DefaultBackgroundColor = Color.Black;
            w.CoreWebView2InitializationCompleted += delegate
            {
                try { w.CoreWebView2.Navigate(url); } catch (Exception e) { Log("child nav: " + e.Message); }
            };
            f.Controls.Add(w);
            f.Show();
            // Subscribing to InitializationCompleted does not START initialisation — nothing does
            // until EnsureCoreWebView2Async (or Source=) is called. Without this the event never
            // fires, Navigate never runs, and the child is a permanently black window.
            w.EnsureCoreWebView2Async(_env);
        }
        catch (Exception ex) { Log("child window failed: " + ex.Message); }
    }

    void Pin()
    {
        _progman = FindWindowW("Progman", null);
        // Win10/11: ask Progman to spawn the "behind the icons" WorkerW. On builds where the desktop
        // wallpaper is painted on top of a plain Progman child, this splits the paint onto a WorkerW
        // BELOW us — without it a SetParent-to-Progman child stays hidden under the wallpaper (the
        // "engine runs but the desktop is blank/shows the default wallpaper" case). Harmless if already split.
        IntPtr smRes;
        SendMessageTimeoutW(_progman, 0x052C, IntPtr.Zero, IntPtr.Zero, 0x0002 /*SMTO_ABORTIFHUNG*/, 1000, out smRes);
        IntPtr defview = FindWindowExW(_progman, IntPtr.Zero, "SHELLDLL_DefView", null);
        IntPtr hwnd = this.Handle;
        long style = (long)GetWindowLongPtrW(hwnd, GWL_STYLE);
        style = (style & ~(WS_POPUP | WS_CAPTION | WS_THICKFRAME | WS_BORDER)) | WS_CHILD | WS_CLIPSIBLINGS | WS_CLIPCHILDREN;
        SetWindowLongPtrW(hwnd, GWL_STYLE, (IntPtr)style);
        // WS_EX_NOACTIVATE: the wallpaper must NEVER become the foreground/active window. Without this,
        // a forwarded click let Chromium activate our window, so the next desktop click was an "activating
        // click" and icon double-click broke. Keyboard still reaches the chat via AttachThreadInput+SetFocus.
        long ex = (long)GetWindowLongPtrW(hwnd, GWL_EXSTYLE);
        SetWindowLongPtrW(hwnd, GWL_EXSTYLE, (IntPtr)(ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW));
        SetParent(hwnd, _progman);
        int w = GetSystemMetrics(0), h = GetSystemMetrics(1);
        // Z-order below the icons. If 0x052C reparented SHELLDLL_DefView under a WorkerW (a known
        // Win10-vs-some-Win11 split), the lookup above returns Zero — and SetWindowPos treats Zero as
        // HWND_TOP, which would slam the wallpaper OVER the icons and break double-click. Fall back to
        // HWND_BOTTOM (1) so we can never land on top of the icons even when DefView isn't found.
        IntPtr insertAfter = defview != IntPtr.Zero ? defview : (IntPtr)1;   // (IntPtr)1 = HWND_BOTTOM
        SetWindowPos(hwnd, insertAfter, 0, 0, w, h, SWP_NOACTIVATE | SWP_SHOWWINDOW);
        _pinned = true;   // from now on WndProc keeps us below the icons on every z-order change
        Log("pinned progman=" + _progman + " defview=" + defview + " hwnd=" + hwnd + " " + w + "x" + h);
    }

    // Re-assert the wallpaper's z-order directly below the desktop icons. Called on a watchdog timer so
    // the window can never drift on top of the icons (which is what made them "disappear").
    void RepinZ()
    {
        if (_progman == IntPtr.Zero) return;
        IntPtr defview = FindWindowExW(_progman, IntPtr.Zero, "SHELLDLL_DefView", null);
        if (defview != IntPtr.Zero) SetWindowPos(this.Handle, defview, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
    }

    IntPtr FindInput()
    {
        _enumFound = IntPtr.Zero; _enumArea = 0;
        if (_enumProc == null) _enumProc = new EnumProc(EnumCb);
        EnumChildWindows(this.Handle, _enumProc, IntPtr.Zero);
        return _enumFound;
    }
    static bool EnumCb(IntPtr h, IntPtr l)
    {
        StringBuilder c = new StringBuilder(64); GetClassNameW(h, c, 64);
        if (c.ToString() == "Chrome_WidgetWin_1")
        {
            RECT r; GetWindowRect(h, out r);
            int a = (r.right - r.left) * (r.bottom - r.top);
            if (a > _enumArea) { _enumArea = a; _enumFound = h; }
        }
        return true;
    }

    static System.Threading.SynchronizationContext _uiCtx;   // the UI message loop, for deferring focus

    void InstallHooks()
    {
        _uiCtx = System.Threading.SynchronizationContext.Current;
        IntPtr hMod = GetModuleHandleW(null);
        _mouseProc = new HookProc(MouseProc);
        _mouseHook = SetWindowsHookExW(WH_MOUSE_LL, _mouseProc, hMod, 0);
        // NO keyboard hook: a click's EnsureFocus() gives the Chromium window REAL keyboard focus, so
        // Windows delivers keystrokes AND IME composition (Chinese/日本語) to it natively. Forwarding
        // keys on top of that doubled every character. Mouse still must be forwarded (hit-tested by
        // z-order, so desktop clicks never reach a behind-icons window).
        SetupIconHitTest();
        RefreshIconRects();
        var rt = new Timer(); rt.Interval = 1500; rt.Tick += delegate { RefreshIconRects(); }; rt.Start();
    }

    // Cache every desktop-icon rectangle (screen coords). Done on the UI thread via a timer — NEVER inside
    // the mouse hook — so the hook can decide "over an icon?" with a cheap cached rect test and no blocking call.
    static RECT[] _iconRects = new RECT[0];
    void RefreshIconRects()
    {
        if (_icons == IntPtr.Zero || _iconMem == IntPtr.Zero) return;
        int n = SendMessageW(_icons, 0x1004 /* LVM_GETITEMCOUNT */, IntPtr.Zero, IntPtr.Zero).ToInt32();
        if (n < 0) n = 0; if (n > 1000) n = 1000;
        RECT[] arr = new RECT[n]; int cnt = 0;
        for (int i = 0; i < n; i++)
        {
            byte[] rb = new byte[16]; IntPtr w;                       // left=0 => LVIR_BOUNDS
            WriteProcessMemory(_iconProc, _iconMem, rb, (IntPtr)16, out w);
            SendMessageW(_icons, 0x100E /* LVM_GETITEMRECT */, (IntPtr)i, _iconMem);
            byte[] rb2 = new byte[16]; IntPtr rd;
            if (!ReadProcessMemory(_iconProc, _iconMem, rb2, (IntPtr)16, out rd)) continue;
            POINT tl; tl.x = BitConverter.ToInt32(rb2, 0); tl.y = BitConverter.ToInt32(rb2, 4);
            POINT br; br.x = BitConverter.ToInt32(rb2, 8); br.y = BitConverter.ToInt32(rb2, 12);
            ClientToScreen(_icons, ref tl); ClientToScreen(_icons, ref br);
            RECT r; r.left = tl.x; r.top = tl.y; r.right = br.x; r.bottom = br.y;
            arr[cnt++] = r;
        }
        RECT[] outp = new RECT[cnt]; Array.Copy(arr, outp, cnt); _iconRects = outp;
        if (!_dumped && cnt > 0) { _dumped = true; for (int i = 0; i < cnt; i++) Log("ICONRECT[" + i + "] " + outp[i].left + "," + outp[i].top + " " + (outp[i].right - outp[i].left) + "x" + (outp[i].bottom - outp[i].top)); }
    }
    static bool _dumped = false;
    static bool OverIconCached(int sx, int sy)
    {
        RECT[] a = _iconRects;
        for (int i = 0; i < a.Length; i++) if (sx >= a[i].left && sx < a[i].right && sy >= a[i].top && sy < a[i].bottom) return true;
        return false;
    }
    static void DetachInput()
    {
        // Undo the AttachThreadInput(mt, it, true) EnsureFocus made — a cross-process input attachment
        // left dangling when our thread dies is a classic way to wedge the system input queue.
        try
        {
            if (_attached && _input != IntPtr.Zero)
            {
                uint ipid; uint it = GetWindowThreadProcessId(_input, out ipid);
                uint mt = GetCurrentThreadId();
                if (it != mt) AttachThreadInput(mt, it, false);
            }
        }
        catch { }
        _attached = false;
    }

    static bool _cleaned;
    void Cleanup()
    {
        if (_cleaned) return; _cleaned = true;
        // Invalidate every renderer receipt before tearing down timers/hooks. Cleanup can also run
        // from ProcessExit/UnhandledException, not only the quit-event path that sets this earlier.
        _shutdownRequested = true;
        _commandRequestId++;
        if (_commandRequestId <= 0) _commandRequestId = 1;
        _commandPageOpen = false;
        _commandVisibleRequested = false;
        CancelCommandReveal();
        CancelCommandFocusLease();
        _pendingPreparation = false;
        _commandPrepared = false;
        _commandPreparationRequestId = -1;
        _pendingPresentation = false;
        _commandPresentationAuthorizedRequestId = -1;
        _commandPresentationAuthorizationMode = "";
        _commandVerifiedFocusTicks = 0;
        _commandSurfaceReady = false;
        _commandLayoutPhase = "compact";
        if (_hotKeyOwnershipTimer != null)
        {
            try { _hotKeyOwnershipTimer.Stop(); _hotKeyOwnershipTimer.Dispose(); } catch { }
            _hotKeyOwnershipTimer = null;
        }
        if (_hotKeyRegistered && IsHandleCreated)
        {
            try { UnregisterHotKey(Handle, COMMAND_HOTKEY_ID); } catch { }
            _hotKeyRegistered = false;
        }
        if (_commandMode && !string.IsNullOrEmpty(_commandStatusPath))
        {
            try { if (File.Exists(_commandStatusPath)) File.Delete(_commandStatusPath); } catch { }
        }
        if (_mouseHook != IntPtr.Zero) UnhookWindowsHookEx(_mouseHook);
        _mouseHook = IntPtr.Zero;
        _mouseShortcutHeld = false;
        if (_activeInstance == this) _activeInstance = null;
        if (_keyHook != IntPtr.Zero) UnhookWindowsHookEx(_keyHook);
        DetachInput();
        try { if (_web != null) { _web.Dispose(); } } catch { }   // dispose WebView2 -> browser process exits cleanly (no orphaned COM)
    }

    // Resolve the desktop icon ListView and prepare a remote LVHITTESTINFO in explorer's address space,
    // so we can ask "is a real icon under the cursor?" (LVM_HITTEST) before deciding to forward a click.
    void SetupIconHitTest()
    {
        IntPtr defview = FindWindowExW(_progman, IntPtr.Zero, "SHELLDLL_DefView", null);
        _icons = FindWindowExW(defview, IntPtr.Zero, "SysListView32", null);
        if (_icons == IntPtr.Zero) { Log("icons listview not found"); return; }
        uint pid; GetWindowThreadProcessId(_icons, out pid);
        _iconProc = OpenProcess(0x0008 | 0x0010 | 0x0020, false, pid); // VM_OPERATION | VM_READ | VM_WRITE
        if (_iconProc != IntPtr.Zero) _iconMem = VirtualAllocEx(_iconProc, IntPtr.Zero, (IntPtr)32, 0x3000, 0x04);
        Log("iconhittest icons=" + _icons + " proc=" + _iconProc + " mem=" + _iconMem);
    }
    static bool OverIcon(int sx, int sy)
    {
        if (_icons == IntPtr.Zero || _iconMem == IntPtr.Zero) return false;
        POINT p; p.x = sx; p.y = sy; ScreenToClient(_icons, ref p);
        byte[] buf = new byte[32];
        BitConverter.GetBytes(p.x).CopyTo(buf, 0);
        BitConverter.GetBytes(p.y).CopyTo(buf, 4);
        IntPtr wrote;
        WriteProcessMemory(_iconProc, _iconMem, buf, (IntPtr)32, out wrote);
        IntPtr r = SendMessageW(_icons, 0x1012 /* LVM_HITTEST */, IntPtr.Zero, _iconMem);
        return r.ToInt64() >= 0;   // >=0 means an icon item is under the cursor
    }

    // Cheap rectangle test for "is this click in the chat box?" (bottom-center, ~92px above the taskbar).
    // Used to decide whether to grab keyboard focus — no cross-process calls, safe inside the LL hook.
    static bool InChat(int sx, int sy)
    {
        int w = GetSystemMetrics(0), h = GetSystemMetrics(1);
        int cw = Math.Min(680, (int)(w * 0.92));
        int cx = w / 2, halfx = cw / 2 + 30;
        int bottom = h - 92 + 8, top = h - 92 - 380;   // generous upward for a grown log/composer
        return sx >= cx - halfx && sx <= cx + halfx && sy >= top && sy <= bottom;
    }

    static bool DesktopIsForeground()
    {
        IntPtr fg = GetForegroundWindow();
        if (fg == _progman) return true;
        StringBuilder c = new StringBuilder(32); GetClassNameW(fg, c, 32);
        string s = c.ToString();
        return s == "WorkerW" || s == "Progman";
    }

    static IntPtr MouseProc(int nCode, IntPtr wParam, IntPtr lParam)
    {
        // Keep this callback CHEAP — it runs for every mouse event system-wide. No file I/O, no blocking
        // calls, and a fast early-out over desktop icons so Explorer's click/double-click is never delayed.
        if (nCode >= 0)
        {
            int msg = (int)wParam;
            if (_commandMode && _mouseShortcut != "off")
            {
                MSLLHOOKSTRUCT shortcutEvent = (MSLLHOOKSTRUCT)Marshal.PtrToStructure(
                    lParam, typeof(MSLLHOOKSTRUCT));
                // Only physical input may summon Collie. Desktop automation and SendInput should not
                // accidentally open the microphone-bearing command surface.
                if ((shortcutEvent.flags & LLMHF_INJECTED) == 0)
                {
                    bool down = false, up = false, matches = false;
                    if (_mouseShortcut == "middle")
                    {
                        down = msg == WM_MBUTTONDOWN; up = msg == WM_MBUTTONUP; matches = down || up;
                    }
                    else if (msg == WM_XBUTTONDOWN || msg == WM_XBUTTONUP)
                    {
                        uint button = (shortcutEvent.mouseData >> 16) & 0xFFFF;
                        uint wanted = _mouseShortcut == "xbutton2" ? 2u : 1u;
                        matches = button == wanted;
                        down = matches && msg == WM_XBUTTONDOWN;
                        up = matches && msg == WM_XBUTTONUP;
                    }
                    if (matches)
                    {
                        if (down && !_mouseShortcutHeld)
                        {
                            _mouseShortcutHeld = true;
                            CollieWallpaper target = _activeInstance;
                            if (target != null && !target.IsDisposed)
                            {
                                try { target.BeginInvoke((MethodInvoker)delegate { target.ToggleCommand(); }); }
                                catch { }
                            }
                        }
                        if (up) _mouseShortcutHeld = false;
                        // The configured side/middle click belongs to Collie; allowing it to continue
                        // would also navigate Back/Forward or auto-scroll in the foreground app.
                        return new IntPtr(1);
                    }
                }
            }
            if (_input != IntPtr.Zero)
            {
            bool isBtn = (msg == WM_LBUTTONDOWN || msg == WM_LBUTTONUP || msg == WM_RBUTTONDOWN || msg == WM_RBUTTONUP);
            if (isBtn || msg == WM_MOUSEMOVE || msg == WM_MOUSEWHEEL)
            {
                // THROTTLE moves to ~70Hz. Forwarding every raw move floods Chromium (behind the icons)
                // with repaints → flicker + laggy clicks. Buttons/wheel are rare, never throttled.
                if (msg == WM_MOUSEMOVE)
                {
                    int now = Environment.TickCount;
                    if (now - _lastMove < 14) return CallNextHookEx(_mouseHook, nCode, wParam, lParam);
                    _lastMove = now;
                }
                MSLLHOOKSTRUCT m = (MSLLHOOKSTRUCT)Marshal.PtrToStructure(lParam, typeof(MSLLHOOKSTRUCT));
                // over a real icon on a click => do nothing (short-circuits before any other work)
                if (!(isBtn && OverIconCached(m.pt.x, m.pt.y)) && DesktopIsForeground())
                {
                    if (msg == WM_MOUSEWHEEL)
                    {
                        int delta = (short)((m.mouseData >> 16) & 0xFFFF);
                        PostMessageW(_input, WM_MOUSEWHEEL, (IntPtr)(delta << 16), (IntPtr)((m.pt.y << 16) | (m.pt.x & 0xFFFF)));
                    }
                    else
                    {
                        // DEFER focus off the hook callback. EnsureFocus() does a synchronous, cross-process
                        // AttachThreadInput+SetFocus; running it INSIDE a WH_MOUSE_LL callback stalls the
                        // SYSTEM-WIDE mouse queue (a slow/blocked call froze left-click everywhere). BeginInvoke
                        // queues it onto our message loop, so the hook returns immediately.
                        if (msg == WM_LBUTTONDOWN) { _buttons |= MK_LBUTTON; if (InChat(m.pt.x, m.pt.y)) { var ctx = _uiCtx; if (ctx != null) { try { ctx.Post(delegate { EnsureFocus(); }, null); } catch { } } } }
                        else if (msg == WM_LBUTTONUP) _buttons &= ~MK_LBUTTON;
                        else if (msg == WM_RBUTTONDOWN) _buttons |= MK_RBUTTON;
                        else if (msg == WM_RBUTTONUP) _buttons &= ~MK_RBUTTON;
                        POINT c = m.pt; ScreenToClient(_input, ref c);
                        PostMessageW(_input, (uint)msg, (IntPtr)_buttons, (IntPtr)((c.y << 16) | (c.x & 0xFFFF)));
                    }
                }
            }
            }
        }
        return CallNextHookEx(_mouseHook, nCode, wParam, lParam);
    }

    static IntPtr KeyProc(int nCode, IntPtr wParam, IntPtr lParam)
    {
        if (nCode >= 0 && _input != IntPtr.Zero && DesktopIsForeground())
        {
            int msg = (int)wParam;
            KBDLLHOOKSTRUCT k = (KBDLLHOOKSTRUCT)Marshal.PtrToStructure(lParam, typeof(KBDLLHOOKSTRUCT));
            bool down = (msg == WM_KEYDOWN || msg == WM_SYSKEYDOWN);
            uint scan = k.scanCode;
            IntPtr lp = down ? (IntPtr)(1 | (int)(scan << 16)) : (IntPtr)(1 | (int)(scan << 16) | (0xC0 << 24));
            // Post ONLY WM_KEYDOWN/WM_KEYUP — Chromium's own message pump runs TranslateMessage and
            // generates WM_CHAR itself. Posting WM_CHAR too would double every character.
            PostMessageW(_input, (uint)(down ? WM_KEYDOWN : WM_KEYUP), (IntPtr)k.vkCode, lp);
        }
        return CallNextHookEx(_keyHook, nCode, wParam, lParam);
    }
}
