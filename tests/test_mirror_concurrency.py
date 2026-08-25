"""Deterministic concurrency regressions for the mirror and global live SSE queues."""

import queue
import threading
import time

import pytest

from harness import webapp
from harness.webapp import Handler


@pytest.fixture(autouse=True)
def _clean_feed_state():
    with Handler._mirror_lock:
        Handler._mirror_subs.clear()
        Handler._mirror_backlog.clear()
        Handler._mirror_terminals.clear()
    with Handler._live_lock:
        Handler._live_subs.clear()
    yield
    with Handler._mirror_lock:
        Handler._mirror_subs.clear()
        Handler._mirror_backlog.clear()
        Handler._mirror_terminals.clear()
    with Handler._live_lock:
        Handler._live_subs.clear()


class _CoordinatedFullQueue(queue.Queue):
    """Force the old multi-operation eviction path to interleave deterministically.

    The fixed implementation works under Queue.mutex and intentionally never calls these public
    methods. The pre-fix implementation let both producers observe ``Full`` and separately mutate
    the queue. The bounded implementation serializes priority eviction, so both newest producer
    events settle as one hard-capacity window regardless of their scheduling order.
    """

    def __init__(self):
        super().__init__(maxsize=2)
        self._full = threading.Barrier(2)
        self._drain = threading.Barrier(2)
        self._seen_full = set()
        self._seen_drain = set()
        self._seen_lock = threading.Lock()

    def _rendezvous_once(self, seen, barrier):
        ident = threading.get_ident()
        with self._seen_lock:
            first = ident not in seen
            seen.add(ident)
        if first:
            barrier.wait(timeout=3)

    def put_nowait(self, item):
        try:
            return super().put_nowait(item)
        except queue.Full:
            self._rendezvous_once(self._seen_full, self._full)
            raise

    def get_nowait(self):
        item = super().get_nowait()
        self._rendezvous_once(self._seen_drain, self._drain)
        return item


def test_mirror_put_serializes_concurrent_priority_eviction():
    feed = _CoordinatedFullQueue()
    assert Handler._mirror_put(feed, "token", {"t": "prose"})
    assert Handler._mirror_put(feed, "start", {"id": "original"})

    errors = []

    def publish(kind):
        try:
            assert Handler._mirror_put(feed, kind, {"id": kind})
        except BaseException as exc:  # surface thread failures in the test process
            errors.append(exc)

    producers = [
        threading.Thread(target=publish, args=("tool",), name="mirror-tool"),
        threading.Thread(target=publish, args=("done",), name="mirror-done"),
    ]
    for producer in producers:
        producer.start()
    for producer in producers:
        producer.join(timeout=5)

    assert not errors
    assert all(not producer.is_alive() for producer in producers)
    with feed.mutex:
        kinds = [kind for kind, _ in feed.queue]
        unfinished = feed.unfinished_tasks
    assert sorted(kinds) == ["done", "tool"]
    assert len(kinds) == feed.maxsize
    assert unfinished == len(kinds)


def test_stalled_structural_feed_stays_bounded_and_keeps_newest_done():
    """Ten thousand structural frames cannot turn one stalled SSE tab into a memory leak."""
    feed = queue.Queue(maxsize=64)

    for sequence in range(10_000):
        if sequence % 7 == 0:
            kind = "session_checkpoint"
            data = {"session": "stalled", "run": "r1", "sequence": sequence}
        else:
            kind = "tool"
            data = {"name": "step", "sequence": sequence}
        assert Handler._mirror_put(feed, kind, data)
        assert feed.qsize() <= feed.maxsize

    with feed.mutex:
        checkpoint_rows = [data for kind, data in feed.queue
                           if kind == "session_checkpoint"]
    assert checkpoint_rows == [{"session": "stalled", "run": "r1", "sequence": 9996}]

    first_done = {"session": "stalled", "run": "r1", "sequence": 1}
    assert Handler._mirror_put(feed, "done", first_done)
    for sequence in range(512):
        assert Handler._mirror_put(feed, "tool", {"name": "late", "sequence": sequence})
        assert feed.qsize() <= feed.maxsize

    newer_done = {"session": "stalled", "run": "r1", "sequence": 2}
    assert Handler._mirror_put(feed, "done", newer_done)
    with feed.mutex:
        queued = list(feed.queue)
        unfinished = feed.unfinished_tasks
    done_rows = [data for kind, data in queued if kind == "done"]
    assert done_rows == [newer_done]
    assert len(queued) <= feed.maxsize
    assert unfinished == len(queued)


def test_mirror_join_delivers_join_window_event_exactly_once():
    sid = "mirror-join-race"
    emitted = []
    handler = object.__new__(Handler)
    handler._sse_open = lambda: None
    handler._send_json = lambda payload, status=200: (status, payload)

    def emit(kind, data):
        emitted.append((kind, data))
        if kind == "mirror_hello":
            # This publication is deliberately inside the historical gap between subscriber
            # registration and backlog snapshot. It must choose the live path only.
            Handler._mirror_pub(sid, "tool", {"id": "during-join"})
            Handler._mirror_pub(sid, "token", {"t": "stop"})
        elif kind == "token":
            raise ConnectionResetError("end deterministic mirror stream")

    handler._sse = emit
    handler._serve_mirror(sid)

    kinds = [kind for kind, _ in emitted]
    assert kinds.count("tool") == 1
    assert "mirror_replay" not in kinds
    with Handler._mirror_lock:
        assert sid not in Handler._mirror_subs


def test_mirror_late_join_replays_done_exactly_once_and_closes(monkeypatch):
    """A run finishing before subscriber registration leaves a bounded terminal handoff."""
    sid = "mirror-done-before-join"
    done = {"session": sid, "run": "r-terminal", "answer": "landed"}
    Handler._mirror_pub(sid, "done", done)

    # If the regression returns and no terminal is replayed, fail immediately instead of spending
    # 15 seconds in the keep-alive loop. The fixed path returns before reading its live queue.
    real_queue = queue.Queue

    class _NoWaitQueue(real_queue):
        def get(self, *args, **kwargs):
            raise ConnectionResetError("no terminal replay")

    monkeypatch.setattr(webapp.queue, "Queue", _NoWaitQueue)
    emitted = []
    handler = object.__new__(Handler)
    handler._sse_open = lambda: None
    handler._send_json = lambda payload, status=200: (status, payload)
    handler._sse = lambda kind, data: emitted.append((kind, data))

    handler._serve_mirror(sid)

    assert [kind for kind, _ in emitted].count("done") == 1
    assert next(data for kind, data in emitted if kind == "done") == {
        "session": sid, "run": "r-terminal"}
    assert [kind for kind, _ in emitted] == ["mirror_hello", "mirror_replay", "done"]
    with Handler._mirror_lock:
        assert sid not in Handler._mirror_subs


def test_mirror_terminal_tombstones_are_bounded_expiring_and_start_scoped():
    limit = Handler._MIRROR_TERMINALS
    for index in range(limit + 12):
        sid = "terminal-%03d" % index
        Handler._mirror_pub(sid, "done", {"session": sid, "run": "r"})

    with Handler._mirror_lock:
        assert len(Handler._mirror_terminals) == limit
        assert "terminal-000" not in Handler._mirror_terminals
        assert "terminal-%03d" % (limit + 11) in Handler._mirror_terminals
        Handler._mirror_terminals["expired"] = (
            time.monotonic() - Handler._MIRROR_TERMINAL_TTL - 1,
            {"session": "expired", "run": "old"},
        )
        Handler._mirror_prune_terminals_locked()
        assert "expired" not in Handler._mirror_terminals

    sid = "terminal-new-run"
    Handler._mirror_pub(sid, "done", {"session": sid, "run": "old"})
    Handler._mirror_pub(sid, "start", {"session": sid, "run": "new"})
    with Handler._mirror_lock:
        assert sid not in Handler._mirror_terminals
        assert Handler._mirror_backlog[sid][-1] == (
            "start", {"session": sid, "run": "new"})
