"""Pin the ambient-desktop backend (harness.desktop) — the widget/music brains behind the live
wallpaper. Deterministic ($0, no network, no Windows APIs, no yt-dlp): the model, yt-dlp, and the
system clock are all stubbed, so this tests the LOGIC — verb/mood cleaning, LRC parsing, lyric-query
building, tokenizing, JSON extraction, music-intent shaping, config merge/round-trip, the song-picker
scoring, and resolve_audio's source fallback + hard time cap.

Run: python tests/test_desktop.py   (exit 0 = all green)
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import desktop  # noqa: E402

_fails = []


def check(cond, msg):
    if not cond:
        _fails.append(msg)
        print("  FAIL:", msg)


# ── pure text helpers ─────────────────────────────────────────────────────────────────────────
def test_clean_terms():
    print("test_clean_terms")
    check(desktop._clean_terms("放点lofi") == "lofi hip hop radio",
          "a play-verb + mood keyword resolves to the stable search phrase")
    check(desktop._clean_terms("play some jazz") == "relaxing jazz music",
          "english verb stripped, jazz mood mapped")
    check(desktop._clean_terms("low fire music") == "lofi hip hop radio",
          "a common Web Speech homophone is normalized before search")
    check(desktop._clean_terms("") == "lofi hip hop radio",
          "empty query has a sane default, never blank")
    check(desktop._clean_terms("周杰伦 稻香") == "周杰伦 稻香",
          "a bare artist+song (no verb, no mood) passes through verbatim")
    check(desktop._clean_terms("放 周杰伦") == "周杰伦",
          "the shortest verb '放' is stripped and trailing punctuation/的 cleaned")


def test_parse_lrc():
    print("test_parse_lrc")
    lines = desktop._parse_lrc("[00:12.50]hello\n[00:15.00]world")
    check(lines == [{"t": 12.5, "text": "hello"}, {"t": 15.0, "text": "world"}],
          "two timestamped lines parse to sorted {t,text}")
    multi = desktop._parse_lrc("[00:02.00][00:01.00]same")
    check(multi == [{"t": 1.0, "text": "same"}, {"t": 2.0, "text": "same"}],
          "multiple timestamps on one line fan out and re-sort by time")
    check(desktop._parse_lrc("[ti:junk]\n[00:03.00]x") == [{"t": 3.0, "text": "x"}],
          "metadata-only tag lines (no mm:ss) are dropped")
    stripped = desktop._parse_lrc("[00:01.00][00:01.50]a b")
    check(all("[" not in x["text"] for x in stripped), "every [mm:ss] tag is removed from the text")
    check(desktop._parse_lrc("") == [], "empty input -> empty list, not a crash")


def test_lyric_queries():
    print("test_lyric_queries")
    qs = desktop._lyric_queries("稻香 (Official Video)", "周杰伦")
    check(qs and qs[0] == "稻香", "the cleaned song name is the first, most-precise guess")
    check("周杰伦 稻香" in qs, "artist+song is offered as a query")
    check(len(qs) == len(set(q.lower() for q in qs)), "no duplicate queries")
    check(len(qs) <= 6, "at most 6 queries")
    check(all(q.strip() == q and q for q in qs), "queries are stripped and non-empty")
    check(desktop._lyric_queries("", "") == [] or all(desktop._lyric_queries("", "")),
          "empty title+artist yields nothing usable, never a blank query")


def test_tokens():
    print("test_tokens")
    t = desktop._tokens("Hello 世界 ab X")
    check("hello" in t and "世界" in t and "ab" in t, "ascii words + CJK bigrams tokenize, lowercased")
    check("x" not in t, "single characters are excluded (too weak to match on)")
    check(desktop._tokens("") == set(), "empty string -> empty set")


def test_json_obj():
    print("test_json_obj")
    check(desktop._json_obj('{"music": true}') == {"music": True}, "a bare JSON object parses")
    check(desktop._json_obj('noise {"a": 1} tail') == {"a": 1}, "an object embedded in prose is extracted")
    check(desktop._json_obj("not json at all") is None, "no object -> None")
    check(desktop._json_obj("[1, 2, 3]") is None, "a JSON array is not a dict -> None")
    check(desktop._json_obj("") is None, "empty -> None")


# ── music intent (model stubbed, exactly like the router tests) ───────────────────────────────
class _Prov:
    def __init__(self, text, boom=False):
        self.text, self.boom, self.calls = text, boom, 0

    def complete(self, system, messages, tools):
        self.calls += 1
        if self.boom:
            raise RuntimeError("model down")
        return type("C", (), {"text": self.text})()


def _with_provider(prov, fn):
    real = desktop._router_provider
    desktop._router_provider = lambda: prov
    try:
        return fn()
    finally:
        desktop._router_provider = real


def test_music_intent():
    print("test_music_intent")
    prov = _Prov('{"music":true,"query":"周杰伦 稻香","artist":"周杰伦","title":"稻香","duration_seconds":0}')
    d = _with_provider(prov, lambda: desktop.music_intent("放点周杰伦的稻香"))
    check(d == {"music": True, "action": "play", "query": "周杰伦 稻香", "artist": "周杰伦", "title": "稻香", "duration_seconds": 0} and
          prov.calls == 1,
          "an explicit music command is semantically judged and uses the provider JSON")

    chinese_provider = _Prov(
        '{"music":true,"query":"轻音乐","artist":"","title":"","duration_seconds":30}')
    chinese = _with_provider(chinese_provider,
                             lambda: desktop.music_intent("呃，给我播放一小段轻音乐"))
    check(chinese == {"music": True, "action": "play", "query": "轻音乐", "artist": "", "title": "", "duration_seconds": 30} and
          chinese_provider.calls == 1,
          "the exact spoken Mandarin command is judged by the model")

    no = _with_provider(_Prov('{"music":false}'), lambda: desktop.music_intent("打开 VS Code"))
    check(no == {"music": False, "action": "none", "query": ""}, "a non-music command is rejected cleanly")

    video_provider = _Prov('{"music":false}')
    video = _with_provider(video_provider, lambda: desktop.music_intent("播放一下刚才的视频"))
    check(video == {"music": False, "action": "none", "query": ""} and video_provider.calls == 1,
          "a semantically non-music playback request is not hijacked")

    unused = _Prov("should not be called")
    empty = _with_provider(unused, lambda: desktop.music_intent("   "))
    check(empty == {"music": False, "action": "none", "query": ""} and unused.calls == 0,
          "blank input short-circuits without ever calling the model")

    stop_provider = _Prov("should not be called")
    stop = _with_provider(stop_provider, lambda: desktop.music_intent("停止播放音乐"))
    check(stop["action"] == "stop" and stop["music"] is True and stop_provider.calls == 0,
          "a literal stop remains available without a classifier provider")

    err = _with_provider(_Prov("", boom=True), lambda: desktop.music_intent("推荐适合工作的声音"))
    check(err["music"] is False and "error" in err,
          "a model crash degrades to not-music with an error field, never raises")


def test_music_control_intent_has_atomic_stop_and_replace_actions():
    stop_provider = _Prov("must not be called")
    stop = _with_provider(stop_provider, lambda: desktop.music_intent("停止播放音乐"))
    assert stop["music"] is True and stop["action"] == "stop" and stop_provider.calls == 0

    replace_provider = _Prov(
        '{"action":"replace","music":true,"query":"爵士","artist":"","title":""}')
    replace = _with_provider(
        replace_provider, lambda: desktop.music_intent("停止现在的音乐，然后播放爵士"))
    assert replace["music"] is True and replace["action"] == "replace"
    assert replace["query"] == "爵士" and replace_provider.calls == 1


def test_stop_audio_covers_collie_and_the_system_player():
    from harness import webapp

    calls = []
    class FakeDesktop:
        @staticmethod
        def playing_here():
            return {"track": None}
        @staticmethod
        def stop_here():
            calls.append("collie")
            return {"ok": False}
        @staticmethod
        def media(command):
            calls.append("system:" + command)
            return True

    stopped = webapp._stop_desktop_audio(FakeDesktop)
    assert calls == ["collie", "system:stop"]
    assert stopped["ok"] is True and stopped["system_stop_sent"] is True
    assert webapp._intent_summary(stopped) == "Stopped the music."


def test_windows_stream_stop_waits_for_the_exact_player(monkeypatch):
    from harness import plat

    class Proc:
        returncode = None
        terminated = False
        waited = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            self.waited = True
            self.returncode = 0
            return 0

    proc = Proc()
    monkeypatch.setattr(plat, "is_windows", lambda: True)
    assert plat.stop_stream(proc) is True
    assert proc.terminated is True and proc.waited is True and proc.poll() == 0


def test_failed_music_stop_keeps_the_retry_handle(monkeypatch):
    class Proc:
        @staticmethod
        def poll():
            return None

    proc, track = Proc(), {"title": "Still audible"}
    desktop._playing.update(proc=proc, track=track, generation=10)
    monkeypatch.setattr(desktop.plat, "stop_stream", lambda current: False)
    monkeypatch.setattr(desktop, "_np_read", lambda: None)
    monkeypatch.setattr(desktop, "_hide_indicator", lambda: None)
    monkeypatch.setattr(desktop, "_np_clear", lambda: (_ for _ in ()).throw(
        AssertionError("an unproven stop must not erase the receipt")))

    assert desktop.stop_here() == {"ok": False}
    assert desktop._playing["proc"] is proc and desktop._playing["track"] is track
    desktop._playing.update(proc=None, track=None)


# ── config merge + round-trip (temp dir, real defaults) ──────────────────────────────────────
def test_config_roundtrip(tmp):
    print("test_config_roundtrip")
    real_dir, real_path = desktop.COLLIE_DIR, desktop.CONFIG_PATH
    desktop.COLLIE_DIR = tmp
    desktop.CONFIG_PATH = os.path.join(tmp, "desktop.json")
    try:
        check(desktop.config_mtime() == 0.0, "mtime of a missing config is 0.0, not an error")
        fresh = desktop.load_config()
        check("clock" in fresh["widgets"] and fresh["widgets"]["clock"]["slot"] == "tr",
              "a missing config loads the defaults (clock top-right)")
        check(isinstance(fresh["widgets"]["launcher"].get("apps"), list),
              "the launcher dock is seeded to a list, never left absent")

        cfg = desktop.load_config()
        cfg["widgets"]["clock"]["slot"] = "bl"                 # user drags the clock bottom-left
        cfg["widgets"]["mine"] = {"on": True, "slot": "br"}    # an unknown widget the agent added
        desktop.save_config(cfg)
        check(os.path.exists(desktop.CONFIG_PATH), "save writes the config file")
        check(desktop.config_mtime() > 0.0, "mtime is real after a save")

        back = desktop.load_config()
        check(back["widgets"]["clock"]["slot"] == "bl", "a moved slot survives the round-trip")
        check(back["widgets"]["mine"] == {"on": True, "slot": "br"},
              "an unknown widget is preserved (config is the agent-editable source of truth)")
        check(back["widgets"]["brand"]["slot"] == "center",
              "untouched defaults still fill in after a partial saved config")
    finally:
        desktop.COLLIE_DIR, desktop.CONFIG_PATH = real_dir, real_path


def test_launch_and_media_reject_bad_input():
    print("test_launch_and_media_reject_bad_input")
    check(desktop.launch("") is False, "launch('') is a no-op False, not a crash")
    check(desktop.launch(os.path.join(desktop.HOME, "no", "such", "file.exe")) is False,
          "launch of a non-existent path returns False (never invents a target)")
    check(desktop.media("not-a-key") is False, "an unknown media command is rejected")


def test_play_here_reports_resolved_media_not_search_terms(monkeypatch):
    """Now Playing is an observation of the player, never an echo of the request."""
    class Proc:
        pid = 123

        @staticmethod
        def poll():
            return None

    monkeypatch.setattr(desktop, "stop_here", lambda: {"ok": False})
    monkeypatch.setattr(desktop, "_install_reaper", lambda: None)
    monkeypatch.setattr(desktop, "_np_write", lambda track, pid: None)
    monkeypatch.setattr(desktop, "_show_indicator", lambda title: False)
    monkeypatch.setattr(desktop.plat, "play_stream", lambda *a, **k: Proc())
    monkeypatch.setattr(desktop, "resolve_audio", lambda *a, **k: {
        "ok": True, "url": "u://actual", "title": "Official upload title",
        "track": "Actual Track", "artist": "Actual Artist", "uploader": "Uploader",
        "duration": 201, "source": "youtube",
    })

    result = desktop.play_here("wrong speech keywords", artist="Requested Artist",
                               title="Requested Song")
    assert result["display_title"] == "Actual Track"
    assert result["uploader"] == "Actual Artist"
    assert desktop._playing["track"]["title"] == "Actual Track"
    assert "wrong speech keywords" not in desktop._playing["track"]["title"]
    desktop._playing.update(proc=None, track=None)


def test_somafm_current_track_uses_official_history_metadata():
    current = desktop._somafm_current_track({"songs": [
        {"title": "A Real Song", "artist": "A Real Artist"},
        {"title": "An Older Song", "artist": "Another Artist"},
    ]})
    assert current == {"title": "A Real Song", "uploader": "A Real Artist"}
    assert desktop._somafm_current_track({"songs": []}) is None


# ── song picker scoring (yt-dlp stubbed) ─────────────────────────────────────────────────────
def _stub_run(stdout):
    import json as _j

    def run(*a, **k):
        return type("R", (), {"stdout": _j.dumps(stdout), "returncode": 0})()
    return run


def test_pick_song_scoring():
    print("test_pick_song_scoring")
    entries = {"entries": [
        {"id": "song", "title": "稻香 (Audio)", "channel": "Jay Chou - Topic", "duration": 223, "url": "u://song"},
        {"id": "gossip", "title": "Jay Chou EXPOSED drama reaction", "channel": "TeaSpill", "duration": 600, "url": "u://gossip"},
        {"id": "live", "title": "稻香 live performance", "channel": "concert", "duration": 300, "url": "u://live"},
    ]}
    real = desktop.subprocess.run
    desktop.subprocess.run = _stub_run(entries)
    try:
        best = desktop._pick_song("yt-dlp", "周杰伦 稻香", "youtube")
        check(best == "u://song", "the Topic/audio track beats the gossip 'EXPOSED' and the live cut")
        nogossip = desktop._pick_song("yt-dlp", "x", "youtube", exclude={"song"})
        check(nogossip == "u://live", "with the top pick excluded, it never falls to the blocklisted gossip clip")
        empty = desktop._pick_song("yt-dlp", "x", "youtube", exclude={"song", "gossip", "live"})
        check(empty is None, "excluding every candidate yields None")
    finally:
        desktop.subprocess.run = real

    desktop.subprocess.run = _stub_run({"entries": []})
    try:
        check(desktop._pick_song("yt-dlp", "x", "youtube") is None, "no entries -> None")
    finally:
        desktop.subprocess.run = real


# ── resolve_audio: source fallback, formats fallback, exclude passthrough, hard time cap ──────
def test_resolve_audio_unavailable():
    print("test_resolve_audio_unavailable")
    # ytdlp_cmd() is the single entry point now: it returns the argv prefix (the zipapp needs an
    # interpreter in front of it) and falls back PATH -> zipapp -> platform binary, so stubbing
    # _ensure_ytdlp no longer reaches this path.
    real = desktop.ytdlp_cmd
    desktop.ytdlp_cmd = lambda: []
    try:
        r = desktop.resolve_audio("放点lofi")
        check(r == {"ok": False, "error": "yt-dlp unavailable"}, "no yt-dlp -> a clear error, no crash")
    finally:
        desktop.ytdlp_cmd = real


def test_resolve_audio_fallback_and_exclude():
    print("test_resolve_audio_fallback_and_exclude")
    real_ens, real_ex = desktop._ensure_ytdlp, desktop._extract_one
    desktop._ensure_ytdlp = lambda: "yt-dlp"
    seen = []

    def ex(exe, terms, source, exclude=()):
        seen.append((source, tuple(exclude), terms))
        if source == "youtube":
            return None                                     # first source dry
        return {"url": "u://b", "title": "T", "id": "vid", "uploader": "U", "duration": 200}

    desktop._extract_one = ex
    try:
        r = desktop.resolve_audio("放点周杰伦 稻香", artist="周杰伦", title="稻香", exclude=("old",))
        check(r["ok"] and r["url"] == "u://b" and r["source"] == "bilibili",
              "youtube dry -> falls back to bilibili and returns its stream")
        check(r["terms"] == "周杰伦 稻香", "structured artist+title drive the search terms")
        check(("old",) in [e[1] for e in seen], "the exclude set is threaded through to extraction")

        seen.clear()
        cn = desktop.resolve_audio("放点音乐", region="cn")
        check(cn["source"] == "bilibili" and seen[0][0] == "bilibili",
              "region CN tries bilibili FIRST (youtube is blocked there)")
    finally:
        desktop._ensure_ytdlp, desktop._extract_one = real_ens, real_ex


def test_resolve_audio_formats_fallback():
    print("test_resolve_audio_formats_fallback")
    real_ens, real_ex = desktop._ensure_ytdlp, desktop._extract_one
    desktop._ensure_ytdlp = lambda: "yt-dlp"
    desktop._extract_one = lambda *a, **k: {
        "id": "v", "title": "T",
        "formats": [{"acodec": "none", "url": "u://video"}, {"acodec": "mp4a", "url": "u://audio"}]}
    try:
        r = desktop.resolve_audio("放点lofi")
        check(r["ok"] and r["url"] == "u://audio",
              "when the info dict has no top-level url, an audio-bearing format is used")
    finally:
        desktop._ensure_ytdlp, desktop._extract_one = real_ens, real_ex


def test_resolve_audio_no_source():
    print("test_resolve_audio_no_source")
    real_ens, real_ex = desktop._ensure_ytdlp, desktop._extract_one
    desktop._ensure_ytdlp = lambda: "yt-dlp"
    desktop._extract_one = lambda *a, **k: None
    try:
        r = desktop.resolve_audio("放点lofi")
        check(r["ok"] is False and r["error"] == "no playable source",
              "both sources dry -> a definite 'no playable source', not a hang")
    finally:
        desktop._ensure_ytdlp, desktop._extract_one = real_ens, real_ex


def test_resolve_audio_hard_time_cap():
    print("test_resolve_audio_hard_time_cap")
    real_ens, real_ex, real_mono = desktop._ensure_ytdlp, desktop._extract_one, time.monotonic
    desktop._ensure_ytdlp = lambda: "yt-dlp"
    calls = {"n": 0}

    def ex(*a, **k):
        calls["n"] += 1
        return {"url": "u://x", "id": "i", "title": "t"}

    desktop._extract_one = ex
    ticks = iter([0.0, 10_000.0, 10_000.0, 10_000.0])   # deadline = 60; next check is already past it
    time.monotonic = lambda: next(ticks)
    try:
        r = desktop.resolve_audio("放点lofi")
        check(r["ok"] is False and r["error"] == "timeout",
              "past the 60s budget, it returns 'timeout' rather than starting another slow source")
        check(calls["n"] == 0, "the time cap trips BEFORE spending a second on extraction")
    finally:
        desktop._ensure_ytdlp, desktop._extract_one, time.monotonic = real_ens, real_ex, real_mono


def main():
    import tempfile
    test_clean_terms()
    test_parse_lrc()
    test_lyric_queries()
    test_tokens()
    test_json_obj()
    test_music_intent()
    with tempfile.TemporaryDirectory() as tmp:
        test_config_roundtrip(tmp)
    test_launch_and_media_reject_bad_input()
    # pytest supplies monkeypatch for the behavioral player test; the standalone runner keeps its
    # original zero-fixture coverage and the full suite exercises the test above.
    test_pick_song_scoring()
    test_resolve_audio_unavailable()
    test_resolve_audio_fallback_and_exclude()
    test_resolve_audio_formats_fallback()
    test_resolve_audio_no_source()
    test_resolve_audio_hard_time_cap()
    if _fails:
        print("\n%d FAILED" % len(_fails))
        sys.exit(1)
    print("\nall green")


if __name__ == "__main__":
    main()
