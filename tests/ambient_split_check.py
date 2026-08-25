"""Real-browser check for the ground/panel split of the ambient desktop.

The desktop is two windows now. `?ground=1` draws the wallpaper behind the icons and takes no
input at all; `?panel=1` is an ordinary window on the desktop holding every control, which the
host clips to the rectangles the page reports. The properties that make that architecture work are
invisible in a screenshot, so they are asserted here:

  * neither half draws the other's furniture (otherwise widgets render twice, once uninteractive)
  * the panel reports regions, and they actually cover its widgets
  * the panel leaves most of the screen unclaimed — that is the desktop showing through
  * both halves paint the same background, because the panel is clipped rather than transparent
    and the seam has to be invisible

    python tests/browser_suite.py ambient_split_check
"""
import os
import sys
import tempfile

from PIL import Image

WEB = os.environ.get("COLLIE_WEB", "http://127.0.0.1:8795")
TOKEN = os.environ.get("COLLIE_TOKEN", "")
RESULTS = []

# The rectangles the page reports, computed exactly as reportRegions() does.
RECTS_JS = """() => {
    var pad = 80, out = [];
    document.querySelectorAll(".opsbar, .ops-panel, .slot > *, .dock, .reply, .editbtn, .edithint, .dz")
      .forEach(function (el){
        if (el.hasAttribute("hidden")) return;
        if (el.classList.contains("brand") || el.classList.contains("clock")) return;
        var c = getComputedStyle(el);
        if (c.display === "none" || c.visibility === "hidden" || parseFloat(c.opacity) < 0.02) return;
        var r = el.getBoundingClientRect();
        if (r.width < 2 || r.height < 2) return;
        out.push([Math.max(0, Math.round(r.left) - pad), Math.max(0, Math.round(r.top) - pad),
                  Math.round(r.width) + pad * 2, Math.round(r.height) + pad * 2]);
      });
    return out; }"""

HIT_RECTS_JS = """() => {
    var out = [], pad = 4;
    document.querySelectorAll("button, textarea, input, select, a[href], [contenteditable='true'], [role='button']")
      .forEach(function (el){
        if (el.hasAttribute('hidden') || el.disabled) return;
        var c = getComputedStyle(el);
        if (c.display === 'none' || c.visibility === 'hidden' || c.pointerEvents === 'none' ||
            parseFloat(c.opacity) < 0.02) return;
        var r = el.getBoundingClientRect();
        if (r.width < 2 || r.height < 2) return;
        out.push([Math.max(0, Math.round(r.left) - pad), Math.max(0, Math.round(r.top) - pad),
                  Math.round(r.width) + pad * 2, Math.round(r.height) + pad * 2]);
      });
    return out; }"""


def check(name, condition, detail=""):
    ok = bool(condition)
    RESULTS.append((name, ok))
    print(("  PASS " if ok else "  FAIL ") + name + ((" :: " + detail) if detail and not ok else ""))


def visible(page, selector):
    return page.evaluate(
        "s => { var e = document.querySelector(s); if (!e) return false;"
        " var c = getComputedStyle(e); return c.display !== 'none' && c.visibility !== 'hidden'; }",
        selector)


def union_area(rects):
    """Area the native OR-combined window region actually occupies.

    Summing rectangle areas double-counts the generous 80px interaction padding wherever the
    clock column, field and dock overlap. The Win32 region combines them, so measure that union.
    """
    xs = sorted({x for x, _, w, _ in rects} | {x + w for x, _, w, _ in rects})
    area = 0
    for left, right in zip(xs, xs[1:]):
        spans = sorted((y, y + h) for x, y, w, h in rects if x < right and x + w > left)
        if not spans:
            continue
        covered = 0
        start, end = spans[0]
        for y1, y2 in spans[1:]:
            if y1 <= end:
                end = max(end, y2)
            else:
                covered += end - start
                start, end = y1, y2
        area += (right - left) * (covered + end - start)
    return area


def main():
    from playwright.sync_api import sync_playwright

    W, H = 1440, 900
    with sync_playwright() as pw:
        browser = pw.chromium.launch()

        # ── the panel: every control, no full-screen ornament ──────────────────────────────────
        panel = browser.new_page(viewport={"width": W, "height": H})
        errs = []
        panel.on("pageerror", lambda e: errs.append(str(e)))
        panel.goto(WEB + "/ambient?panel=1", wait_until="load")
        panel.wait_for_selector(".today", timeout=10000)
        panel.wait_for_selector("html.ambient-ready", timeout=10000)
        check("panel is tagged as the panel",
              panel.evaluate("() => document.documentElement.classList.contains('role-panel')"))
        check("panel holds the controls",
              all(visible(panel, s) for s in (".opsbar", ".dock", "#slot-tr", ".today")))
        check("panel leaves the full-screen ornament to the ground",
              not visible(panel, ".galaxy") and not visible(panel, ".lyrics"))
        # ornament is the ground's job — but it must stay in the panel's LAYOUT, or the day (which
        # sits under the clock in the same column) moves when the clock is not drawn
        # the panel paints a SUPERSET: it must draw the ornament too, or a control's clip rectangle
        # that overlaps the clock punches a rectangular bite out of it
        check("panel paints the ornament as well as the controls",
              panel.evaluate("() => { var c = document.querySelector('.clock');"
                             " return !!c && getComputedStyle(c).visibility === 'visible'; }"))

        # the regions it would send the host, computed the same way the page computes them
        rects = panel.evaluate(RECTS_JS)
        check("the panel reports regions at all", len(rects) >= 3, str(len(rects)))

        # Painting needs shadow padding; clicking does not. The resting surface now contains only
        # deliberate controls — focus and peer anchors — so those and the composer belong to the
        # tight hit shape without turning the whole visual column into a click target.
        hit_rects = panel.evaluate(HIT_RECTS_JS)
        hit_contract = panel.evaluate("""(rects) => {
            function covered(el){
                var r = el.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
                return rects.some(function(q){ return q[0] <= x && x < q[0] + q[2] &&
                                                       q[1] <= y && y < q[1] + q[3]; });
            }
            return {focus: covered(document.querySelector('.context-focus')),
                    composer: covered(document.querySelector('#input'))};
        }""", hit_rects)
        check("the focus remains inside the panel hit shape", hit_contract["focus"], str(hit_contract))
        check("the composer remains inside the panel hit shape", hit_contract["composer"], str(hit_contract))

        # every widget must fall inside one of them, or the host clips it away and it vanishes
        covered = panel.evaluate("""(rects) => {
            var bad = [];
            document.querySelectorAll(".today, .dock, .opsbar").forEach(function (el){
                var r = el.getBoundingClientRect();
                var ok = rects.some(function (q){
                    return r.left >= q[0] && r.top >= q[1] &&
                           r.right <= q[0] + q[2] && r.bottom <= q[1] + q[3]; });
                if (!ok) bad.push(el.className);
            });
            return bad; }""", rects)
        check("every widget falls inside a reported region", not covered, str(covered))

        # Out-of-flow surfaces are the trap. `.reply` is position:absolute inside .dock, so
        # .dock's bounding box excludes it entirely: reporting only .dock sliced the reply in
        # half, and no amount of padding could fix a rectangle measuring the wrong box. Open a
        # tall one and assert it is covered.
        panel.evaluate("""() => {
            document.getElementById('replyBody').innerHTML =
              new Array(40).join('a long reply that grows the box<br>');
            document.getElementById('reply').className = 'reply show'; }""")
        panel.wait_for_timeout(400)
        covered_reply = panel.evaluate("""(rects) => {
            var r = document.getElementById('reply').getBoundingClientRect();
            if (r.height < 50) return 'reply did not grow';
            var ok = rects.some(function (q){
                return r.left >= q[0] && r.top >= q[1] &&
                       r.right <= q[0] + q[2] && r.bottom <= q[1] + q[3]; });
            return ok ? '' : ('reply ' + Math.round(r.top) + '..' + Math.round(r.bottom) + ' not covered');
        }""", panel.evaluate(RECTS_JS))
        check("an open reply is inside a region", covered_reply == "", covered_reply)
        panel.evaluate("""() => { document.getElementById('reply').className = 'reply';
            document.getElementById('replyBody').innerHTML = ''; }""")

        # and the region must NOT be most of the screen, or the desktop is covered and unusable
        claimed = union_area(rects)
        check("the panel claims a minority of the screen",
              claimed < W * H * 0.55, "%d%% of the viewport" % round(100 * claimed / (W * H)))

        panel_bg = panel.evaluate("() => getComputedStyle(document.body).backgroundImage.slice(0, 120)")

        # ── the ground: the wallpaper, and nothing you can press ───────────────────────────────
        ground = browser.new_page(viewport={"width": W, "height": H})
        ground.on("pageerror", lambda e: errs.append(str(e)))
        ground.goto(WEB + "/ambient?ground=1", wait_until="load")
        ground.wait_for_selector(".blooms", timeout=10000)
        ground.wait_for_selector("html.ambient-ready", timeout=10000)
        check("ground is tagged as the ground",
              ground.evaluate("() => document.documentElement.classList.contains('role-ground')"))
        check("ground carries no controls",
              not any(visible(ground, s) for s in (".opsbar", ".dock", ".editbtn")) and
              ground.evaluate("() => { var t = document.querySelector('.today');"
                              " return !t || getComputedStyle(t).visibility === 'hidden'; }"))
        check("ground draws the ornament", visible(ground, ".clock") and
              ground.evaluate("() => getComputedStyle(document.querySelector('.clock')).visibility") == "visible")

        # the two halves must lay out identically, or a widget lands on different pixels in each
        geom = []
        for pg in (panel, ground):
            geom.append(pg.evaluate("() => { var c = document.querySelector('.clock');"
                                    " var r = c.getBoundingClientRect();"
                                    " return [Math.round(r.left), Math.round(r.top), Math.round(r.width)]; }"))
        check("both halves lay out the same", geom[0] == geom[1], str(geom))
        check("ground still draws the wallpaper", visible(ground, ".blooms") and visible(ground, ".vignette"))

        # the panel is clipped, not transparent, so its ground must match the ground's exactly
        ground_bg = ground.evaluate("() => getComputedStyle(document.body).backgroundImage.slice(0, 120)")
        check("both halves paint the same background", panel_bg == ground_bg,
              "%r vs %r" % (panel_bg[:60], ground_bg[:60]))

        # ── solo: the way back, unchanged ──────────────────────────────────────────────────────
        solo = browser.new_page(viewport={"width": W, "height": H})
        solo.on("pageerror", lambda e: errs.append(str(e)))
        solo.goto(WEB + "/ambient", wait_until="load")
        solo.wait_for_selector(".today", timeout=10000)
        solo.wait_for_selector("html.ambient-ready", timeout=10000)
        check("solo still renders the whole desktop",
              solo.evaluate("() => document.documentElement.classList.contains('role-solo')") and
              all(visible(solo, s) for s in (".blooms", ".opsbar", ".dock", ".today")))

        # ── the seam ───────────────────────────────────────────────────────────────────────────
        # The panel is clipped, not transparent, so inside a clip rectangle its pixels must equal
        # the ground's — otherwise the rectangle shows as a band. Two things break that and both
        # have: ornament the panel skipped (a rectangular bite out of the clock, 200/255) and a
        # widget's own drop shadow cut mid-fade (a band round the composer, 13/255). Compare the
        # two renders along the boundary of the UNION of the rectangles; a point on one rectangle's
        # edge that falls inside a neighbour is interior, not seam.
        shots = {}
        for role, pg in (("panel", panel), ("ground", ground)):
            f = os.path.join(tempfile.gettempdir(), "collie_seam_%s.png" % role)
            pg.screenshot(path=f)
            shots[role] = Image.open(f).convert("RGB")

        def interior(px, py, skip):
            for i, q in enumerate(rects):
                if i != skip and q[0] <= px < q[0] + q[2] and q[1] <= py < q[1] + q[3]:
                    return True
            return False

        pan, gnd = shots["panel"], shots["ground"]
        worst, where = 0, ""
        for i, (x, y, w, h) in enumerate(rects):
            pts = [(x + t2, y + 1) for t2 in range(0, w, 3)] + [(x + t2, y + h - 2) for t2 in range(0, w, 3)]                 + [(x + 1, y + t2) for t2 in range(0, h, 3)] + [(x + w - 2, y + t2) for t2 in range(0, h, 3)]
            for px, py in pts:
                if not (0 <= px < W and 0 <= py < H) or interior(px, py, i):
                    continue
                d = max(abs(m - n) for m, n in zip(pan.getpixel((px, py)), gnd.getpixel((px, py))))
                if d > worst:
                    worst, where = d, "at (%d,%d) panel=%s ground=%s" % (px, py, pan.getpixel((px, py)),
                                                                        gnd.getpixel((px, py)))
        check("the clip edge is invisible", worst <= 3, "max channel delta %d %s" % (worst, where))

        mine = [e for e in errs if "ipapi.co" not in e and "ERR_FAILED" not in e]
        check("no page errors in any role", not mine, "; ".join(mine[:3]))
        browser.close()

    bad = [n for n, ok in RESULTS if not ok]
    print("\n%d/%d checks passed" % (len(RESULTS) - len(bad), len(RESULTS)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
