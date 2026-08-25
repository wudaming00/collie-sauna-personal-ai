# The desktop app

Collie is more than a terminal tool: the Windows installer and Apple-silicon DMG provide a native
desktop home, while `collie web` exposes the same work surface in a browser on every platform.
Windows can also install the optional live wallpaper and real-browser bridge from Setup.

Collie's desktop identity follows one simple rule: **one computer = one Collie**. The native app,
global command capsule, browser Workbench, wallpaper, and paired phone all address that same operator.
Models and specialist workers may change behind it; they are resources, not separate companions.

## Work queue

The default **Work** destination is an operational queue, not a chat dashboard:

1. **Needs You** appears first only when an exact decision, identity step, or authority expansion is
   waiting.
2. **Open Missions** shows durable outcomes Collie still owns, including waiting and paused work.
3. **Recent outcomes** shows what finished and whether independent evidence supports it.

Missions is the durable-work index; Activity is the receipt and audit ledger; Library contains
capabilities; Pack shows brains, workers, connections, and devices. The single bottom composer starts
a new outcome, while the contextual inspector holds run controls, timeline, diffs, and evidence.

## Native window

```bash
collie app
```

Opens the Work queue in a real desktop window instead of a browser tab. Windows uses WebView2; the
packaged macOS app uses its native WebKit host. Source installs fall back to the browser GUI when a
native host is unavailable.

## Global command shortcut

```bash
collie command --install     # keep the capsule ready at logon
collie command --stop        # stop the hidden host for this login
collie command --uninstall   # remove its logon autostart
```

On Windows, press **Ctrl+Shift+Space** from any app to open the small Collie capsule. With **Voice
input in command capsule** on, the first press begins listening and a second press stops and submits.
With voice input off, the same shortcut opens or focuses the typed field; press Enter to submit. The
capsule hands the outcome into the same Run/Mission queue as the Workbench and then closes—it does
not create another chat silo. The main Windows installer enables this host by default. The host and
wallpaper are separate processes.

Collie reports the shortcut as ready only after Windows confirms the global registration. If another
app owns the chord, Settings shows the native error and restores the prior binding. The two controls
under **Settings → Desktop & devices** are deliberately independent:

- **Open Collie shortcut → Off** unregisters the entire global shortcut. It is not a voice-only off
  switch; the ordinary in-app composer remains available.
- **Voice input in command capsule → Off** keeps the global shortcut and typed capsule available,
  while the dedicated native host denies microphone permission.

When voice is on, automatic microphone permission is limited to the dedicated capsule on Collie's
exact loopback origin. Transcription uses the embedded browser/OS Web Speech service and may use its
cloud recognition service. Turn voice input off when that data path is not appropriate.

## Live star-map wallpaper

```bash
collie wallpaper --install     # behind your desktop icons, starts at logon
collie wallpaper --stop        # stop the running engine
collie wallpaper --uninstall   # remove the logon autostart
```

A live desktop background that renders Collie's star-map. On Windows it draws *behind* your icons
via a WebView2 engine (built once on first run from the shipped C# source — no .NET SDK needed);
elsewhere it degrades to a borderless full-screen window. Per-user, no admin.

## Real-browser bridge

```bash
collie browser-bridge            # run the bridge in the foreground
collie browser-bridge --install  # start it at logon
```

Lets Collie's `browser_*` tools drive **your** already-logged-in Chrome, instead of a fresh headless
browser that isn't signed in to anything. It works with a small Chrome extension that polls a
localhost bridge:

1. Run the bridge (`collie browser-bridge`, or install it at logon).
2. Load the extension from `harness/browser_ext` (Chrome → Extensions → Load unpacked). The installer
   bundles it; developers point Chrome at the folder in the collie they're running.
3. The extension's popup shows a status dot — green means Collie can drive the tab.

!!! warning "Load the extension from the collie you actually run"
    If Chrome loads the extension from a *different* checkout than the collie you're running, every
    fix looks like it did nothing. The popup warns on a version mismatch — the bridge reports the
    version it expects, the extension reports the version Chrome loaded, and they must match.

### Security

The bridge is localhost-only and refuses any request missing its CSRF header; it checks `Origin`
and `Host`. Untrusted page content Collie reads is fenced as data (prompt-injection defense).

## Uninstalling

The installer's *Uninstall* entry (or *Add or remove programs*) stops the native hosts, removes their
logon autostarts, and deletes the app — no leftovers.
