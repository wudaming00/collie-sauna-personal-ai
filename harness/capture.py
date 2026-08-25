"""Capture: a dictated sentence in, diary line or calendar event out.

The phone/watch side of "Collie as the user layer": a Shortcut (or any
client) POSTs one sentence; this module decides whether it is a calendar
event or a diary line and lands it — markdown the user owns for diary, a
prefilled Google Calendar page for events (one human Save click, no
OAuth, no API quota). Every utterance is also appended to inbox.md
before routing, so capture never loses anything.

Deterministic zh/en parsing of spoken time expressions, on purpose: it
answers in milliseconds, offline, and its failure mode is honest (an
unparsed sentence is a diary line flagged for review, not a hallucinated
meeting). Rules that earned their place:

- An explicit future date or clock time makes an event; 今天/昨天 alone
  do not (people narrate their day far more often than they schedule it).
- Past narration (昨天/前天) wins over any date it happens to mention —
  "昨天8月13号面试" is a diary line, never a next-year event.
- Scheduling-flavoured words without a usable time set needs_review.

All datetimes are naive local time; the desktop is the timezone. This
module carries no personal endpoints: token, data dir, relay URL and
timezone come from settings (Capture group). A relay, when configured,
is any HTTPS mailbox honouring POST /q {text,token} + drain-on-GET /q.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import threading
import time as _time
import webbrowser
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import settings

DEFAULT_DURATION_MIN = 60
TOKEN_FILE = os.path.expanduser("~/.collie/capture-token")   # 0600; see load_config
MAX_BODY = 8 * 1024
WEEKDAY_CN = "一二三四五六日"

# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_num(s: str) -> int | None:
    """两 → 2, 十 → 10, 十一 → 11, 二十三 → 23."""
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if "十" in s:
        head, _, tail = s.partition("十")
        tens = _CN_DIGITS.get(head, 1) if head else 1
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        if (head and head not in _CN_DIGITS) or (tail and tail not in _CN_DIGITS):
            return None
        return tens * 10 + ones
    total = 0
    for ch in s:
        if ch not in _CN_DIGITS:
            return None
        total = total * 10 + _CN_DIGITS[ch]
    return total


_NUM = r"(?:\d{1,2}|[一二两三四五六七八九十]{1,3})"

_RELATIVE_DAYS = {"今天": 0, "今晚": 0, "明天": 1, "明早": 1, "明晚": 1,
                  "后天": 2, "大后天": 3, "昨天": -1, "前天": -2,
                  "today": 0, "tonight": 0, "tomorrow": 1, "yesterday": -1}

_WEEKDAYS_CN = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_WEEKDAYS_EN = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
                "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thurs": 3,
                "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}

_RE_RELATIVE = re.compile("|".join(sorted(_RELATIVE_DAYS, key=len, reverse=True)))
_RE_WEEK_CN = re.compile(r"(下+个?|这个?)?(?:周|星期|礼拜)([一二三四五六日天])")
_RE_DATE_CN = re.compile(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})[号日]?")
_RE_WEEK_EN = re.compile(r"\b(next\s+)?(" + "|".join(_WEEKDAYS_EN) + r")\b", re.I)
_RE_DATE_EN = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b", re.I)
_MONTHS_EN = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}

_PERIODS = {"凌晨": "am_early", "早上": "am", "早晨": "am", "上午": "am",
            "中午": "noon", "下午": "pm", "傍晚": "pm", "晚上": "pm", "夜里": "pm"}

_RE_CLOCK_CN = re.compile(
    r"(凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里)?"
    rf"({_NUM})点(半|一刻|三刻|({_NUM})分?)?")
_RE_CLOCK_HM = re.compile(
    r"(凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里)?(\d{1,2})[:：](\d{2})\s*(am|pm)?", re.I)
_RE_CLOCK_EN = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.I)

_INTENT_WORDS = re.compile(
    r"提醒我?|别忘了?|记得|约了?|会议|开会|面试|复诊|预约|截止|交房租|"
    r"\bremind\b|\bmeeting\b|\bappointment\b|\bcall\b|\bdeadline\b|\bdue\b", re.I)

_LEAD_VERBS = re.compile(
    r"^(?:帮我)?(?:记一下|记录一下|记下|加个日程|安排一下|安排|提醒我|记一笔|备忘|"
    r"note that|note|remind me to|remind me|schedule)\s*[,，:：]?\s*", re.I)

_TRAILING_PUNCT = re.compile(r"[,，。.、;；:：\s]+$")
_GLUE = re.compile(r"^[,，。.、;；:：的\s]+|(?<=[一-鿿])[,，、]\s*(?=[一-鿿])")

_RE_PAST = re.compile(r"昨天|前天|\byesterday\b", re.I)


@dataclass
class Capture:
    kind: str                    # "event" | "diary"
    raw: str
    title: str
    start: datetime | None = None
    end: datetime | None = None
    all_day: bool = False
    needs_review: bool = False
    matched: list[str] = field(default_factory=list)   # spans consumed by parsing


def _resolve_hour(hour: int, period: str | None, minute: int) -> time | None:
    if hour > 23 or minute > 59:
        return None
    if period == "pm" and hour < 12:
        hour += 12
    elif period == "noon" and hour < 6:
        hour += 12               # 中午一点 → 13:00
    return time(hour, minute)


def _parse_clock(text: str, spans: list[str]) -> time | None:
    m = _RE_CLOCK_HM.search(text)
    if m:
        period_raw, h, mi, ampm = m.groups()
        period = _PERIODS.get(period_raw) if period_raw else (ampm.lower() if ampm else None)
        t = _resolve_hour(int(h), period, int(mi))
        if t:
            spans.append(m.group(0))
            return t
    m = _RE_CLOCK_CN.search(text)
    if m:
        period_raw, h_raw, frac, frac_num = m.groups()
        hour = _cn_num(h_raw)
        if hour is not None:
            minute = {"半": 30, "一刻": 15, "三刻": 45}.get(frac or "", 0)
            if frac_num:
                minute = _cn_num(frac_num) or 0
            t = _resolve_hour(hour, _PERIODS.get(period_raw) if period_raw else None, minute)
            if t:
                spans.append(m.group(0))
                return t
    m = _RE_CLOCK_EN.search(text)
    if m:
        h, mi, ampm = m.groups()
        t = _resolve_hour(int(h), ampm.lower(), int(mi) if mi else 0)
        if t:
            spans.append(m.group(0))
            return t
    return None


def _parse_date(text: str, today: date, spans: list[str]) -> tuple[date | None, str]:
    m = _RE_DATE_CN.search(text)
    if m:
        y, mo, d = m.groups()
        try:
            found = date(int(y) if y else today.year, int(mo), int(d))
        except ValueError:
            found = None
        if found:
            if not y and found < today:
                found = date(today.year + 1, int(mo), int(d))
            spans.append(m.group(0))
            return found, "explicit"
    m = _RE_DATE_EN.search(text)
    if m:
        mo = _MONTHS_EN[m.group(1).lower()[:3]]
        try:
            found = date(today.year, mo, int(m.group(2)))
        except ValueError:
            found = None
        if found:
            if found < today:
                found = date(today.year + 1, mo, int(m.group(2)))
            spans.append(m.group(0))
            return found, "explicit"
    m = _RE_WEEK_CN.search(text)
    if m:
        prefix, day_ch = m.groups()
        target = _WEEKDAYS_CN[day_ch]
        if prefix and prefix.startswith("下"):
            weeks = prefix.count("下")
            monday_next = today + timedelta(days=(7 - today.weekday()) + 7 * (weeks - 1))
            found = monday_next + timedelta(days=target)
        else:
            found = today + timedelta(days=(target - today.weekday()) % 7)
        spans.append(m.group(0))
        return found, "weekday"
    m = _RE_WEEK_EN.search(text)
    if m and m.group(2).lower() in _WEEKDAYS_EN:
        target = _WEEKDAYS_EN[m.group(2).lower()]
        if m.group(1):
            found = today + timedelta(days=7 - today.weekday()) + timedelta(days=target)
        else:
            found = today + timedelta(days=(target - today.weekday()) % 7)
        spans.append(m.group(0))
        return found, "weekday"
    m = _RE_RELATIVE.search(text)
    if m:
        offset = _RELATIVE_DAYS[m.group(0)]
        spans.append(m.group(0))
        return today + timedelta(days=offset), "relative" if offset > 0 else "today_or_past"
    return None, ""


def _title_from(raw: str, spans: list[str]) -> str:
    t = raw
    for s in spans:
        t = t.replace(s, " ", 1)
    t = _LEAD_VERBS.sub("", t.strip())
    t = _GLUE.sub("", t)
    t = _TRAILING_PUNCT.sub("", t).strip()
    t = re.sub(r"\s{2,}", " ", t)
    return t or raw.strip()


def classify(raw: str, now: datetime | None = None) -> Capture:
    now = now or datetime.now()
    text = raw.strip()
    spans: list[str] = []

    if _RE_PAST.search(text):
        return Capture(kind="diary", raw=raw, title=text)

    day, day_src = _parse_date(text, now.date(), spans)
    clock = _parse_clock(text, spans)

    past = day is not None and day < now.date()
    today_no_clock = day_src == "today_or_past" and day == now.date() and clock is None

    if past or today_no_clock or (day is None and clock is None):
        needs = bool(_INTENT_WORDS.search(text)) and not past
        return Capture(kind="diary", raw=raw, title=text, needs_review=needs, matched=spans)

    if clock is not None:
        if day is None:
            day = now.date()
        start = datetime.combine(day, clock)
        if start <= now and day == now.date():
            bumped = start + timedelta(hours=12)          # "3点" said in the morning
            start = bumped if bumped > now else start + timedelta(days=1)
        end = start + timedelta(minutes=DEFAULT_DURATION_MIN)
        return Capture(kind="event", raw=raw, title=_title_from(raw, spans),
                       start=start, end=end, matched=spans)

    start = datetime.combine(day, time(0, 0))             # date only → all-day
    return Capture(kind="event", raw=raw, title=_title_from(raw, spans),
                   start=start, end=start + timedelta(days=1), all_day=True,
                   matched=spans)

# --------------------------------------------------------------------------
# landing
# --------------------------------------------------------------------------


@dataclass
class Config:
    token: str
    data_dir: Path
    port: int = 8823
    auto_open: bool = True
    relay_url: str = ""          # optional cloud mailbox for out-of-home captures
    tz: str = ""                 # IANA name for the gcal ctz param; empty = calendar default

    @property
    def diary_dir(self) -> Path:
        return self.data_dir / "diary"

    @property
    def inbox(self) -> Path:
        return self.data_dir / "inbox.md"


def load_config() -> Config:
    """Settings-backed config; mints and persists a token on first use."""
    # In its own file, not in settings.json. `settings.save` persists only keys in SCHEMA, and this
    # one deliberately has no panel entry — so `update()` accepted it, dropped it, and every call
    # minted a fresh token. The endpoint then rejected the token `capture setup` had just printed,
    # which is the whole LAN path (the Shortcut, the phone's outbox) unable to authenticate at all.
    # A credential also wants 0600, which settings.json does not promise.
    token = ""
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            token = f.read().strip()
    except OSError:
        token = ""
    if not token:
        token = settings.get("CAPTURE_TOKEN", "") or ""      # carry one forward if it ever landed
    if not token:
        token = secrets.token_urlsafe(24)
    try:
        os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(token + "\n")
        from . import plat
        plat.chmod_private(TOKEN_FILE)
    except Exception:
        pass      # a read-only home means a token that lasts this process, not a refusal to serve
    data_dir = settings.get("CAPTURE_DIR", "") or str(Path.home() / "Documents" / "CollieCapture")
    return Config(token=token, data_dir=Path(data_dir),
                  port=int(settings.get("CAPTURE_PORT", 8823) or 8823),
                  auto_open=str(settings.get("CAPTURE_OPEN", "on")) != "off",
                  relay_url=settings.get("CAPTURE_RELAY", "") or "",
                  tz=settings.get("CAPTURE_TZ", "") or "")


def _append(path: Path, line: str, header: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="\n") as f:
        if fresh and header:
            f.write(header + "\n\n")
        f.write(line + "\n")


def gcal_url(cap: Capture, tz: str = "") -> str:
    from urllib.parse import quote
    if cap.all_day:
        dates = f"{cap.start:%Y%m%d}/{cap.end:%Y%m%d}"
    else:
        dates = f"{cap.start:%Y%m%dT%H%M%S}/{cap.end:%Y%m%dT%H%M%S}"
    url = ("https://calendar.google.com/calendar/render?action=TEMPLATE"
           f"&text={quote(cap.title)}&dates={dates}"
           f"&details={quote('via collie capture: ' + cap.raw)}")
    if tz:
        url += f"&ctz={quote(tz, safe='')}"
    return url


def land(cap: Capture, cfg: Config, now: datetime | None = None,
         open_browser: bool | None = None) -> dict:
    """Write inbox + diary, open the calendar page for events."""
    now = now or datetime.now()
    day_file = cfg.diary_dir / f"{now:%Y}" / f"{now:%Y-%m-%d}.md"
    header = f"# {now:%Y-%m-%d} 周{WEEKDAY_CN[now.weekday()]}"

    result: dict = {"kind": cap.kind, "title": cap.title}
    if cap.kind == "event":
        when = f"{cap.start:%m-%d}" if cap.all_day else f"{cap.start:%m-%d %H:%M}"
        url = gcal_url(cap, cfg.tz)
        _append(day_file, f"- **{now:%H:%M}** {cap.raw}　→ 📅 {cap.title}({when})", header)
        _append(cfg.inbox, f"- {now:%Y-%m-%d %H:%M} [event] {cap.raw} → {cap.title} @ {when}")
        result.update(when=when, calendar_url=url)
        if cfg.auto_open if open_browser is None else open_browser:
            webbrowser.open(url)
            result["opened"] = True
    else:
        mark = " ⚠️待定" if cap.needs_review else ""
        _append(day_file, f"- **{now:%H:%M}** {cap.raw}{mark}", header)
        _append(cfg.inbox,
                f"- {now:%Y-%m-%d %H:%M} [{'review' if cap.needs_review else 'diary'}] {cap.raw}")
        result["needs_review"] = cap.needs_review

    result["diary_file"] = str(day_file)
    return result

# --------------------------------------------------------------------------
# service
# --------------------------------------------------------------------------


def _log(cfg: Config, msg: str) -> None:
    """Console + service.log — a pythonw service at logon has no console."""
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        with (cfg.data_dir / "service.log").open("a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
    except OSError:
        pass


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))       # no packets sent; picks the LAN-facing interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class Handler(BaseHTTPRequestHandler):
    cfg: Config = None  # injected by serve()

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/capture":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY:
            self._send(413, {"error": "body size"})
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"error": "bad json"})
            return

        auth = self.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip() or str(data.get("token", ""))
        if token != self.cfg.token:
            self._send(401, {"error": "bad token"})
            return

        text = str(data.get("text", "")).strip()
        if not text:
            self._send(400, {"error": "empty text"})
            return

        cap = classify(text)
        result = land(cap, self.cfg)
        _log(self.cfg, f"lan {cap.kind}: {text}")
        self._send(200, result)

    def log_message(self, *args) -> None:  # default access log is noise
        pass


def _poll_relay(cfg: Config) -> None:
    """Drain the cloud mailbox: captures made away from home land when we poll.

    Dedupes by queue key because mailbox list/delete may be eventually
    consistent."""
    import urllib.request
    seen: dict[str, None] = {}
    failures = 0
    while True:
        try:
            req = urllib.request.Request(
                cfg.relay_url.rstrip("/") + "/q",
                # Cloudflare 403s the default python-urllib UA before any
                # worker runs — the UA header is load-bearing.
                headers={"Authorization": f"Bearer {cfg.token}",
                         "User-Agent": "collie-capture/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                items = json.loads(r.read().decode("utf-8")).get("items", [])
            failures = 0
            for it in items:
                key = it.get("key", "")
                if key in seen:
                    continue
                seen[key] = None
                if len(seen) > 500:
                    seen.pop(next(iter(seen)))
                text = str(it.get("text", "")).strip()
                if not text:
                    continue
                cap = classify(text)
                land(cap, cfg)
                _log(cfg, f"relay {cap.kind}: {text}")
        except Exception as e:  # noqa: BLE001 — the poller must outlive bad networks
            failures += 1
            if failures in (1, 10):
                _log(cfg, f"relay poll error ({failures}x): {e}")
        _time.sleep(300 if failures >= 3 else 60)


def serve(cfg: Config) -> None:
    Handler.cfg = cfg
    server = ThreadingHTTPServer(("0.0.0.0", cfg.port), Handler)
    _log(cfg, f"capture listening on http://{lan_ip()}:{cfg.port}/capture"
              + (f" + relay {cfg.relay_url}" if cfg.relay_url else ""))
    if cfg.relay_url:
        threading.Thread(target=_poll_relay, args=(cfg,), daemon=True).start()
    server.serve_forever()
