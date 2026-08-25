"""Which computer-bound Collie is on the other end of this connection.

One computer owns one stable Collie identity.  Names and Slack aliases may change, providers and
repositories certainly change, but the phone, memory profile and desktop surface must continue to
address the same Collie id.  The web surface also serves its face from the one shared generator.

    python3 tests/test_whoami.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(ok, what):
    print(("  PASS " if ok else "  FAIL ") + what)
    if not ok:
        fails.append(what)


def test_unpersistable_identity_uses_one_shared_machine_fallback(monkeypatch):
    from harness import brain_router, remote_identity, webapp

    old = webapp._COLLIE_DEVICE_ID
    monkeypatch.setattr(remote_identity, "load_or_create",
                        lambda: (_ for _ in ()).throw(OSError("read-only state")))
    try:
        webapp._COLLIE_DEVICE_ID = ""
        assert webapp.collie_device_id() == brain_router.collie_device_id()
        assert webapp.collie_device_id().startswith("host-")
    finally:
        webapp._COLLIE_DEVICE_ID = old


def main():
    from harness import webapp, slackbot, settings

    real_kennel, real_name = slackbot.load_kennel, webapp.DOG_NAME
    real_collie_id = webapp._COLLIE_DEVICE_ID
    real_get, real_pinned = settings.get, settings.pinned
    try:
        webapp._COLLIE_DEVICE_ID = "collie-machine-test"
        # --name remains a compatible workspace alias, but cannot rename the computer-bound Collie.
        webapp.DOG_NAME = "BigMac"
        settings.get = lambda key, default=None: "Saved name" if key == "COMPANION_NAME" else real_get(key, default)
        settings.pinned = lambda key: False
        slackbot.load_kennel = lambda: {"BigMac": {}, "Juno": {}}
        me = webapp.whoami()
        check(me["name"] == "Saved name", "the canonical companion name survives workspace aliases")
        check(me["name_source"] == "settings" and me["name_editable"] is True,
              "the single computer-bound name stays editable in Settings")
        check(me["context"]["workspace_alias"] == "BigMac",
              "--name is retained only as work context")
        check(me["machine"] and me["os"] and me["fingerprint"],
              "with the machine, its OS and the fingerprint that survives a rename")
        check(me["collie_id"] == "collie-machine-test",
              "with one stable computer-bound Collie id independent of its display name")
        check(me["repo"] == os.getcwd(), "and the repository it is standing in")
        check(me["avatar"].startswith("/api/avatar.png?v=") and len(me["avatar"].split("=", 1)[1]) == 12,
              "pointing at a name-versioned face served from here")
        check("autonomy" not in me,
              "and NO autonomy: this server does not enforce one, and a limit it only states is "
              "the defect that was just taken out of the Slack side")
        check("token" not in json.dumps(me).lower(),
              "nothing in the payload is a credential")

        # Kennel membership is work routing context and never changes the canonical identity.
        webapp.DOG_NAME = ""
        check(webapp.whoami()["name"] == "Saved name",
              "the editable display setting wins over a single kennel fallback")
        settings.pinned = lambda key: key == "COMPANION_NAME"
        pinned = webapp.whoami()
        check(pinned["name_source"] == "environment" and pinned["name_editable"] is False,
              "a pinned COLLIE_COMPANION_NAME is reported as authoritative too")
        settings.pinned = lambda key: False
        settings.get = lambda key, default=None: "" if key == "COMPANION_NAME" else real_get(key, default)
        slackbot.load_kennel = lambda: {"BigMac": {}}
        kennel = webapp.whoami()
        check(kennel["name"] == "Collie" and kennel["name_source"] == "default",
              "a kennel member cannot silently become the computer's Collie name")
        slackbot.load_kennel = lambda: {"BigMac": {}, "Juno": {}}
        unnamed = webapp.whoami()
        check(unnamed["name"] == "Collie" and unnamed["name_source"] == "default",
              "the stable generic name is used until the owner chooses one")
        slackbot.load_kennel = lambda: (_ for _ in ()).throw(OSError("no kennel"))
        check(webapp.whoami()["name"] == "Collie", "an unreadable kennel cannot affect identity")
    finally:
        slackbot.load_kennel, webapp.DOG_NAME = real_kennel, real_name
        webapp._COLLIE_DEVICE_ID = real_collie_id
        settings.get, settings.pinned = real_get, real_pinned

    # The face: same generator as Slack's, so one dog is one colour everywhere.
    from harness import avatar
    a = avatar.png("BigMac")
    check(a[:8] == b"\x89PNG\r\n\x1a\n", "the avatar endpoint has a PNG to serve")
    check(avatar.png("BigMac") == a, "identical for the same name — a face is not a random draw")
    check(avatar.png("Juno") != a, "and different for a different one, which is the whole point")

    src = open(os.path.join(ROOT, "harness", "webapp.py"), encoding="utf-8").read()
    check('path == "/api/whoami"' in src and 'path == "/api/avatar.png"' in src,
          "both are routed")
    i_gate = src.find("_peer_ok")
    check(0 < i_gate < src.find('path == "/api/whoami"'),
          "behind the pairing gate: which dog this is, and which repo it stands in, is not public")

    print("\n  " + ("%d FAILED" % len(fails) if fails else "whoami: all green"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
