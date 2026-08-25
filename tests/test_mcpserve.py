"""Contract tests for the Collie-as-MCP-server security boundary."""
from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request

from harness import mcpserve


def _activate_tools(monkeypatch, *, writes: bool):
    read_tools = [tool for tool in mcpserve.TOOLS if tool["name"] not in mcpserve._WRITE_NAMES]
    active = read_tools + (list(mcpserve.WRITE_TOOL_DEFS) if writes else [])
    monkeypatch.setattr(mcpserve, "TOOLS", active)
    monkeypatch.setattr(mcpserve, "_BY_NAME", {tool["name"]: tool for tool in active})
    return active


def _initialize(server: mcpserve.McpServer) -> dict:
    reply = server.handle({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "clientInfo": {"name": "contract-test", "version": "1"},
        },
    })
    return reply["result"]


def test_default_capabilities_are_read_only_and_truthful(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    active = _activate_tools(monkeypatch, writes=False)
    result = _initialize(mcpserve.McpServer(token="test-secret"))

    assert not (mcpserve._WRITE_NAMES & {tool["name"] for tool in active})
    assert result["instructions"].endswith("All exposed tools are read-only.")
    assert not (mcpserve._WRITE_NAMES & {tool["name"] for tool in mcpserve._public_tools()})


def test_write_capabilities_are_opt_in_and_truthful(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    active = _activate_tools(monkeypatch, writes=True)
    result = _initialize(mcpserve.McpServer(token="test-secret"))

    assert mcpserve._WRITE_NAMES <= {tool["name"] for tool in active}
    assert "Write tools are enabled" in result["instructions"]
    assert "still require the owner's approval" in result["instructions"]


def test_write_is_refused_before_mutation_when_audit_is_unavailable(monkeypatch):
    active = _activate_tools(monkeypatch, writes=True)
    called = []
    tool_name = "collie_task_add"
    replacement = dict(next(tool for tool in active if tool["name"] == tool_name))
    replacement["fn"] = lambda args: called.append(args) or "mutated"
    monkeypatch.setitem(mcpserve._BY_NAME, tool_name, replacement)
    server = mcpserve.McpServer(token="test-secret")
    monkeypatch.setattr(server, "audit", lambda event, detail: False)

    reply = server.handle({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": tool_name, "arguments": {"title": "Do not create me"}},
    })

    assert reply["result"]["isError"] is True
    assert "audit trail is unavailable" in reply["result"]["content"][0]["text"]
    assert called == []


def _post(url: str, payload: dict):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Connection": "close"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_http_endpoint_hides_wrong_paths_and_accepts_json_rpc(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    _activate_tools(monkeypatch, writes=False)
    _server, httpd, path = mcpserve.serve(port=0, token="test-secret", block=False)
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    try:
        code, body = _post(base + "/wrong/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert code == 404 and body == {"error": "not found"}

        code, initialized = _post(base + path, {
            "jsonrpc": "2.0", "id": 2, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "clientInfo": {"name": "http-test"}},
        })
        assert code == 200 and initialized["result"]["serverInfo"]["name"] == "collie"

        code, listed = _post(base + path, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        assert code == 200 and len(listed["result"]["tools"]) == 9
        assert not (mcpserve._WRITE_NAMES & {tool["name"] for tool in listed["result"]["tools"]})

        connection = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        connection.request("POST", path, body=b"", headers={"Content-Length": str(mcpserve._MAX_BODY + 1)})
        response = connection.getresponse()
        assert response.status == 400
        assert json.loads(response.read()) == {"error": "bad content length"}
        connection.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
