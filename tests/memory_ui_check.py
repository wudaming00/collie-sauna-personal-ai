"""Browser check for the compact, reviewable Settings Memory profile.

Run with: python tests/browser_suite.py memory_ui_check
"""

import json
import os
import sys
import time


WEB = os.environ.get("COLLIE_WEB", "http://127.0.0.1:8795")
RESULTS = []


def check(name, condition, detail=""):
    ok = bool(condition)
    RESULTS.append((name, ok))
    print(("  PASS " if ok else "  FAIL ") + name + ((" :: " + detail) if detail and not ok else ""))


def wait_for(page, predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        page.wait_for_timeout(25)
    return False


SNAPSHOT = {
    "project": r"C:\workspace\collie-uiux-rebuild",
    "collie_id": "device-test",
    "pending": 1,
    "profile": [
        {"id": 1, "kind": "preference", "status": "attested", "attribute": "response_style",
         "value": "concise", "project": "global", "device_id": ""},
        {"id": 2, "kind": "habit", "status": "verified", "attribute": "verification_depth",
         "value": "thorough", "project": r"C:\workspace\collie-uiux-rebuild",
         "device_id": "", "observations": 4},
    ],
    "claims": [
        {"id": 3, "kind": "habit", "status": "proposed", "attribute": "preferred_tone",
         "value": "formal", "text": "preferred_tone = formal", "project": "global",
         "device_id": "", "observations": 1},
    ],
}


def main():
    from playwright.sync_api import sync_playwright

    writes = []

    def memory_route(route):
        request = route.request
        if request.method == "GET":
            route.fulfill(status=200, content_type="application/json", body=json.dumps(SNAPSHOT))
            return
        body = request.post_data_json or {}
        writes.append((request.url.split("?", 1)[0].rsplit("/", 1)[-1], body))
        route.fulfill(status=200, content_type="application/json", body='{"ok":true,"claim":{}}')

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 390, "height": 760})
        context.add_init_script("""(() => {
          sessionStorage.setItem('collie-name-onboard-skip', '1');
          sessionStorage.setItem('collie-onboard-skip', '1');
        })()""")
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/api/memory*", memory_route)
        page.route("**/api/memory/**", memory_route)
        page.goto(WEB, wait_until="load")
        page.click("#utilityTrigger")
        page.click("#settingsBtn")
        page.wait_for_selector("#setOverlay.open")
        page.wait_for_selector('.set-nav[data-cat="memory"]')
        page.click('.set-nav[data-cat="memory"]')
        page.wait_for_selector('[data-memory-kind="proposal"]')

        panel_text = page.locator("#memoryInspector").inner_text()
        check("memory separates confirmed preferences", "Confirmed preferences" in panel_text and "response_style" in panel_text)
        check("memory separates verified habits", "Verified habits" in panel_text and "verification_depth" in panel_text)
        check("proposals remain visibly quarantined", "Pending proposals" in panel_text and
              "Proposals never steer Collie" in panel_text and "preferred_tone" in panel_text)

        page.once("dialog", lambda dialog: dialog.accept())
        page.click('[data-memory-kind="preference"] [data-memory-action="invalidate"]')
        wait_for(page, lambda: any(kind == "review" and body.get("action") == "invalidate" for kind, body in writes))
        check("Forget uses lifecycle invalidation", any(kind == "review" and body.get("id") == 1 and
              body.get("action") == "invalidate" for kind, body in writes), str(writes))

        page.click('[data-memory-kind="proposal"] [data-memory-action="attest"]')
        wait_for(page, lambda: any(kind == "review" and body.get("action") == "attest" for kind, body in writes))
        check("proposal confirmation is explicit", any(kind == "review" and body.get("id") == 3 and
              body.get("action") == "attest" for kind, body in writes), str(writes))

        page.fill("#memoryAttribute", "answer_format")
        page.fill("#memoryValue", "bullets")
        page.check("#memoryDeviceOnly")
        page.click(".memory-save")
        wait_for(page, lambda: any(kind == "preference" for kind, _ in writes))
        saved = next((body for kind, body in writes if kind == "preference"), {})
        check("explicit preference carries scope and device boundary",
              saved.get("attribute") == "answer_format" and saved.get("value") == "bullets" and
              saved.get("project") == SNAPSHOT["project"] and saved.get("device_only") is True, str(saved))

        inspector = page.locator("#memoryInspector")
        check("memory panel does not overflow 390px", inspector.evaluate("e => e.scrollWidth <= e.clientWidth"))
        targets = page.locator("#memoryInspector button:visible").evaluate_all(
            "els => els.map(e => ({w:e.getBoundingClientRect().width,h:e.getBoundingClientRect().height}))")
        check("memory actions remain 44px tall on mobile", all(row["h"] >= 44 for row in targets), str(targets))
        check("memory panel produces no page errors", not errors, "; ".join(errors))
        browser.close()

    failed = [name for name, ok in RESULTS if not ok]
    if failed:
        print("memory_ui_check: %d failed" % len(failed))
        return 1
    print("memory_ui_check: all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
