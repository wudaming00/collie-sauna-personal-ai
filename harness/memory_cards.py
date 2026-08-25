"""Deterministic Sauna-compatible views over Collie's typed Memory.

The seven Markdown cards are deliberately a projection, not another memory database.  A card is
never parsed back into a claim.  This preserves Collie's stronger trust model (status, evidence,
confidence, scope and provenance) while giving a cloud adapter the compact files Sauna expects.
"""
from __future__ import annotations

import html
import os
import re
import secrets
import time

from .memory import RECALLABLE_STATUSES
from .personal_core import MEMORY_CARDS

__all__ = ["MemoryCardProjector", "CARD_TITLES"]


CARD_TITLES = {
    "user_preferences": "User Preferences",
    "rules": "Rules",
    "user_profile": "User Profile",
    "your_tools": "Your Tools",
    "assistant_identity": "Assistant Identity",
    "user_relationships": "User Relationships",
    "recent_activity": "Recent Activity",
}

_TOOL_WORDS = re.compile(
    r"(?i)\b(tool|app|software|editor|ide|browser|shell|terminal|cli|operating system|os|"
    r"python|node|git|github|windows|macos|linux|vscode|cursor)\b"
)


def _line(value, limit: int = 600) -> str:
    """Make untrusted text a single safe Markdown list item."""
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    return html.escape(value[:limit], quote=False).replace("`", "'")


def _claim_card(claim: dict) -> str | None:
    """Classify a trusted claim without asking a model or inventing a new fact."""
    kind = str(claim.get("kind") or "fact").lower()
    subject = str(claim.get("subject") or "project").lower()
    searchable = " ".join(str(claim.get(k) or "") for k in ("attribute", "keys", "text"))
    if subject == "collie":
        return "assistant_identity"
    if kind in ("preference", "habit"):
        return "user_preferences"
    if subject == "device" or _TOOL_WORDS.search(searchable):
        return "your_tools"
    if kind in ("procedure", "decision"):
        return "rules"
    if subject == "owner" or kind == "identity":
        return "user_profile"
    if subject == "external":
        return "user_relationships"
    # Project observations and arbitrary legacy facts stay in typed Memory.  Forcing them into a
    # person card would change their meaning, so this compact projection is intentionally lossy.
    return None


class MemoryCardProjector:
    """Render seven compact files from Memory claims and Personal State.

    ``memory`` needs ``list_claims`` and ``state`` needs ``people`` / ``recent_activity``.  Keeping
    this adapter duck-typed makes the projection easy to test and usable during staged migrations.
    """

    def __init__(self, state, memory, *, output_dir: str | None = None):
        self.state = state
        self.memory = memory
        root = os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie")
        self.output_dir = output_dir or os.path.join(root, "state", "memory")

    def project(self, *, project: str = "global", claim_limit: int = 1000,
                activity_limit: int = 40) -> dict:
        """Return card text plus audit-friendly projection statistics; do not write files."""
        now = int(time.time())
        cards = {name: [] for name in MEMORY_CARDS}
        stats = {"considered": 0, "included_claims": 0, "untrusted": 0,
                 "inactive": 0, "unclassified": 0}
        claims = self.memory.list_claims(project=project, limit=claim_limit)
        admitted = []
        for claim in reversed(claims):
            stats["considered"] += 1
            if claim.get("status") not in RECALLABLE_STATUSES:
                stats["untrusted"] += 1
                continue
            if claim.get("superseded_by") is not None or (
                    claim.get("expires_at") is not None and int(claim["expires_at"]) <= now) or (
                    claim.get("valid_from") is not None and int(claim["valid_from"]) > now) or (
                    claim.get("valid_to") is not None and int(claim["valid_to"]) <= now):
                stats["inactive"] += 1
                continue
            admitted.append(claim)
        winners = {}
        for claim in admitted:
            key = str(claim.get("conflict_key") or "")
            if not key:
                continue
            stamp = int(claim.get("valid_from") or claim.get("observed_at") or
                        claim.get("created_at") or 0)
            prior = winners.get(key)
            prior_stamp = (int(prior.get("valid_from") or prior.get("observed_at") or
                               prior.get("created_at") or 0) if prior else -1)
            if prior is None or (stamp, int(claim["id"])) > (prior_stamp, int(prior["id"])):
                winners[key] = claim
        winner_ids = {int(claim["id"]) for claim in winners.values()}
        for claim in admitted:
            if claim.get("conflict_key") and int(claim["id"]) not in winner_ids:
                stats["inactive"] += 1
                continue
            card = _claim_card(claim)
            if not card:
                stats["unclassified"] += 1
                continue
            text = _line(claim.get("text"))
            if not text:
                continue
            confidence = float(claim.get("confidence") or 0.0)
            source = _line(claim.get("review_source") or claim.get("source") or "unknown", 80)
            cards[card].append(
                "- %s\n  - Trust: `%s` · confidence %.2f · source `%s` · claim `%s`" %
                (text, claim.get("status"), confidence, source, claim.get("id")))
            stats["included_claims"] += 1

        for person in self.state.people():
            name = _line(person.get("name"), 120)
            if not name:
                continue
            detail = " · ".join(x for x in (
                _line(person.get("role"), 120), _line(person.get("org"), 120)) if x)
            notes = _line(person.get("notes"), 260)
            item = "- %s%s" % (name, " — " + detail if detail else "")
            if notes:
                item += "\n  - %s" % notes
            item += "\n  - Source: Personal State person `%s`" % _line(person.get("id"), 100)
            cards["user_relationships"].append(item)

        for activity in reversed(self.state.recent_activity(limit=activity_limit)):
            summary = _line(activity.get("summary"), 400)
            if summary:
                cards["recent_activity"].append(
                    "- %s\n  - At: `%s` · actor `%s` · activity `%s`" %
                    (summary, int(activity.get("at") or 0),
                     _line(activity.get("actor"), 40), activity.get("id")))

        rendered = {}
        for name in MEMORY_CARDS:
            body = "\n".join(cards[name]) if cards[name] else "_No trusted entries yet._"
            text = (
                "# %s\n\n"
                "> Generated by Collie from typed local state. Read-only projection; edits here "
                "are not imported.\n\n%s\n" % (CARD_TITLES[name], body)
            )
            rendered[name] = {
                "name": name,
                "title": CARD_TITLES[name],
                "filename": name.replace("_", "-") + ".md",
                "count": len(cards[name]),
                "text": text,
            }
        return {"project": project, "cards": rendered, "stats": stats}

    def render(self, **kwargs) -> dict:
        """Atomically write all cards and return the same data with absolute paths."""
        result = self.project(**kwargs)
        os.makedirs(self.output_dir, exist_ok=True)
        try:
            os.chmod(self.output_dir, 0o700)
        except OSError:
            pass
        for card in result["cards"].values():
            path = os.path.abspath(os.path.join(self.output_dir, card["filename"]))
            temp = "%s.%d.%s.tmp" % (path, os.getpid(), secrets.token_hex(4))
            try:
                # Each request owns a same-directory temporary name.  ``os.replace`` is atomic;
                # a shared ``.tmp`` name would still race when two web handlers render together.
                with open(temp, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(card["text"])
                try:
                    os.chmod(temp, 0o600)
                except OSError:
                    pass
                os.replace(temp, path)
            finally:
                if os.path.exists(temp):
                    try:
                        os.remove(temp)
                    except OSError:
                        pass
            card["path"] = path
        return result
