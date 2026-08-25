"""What the world is writing about the things this person is working on.

The desktop already knows the goals and projects, so a "news" strand here has no business being a
generic feed — it should be about the work in front of them. The trap is that deriving search terms
from their own words goes wrong in an embarrassing way: this person's projects are called *Collie*
and *Sauna*, which return border collies and heat therapy, while *Wordware* — the company they are
interviewing at on Monday — returns exactly the funding thread that matters. The signal is real and
the extraction is unreliable, which is the same shape as everything else in the personal layer.

So Collie NOMINATES and the person PICKS. Candidates come only from their own goals and projects,
never from a guess about what someone like them reads; the topic in use is always on screen; and
nothing is fetched until a topic has actually been chosen. One click to choose, one to move on.

Stories come from the Hacker News search API (no key, no account). Server-side because the browser
would be blocked by CORS, and cached because headlines do not change faster than the desktop
restarts.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request

__all__ = ["candidates", "stories", "TTL"]

TTL = 1800.0                    # half an hour; a wallpaper is not a news ticker
_CACHE: dict[str, tuple[float, list]] = {}

# Words that describe doing work rather than a subject worth reading about. A candidate list full
# of "prepare" and "build" is noise the person has to wade through before reaching the real ones.
_CHORE = {
    "prepare", "preparing", "prep", "build", "building", "fix", "fixing", "ship", "shipping",
    "write", "writing", "draft", "review", "reviewing", "plan", "planning", "rehearse", "test",
    "testing", "update", "updating", "finish", "add", "adding", "run", "running", "check",
    "demo", "notes", "note", "task", "tasks", "todo", "work", "working", "project", "goal",
}
# Deliberately NOT chores: words like "interview" or "migration" name a subject people write about,
# even though they show up in task titles. Dropping them would throw away real candidates.
_SKIP = {"the", "a", "an", "to", "of", "for", "and", "or", "in", "on", "with", "my", "me", "this",
         "that", "is", "it", "at", "by", "from", "into", "out", "new", "old", "app", "v1", "v2"}


def _terms(text: str) -> list[str]:
    """Candidate subjects in one title, most specific first.

    The whole phrase counts too: "Sauna by Wordware" is a better search than either half, and a
    multi-word term is far less likely to collide with an everyday noun than a single one.
    """
    text = (text or "").strip()
    if not text:
        return []
    out = []
    words = [w for w in re.split(r"[^\w一-鿿]+", text) if w]
    keep = [w for w in words if w.lower() not in _SKIP and w.lower() not in _CHORE and len(w) > 2]
    if len(keep) > 1:
        out.append(" ".join(keep))                      # the phrase, minus the chore words
    out.extend(keep)
    return out


def candidates(state, limit: int = 6) -> list[str]:
    """Subjects drawn from this person's own goals and projects. Never a guess about their taste."""
    seen, out = set(), []
    sources = []
    try:
        sources += [g.get("title", "") for g in state.goals()[:4]]
    except Exception:
        pass
    try:
        sources += [p.get("name", "") for p in state.projects()[:4]]
    except Exception:
        pass
    for text in sources:
        for t in _terms(text):
            key = t.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(t)
    # Proper nouns first. A name — Wordware, Postgres, Kubernetes — is almost always the subject
    # worth reading about, while a lower-case word lifted out of a task title ("interview") is
    # almost always too generic to search on. Without this the goal titles filled every slot and
    # pushed the one genuinely useful candidate off the end of the list.
    def rank(t):
        cap = t[:1].isupper()
        multi = " " in t
        return (0 if (cap and not multi) else (1 if multi else 2), -len(t), t.lower())
    out.sort(key=rank)
    return out[:limit]


def stories(topic: str, limit: int = 4) -> list[dict]:
    """Recent Hacker News stories about ``topic``. Empty when the lookup is unavailable."""
    topic = (topic or "").strip()
    if not topic:
        return []
    key = topic.lower()
    hit = _CACHE.get(key)
    now = time.time()
    if hit and (now - hit[0]) < TTL:
        return [dict(x) for x in hit[1]]
    out = []
    try:
        url = ("https://hn.algolia.com/api/v1/search?tags=story&hitsPerPage=%d&query=%s"
               % (max(1, limit * 3), urllib.parse.quote(topic)))
        req = urllib.request.Request(url, headers={"User-Agent": "collie-desktop/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        for h in data.get("hits") or []:
            title = (h.get("title") or "").strip()
            link = h.get("url") or ("https://news.ycombinator.com/item?id=%s" % h.get("objectID"))
            if not title or not str(link).startswith(("http://", "https://")):
                continue
            out.append({"title": title[:160], "url": link, "points": int(h.get("points") or 0),
                        "at": int(h.get("created_at_i") or 0), "source": "Hacker News"})
            if len(out) >= limit:
                break
    except Exception:
        out = []
    _CACHE[key] = (now, [dict(x) for x in out])
    return out
