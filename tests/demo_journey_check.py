"""Browser rehearsal of the exact no-model interview path in docs/DEMO.md.

    python tests/browser_suite.py demo_journey_check
"""
import json
import os
import sys
import urllib.request


WEB = os.environ.get("COLLIE_WEB", "http://127.0.0.1:8795")
TOKEN = os.environ.get("COLLIE_TOKEN", "")
RESULTS = []


def check(name, condition, detail=""):
    ok = bool(condition)
    RESULTS.append((name, ok))
    print(("  PASS " if ok else "  FAIL ") + name + ((" :: " + detail) if detail and not ok else ""))


def api(path, body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(WEB + path + ("&" if "?" in path else "?") + "token=" + TOKEN,
                                 data=data, method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read())


def main():
    from playwright.sync_api import sync_playwright

    api("/api/state/demo", {"action": "reset"})
    seeded = api("/api/state/demo", {"action": "seed"})
    check("clean interview scenario seeded", seeded.get("ok"))
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("console", lambda message: errors.append(message.text)
                    if message.type == "error" else None)

            page.goto(WEB + "/?page=today&demo=1", wait_until="load")
            page.wait_for_selector(".ps-page[data-ps-view=today] .ps-goal", timeout=10000)
            check("demo opens directly on Today", page.inner_text("#pageTitle").strip() == "Today")
            check("demo mode suppresses both onboarding dialogs",
                  not page.is_visible("#nameOverlay") and not page.is_visible("#obOverlay"))
            check("visible story is clean", "[Test]" not in page.inner_text("body"))

            page.click("#navMemory")
            page.wait_for_selector(".ps-page[data-ps-view=memory] .ps-wf", timeout=10000)
            memory = page.inner_text("#psPage")
            check("hybrid memory story has decisions, people, projects and a learned workflow",
                  all(label in memory for label in ("Decisions", "People", "Projects", "Learned workflows",
                                                    "After research, write it up")), memory[:400])

            page.evaluate("document.getElementById('sideMore').open = true")
            page.wait_for_selector("#sideMorePopover", state="visible", timeout=3000)
            page.click("#navDevices")
            page.wait_for_selector(".ps-page[data-ps-view=devices] [data-ps-connect]", timeout=10000)
            page.click("[data-ps-connect]")
            page.wait_for_selector("[data-ps-restore]", timeout=10000)
            devices = page.inner_text("#psPage")
            check("one click adds the cloud runtime and continuity controls",
                  "Sauna Cloud" in devices and "Restore from Sauna" in devices, devices[:400])

            page.click("#navToday")
            page.wait_for_selector(".ps-page[data-ps-view=today]", timeout=10000)
            prompt = "Research the remaining competitors tonight and give me a report tomorrow morning."
            page.locator("#input").fill(prompt)
            page.wait_for_selector("#psRunOn:not([hidden])", timeout=10000)
            page.locator('input[name="psRunOn"][value="cloud"]').check()
            page.click("#send")
            page.wait_for_selector(".ps-cloud-card", timeout=10000)
            card = page.inner_text(".ps-cloud-card")
            check("scheduled request becomes an honest cloud handoff receipt",
                  "Scheduled on Sauna Cloud" in card and "prototype" in card and
                  "never reported as done" in card, card[:400])

            ambient = browser.new_page(viewport={"width": 1440, "height": 900})
            ambient.goto(WEB + "/ambient", wait_until="load")
            ambient.wait_for_selector('[data-field-kind="agent"]:not([hidden])', timeout=10000)
            check("ambient desktop gains a peer-level Collie work object",
                  "Working" in ambient.inner_text('[data-field-kind="agent"]'))
            ambient.click('[data-field-kind="agent"]')
            ambient.wait_for_selector('[data-field-pane="agent"]:not([hidden]) #wAgentRuns:not([hidden])',
                                      timeout=10000)
            agent = ambient.inner_text('[data-field-pane="agent"]')
            check("agent detail shows ownership, status, and honest unknown progress",
                  prompt[:-1] in agent and "scheduled" in agent.lower() and
                  "measurable progress has not been reported yet" in agent.lower(), agent[:500])
            check("rehearsal produced no JavaScript errors", not errors, "; ".join(errors[:3]))
            browser.close()
    finally:
        try:
            api("/api/sauna/disconnect", {})
            api("/api/state/demo", {"action": "reset"})
        except Exception:
            pass
    failed = [name for name, ok in RESULTS if not ok]
    print("\n%d/%d checks passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
