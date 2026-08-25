"""Real-browser check for the WALLPAPER's personal layer (/ambient `today` widget).

The ambient desktop is a separate page from the app window with its own widget system, so none of
the personal-layer coverage in personal_ui_check.py touches it. This is that surface's own gate.

What the block is: a quiet today surface. One sentence says what is next; the person's real to-dos
rest as a summary beneath it, with at most one contextual preview. A deliberate press reveals the
disclosure rows and each opens in place for context and actions, while memory stays secondary.

The assertions here are the ones a screenshot cannot make: that it is not a card, that no vendor
name is used as a heading over the person's day, that the poll never drives their browser, that an
idle wallpaper never animates, that one cursor owns the surface, and that "do this" actually runs.

    python tests/browser_suite.py ambient_ui_check
"""
import json
import os
import sys
import time
import urllib.request

WEB = os.environ.get("COLLIE_WEB", "http://127.0.0.1:8795")
TOKEN = os.environ.get("COLLIE_TOKEN", "")
RESULTS = []
SHOTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_scratch_shots")

WEATHER_FIXTURE = {
    "ok": True,
    "city": "Testville",
    "region": "California",
    "country_code": "US",
    "timezone": "America/Los_Angeles",
    "observed_at": "2026-08-25T12:00",
    "temp_c": 21,
    "feels_c": 21,
    "humidity": 48,
    "precip_mm": 0,
    "wind_kph": 7,
    "wind_deg": 270,
    "is_day": 1,
    "code": 1,
    "hourly": [],
    "daily": [],
    "source": "Open-Meteo",
}


def check(name, condition, detail=""):
    ok = bool(condition)
    RESULTS.append((name, ok))
    print(("  PASS " if ok else "  FAIL ") + name + ((" :: " + detail) if detail and not ok else ""))


def api(path, body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(WEB + path + ("&" if "?" in path else "?") + "token=" + TOKEN, data=data,
                                 method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def shot(page, name):
    try:
        os.makedirs(SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(SHOTS, name + ".png"), full_page=False)
    except Exception:
        pass


def stub_weather(page):
    """Keep the UI gate independent of public geo/weather availability.

    desktop.weather has its own response-shaping tests.  This suite verifies how
    the browser renders that response, so reaching ipapi/Open-Meteo here only
    made a deterministic layout check depend on the runner's outbound network.
    """
    body = json.dumps(WEATHER_FIXTURE)
    page.route("**/api/desktop/weather*", lambda route: route.fulfill(
        status=200, content_type="application/json", body=body))


def main():
    from playwright.sync_api import sync_playwright

    # A manifest the person could have been handed. It points at a host that does not resolve on
    # purpose: this covers the plumbing — catalogue, switch, placement, rendering — without making
    # the suite depend on somebody else's API being up.
    wdir = os.path.join(os.environ.get("COLLIE_STATE_DIR", ""), "widgets")
    try:
        os.makedirs(wdir, exist_ok=True)
        with open(os.path.join(wdir, "testfeed.json"), "w", encoding="utf-8") as fh:
            json.dump({"id": "testfeed", "title": "Test Feed", "note": "A widget from a file.",
                       "url": "https://127.0.0.1:1/nothing", "rows": "items", "label": "name"}, fh)
    except Exception:
        pass

    seeded = api("/api/state/demo", {"action": "seed"})
    check("demo seeded over the API", seeded.get("ok"))

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        stub_weather(page)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append("console.error: " + m.text) if m.type == "error" else None)

        page.goto(WEB + "/ambient", wait_until="load")
        page.wait_for_selector(".today", timeout=10000)
        check("the wallpaper renders the today widget", page.locator(".today").count() == 1)
        check("registered as a movable widget",
              page.locator('.slot .widget[data-widget="today"]').count() == 1)

        # set INTO the wallpaper, not floated over it: the clock and the music row carry no card,
        # and neither may this
        check("no card chrome around the day", page.locator(".today.card").count() == 0)
        box = page.evaluate("() => { var s = getComputedStyle(document.querySelector('.today'));"
                            " return {bg: s.backgroundColor, border: s.borderTopWidth}; }")
        check("the day has no panel background or border",
              box["bg"] in ("rgba(0, 0, 0, 0)", "transparent") and box["border"] == "0px", str(box))

        # ── the sentence ───────────────────────────────────────────────────────────────────────
        page.wait_for_selector(".lead .h", timeout=10000)
        lead = page.inner_text(".lead")
        check("the sentence says something real", len(lead.strip()) > 3 and "Loading" not in lead, lead[:120])

        brief = api("/api/state/today")
        nxt = (brief.get("upcoming") or [{}])[0]
        want = nxt.get("title") or (brief.get("focus_task") or {}).get("title") or ""
        if want.lower().startswith("focus:"):
            want = want.split(":", 1)[1].strip()
        check("the sentence names the next thing", bool(want) and want[:30] in lead,
              "%r vs %r" % (want, lead[:160]))

        # it must be the ONLY large type on the block — that is what makes it a sentence rather
        # than another row. Everything at one size is a list again, which is what this replaced.
        # only elements that actually render text — a bare container inherits a size it never paints
        sizes = page.evaluate("() => Array.from(document.querySelectorAll('.today *')).filter("
                              "function(e){ var s=getComputedStyle(e); return s.display!=='none' &&"
                              " s.visibility!=='hidden' && Array.prototype.some.call(e.childNodes, function(n){"
                              " return n.nodeType === 3 && n.textContent.trim(); }); })"
                              ".map(function(e){ return parseFloat(getComputedStyle(e).fontSize); })"
                              ".sort(function(a,b){ return b-a; })")
        check("exactly one thing is set large", sizes[0] >= 16 and sizes[1] < sizes[0] - 3,
              "largest sizes " + str(sizes[:4]))

        # THE SCALE. Before it existed this block used ten font sizes, twelve weights and seventeen
        # spacings — sediment from a dozen rounds of tweaking, and the reason it never looked
        # settled. Assert nothing outside the scale ever renders, or the sediment comes straight
        # back the next time someone nudges one number.
        used = page.evaluate("() => { var out = {sizes: {}, weights: {}};"
                             " document.querySelectorAll('.today *, .cw *, .gallery *').forEach("
                             " function(e){ var has = Array.prototype.some.call(e.childNodes,"
                             "   function(n){ return n.nodeType === 3 && n.textContent.trim(); });"
                             "  if (!has) return; var s = getComputedStyle(e);"
                             "  if (s.display === 'none' || s.visibility === 'hidden') return;"
                             "  out.sizes[parseFloat(s.fontSize)] = 1;"
                             "  out.weights[s.fontWeight] = 1; });"
                             " return {sizes: Object.keys(out.sizes).map(Number).sort(function(a,b){return b-a;}),"
                             "         weights: Object.keys(out.weights).map(Number).sort(function(a,b){return b-a;})}; }")
        check("every size comes from the scale",
              set(used["sizes"]) <= {19.0, 13.0, 11.0, 9.0}, str(used["sizes"]))
        check("every weight comes from the scale",
              set(used["weights"]) <= {800, 620, 600, 560, 460}, str(used["weights"]))

        # ── the surface: one focus, then peer anchors; glass only after intent ────────────────
        nodes = page.locator("#wField [data-field-kind]")
        check("focus, tasks, later, memory and Collie share one field", nodes.count() == 5,
              str(nodes.count()))
        check("the field is settled by default",
              page.locator("#wFieldLens").is_hidden() and
              page.get_attribute("#wField", "data-selected") is None)
        check("the surface does not literalise relations as diagram lines",
              page.locator(".field-link").count() == 0)
        check("category counters are gone", page.locator(".context-signals").count() == 0)
        check("the resting surface has no glass card",
              page.evaluate("() => { var s=getComputedStyle(document.querySelector('.field-space'));"
                            " return s.borderTopWidth==='0px' && ['transparent','rgba(0, 0, 0, 0)'].includes(s.backgroundColor); }"))
        check("tasks, later, memory and Collie are peer anchors",
              page.locator(".ambient-rail .ambient-anchor").count() == 4)
        check("task rows live in on-demand detail", page.locator('[data-field-pane="task"] .day-list').count() == 1)

        # Focus is four facts and an action, not a dashboard canvas. Its glass should end just after
        # the action, and the action must have a complete default shape before hover supplies feedback.
        page.click("#wFocusNode"); page.wait_for_timeout(450)
        focus_geometry = page.evaluate("""() => {
            var lens=document.getElementById('wFieldLens'), button=document.querySelector('#wFocusActions .act');
            var pane=document.querySelector('[data-field-pane="focus"]'), details=document.getElementById('wFocusDetails');
            var lr=lens.getBoundingClientRect(), br=button.getBoundingClientRect(), s=getComputedStyle(button);
            return {height:Math.round(lr.height), spare:Math.round(lr.bottom-br.bottom),
                    pane:Math.round(pane.getBoundingClientRect().height), details:Math.round(details.getBoundingClientRect().height),
                    scroll:Math.round(lens.scrollHeight), position:getComputedStyle(lens).position,
                    background:s.backgroundColor, left:parseFloat(s.borderLeftWidth),
                    right:parseFloat(s.borderRightWidth)}; }""")
        check("focus detail follows its content instead of leaving a blank canvas",
              focus_geometry["height"] <= focus_geometry["scroll"] + 2 and
              0 <= focus_geometry["spare"] < 65, str(focus_geometry))
        check("the focus action is fully drawn before hover",
              focus_geometry["background"] not in ("transparent", "rgba(0, 0, 0, 0)") and
              focus_geometry["left"] > 0 and focus_geometry["right"] > 0, str(focus_geometry))
        page.click("#wFieldClose"); page.wait_for_timeout(250)

        # The clock already owns time. A second abstract timeline used space needed by the work.
        ticks = page.locator(".ribbon .tick")
        n_ticks = ticks.count()
        check("the abstract ribbon is not rendered", not page.locator("#wRibbon").is_visible())

        # the column has to read as ONE object: three widgets right-aligned independently gave
        # every one a different left edge, which is what made it feel unsettled
        edges = page.evaluate("() => ['.clock', '.music', '.today'].map(function(s){"
                              " var e = document.querySelector(s);"
                              " return e ? Math.round(e.getBoundingClientRect().left) : -1; })")
        check("the column shares one left edge", len(set(edges)) == 1 and edges[0] > 0, str(edges))
        rights = page.evaluate("() => ['.clock', '.music', '.today'].map(function(s){"
                               " var e = document.querySelector(s);"
                               " return e ? Math.round(e.getBoundingClientRect().right) : -1; })")
        check("the column shares one right edge", len(set(rights)) == 1, str(rights))

        # ── intelligence may emphasize an anchor, but never opens content at rest ──────────────
        list_mode = page.get_attribute(".day-list", "data-mode")
        check("the task detail never opens itself", page.locator('[data-field-pane="task"]').is_hidden(), str(list_mode))
        visible_rows = page.locator("#wDayRows .trow:visible").count()
        check("automatic relevance changes emphasis rather than geometry", visible_rows == 0, str(visible_rows))
        if list_mode == "peek":
            check("a relevant task anchor is emphasized and explains why",
                  "is-relevant" in (page.get_attribute("#wTaskToggle", "class") or "") and
                  len(page.get_attribute("#wTaskToggle", "title") or "") > 5)
        check("suggestions are not presented as the person's to-dos",
              page.locator("#wDayRows .trow.ask").count() == 0)

        # ── pressing do-this runs it ───────────────────────────────────────────────────────────
        started = []
        page.on("request", lambda r: started.append(r.url) if "/api/stream" in r.url else None)
        page.click("#wTaskToggle")
        page.wait_for_timeout(250)
        check("one deliberate press opens task detail in glass",
              page.get_attribute("#wField", "data-selected") == "task" and
              page.locator('[data-field-pane="task"]').is_visible() and
              page.locator("#wDayRows").is_visible() and page.locator("#wTaskControls").is_visible())
        # a row opens on demand rather than wearing every control at all times
        check("rows start closed", page.locator("#wDayRows .trow.open").count() == 0)
        page.locator("#wDayRows .trow .tt").first.click()
        page.wait_for_timeout(350)
        check("clicking a row opens its actions",
              page.locator("#wDayRows .trow.open").count() == 1 and
              page.locator("#wDayRows .trow.open .act").count() >= 1)
        row_buttons = page.locator("#wDayRows .trow .tt")
        row_button_count = row_buttons.count()
        if row_button_count >= 2:
            row_buttons.nth(1).click(); page.wait_for_timeout(200)
            check("opening another to-do closes the previous one",
                  page.locator("#wDayRows .trow.open").count() == 1 and
                  row_buttons.nth(0).get_attribute("aria-expanded") == "false")
            row_buttons.nth(0).click(); page.wait_for_timeout(200)
        shot(page, "ambient_todo_open")
        row_run = page.locator("#wDayRows .trow.open .act.primary")
        lead_run = page.locator(".lead .go")
        if row_run.count():
            row_run.first.click()
        elif lead_run.count() and lead_run.is_visible():
            lead_run.click()
        page.wait_for_timeout(1200)
        check("pressing do-this starts the work", bool(started), "no /api/stream request")
        check("it does not just stage text in the composer",
              page.input_value("#input").strip() == "", page.input_value("#input"))
        page.wait_for_timeout(2500)                     # let the mock run finish

        # ── Sauna is a sync state, not a dependency ────────────────────────────────────────────
        page.reload(wait_until="load")
        page.wait_for_selector(".lead .h", timeout=10000)
        check("nothing about Sauna until it is connected", page.locator("#wFoot").is_hidden())

        api("/api/sauna/connect", {"account": "check@example.com"})
        cloud = api("/api/sauna/handoff", {"text": "[Test] Trace the desktop execution state"})
        cloud_id = (cloud.get("cloud_task") or {}).get("id")
        if cloud_id:
            api("/api/sauna/cloud-task", {"id": cloud_id, "status": "running"})
        page.reload(wait_until="load")
        page.wait_for_selector(".lead .h", timeout=10000)
        settled = ("() => { var f = document.getElementById('wFoot'), s = document.getElementById('wSync');"
                   " return !!f && !f.hidden && !!s && /sauna/i.test(s.textContent); }")
        ok = True
        try:
            page.wait_for_function(settled, timeout=15000)
        except Exception:
            ok = False
        page.click('[data-field-kind="agent"]')
        page.wait_for_selector('[data-field-pane="agent"]:not([hidden])', timeout=3000)
        check("connecting Sauna makes its controls available inside Collie",
              ok and page.locator("#wFoot").is_visible(), page.inner_text(".today")[:200])
        check("a delegated task becomes a persistent desktop execution object",
              bool(cloud_id) and page.locator('#wAgentRuns .execution-card[data-source="cloud"][data-state="running"]').count() == 1,
              page.inner_text("#wAgentRuns")[:200])
        check("unknown progress is honest activity rather than a made-up percentage",
              page.locator('#wAgentRuns .execution-card[data-source="cloud"] .execution-progress.is-live').count() == 1)
        if cloud_id:
            api("/api/sauna/cloud-task", {"id": cloud_id, "status": "done", "result": "Desktop execution state verified."})
            page.reload(wait_until="load"); page.wait_for_selector(".lead .h", timeout=10000)
            page.click('[data-field-kind="agent"]')
            page.wait_for_selector('[data-field-pane="agent"]:not([hidden])', timeout=3000)
        check("completion remains visible with its result and no running animation",
              page.locator('#wAgentRuns .execution-card[data-source="cloud"][data-state="done"]').count() == 1 and
              "Desktop execution state verified." in page.inner_text(
                  '#wAgentRuns .execution-card[data-source="cloud"]') and
              page.locator('#wAgentRuns .execution-card[data-source="cloud"] .execution-progress.is-live').count() == 0)

        # structural, not textual: the person's own data legitimately says "Sauna interview" —
        # what must not exist is a vendor name used as a heading over their day
        heads = page.evaluate("() => Array.from(document.querySelectorAll("
                              "'.today .dayname, .today .rel, .today .quiet')).map(function(e){"
                              " return e.textContent.trim(); })")
        check("no label over the day names a vendor",
              all("sauna" not in h.lower() for h in heads), str(heads))

        check("composer starts pointed at this computer",
              "to-sauna" not in (page.get_attribute("#composer", "class") or ""))
        page.click("#wAsk")
        check("the Ask control repoints the composer",
              "to-sauna" in (page.get_attribute("#composer", "class") or "") and
              "on" in (page.get_attribute("#wAsk", "class") or ""))
        check("the destination pill becomes visible", page.locator("#dest").is_visible())
        page.click("#dest")
        check("the pill hands the composer back to Collie",
              "to-sauna" not in (page.get_attribute("#composer", "class") or "") and
              not page.locator("#dest").is_visible())

        # ── THE rule for a wallpaper: polling must not drive the person's browser ──────────────
        calls = []
        page.on("request", lambda r: calls.append(r.url) if "/api/sauna/inbox" in r.url else None)
        page.reload(wait_until="load")
        page.wait_for_selector(".lead .h", timeout=10000)
        time.sleep(2.0)
        check("the inbox poll is cache-only",
              bool(calls) and all("refresh=0" in u for u in calls), "; ".join(calls)[:200])

        # ── completing writes through to the same store the app window reads ───────────────────
        tick = page.locator("#wDayRows .trow .mark:not(.dot)")
        if tick.count():
            if page.get_attribute("#wField", "data-selected") != "task":
                page.click("#wTaskToggle")
                page.wait_for_timeout(250)
            before_n = len(api("/api/state/today").get("tasks", {}).get("done_today", []))
            after_n = before_n
            tick.first.click()
            for _ in range(20):
                after_n = len(api("/api/state/today").get("tasks", {}).get("done_today", []))
                if after_n > before_n:
                    break
                time.sleep(0.25)
            check("ticking a to-do really completes it", after_n > before_n, "%d -> %d" % (before_n, after_n))

            # a widget reporting what it just did must not dim the desktop the way a run does
            page.wait_for_timeout(500)
            check("using a control does not fade the widgets",
                  "chatting" not in (page.get_attribute("body", "class") or ""),
                  page.get_attribute("body", "class"))

        # ── related: Collie nominates, the person picks ────────────────────────────────────────
        # Nothing may be fetched or shown as "your news" until a subject has been chosen. The
        # candidates are words like "Collie" and "Sauna"; searched blind they return border collies
        # and heat therapy, so presenting a guess as a feed would be a confident lie.
        if page.locator("#wFieldLens").is_visible():
            page.click("#wFieldClose")
            page.wait_for_timeout(250)
        page.click('[data-field-kind="memory"]')
        page.wait_for_selector("#wRel:not([hidden])", timeout=15000)
        check("related offers subjects before assuming one",
              page.locator("#wRel .chip").count() >= 1 and page.locator("#wRel .story").count() == 0,
              page.inner_text("#wRel")[:120])
        picked = page.locator("#wRel .chip").first.inner_text()
        page.locator("#wRel .chip").first.click()
        page.wait_for_selector("#wRel .topic", timeout=20000)
        check("picking a subject shows it and keeps it visible",
              page.inner_text("#wRel .topic").strip() == picked.strip(),
              "%r vs %r" % (page.inner_text("#wRel .topic"), picked))
        check("the subject persists in the person's own state",
              api("/api/state/related").get("topic", "").strip() == picked.strip())
        page.wait_for_timeout(2500)
        check("stories are about the chosen subject",
              page.locator("#wRel .story").count() >= 1 or "Nothing new" in page.inner_text("#wRel"),
              page.inner_text("#wRel")[:160])


        # ── the gallery: the desktop is a set you choose from ──────────────────────────────────
        # There was no way to add or remove a widget before — the set was whatever the defaults
        # said. Windows and macOS both answer this with a gallery, and the pencil already owned
        # editing, so it opens there.
        page.click("#editbtn")
        page.wait_for_selector("#gallery:not([hidden])", timeout=10000)
        page.wait_for_selector("#gallery .nm", timeout=10000)
        names = page.evaluate("() => Array.from(document.querySelectorAll('#gallery .nm'))"
                              ".map(function(e){ return e.textContent.trim(); })")
        check("the gallery lists what can be placed", len(names) >= 6, str(names))
        check("an installed widget appears beside the built-in ones",
              any("Test Feed" in n for n in names), str(names))

        # turning one on places it; turning it off takes it away
        idx = [i for i, n in enumerate(names) if "Test Feed" in n][0]
        sw = page.locator("#gallery .sw").nth(idx)
        was_on = "on" in (sw.get_attribute("class") or "")
        if was_on:
            sw.click(); page.wait_for_timeout(600)
        sw = page.locator("#gallery .sw").nth(idx)
        check("an installed widget starts unplaced", "on" not in (sw.get_attribute("class") or ""))
        sw.click()
        page.wait_for_selector(".slot .cw", timeout=10000)
        check("turning it on places it on the desktop", page.locator(".slot .cw").count() >= 1)
        check("it draws through the shared row layout, never its own markup",
              page.evaluate("() => { var c = document.querySelector('.slot .cw');"
                            " return !!c && c.querySelector('script') === null; }"))
        page.locator("#gallery .sw").nth(idx).click()
        page.wait_for_timeout(700)
        check("turning it off takes it away", page.locator(".slot .cw").count() == 0)
        page.click("#editbtn")
        page.wait_for_timeout(300)
        check("the pencil closes the gallery again", page.locator("#gallery").is_hidden())

        if page.locator("#wFieldLens").is_visible():
            page.click("#wFieldClose")
            page.wait_for_timeout(350)
        shot(page, "ambient_today")
        try:
            page.locator(".today").screenshot(path=os.path.join(SHOTS, "today_block.png"))
        except Exception:
            pass
        page.click('[data-field-kind="memory"]')
        page.wait_for_timeout(350)
        shot(page, "ambient_field_memory")

        # ── the flicker class of bug ───────────────────────────────────────────────────────────
        # The host forwards raw mouse moves into this Chromium, so the surface repaints ~70x a
        # second. Anything re-rasterised on each of those shimmers, and anything that argues with
        # Explorer over the cursor strobes. Neither shows in a screenshot.
        page.reload(wait_until="load")
        page.wait_for_selector(".lead .h", timeout=10000)
        page.wait_for_timeout(1500)
        running = page.evaluate("() => document.getAnimations().filter(function(a){"
                                " return a.playState === 'running' &&"
                                " (!a.effect || a.effect.getTiming().iterations === Infinity); })"
                                ".map(function(a){ return (a.effect && a.effect.target &&"
                                " a.effect.target.className) || '?'; })")
        check("nothing on the idle wallpaper animates forever", not running,
              str(running) + " (targets: " + page.evaluate(
                  "() => document.getAnimations().map(function(a){ var t = a.effect && a.effect.target;"
                  " return t ? (t.tagName + '.' + (t.getAttribute('class') || '')) : '?'; }).join(', ')") + ")")

        # the gate above only bites if the thing that animates is on screen: the weather icon was
        # the source and it had never once loaded, so an empty slot was passing for a clean one
        wx = page.evaluate("() => { var e = document.getElementById('wWx');"
                           " return e ? e.textContent.trim() : 'missing'; }")
        check("the weather actually loaded", wx not in ("", "missing"), repr(wx))

        cursors = page.evaluate("() => ['#input', '.today', 'body'].map(function(s){"
                                " var e = document.querySelector(s);"
                                " return e ? getComputedStyle(e).cursor : 'missing'; })")
        check("one cursor for the whole surface", all(c == "default" for c in cursors), str(cursors))

        check("the music row is still there", page.locator(".music").count() == 1)
        # Music started by the global capsule lives in a server-owned ffplay process rather than
        # this page's <audio>. Its persisted live PID must expand the same desktop control.
        np_path = os.path.join(os.environ.get("COLLIE_STATE_DIR", ""), "nowplaying.json")
        with open(np_path, "w", encoding="utf-8") as fh:
            json.dump({"title": "轻音乐", "uploader": "SomaFM", "duration": None,
                       "clip_seconds": 30, "source": "somafm", "pid": os.getpid()}, fh)
        page.wait_for_selector(".music.external.active", timeout=5000)
        check("capsule playback expands the desktop music control",
              page.locator(".music.external.active").count() == 1 and
              "轻音乐" in page.locator("#wNp").inner_text() and
              page.locator(".music.external .mbtn.pp").is_visible())
        check("capsule playback keeps a stable three-button transport",
              page.locator(".music.external .mbtn").count() == 3 and
              all(page.locator(".music.external .mbtn").nth(i).is_visible() for i in range(3)))
        # A player recovered only from its PID has no analyser attached in this web process. The
        # truthful state is a resting centre line — never the old timer-driven sine animation.
        heights_before = page.locator("#wEq span").evaluate_all(
            "els => els.map(e => e.style.height)")
        page.wait_for_timeout(350)
        heights_after = page.locator("#wEq span").evaluate_all(
            "els => els.map(e => e.style.height)")
        check("missing audio samples never fall back to a fake waveform",
              heights_before == heights_after and set(heights_after) == {"6%"},
              repr([heights_before, heights_after]))
        os.remove(np_path)
        page.wait_for_function("() => !document.querySelector('.music').classList.contains('active')",
                               timeout=5000)
        check("the desktop music control collapses after playback ends",
              page.locator(".music.active").count() == 0)
        stack = page.evaluate("() => Array.from(document.querySelector('#slot-tr').children).map("
                              "function(e){ return e.className.split(' ')[0]; })")
        check("clock, then music, then the day",
              stack.index("clock") < stack.index("music") < stack.index("today"), str(stack))

        # The owner's primary locale in the reference surface is Chinese. Long translated labels
        # have to fit the same quiet geometry; checking only English misses the real compact view.
        zh = browser.new_page(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        stub_weather(zh)
        zh.goto(WEB + "/ambient", wait_until="load")
        zh.wait_for_selector(".lead .h", timeout=10000)
        labels = zh.evaluate("() => Array.from(document.querySelectorAll('.ambient-anchor .node-kind'))"
                             ".map(function(e){return e.textContent.trim();})")
        check("the peer anchors follow the person's Chinese setting",
              labels[:3] == ["待办", "稍后", "记忆"], str(labels))
        shot(zh, "ambient_field_zh")
        zh_rows = zh.locator("#wDayRows .trow .tt")
        if zh_rows.count():
            if zh.get_attribute("#wField", "data-selected") != "task":
                zh.click("#wTaskToggle"); zh.wait_for_timeout(200)
            zh_rows.first.click(); zh.wait_for_timeout(250)
            shot(zh, "ambient_todo_open_zh")
        if zh.locator("#wFieldLens").is_visible():
            zh.click("#wFieldClose"); zh.wait_for_timeout(200)
        zh.click('[data-field-kind="memory"]')
        zh.wait_for_timeout(350)
        shot(zh, "ambient_field_memory_zh")
        zh.close()

        # The actual compact desktop crop must stay composed: focus, horizon and anchors remain in
        # separate bands, and every anchor stays inside the field instead of colliding with time.
        compact = browser.new_page(viewport={"width": 874, "height": 902}, locale="zh-CN")
        stub_weather(compact)
        compact.goto(WEB + "/ambient", wait_until="load")
        compact.wait_for_selector(".lead .h", timeout=10000)
        rail_inside = compact.evaluate("() => { var f=document.querySelector('#wField').getBoundingClientRect();"
                                       " var r=document.querySelector('.ambient-rail').getBoundingClientRect();"
                                       " return r.left>=f.left && r.right<=f.right && r.top>=f.top && r.bottom<=f.bottom; }")
        check("the compact surface keeps the peer rail inside its field", rail_inside)
        if compact.locator("#wDayRows .trow").count() and compact.get_attribute("#wField", "data-selected") != "task":
            compact.click("#wTaskToggle"); compact.wait_for_timeout(200)
        geometry = compact.evaluate("() => { var f=document.querySelector('#wField').getBoundingClientRect();"
                                    " var a=Array.from(document.querySelectorAll('.day-list .trow')).map("
                                    " function(e){return e.getBoundingClientRect();});"
                                    " var list=document.querySelector('.day-list').getBoundingClientRect();"
                                    " return {inside:a.every(function(r){return r.left>=f.left && r.right<=f.right;}),"
                                    " lens:list.top>=f.top && list.bottom<=f.bottom,"
                                    " field:[f.left,f.right,f.top,f.bottom],"
                                    " list:[list.left,list.right,list.top,list.bottom]}; }")
        check("the compact surface keeps every to-do row inside its field", geometry["inside"], str(geometry))
        check("the compact task detail stays inside its field", geometry["lens"], str(geometry))
        shot(compact, "ambient_field_zh_compact")
        compact.close()

        # ipapi.co is the clock widget's geolocation lookup for weather; it predates this widget and
        # is simply unreachable from a sandboxed headless run. Everything else is a real failure.
        mine = [e for e in errors if "ipapi.co" not in e and "ERR_FAILED" not in e]
        check("no page errors", not mine, "; ".join(mine[:3]))
        browser.close()

    api("/api/sauna/disconnect", {})
    api("/api/state/demo", {"action": "reset"})
    bad = [n for n, ok in RESULTS if not ok]
    print("\n%d/%d checks passed" % (len(RESULTS) - len(bad), len(RESULTS)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
