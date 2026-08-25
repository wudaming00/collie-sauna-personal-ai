"""Desktop widgets you can choose, and add to.

The three systems worth copying from disagree, and the disagreement is instructive. Übersicht makes
a widget a ``.jsx`` file exporting ``command`` and ``render`` — arbitrary JavaScript plus arbitrary
shell, maximum power and no safety story at all. Rainmeter makes a skin a plain-text ``.ini``, with
DLLs and Lua as an escape hatch nobody needs for a clock. Windows 11 widgets are Adaptive Cards:
JSON with a fixed component set — "like HTML, but constrained" — where the provider returns a
template and its data separately.

The two built for ordinary people are both declarative. That settles it here, because Collie's
wallpaper is not a neutral canvas: it sits above the desktop, it holds the loopback API token, and
it can start runs on this machine. A widget that is arbitrary JavaScript is a code-execution vector
the moment one is shared — and widgets are exactly the kind of thing people share. So:

    a widget is a JSON manifest, its data is fetched by the SERVER, and it draws through a fixed
    set of layouts the page already knows how to render.

No eval, no injected HTML, no third-party script on the page. The cost is that a widget cannot do
something the layouts do not cover; the benefit is that installing one cannot cost you the machine.
Anything genuinely bespoke belongs in the harness as a real module, where it goes through review.

Manifests live in ``~/.collie/widgets/*.json``. Two shapes, both ending in labelled rows:

    one call, an array in the response          one call PER item (stocks, where each is a request)
    ------------------------------------        --------------------------------------------------
    {"id": "hn", "title": "Top of HN",          {"id": "stocks", "title": "Stocks",
     "url": "https://…",                         "each": ["AAPL", "NVDA"],
     "rows": "hits",                             "url": "https://…/chart/{item}",
     "label": "title",                           "label": "{item}",
     "value": "points"}                          "value": "chart.result.0.meta.regularMarketPrice",
                                                 "against": "chart.result.0.meta.chartPreviousClose"}
"""
from __future__ import annotations

import glob
import json
import os
import re
import time
import urllib.parse
import urllib.request

from .personal_state import state_dir

__all__ = ["BUILTIN", "catalog", "custom", "read", "data", "widget_dir"]

TTL_MIN, TTL_MAX = 30.0, 6 * 3600.0
_CACHE: dict[str, tuple[float, dict]] = {}
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# The widgets the page draws itself. They live in the same catalogue as the installed ones so the
# picker has one concept to show rather than "yours" and "ours".
BUILTIN = [
    {"id": "today", "title": "Today", "note": "What is next, the days ahead, and what is waiting."},
    {"id": "clock", "title": "Clock", "note": "Time, date and the weather where you are."},
    {"id": "music", "title": "Music", "note": "What is playing, and the controls for it."},
    {"id": "launcher", "title": "Launcher", "note": "The apps you open most."},
    {"id": "projects", "title": "Projects", "note": "Repositories on this machine, one click to open."},
    {"id": "system", "title": "System", "note": "CPU and memory right now."},
    {"id": "brand", "title": "Collie mark", "note": "The dog, in the middle of the desktop."},
]
BUILTIN_IDS = {w["id"] for w in BUILTIN}


def widget_dir() -> str:
    return os.path.join(state_dir(), "widgets")


# ------------------------------------------------------------------ manifests
def _clean(raw: dict, path: str) -> dict | None:
    """A manifest, or None. Everything unrecognised is dropped rather than trusted."""
    try:
        wid = str(raw.get("id") or os.path.splitext(os.path.basename(path))[0]).strip().lower()
        if not _ID.match(wid) or wid in BUILTIN_IDS:
            return None
        url = str(raw.get("url") or "").strip()
        if not url.startswith("https://"):
            return None                      # https only: this runs on the person's machine
        each = raw.get("each")
        each = [str(x)[:24] for x in each][:8] if isinstance(each, list) else []
        out = {
            "id": wid,
            "title": str(raw.get("title") or wid)[:32],
            "note": str(raw.get("note") or "")[:120],
            "kind": "custom",
            "url": url[:400],
            "each": each,
            "rows": str(raw.get("rows") or "")[:80],
            "label": str(raw.get("label") or "")[:80],
            "value": str(raw.get("value") or "")[:80],
            "against": str(raw.get("against") or "")[:80],
            "suffix": str(raw.get("suffix") or "")[:8],
            "refresh": max(TTL_MIN, min(TTL_MAX, float(raw.get("refresh") or 300))),
            "path": path,
        }
        if not out["each"] and not out["rows"]:
            return None                      # one shape or the other; neither means nothing to draw
        return out
    except Exception:
        return None


def custom() -> list[dict]:
    """Manifests installed under ~/.collie/widgets, newest name order, bad ones simply absent."""
    out = []
    for path in sorted(glob.glob(os.path.join(widget_dir(), "*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception:
            continue
        got = _clean(raw, path) if isinstance(raw, dict) else None
        if got:
            out.append(got)
    return out


def read(widget_id: str) -> dict | None:
    for w in custom():
        if w["id"] == widget_id:
            return w
    return None


def catalog(config: dict | None = None) -> list[dict]:
    """Every widget that can be placed, with whether it is placed and where."""
    placed = ((config or {}).get("widgets") or {})
    out = []
    for w in BUILTIN:
        cfg = placed.get(w["id"]) or {}
        out.append(dict(w, kind="builtin", on=bool(cfg.get("on")), slot=cfg.get("slot") or "tr"))
    for w in custom():
        cfg = placed.get(w["id"]) or {}
        out.append({"id": w["id"], "title": w["title"], "note": w["note"], "kind": "custom",
                    "on": bool(cfg.get("on")), "slot": cfg.get("slot") or "tr"})
    return out


# ------------------------------------------------------------------ data
def _dig(obj, path: str):
    """`a.b.0.c` through dicts and lists. Returns None rather than raising on a wrong turn."""
    cur = obj
    for part in (path or "").split("."):
        if part == "":
            continue
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "collie-desktop/1.0"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _fmt(v) -> str:
    if isinstance(v, float):
        return "%.2f" % v if abs(v) < 1000 else format(int(round(v)), ",d")
    return str(v)


def data(widget_id: str) -> dict:
    """Fetch and shape one widget's rows. Server-side: the browser would be blocked by CORS, and a
    fetch that happens here can be timed out, cached and kept off the page's origin."""
    w = read(widget_id)
    if not w:
        return {"ok": False, "error": "unknown widget"}
    now = time.time()
    hit = _CACHE.get(widget_id)
    if hit and (now - hit[0]) < w["refresh"]:
        return dict(hit[1])
    rows = []
    try:
        if w["each"]:
            for item in w["each"]:
                doc = _get(w["url"].replace("{item}", urllib.parse.quote(item)))
                val = _dig(doc, w["value"])
                if val is None:
                    continue
                row = {"label": (w["label"] or "{item}").replace("{item}", item),
                       "value": _fmt(val) + w["suffix"]}
                base = _dig(doc, w["against"]) if w["against"] else None
                if isinstance(val, (int, float)) and isinstance(base, (int, float)) and base:
                    row["delta"] = round((val - base) / base * 100, 2)
                rows.append(row)
        else:
            doc = _get(w["url"])
            arr = _dig(doc, w["rows"])
            for it in (arr or [])[:6]:
                label = _dig(it, w["label"]) if w["label"] else None
                if label is None:
                    continue
                row = {"label": str(label)[:80]}
                if w["value"]:
                    val = _dig(it, w["value"])
                    if val is not None:
                        row["value"] = _fmt(val) + w["suffix"]
                rows.append(row)
        out = {"ok": True, "id": widget_id, "title": w["title"], "rows": rows[:6], "at": int(now)}
    except Exception as exc:
        out = {"ok": False, "id": widget_id, "title": w["title"], "rows": [],
               "error": str(exc)[:120], "at": int(now)}
    _CACHE[widget_id] = (now, dict(out))
    return out
