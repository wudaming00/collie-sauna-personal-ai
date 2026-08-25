"""Real-browser check for the personal layer (Today · Tasks · Notes · Calendar · Journal · Memory · Devices,
capsule context chips, Sauna settings pane). Runs against the isolated mock server:

    python tests/browser_suite.py personal_ui_check

Screenshots land in _scratch_shots/ (gitignored scratch) for a human look; assertions are the gate.
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


def check(name, condition, detail=""):
    ok = bool(condition)
    RESULTS.append((name, ok))
    print(("  PASS " if ok else "  FAIL ") + name + ((" :: " + detail) if detail and not ok else ""))


def api(path, body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(WEB + path + ("&" if "?" in path else "?") + "token=" + TOKEN, data=data,
                                 method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def shot(page, name):
    try:
        os.makedirs(SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(SHOTS, name + ".png"), full_page=False)
    except Exception:
        pass


def open_nav(page, name):
    """Open a durable lens or a progressively disclosed secondary surface."""
    if name not in ("Today", "Memory", "Home"):
        page.evaluate("document.getElementById('sideMore').open = true")
        page.wait_for_selector("#sideMorePopover", state="visible", timeout=3000)
    page.click("#nav" + name)


def main():
    from playwright.sync_api import sync_playwright

    seeded = api("/api/state/demo", {"action": "seed"})
    check("demo seeded over the API", seeded.get("ok"))
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        # Match the narrow native-window width used in the interview rehearsal;
        # this is where the product rail becomes icon-only and More must still work.
        page = browser.new_page(viewport={"width": 1178, "height": 850})
        page.add_init_script("sessionStorage.setItem('collie-name-onboard-skip','1'); sessionStorage.setItem('collie-onboard-skip','1');")
        page.goto(WEB + "/", wait_until="load")
        page.wait_for_selector("#navToday", timeout=10000)
        check("rail keeps three core lenses and progressively discloses secondary tools",
              all(page.locator("#nav" + n).count() == 1 for n in ("Today", "Home", "Memory", "More")) and
              all(page.locator("#nav" + n).count() == 1 for n in ("Tasks", "Notes", "Calendar", "Journal", "Devices")))

        # Today
        open_nav(page, "Today")
        page.wait_for_selector(".ps-page[data-ps-view=today] .ps-goal", timeout=10000)
        text = page.inner_text("#psPage")
        check("Today shows the goal with progress and the workflow chip",
              "Prepare for Sauna interview" in text and "Interview preparation" in text and "%" in text, text[:300])
        low = text.lower()   # .ps-k labels are uppercased by CSS and innerText reflects that
        check("Today leads with the upcoming interview, goal progress and next actions",
              "sauna interview" in low and "prepare for sauna interview" in low and "build collie prototype" in low, text[:300])
        check("Today keeps context available without expanding it by default", "Context" in text and "Sauna" in text and
              not page.locator(".ps-context-section").get_attribute("open"))
        check("Today keeps the composer available", page.is_visible("#composer"))
        check("page title follows the destination", page.inner_text("#pageTitle").strip() == "Today")
        shot(page, "today")
        # mark a task done from Today -> suggestion appears
        first_open = page.locator(".ps-task:not(.ps-done) .ps-check").first
        first_open.click()
        page.wait_for_timeout(900)
        page.wait_for_selector(".ps-page[data-ps-view=today] .ps-goal", timeout=10000)
        text2 = page.inner_text("#psPage")
        check("completing a step advances the plan without adding another dashboard card",
              "57%" in text2 and "Prepare system design examples" in text2, text2[:400])
        shot(page, "today-after-done")

        # Tasks / Notes / Calendar / Journal / Memory / Devices
        for nav, marker in (("Tasks", "Tasks"), ("Notes", "Notes"), ("Calendar", "Calendar"),
                            ("Journal", "Journal"), ("Memory", "Memory"), ("Devices", "Devices")):
            open_nav(page, nav)
            page.wait_for_selector(".ps-page[data-ps-view=%s] .product-page-head" % nav.lower(), timeout=10000)
            page.wait_for_timeout(300)
            body = page.inner_text("#psPage")
            check(nav + " page renders its header beside the universal composer", marker in body and page.is_visible("#composer"), body[:200])
            shot(page, nav.lower())
        # Notes has the seeded thesis
        open_nav(page, "Notes")
        page.wait_for_selector(".ps-note", timeout=10000)
        check("Notes lists the seeded notes with relations", "Sauna product thesis" in page.inner_text("#psPage") and "Project" in page.inner_text("#psPage"))
        # Personal state is correctable from the person-facing surface, not merely inspectable.
        page.fill("#psNoteText", "first draft")
        page.fill("#psNoteTitle", "QA correction note")
        page.click("#psNoteSave")
        page.wait_for_selector(".ps-note", timeout=5000)
        page.wait_for_timeout(250)
        note = page.locator(".ps-note").filter(has_text="QA correction note").first
        note.locator(".ps-note-head").click()
        note.locator("[data-ps-note-edit]").click()
        page.fill("#psNoteText", "corrected text")
        page.fill("#psNoteTitle", "Corrected QA note")
        page.click("#psNoteSave")
        page.wait_for_timeout(350)
        check("a note can be corrected in place", "Corrected QA note" in page.inner_text("#psPage") and
              "corrected text" in page.inner_text("#psPage"))
        corrected = page.locator(".ps-note").filter(has_text="Corrected QA note").first
        corrected.locator(".ps-note-head").click()
        page.once("dialog", lambda dialog: dialog.accept())
        corrected.locator("[data-ps-note-delete]").click()
        page.wait_for_timeout(350)
        check("a note can be deleted without leaving a hidden card", "Corrected QA note" not in page.inner_text("#psPage"))

        open_nav(page, "Tasks")
        page.wait_for_selector(".ps-task", timeout=5000)
        before_done = page.locator(".ps-task.ps-done").count()
        reopen = page.locator(".ps-task.ps-done [data-ps-reopen]").first
        check("completed tasks expose an undo path", reopen.count() == 1)
        reopen.click(); page.wait_for_timeout(350)
        check("reopening restores the task to active state", page.locator(".ps-task.ps-done").count() == before_done - 1)

        open_nav(page, "Calendar")
        page.wait_for_selector("#psEventForm", timeout=5000)
        when = time.strftime("%Y-%m-%dT%H:%M", time.localtime(time.time() + 2 * 86400))
        page.fill("#psEventTitle", "QA calendar draft")
        page.fill("#psEventWhen", when)
        page.click("#psEventSave")
        page.wait_for_timeout(350)
        event = page.locator(".ps-event").filter(has_text="QA calendar draft").first
        event.locator("[data-ps-event-edit]").click()
        page.fill("#psEventTitle", "Corrected QA event")
        page.click("#psEventSave")
        page.wait_for_timeout(350)
        check("a calendar event can be corrected", "Corrected QA event" in page.inner_text("#psPage"))
        corrected_event = page.locator(".ps-event").filter(has_text="Corrected QA event").first
        page.once("dialog", lambda dialog: dialog.accept())
        corrected_event.locator("[data-ps-event-delete]").click()
        page.wait_for_timeout(350)
        check("a calendar event can be deleted", "Corrected QA event" not in page.inner_text("#psPage"))
        # Journal has yesterday + today
        open_nav(page, "Journal")
        page.wait_for_selector(".ps-journal", timeout=10000)
        check("Journal shows compressed days", page.locator(".ps-journal").count() >= 1 and "what happened" in page.inner_text("#psPage").lower())
        # Devices shows this computer; connect Sauna through the API and re-render
        api("/api/sauna/connect", {"account": "demo@sauna.ai"})
        open_nav(page, "Devices")
        page.wait_for_selector(".ps-device", timeout=10000)
        page.wait_for_timeout(300)
        dev = page.inner_text("#psPage")
        check("Devices lists this computer and Sauna Cloud after connecting", "this computer" in dev and "Sauna Cloud" in dev, dev[:300])
        shot(page, "devices-connected")
        # Today context panel now shows Sauna adds
        open_nav(page, "Today")
        page.wait_for_selector(".ps-context-section", timeout=10000)
        page.wait_for_timeout(300)
        page.locator(".ps-context-section summary").click()
        ctx = page.inner_text(".ps-context-section")
        check("Context panel shows what Sauna adds once connected", "Active goal" in ctx and "not connected" not in ctx, ctx[:300])
        shot(page, "today-sauna")

        # Capsule: context chips
        page.keyboard.press("Control+Shift+Space")
        page.wait_for_selector("#capsuleLayer", state="visible", timeout=5000)
        page.wait_for_timeout(1500)
        chips = page.inner_text("#capsuleContextChips") if page.locator("#capsuleContextChips").count() else ""
        check("capsule shows context chips (task / Sauna)", ("Task" in chips or "Sauna" in chips), chips)
        shot(page, "capsule")
        page.keyboard.press("Escape")

        # Run-on picker appears for overnight phrasing when Sauna is connected
        page.fill("#input", "Research the remaining competitors tonight and give me a report tomorrow morning")
        page.wait_for_timeout(900)
        check("run-on picker offers Sauna Cloud for overnight work", page.is_visible("#psRunOn"))
        shot(page, "runon")
        page.check('input[name="psRunOn"][value="cloud"]')
        page.click("#send")
        page.wait_for_selector(".ps-cloud-card", timeout=8000)
        check("cloud handoff renders a scheduled card, not a fake result", "Scheduled on Sauna Cloud" in page.inner_text(".ps-cloud-card"))
        shot(page, "cloud-card")

        # Settings → Sauna pane (the settings button lives inside the utility <details> menu; open it the
        # way the menu does, then pick the Sauna category)
        page.evaluate("document.querySelector('.utility-menu') && (document.querySelector('.utility-menu').open = true)")
        page.wait_for_timeout(200)
        if page.locator("#settingsBtn").count() and page.is_visible("#settingsBtn"):
            page.click("#settingsBtn")
        else:
            page.evaluate("typeof openSettings === 'function' && openSettings('sauna')")
        page.wait_for_timeout(500)
        if page.locator('.set-nav[data-cat="sauna"]').count():
            page.click('.set-nav[data-cat="sauna"]')
            page.wait_for_selector("#saunaPane .ps-sync-row", timeout=8000)
            pane = page.inner_text("#saunaPane")
            check("Sauna settings pane shows connection and granular sync", "SYNC WITH SAUNA" in pane and "Journal" in pane and "Screen history" in pane, pane[:300])
            shot(page, "settings-sauna")
        else:
            check("Sauna settings pane reachable", False, "settings modal not found in this build")
        browser.close()
    api("/api/state/demo", {"action": "reset"})
    failed = [n for n, ok in RESULTS if not ok]
    print("\n%d checks, %d failed" % (len(RESULTS), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
