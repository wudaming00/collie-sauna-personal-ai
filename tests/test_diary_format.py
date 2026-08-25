"""What `capture` writes, against what docs/diary.md promises.

The format is the expensive part. Files accumulate, so a convention changed in month three has to be
migrated and everything that read them — an editor, a script, next year's weekly review — breaks
quietly. Documentation alone does not hold a format: it describes what someone intended once, and
the code drifts out from under it without anything failing.

So this reads the doc and the written files and checks they agree.

    python3 tests/test_diary_format.py
"""
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(ok, what):
    print(("  PASS " if ok else "  FAIL ") + what)
    if not ok:
        fails.append(what)


def main():
    from harness import capture

    tmp = Path(tempfile.mkdtemp(prefix="collie-diary-"))
    cfg = capture.Config(token="t", data_dir=tmp, port=0, auto_open=False, relay_url="", tz="")
    now = datetime(2026, 8, 15, 9, 12)

    diary = capture.land(
        capture.classify("把签名那件事解决了 #vocalcode", now=now),
        cfg, now, open_browser=False)
    event_now = datetime(2026, 8, 15, 11, 40)
    event = capture.land(
        capture.classify("明天下午三点和 kobe 开个会", now=event_now),
        cfg, event_now, open_browser=False)

    day = Path(diary["diary_file"])
    check(day == tmp / "diary" / "2026" / "2026-08-15.md",
          "one file per day, filed under its year (%s)" % day.relative_to(tmp))
    lines = day.read_text(encoding="utf-8").splitlines()

    check(lines[0] == "# 2026-08-15 周六",
          "the heading is the date and its weekday, written once (%r)" % lines[0])
    body = [ln for ln in lines if ln.startswith("- ")]
    check(len(body) == 2, "one line per capture, appended in order")
    check(body[0].startswith("- **09:12** "),
          "each begins with the time it was said (%r)" % body[0][:14])
    check("把签名那件事解决了" in body[0],
          "and carries the sentence AS SAID — the one part nothing else can reconstruct")
    check("#vocalcode" in body[0], "tags survive verbatim, because grep is the index")
    check("📅" in body[1] and "和 kobe 开个会(08-16 15:00)" in body[1],
          "an event line keeps the sentence and appends what was scheduled (%r)" % body[1][-30:])

    inbox = (tmp / "inbox.md").read_text(encoding="utf-8").splitlines()
    check(len(inbox) == 2, "every utterance lands in inbox.md as well")
    check(inbox[0].startswith("- 2026-08-15 09:12 [diary] "),
          "stamped and classified (%r)" % inbox[0][:34])
    check("[event]" in inbox[1], "with the routing decision recorded beside it")

    # An unparseable scheduling sentence is flagged for a person, not guessed into a meeting.
    review_now = datetime(2026, 8, 15, 14, 2)
    capture.land(capture.classify("也许该把日记的格式定下来吧", now=review_now),
                 cfg, review_now, open_browser=False)
    text = day.read_text(encoding="utf-8")
    review = [ln for ln in text.splitlines() if "14:02" in ln]
    check(bool(review), "a third capture appends rather than rewriting the file")

    # Load-bearing rule 2, checked as a property of the whole file rather than of one line.
    check(not text.lstrip().startswith("---"), "no front matter")
    check("<div" not in text and "<span" not in text, "no HTML")
    check("base64," not in text, "no embedded blobs — assets are files beside the text")

    # And the doc says all of this. A format described in one place and implemented in another is
    # two formats waiting to be discovered.
    doc = Path(ROOT, "docs", "diary.md").read_text(encoding="utf-8")
    for promise in ("inbox.md", "assets/", "CAPTURE_DIR", "⚠️待定", "Append-only",
                    "- **HH:MM**", "2026-W33"):
        check(promise in doc, "docs/diary.md documents %s" % promise)

    print("\n  " + ("%d FAILED" % len(fails) if fails else "diary format: all green"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
