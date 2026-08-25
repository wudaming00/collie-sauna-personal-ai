"""Regression tests for runtime trust boundaries shared by every surface."""

import os
import json
import stat
import struct

import pytest


def test_redaction_covers_common_synthetic_credential_shapes():
    from harness.redact import redact

    secrets = {
        "bearer": "syntheticBearerToken_1234567890",
        "db": "alice:synthetic-db-password",
        "npm": "npm-synthetic-token-123456",
        "aws": "syntheticAwsSecretAccessKey1234567890ABCD",
        "cookie": "sid=synthetic-cookie-value-123456",
    }
    raw = (
        "Authorization: Bearer %s\n"
        "DATABASE_URL=postgres://%s@db.example/app\n"
        "NPM_TOKEN=%s\n"
        "aws_secret_access_key=%s\n"
        "Cookie: %s\n"
    ) % tuple(secrets.values())
    vault = {}
    masked = redact(raw, vault)

    assert "{{SECRET:" in masked
    for value in secrets.values():
        assert value not in masked
    assert set(vault.values()) >= set(secrets.values())


def test_slack_identity_defaults_to_read_only_propose(tmp_path, monkeypatch):
    from harness import slackbot

    monkeypatch.setattr(slackbot, "STORE", str(tmp_path / "slack.json"))
    monkeypatch.setattr(slackbot, "IDENTITY", str(tmp_path / "legacy.json"))
    ident = slackbot.load_identity("SafeDog")
    assert ident["autonomy"] == "propose"
    assert slackbot.AUTONOMY_MODE[ident["autonomy"]] == "plan"


def test_slack_listener_refuses_empty_channel_or_user_scope(tmp_path, monkeypatch, capsys):
    from harness import slackbot

    monkeypatch.setattr(slackbot, "STORE", str(tmp_path / "slack.json"))
    monkeypatch.setattr(slackbot, "IDENTITY", str(tmp_path / "legacy.json"))
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-synthetic")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-synthetic")
    assert slackbot.main(["--name", "SafeDog", "--provider", "mock",
                          "--channels", "C123"]) == 2
    assert "unscoped listener" in capsys.readouterr().err
    assert slackbot.main(["--name", "SafeDog", "--provider", "mock",
                          "--allow", "U123"]) == 2


def test_slack_task_stage_is_owner_only_and_contains_no_argv_requirement(tmp_path):
    from harness import slackbot

    path = slackbot._private_task_file("synthetic private task", str(tmp_path / "queue.json"))
    try:
        assert open(path, encoding="utf-8").read() == "synthetic private task"
        if os.name != "nt":
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    finally:
        os.unlink(path)


def test_slack_thread_store_serializes_pack_writers(tmp_path, monkeypatch):
    import json
    import threading
    from harness import slackbot

    monkeypatch.setattr(slackbot, "THREADS", str(tmp_path / "threads.json"))
    barrier = threading.Barrier(2)
    def remember(dog):
        barrier.wait()
        slackbot.thread_session("C1", dog, "session-" + dog, dog=dog)
    writers = [threading.Thread(target=remember, args=(dog,)) for dog in ("Rowan", "Juno")]
    for writer in writers: writer.start()
    for writer in writers: writer.join(5)
    stored = json.loads((tmp_path / "threads.json").read_text(encoding="utf-8"))
    assert len(stored) == 2
    assert slackbot.thread_session("C1", "Rowan", dog="Rowan") == "session-Rowan"
    assert slackbot.thread_session("C1", "Juno", dog="Juno") == "session-Juno"


class _SocketSink:
    def __init__(self):
        self.frames = []

    def sendall(self, value):
        self.frames.append(value)

    def close(self):
        pass


def _ws_client(payload: bytes):
    import io
    import threading
    from harness.wsclient import WebSocketClient
    client = WebSocketClient.__new__(WebSocketClient)
    client._reader = io.BytesIO(payload)
    client._sock = _SocketSink()
    client._send_lock = threading.Lock()
    client._closed = False
    client.last_pong = 0
    return client


def test_websocket_rejects_oversize_before_reading_payload(monkeypatch):
    import pytest
    from harness import wsclient

    monkeypatch.setattr(wsclient, "MAX_FRAME_BYTES", 65536)
    header = bytes([0x82, 127]) + struct.pack(">Q", 65537)
    client = _ws_client(header)              # deliberately no advertised payload follows
    with pytest.raises(wsclient.WebSocketError, match="exceeds"):
        client._read_frame()
    assert client._sock.frames              # close 1009 was attempted


def test_websocket_rejects_masked_server_and_fragment_amplification(monkeypatch):
    import pytest
    from harness import wsclient

    masked = bytes([0x81, 0x80]) + b"abcd"
    with pytest.raises(wsclient.WebSocketError, match="must not be masked"):
        _ws_client(masked)._read_frame()

    monkeypatch.setattr(wsclient, "MAX_FRAME_BYTES", 8)
    monkeypatch.setattr(wsclient, "MAX_MESSAGE_BYTES", 10)
    frames = bytes([0x01, 6]) + b"aaaaaa" + bytes([0x80, 6]) + b"bbbbbb"
    with pytest.raises(wsclient.WebSocketError, match="message exceeds"):
        _ws_client(frames).recv_message()


def test_session_ids_carry_at_least_128_random_bits():
    from harness.sessions import new_id
    import base64

    ids = {new_id() for _ in range(1000)}
    assert len(ids) == 1000
    suffix = next(iter(ids)).split("-", 2)[2]
    padded = suffix + "=" * ((4 - len(suffix) % 4) % 4)
    assert len(base64.urlsafe_b64decode(padded)) >= 16


def test_remote_identity_concurrent_instances_do_not_lose_devices(tmp_path, monkeypatch):
    import json
    import threading
    from harness import remote_identity

    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    first = remote_identity.load_or_create()
    second = remote_identity.load_or_create()
    barrier = threading.Barrier(2)

    def add(identity, device):
        barrier.wait()
        identity.add_or_update(device, device * 64, device)

    threads = [threading.Thread(target=add, args=(first, "a")),
               threading.Thread(target=add, args=(second, "b"))]
    for thread in threads: thread.start()
    for thread in threads: thread.join(5)
    assert all(not thread.is_alive() for thread in threads)
    assert {row["device_id"] for row in remote_identity.load_or_create().devices()} == {"a", "b"}
    stored = json.loads((tmp_path / "remote.json").read_text(encoding="utf-8"))
    assert set(stored["devices"]) == {"a", "b"}
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(tmp_path / "remote.json").st_mode) == 0o600


def test_corrupt_remote_identity_never_silently_rotates_device_id(tmp_path, monkeypatch):
    from harness import remote_identity

    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    original = remote_identity.load_or_create().device_id
    path = tmp_path / "remote.json"
    path.write_text("{ definitely not json", encoding="utf-8")
    with pytest.raises(remote_identity.IdentityCorrupt):
        remote_identity.load_or_create()
    assert path.read_text(encoding="utf-8") == "{ definitely not json"
    assert original


def test_remote_identity_recovers_last_valid_backup_without_rotating(tmp_path, monkeypatch):
    from harness import remote_identity

    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    identity = remote_identity.load_or_create()
    original = identity.device_id
    identity.add_or_update("phone", "a" * 64, "phone")
    path = tmp_path / "remote.json"
    assert (tmp_path / "remote.json.bak").exists()
    path.write_text("[]", encoding="utf-8")
    recovered = remote_identity.load_or_create()
    assert recovered.device_id == original
    assert json.loads(path.read_text(encoding="utf-8"))["device_id"] == original


def test_remote_device_revocation_removes_only_its_replay_partition(tmp_path, monkeypatch):
    from harness import remote_identity

    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    identity = remote_identity.load_or_create()
    identity.add_or_update("phone", "a" * 64, "phone")
    identity.add_or_update("tablet", "b" * 64, "tablet")
    identity._mutate(lambda data: data.update(remote_v2_seen={
        "phone": [{"c": "one", "t": 1}], "tablet": [{"c": "two", "t": 1}],
    }))
    assert identity.forget_device("phone")
    reloaded = remote_identity.load_or_create()
    assert "phone" not in reloaded._d.get("remote_v2_seen", {})
    assert "tablet" in reloaded._d.get("remote_v2_seen", {})


def test_legacy_basename_memory_gets_a_read_only_single_checkout_alias(tmp_path):
    from harness.memory import SqliteMemory, project_scope

    repo = tmp_path / "client" / "backend"
    repo.mkdir(parents=True)
    memory = SqliteMemory(str(tmp_path / "memory.db"))
    try:
        legacy_id = memory.remember(
            "legacy customer fact", keys="legacy", project="backend")
        assert memory.recall("customer fact", project="backend")
        canonical = project_scope(str(repo))
        assert [row["id"] for row in memory.recall(
            "customer fact", project=canonical)] == [legacy_id]
        assert memory.legacy_project_alias_status(canonical)["status"] == "read_only_available"
        # Compatibility is a read-only view: the historical row is not moved
        # into the new path-hashed boundary.
        assert memory.get_claim(legacy_id)["project"] == "backend"
    finally:
        memory.close()
