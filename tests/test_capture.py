"""harness/capture.py: the parser's contract pinned to a fixed clock
(Friday 2026-08-14 10:00), landing side effects in a tmp dir, and one
real loopback round trip the way a phone Shortcut calls it."""

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = str(Path(__file__).resolve().parent.parent)

from harness import capture
from harness.capture import Capture, Config, Handler, classify, gcal_url, land

NOW = datetime(2026, 8, 14, 10, 0)  # Friday


def c(text):
    return classify(text, now=NOW)


# --- events with a clock ---------------------------------------------------

def test_tomorrow_afternoon():
    r = c("明天下午3点和房东通话")
    assert r.kind == "event"
    assert r.start == datetime(2026, 8, 15, 15, 0)
    assert r.end == datetime(2026, 8, 15, 16, 0)
    assert r.title == "和房东通话"


def test_weekday_cn_half_hour():
    r = c("周三上午十点半开会")
    assert r.kind == "event"
    assert r.start == datetime(2026, 8, 19, 10, 30)   # next Wednesday
    assert "开会" in r.title


def test_next_week_monday():
    r = c("下周一早上9点面试")
    assert r.start == datetime(2026, 8, 17, 9, 0)     # Mon of next calendar week


def test_tonight_cn_numeral():
    r = c("今天晚上八点半健身")
    assert r.start == datetime(2026, 8, 14, 20, 30)
    assert r.title == "健身"


def test_noon_one_oclock():
    r = c("中午一点吃饭")
    assert r.start == datetime(2026, 8, 14, 13, 0)


def test_bare_clock_future_today():
    r = c("下午3点开会")
    assert r.start == datetime(2026, 8, 14, 15, 0)


def test_bare_clock_past_bumps():
    r = c("9点开会")                                   # said at 10:00 → 21:00 today
    assert r.start == datetime(2026, 8, 14, 21, 0)


def test_en_pm_tomorrow():
    r = c("3:30pm meet with landlord tomorrow")
    assert r.kind == "event"
    assert r.start == datetime(2026, 8, 15, 15, 30)


def test_hm_colon():
    r = c("明天14:30复诊")
    assert r.start == datetime(2026, 8, 15, 14, 30)


# --- all-day events --------------------------------------------------------

def test_explicit_date_allday():
    r = c("8月20号交房租")
    assert r.kind == "event" and r.all_day
    assert r.start == datetime(2026, 8, 20, 0, 0)
    assert r.title == "交房租"


def test_past_date_rolls_to_next_year():
    r = c("1月5号续签合同")
    assert r.start.year == 2027


def test_en_date():
    r = c("aug 20 pay rent")
    assert r.all_day and r.start == datetime(2026, 8, 20, 0, 0)


def test_tomorrow_no_clock_is_allday_event():
    r = c("明天交材料")
    assert r.kind == "event" and r.all_day
    assert r.start == datetime(2026, 8, 15, 0, 0)


# --- diary -----------------------------------------------------------------

def test_today_no_clock_is_diary():
    r = c("今天想了很多关于collie方向的事")
    assert r.kind == "diary"


def test_yesterday_is_diary():
    r = c("昨天8月13号面试感觉不错")   # past narration wins over the date it mentions
    assert r.kind == "diary"


def test_plain_thought():
    r = c("产品定位应该是用户层的agent")
    assert r.kind == "diary" and not r.needs_review


def test_intent_without_time_flags_review():
    r = c("提醒我买牛奶")
    assert r.kind == "diary" and r.needs_review


# --- titles ----------------------------------------------------------------

def test_title_strips_lead_verb():
    r = c("帮我记一下明天下午2点去银行")
    assert r.title == "去银行"


def test_title_never_empty():
    r = c("明天下午3点")
    assert r.title  # falls back to raw text


# --- landing ---------------------------------------------------------------

def cfg_tmp(tmp_path: Path) -> Config:
    return Config(token="t", data_dir=tmp_path, auto_open=False)


def test_event_lands_everywhere(tmp_path):
    cap = classify("明天下午3点和房东通话", now=NOW)
    out = land(cap, cfg_tmp(tmp_path), now=NOW)

    day = tmp_path / "diary" / "2026" / "2026-08-14.md"
    assert day.exists()
    body = day.read_text(encoding="utf-8")
    assert body.startswith("# 2026-08-14 周五")
    assert "和房东通话" in body and "📅" in body

    inbox = (tmp_path / "inbox.md").read_text(encoding="utf-8")
    assert "[event]" in inbox

    assert out["kind"] == "event"
    assert "calendar_url" in out and "opened" not in out


def test_diary_appends_not_duplicates_header(tmp_path):
    cfg = cfg_tmp(tmp_path)
    land(classify("今天想了很多", now=NOW), cfg, now=NOW)
    land(classify("又想了一些", now=NOW), cfg, now=NOW)
    body = (tmp_path / "diary" / "2026" / "2026-08-14.md").read_text(encoding="utf-8")
    assert body.count("# 2026-08-14") == 1
    assert body.count("- **14:23**") == 0  # timestamps come from `now`
    assert body.count("- **10:00**") == 2


def test_review_marker(tmp_path):
    out = land(classify("提醒我买牛奶", now=NOW), cfg_tmp(tmp_path), now=NOW)
    assert out["needs_review"] is True
    inbox = (tmp_path / "inbox.md").read_text(encoding="utf-8")
    assert "[review]" in inbox


def test_gcal_url_timed_allday_and_tz():
    timed = classify("明天下午3点和房东通话", now=NOW)
    u = gcal_url(timed, tz="America/Los_Angeles")
    assert "dates=20260815T150000/20260815T160000" in u
    assert "ctz=America%2FLos_Angeles" in u
    assert "ctz" not in gcal_url(timed)          # no tz configured → calendar default

    allday = classify("8月20号交房租", now=NOW)
    assert "dates=20260820/20260821" in gcal_url(allday)


def test_no_bom_and_lf(tmp_path):
    land(classify("今天心情不错", now=NOW), cfg_tmp(tmp_path), now=NOW)
    raw = (tmp_path / "diary" / "2026" / "2026-08-14.md").read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw


# --- service round trip ----------------------------------------------------

@pytest.fixture()
def server(tmp_path):
    Handler.cfg = Config(token="secret-token", data_dir=tmp_path, auto_open=False)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def post(url, payload, headers=None):
    req = urllib.request.Request(
        url + "/capture", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_health(server):
    with urllib.request.urlopen(server + "/health") as r:
        assert r.status == 200


def test_capture_with_header_auth(server):
    code, body = post(server, {"text": "明天下午3点和房东通话"},
                      {"Authorization": "Bearer secret-token"})
    assert code == 200
    assert body["kind"] == "event" and "calendar_url" in body


def test_capture_with_body_token(server):
    code, body = post(server, {"text": "今天想了很多", "token": "secret-token"})
    assert code == 200 and body["kind"] == "diary"


def test_bad_token_rejected(server):
    code, body = post(server, {"text": "hi", "token": "wrong"})
    assert code == 401


def test_empty_text_rejected(server):
    code, _ = post(server, {"text": "  ", "token": "secret-token"})
    assert code == 400


def test_token_survives_a_second_process():
    """The token `capture setup` prints has to be the token `capture serve` accepts.

    It was minted through `settings.update`, which persists only keys in SCHEMA — and this one
    deliberately has none, so every call produced a fresh token and rejected the one just printed.
    The whole LAN path (the Shortcut, the phone's outbox) could never authenticate; nothing failed
    loudly, because each process was internally consistent.
    """
    import subprocess
    import sys

    first = capture.load_config().token
    assert first == capture.load_config().token, "two calls in one process must agree"

    out = subprocess.run([sys.executable, "-c",
                          "from harness import capture; print(capture.load_config().token)"],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.stdout.strip() == first, "and a second process must agree with the first"


def test_token_file_is_owner_only():
    import os
    import stat
    capture.load_config()
    if os.name != "nt":
        mode = stat.S_IMODE(os.stat(capture.TOKEN_FILE).st_mode)
        assert mode == 0o600, "a bearer token is not a settings value: %s" % oct(mode)
