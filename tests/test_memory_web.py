"""The local Memory surface exposes only this Collie's trusted, scoped profile."""
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
import json
import os
import threading
import urllib.error
import urllib.request

from harness import cli, webapp
from harness.memory import SqliteMemory, project_scope


def _request(url, *, method="GET", body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@contextmanager
def _server(tmp_path):
    old_data = cli.DATA
    old_cwd = os.getcwd()
    old_id = webapp._COLLIE_DEVICE_ID
    cli.DATA = str(tmp_path / "state")
    os.chdir(tmp_path)
    webapp._COLLIE_DEVICE_ID = "collie-memory-web-test"
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        webapp._COLLIE_DEVICE_ID = old_id
        os.chdir(old_cwd)
        cli.DATA = old_data


def test_memory_profile_api_is_scoped_reviewable_and_csrf_protected(tmp_path):
    with _server(tmp_path) as base:
        token = "?token=" + webapp.TOKEN
        current = project_scope(str(tmp_path))

        code, initial = _request(base + "/api/memory" + token)
        assert code == 200
        assert initial["project"] == current
        assert initial["collie_id"] == "collie-memory-web-test"
        assert initial["profile"] == []

        code, error = _request(
            base + "/api/memory/preference", method="POST",
            body={"attribute": "routing.answer_quality", "value": "frontier"})
        assert code == 403 and error["error"] == "forbidden"

        code, saved = _request(
            base + "/api/memory/preference" + token, method="POST",
            body={"attribute": "routing.answer_quality", "value": "frontier",
                  "project": current, "device_only": True,
                  "note": "use the best available model for Collie's own answer"})
        assert code == 200 and saved["ok"]
        assert saved["claim"]["status"] == "attested"
        assert saved["claim"]["device_id"] == "collie-memory-web-test"

        memory = SqliteMemory(cli._paths()[0])
        try:
            guessed = memory.propose(
                "routing.delegate = codex", project=current, kind="preference",
                subject="owner", confidence=0.99, attribute="routing.delegate",
                value="codex", source="agent_inference")
        finally:
            memory.close()

        code, snapshot = _request(base + "/api/memory" + token)
        assert code == 200 and snapshot["pending"] == 1
        profile = {row["attribute"]: row for row in snapshot["profile"]}
        assert profile["routing.answer_quality"]["value"] == "frontier"
        assert "routing.delegate" not in profile, "an unreviewed guess cannot route work"

        code, reviewed = _request(
            base + "/api/memory/review" + token, method="POST",
            body={"id": guessed, "action": "attest", "note": "confirmed locally",
                  "project": current})
        assert code == 200 and reviewed["claim"]["confidence"] == 1.0

        code, snapshot = _request(base + "/api/memory" + token)
        profile = {row["attribute"]: row for row in snapshot["profile"]}
        assert profile["routing.delegate"]["value"] == "codex"

        code, error = _request(
            base + "/api/memory/review" + token, method="POST",
            body={"id": True, "action": "invalidate", "project": current})
        assert code == 400 and "positive integer" in error["error"]

        explicit_id = saved["claim"]["id"]
        code, forgotten = _request(
            base + "/api/memory/review" + token, method="POST",
            body={"id": explicit_id, "action": "invalidate", "project": current,
                  "note": "clear this override"})
        assert code == 200 and forgotten["claim"]["status"] == "invalidated"

        code, snapshot = _request(base + "/api/memory" + token)
        assert "routing.answer_quality" not in {
            row["attribute"] for row in snapshot["profile"]}


def test_memory_preference_rejects_credentials_without_reflecting_them(tmp_path):
    secret = "Synthetic-Vault-Only-73491!"
    with _server(tmp_path) as base:
        token = "?token=" + webapp.TOKEN
        current = project_scope(str(tmp_path))
        code, error = _request(
            base + "/api/memory/preference" + token, method="POST",
            body={"attribute": "password for the staging account",
                  "value": secret, "project": current})
        assert code == 422
        assert error == {
            "error": "credential material belongs in Collie's account vault"}
        assert secret not in json.dumps(error)

        memory = SqliteMemory(cli._paths()[0])
        try:
            assert memory.trusted_profile(current) == {}
        finally:
            memory.close()

        for path in (tmp_path / "state").glob("memory.db*"):
            assert secret.encode("utf-8") not in path.read_bytes()


def test_memory_api_refuses_foreign_project_claims(tmp_path):
    with _server(tmp_path) as base:
        token = "?token=" + webapp.TOKEN
        memory = SqliteMemory(cli._paths()[0])
        try:
            foreign = memory.propose(
                "foreign private memory", project="another-project", scope="another-project")
        finally:
            memory.close()
        code, error = _request(
            base + "/api/memory/review" + token, method="POST",
            body={"id": foreign, "action": "attest", "project": project_scope(str(tmp_path))})
        assert code == 403 and "outside" in error["error"]


def test_hidden_device_claims_cannot_consume_the_review_limit(tmp_path):
    with _server(tmp_path) as base:
        token = "?token=" + webapp.TOKEN
        current = project_scope(str(tmp_path))
        memory = SqliteMemory(cli._paths()[0])
        try:
            visible = memory.propose(
                "visible older preference", project=current, scope=current,
                kind="preference", attribute="response.visible", value="yes")
            for index in range(130):
                memory.propose(
                    "foreign device %d" % index, project=current, scope=current,
                    device_id="other-collie", kind="preference",
                    attribute="hidden.%d" % index, value=True)
        finally:
            memory.close()

        code, snapshot = _request(base + "/api/memory?limit=25&token=" + webapp.TOKEN)
        assert code == 200
        assert visible in {row["id"] for row in snapshot["claims"]}
        assert all(row.get("device_id") in ("", "collie-memory-web-test")
                   for row in snapshot["claims"])


def test_memory_review_requires_exact_project_scope_and_device_context(tmp_path):
    """A numeric id is not a capability for a hidden Mission/other-device claim."""
    with _server(tmp_path) as base:
        token = "?token=" + webapp.TOKEN
        current = project_scope(str(tmp_path))
        memory = SqliteMemory(cli._paths()[0])
        try:
            mission_scoped = memory.propose(
                "mission-private routing hint", project=current,
                scope="mission:private-123", mission_id="private-123",
                kind="preference", subject="owner", attribute="routing.private",
                value="hidden")
            assert memory.promote(
                mission_scoped, status="attested", scope="mission:private-123")
            foreign_device = memory.propose(
                "another Collie's preference", project=current, scope=current,
                device_id="collie-somewhere-else")
            global_claim = memory.propose(
                "global proposal", project="global", scope="global")
        finally:
            memory.close()

        code, snapshot = _request(base + "/api/memory" + token)
        assert code == 200
        visible = {row["id"] for row in snapshot["claims"]}
        assert mission_scoped not in visible
        assert foreign_device not in visible
        assert "routing.private" not in {row["attribute"] for row in snapshot["profile"]}

        for claim_id, project, message in (
                (mission_scoped, current, "scoped"),
                (foreign_device, current, "another Collie"),
                (global_claim, current, "outside")):
            code, error = _request(
                base + "/api/memory/review" + token, method="POST",
                body={"id": claim_id, "action": "attest", "project": project})
            assert code == 403 and message in error["error"]

        # The same global row is reviewable only when the caller explicitly names its exact
        # authorized project/scope boundary.
        code, reviewed = _request(
            base + "/api/memory/review" + token, method="POST",
            body={"id": global_claim, "action": "attest", "project": "global"})
        assert code == 200 and reviewed["claim"]["status"] == "attested"
