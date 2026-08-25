"""Expose this Collie to a person-level cloud as an MCP server.

Collie has always been an MCP *client* (``mcpclient.py``): it reaches out to other people's servers.
This is the other direction — the device offering itself, so a cloud agent that already knows the
person (Sauna) can ask this computer what is actually going on here.

That inversion is the point. ``sauna.py`` models Collie pushing state up to a person layer; this
module lets the person layer pull, on demand, from the machine that has the truth. It is the same
architecture read from the other end, and it is the only shape a real Sauna account can accept
today: Sauna's ``Add MCP or API`` connector takes a **URL** and probes it from the cloud, so the
device has to be the server and needs a public address (see ``collie sauna serve --tunnel``).

Read by default, write on request
---------------------------------
The nine read tools are always present. Four write tools appear only with ``--allow-writes``
(``COLLIE_MCP_WRITES=1``), because a public URL that can *act* on a computer is a different
security decision than one that can describe it, and it should be made deliberately rather than on
the way to a demo.

Even with writes on, the line is drawn inside them: changing the person's own record (a task, a
note) happens directly and is undoable, while asking this computer to *do* something does not
execute — ``collie_request`` puts it in front of the owner with a Run button and it goes through
Collie's ordinary permission gate only once they approve.

What protects it
----------------
* **A secret in the path.** The server answers only on ``/<token>/mcp``; anything else is 404,
  including ``/``. The secret is kept in ``~/.collie/mcpserve-token`` (0600) so a restart does not
  silently break a cloud connection that saved the URL; ``--rotate`` cuts every one of them at once.
* **Loopback bind.** The listener is on 127.0.0.1; reachability is whatever tunnel the operator
  chose, and killing the tunnel ends exposure without touching Collie.
* **An audit line per call**, to the same place the browser bridge writes: what was asked, by whom,
  and how big the answer was — never the answer itself.
* **Bounded answers.** Every tool truncates; a cloud agent cannot use this to siphon the store.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__all__ = ["McpServer", "serve", "TOOLS", "PROTOCOL_VERSIONS"]

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
_DEFAULT_PROTOCOL = "2025-03-26"
_MAX_BODY = 1 << 20          # 1 MiB of JSON-RPC is already absurd
_AUDIT = "mcpserve-audit.log"


def _home() -> str:
    return os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie")


def _clip(text: str, n: int) -> str:
    text = str(text or "")
    return text if len(text) <= n else text[: n - 1] + "…"


# --------------------------------------------------------------------------- tools
def _executive():
    from .executive import default_executive
    return default_executive()


def _t_today(args: dict) -> str:
    ex = _executive()
    return ex.answer(str(args.get("query") or ""))


def _t_tasks(args: dict) -> str:
    s = _executive().state
    include_done = bool(args.get("include_done"))
    rows = s.tasks(include_done=include_done)[: int(args.get("limit") or 40)]
    if not rows:
        return "No tasks." if not include_done else "No tasks recorded."
    out = []
    for t in rows:
        goal = s.goal(t["goal_id"]) if t.get("goal_id") else None
        mark = {"done": "done", "doing": "in progress", "next": "next", "dropped": "dropped"}.get(t["status"], "open")
        out.append("- [%s] %s%s" % (mark, t["title"], (" (goal: %s, %d%%)" % (
            goal["title"], round(goal["progress"] * 100))) if goal else ""))
    return "\n".join(out)


def _t_goals(args: dict) -> str:
    s = _executive().state
    rows = s.goals()
    if not rows:
        return "No active goals."
    out = []
    for g in rows:
        tasks = s.tasks(goal_id=g["id"])
        done = [t["title"] for t in tasks if t["status"] == "done"]
        left = [t["title"] for t in tasks if t["status"] not in ("done", "dropped")]
        due = (" · due %s" % _dt.datetime.fromtimestamp(g["due_at"]).strftime("%a %d %b")) if g.get("due_at") else ""
        out.append("- %s — %d%%%s\n    done: %s\n    remaining: %s" % (
            g["title"], round(g["progress"] * 100), due,
            "; ".join(done[-6:]) or "nothing yet", "; ".join(left[:6]) or "nothing"))
    return "\n".join(out)


def _t_calendar(args: dict) -> str:
    s = _executive().state
    days = max(1, min(90, int(args.get("days") or 14)))
    rows = s.upcoming(days=days, limit=20)
    if not rows:
        return "Nothing scheduled in the next %d days." % days
    out = []
    for e in rows:
        when = _dt.datetime.fromtimestamp(e["start_at"]).strftime("%a %d %b %H:%M")
        line = "- %s — %s" % (e["title"], when)
        if e.get("location"):
            line += " · %s" % _clip(e["location"], 80)
        if e.get("goal"):
            line += "\n    goal: %s (%d%% prepared)" % (e["goal"]["title"], round((e.get("preparation") or 0) * 100))
        if e.get("remaining"):
            line += "\n    remaining: " + "; ".join(t["title"] for t in e["remaining"][:5])
        out.append(line)
    return "\n".join(out)


def _t_notes(args: dict) -> str:
    s = _executive().state
    query = str(args.get("query") or "")
    rows = s.notes(query=query, limit=int(args.get("limit") or 10))
    if not rows:
        # A bare "no match" is a dead end: a caller that cannot see what exists just guesses
        # again — a live Sauna agent retried this eight times in a row. Hand back the titles it
        # could have asked for instead.
        titles = [n["title"] for n in s.notes(limit=40)]
        if not titles:
            return "This person has no notes on this computer yet."
        listing = chr(10).join("- " + t for t in titles)
        return ("No note matches %r. The notes that exist are:" + chr(10) + "%s" + chr(10) +
                "Call this tool again with one of these titles, or with no query to read them all."
                ) % (query, listing)
    blocks = []
    for n in rows:
        when = _dt.datetime.fromtimestamp(n["updated_at"]).strftime("%Y-%m-%d")
        blocks.append("## %s (%s)%s%s" % (n["title"], when, chr(10), _clip(n["body"], 1200)))
    return (chr(10) * 2).join(blocks)

def _t_journal(args: dict) -> str:
    from .personal_state import _journal_markdown
    s = _executive().state
    rows = s.journal(limit=max(1, min(21, int(args.get("days") or 5))))
    if not rows:
        return "No journal entries yet."
    return "\n".join(_journal_markdown(j) for j in rows)


def _t_activity(args: dict) -> str:
    s = _executive().state
    rows = s.recent_activity(limit=max(1, min(120, int(args.get("limit") or 30))))
    if not rows:
        return "No activity recorded."
    return "\n".join("%s [%s] %s" % (
        _dt.datetime.fromtimestamp(a["at"]).strftime("%Y-%m-%d %H:%M"), a["actor"], a["summary"]) for a in rows)


def _t_workflows(args: dict) -> str:
    s = _executive().state
    rows = [w for w in s.workflows() if w["status"] in ("suggested", "confirmed", "automated")]
    if not rows:
        return "No learned workflows yet."
    return "\n".join("- %s (%s, observed in %d goals): %s" % (
        w["name"], w["status"], w["observations"],
        " → ".join(st.get("title", st.get("kind", "")) for st in w["steps"])) for w in rows)


def _t_device(args: dict) -> str:
    """What this computer is doing right now — the thing a cloud agent structurally cannot know."""
    from . import personalweb
    try:
        d = personalweb.device_context(cwd=os.getcwd(), wait=1.2, state=_executive().state)
    except Exception as exc:
        return "device context unavailable: %s" % exc
    fg = d.get("foreground") or {}
    bits = ["computer: %s" % os.environ.get("COMPUTERNAME", os.uname().nodename if hasattr(os, "uname") else "this machine"),
            "local time: %s" % time.strftime("%Y-%m-%d %H:%M %Z")]
    if fg.get("app"):
        bits.append("active app: %s" % _clip(fg["app"], 60))
    if fg.get("title"):
        bits.append("window: %s" % _clip(fg["title"], 120))
    sel = (d.get("selection") or {}).get("text") or ""
    if sel:
        bits.append("selected text (%d chars): %s" % (len(sel), _clip(sel, 400)))
    if (d.get("project") or {}).get("name"):
        bits.append("project: %s" % d["project"]["name"])
    return "\n".join("- " + b for b in bits)


# --------------------------------------------------------------------------- write
# The cloud may now change things here. Where the line sits matters more than that it exists:
#
#   * Personal state — a task, a note — is the person's own record of their own work. Sauna
#     changing it is the product working, and it is undoable in one click. Direct.
#   * Making this computer *do* something is a different thing. A public URL that can run work on
#     a machine is exactly what Collie's gate exists for, so a request lands in front of the owner
#     as a suggestion and runs, gated as usual, only once they say so.
#
# That is not timidity: it is the same rule Collie already applies to itself.

def _t_task_add(args: dict) -> str:
    s = _executive().state
    title = str(args.get("title") or "").strip()
    if not title:
        return "ERROR: a task needs a title"
    goal_id = ""
    goal = str(args.get("goal") or "").strip()
    if goal:
        for g in s.goals(None):
            if goal.lower() in g["title"].lower():
                goal_id = g["id"]
                break
    t = s.add_task(title, goal_id=goal_id, source="sauna")
    s.record_activity("task_created", "Sauna added a task: %s" % title, actor="sauna",
                      task_id=t["id"], goal_id=goal_id)
    return "Added \"%s\"%s. It is now on the person's Today view." % (
        t["title"], (" under the goal %r" % s.goal(goal_id)["title"]) if goal_id else "")


def _t_task_done(args: dict) -> str:
    ex = _executive()
    s = ex.state
    ref = str(args.get("task") or args.get("title") or "").strip()
    if not ref:
        return "ERROR: name the task to complete"
    t = s.task(ref) if ref.startswith("tsk_") else None
    if t is None:
        t, _score = s.match_task(ref, min_score=0.5)
    if t is None:
        open_now = [x["title"] for x in s.tasks(include_done=False)][:20]
        return ("No open task matches %r. The open ones are:\n%s" % (ref, "\n".join("- " + x for x in open_now))
                if open_now else "This person has no open tasks.")
    if t["status"] == "done":
        return "\"%s\" was already done." % t["title"]
    done = s.complete_task(t["id"], actor="sauna")
    try:
        ex.workflows.observe_task_completion(done)
        nxt = ex.workflows.suggest_after(done)
    except Exception:
        nxt = None
    s.build_journal()
    goal = s.goal(done["goal_id"]) if done.get("goal_id") else None
    out = "Completed \"%s\"." % done["title"]
    if goal:
        out += " The goal %r is now %d%%." % (goal["title"], round(goal["progress"] * 100))
    if nxt:
        out += " Next: %s" % nxt["title"]
    return out


def _t_note_save(args: dict) -> str:
    s = _executive().state
    text = str(args.get("text") or "").strip()
    if not text:
        return "ERROR: a note needs text"
    append_to = str(args.get("append_to") or "").strip()
    target = s.find_note(append_to) if append_to else None
    if target is None and append_to:
        for n in s.notes(limit=300):
            if append_to.lower() in n["title"].lower():
                target = n
                break
    if target is not None:
        n = s.append_note(target["id"], text, source="sauna")
        return "Appended to the note %r." % n["title"]
    n = s.add_note(text, title=str(args.get("title") or ""), source="sauna")
    if args.get("decision"):
        s.record_decision(text, actor="sauna")
        return "Saved %r and recorded it as a decision (it enters long-term memory)." % n["title"]
    return "Saved the note %r." % n["title"]


def _t_request(args: dict) -> str:
    s = _executive().state
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return "ERROR: say what Collie should do"
    why = str(args.get("why") or "").strip()
    sug = s.add_suggestion(
        _clip("Sauna asks: %s" % prompt, 120), kind="cloud_request",
        body=((why + " ") if why else "") + "Requested by Sauna from the cloud. Nothing has run yet.",
        action={"type": "run", "prompt": prompt, "stage": "suggest", "workflow": "Sauna"},
        confidence=0.8, source="sauna", dedupe=True)
    s.record_activity("cloud_request", "Sauna asked Collie to: %s" % _clip(prompt, 110), actor="sauna",
                      detail={"suggestion_id": sug["id"], "why": why})
    return ("Queued for the owner. It is on their Today view now as \"%s\" with a Run button; it will "
            "execute on this computer, through the normal permission gate, only once they approve. "
            "Nothing has run yet." % sug["title"])


WRITE_TOOL_DEFS = [
    {"name": "collie_task_add", "fn": _t_task_add,
     "description": "Add a task to the person's Collie (their own device). Use when they agree to do something, "
                    "or when a commitment appears that belongs on their list.",
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string"}, "goal": {"type": "string", "description": "optional goal to file it under"}},
         "required": ["title"]}},
    {"name": "collie_task_done", "fn": _t_task_done,
     "description": "Mark one of the person's tasks complete. This moves the goal, the calendar event's "
                    "preparation and the journal. Name the task in words or give its id.",
     "inputSchema": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}},
    {"name": "collie_note_save", "fn": _t_note_save,
     "description": "Write a note into the person's Collie. `append_to` adds to an existing note by title; "
                    "`decision: true` also records it in long-term memory.",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string"}, "title": {"type": "string"}, "append_to": {"type": "string"},
         "decision": {"type": "boolean"}}, "required": ["text"]}},
    {"name": "collie_request", "fn": _t_request,
     "description": "Ask Collie to DO something on this computer (edit files, drive the browser, run checks). "
                    "This does not execute: it puts the request in front of the owner with a Run button, and it "
                    "runs through Collie's permission gate only after they approve. Say why in `why`.",
     "inputSchema": {"type": "object", "properties": {
         "prompt": {"type": "string"}, "why": {"type": "string"}}, "required": ["prompt"]}},
]

_WRITE_NAMES = frozenset(tool["name"] for tool in WRITE_TOOL_DEFS)


TOOLS = [
    {"name": "collie_today", "fn": _t_today,
     "description": "What matters on this person's computer today: upcoming events with the goal behind them, "
                    "goal progress, current focus, suggestions and recent activity. Ask this first.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string",
                     "description": "optional focus, e.g. 'Sauna interview'"}}}},
    {"name": "collie_goals", "fn": _t_goals,
     "description": "The person's active goals with computed progress: which steps are done and which remain.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "collie_tasks", "fn": _t_tasks,
     "description": "The person's tasks, grouped by goal, with status.",
     "inputSchema": {"type": "object", "properties": {
         "include_done": {"type": "boolean"}, "limit": {"type": "integer"}}}},
    {"name": "collie_calendar", "fn": _t_calendar,
     "description": "Upcoming events, each with the goal it serves, how prepared the person is, and what remains.",
     "inputSchema": {"type": "object", "properties": {"days": {"type": "integer"}}}},
    {"name": "collie_notes", "fn": _t_notes,
     "description": "Search the person's notes (decisions, research, meeting notes) kept on this computer.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "limit": {"type": "integer"}}}},
    {"name": "collie_journal", "fn": _t_journal,
     "description": "The AI-maintained daily journal: what happened, what was decided, open loops, what is next.",
     "inputSchema": {"type": "object", "properties": {"days": {"type": "integer"}}}},
    {"name": "collie_activity", "fn": _t_activity,
     "description": "Raw recent activity on this computer: runs Collie performed, files changed, notes saved.",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
    {"name": "collie_workflows", "fn": _t_workflows,
     "description": "How this person works: sequences Collie has observed often enough to recognise.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "collie_device_now", "fn": _t_device,
     "description": "What the person is doing on this computer at this moment: active app, window, selected text, "
                    "current project and local time. Read live; nothing is stored.",
     "inputSchema": {"type": "object", "properties": {}}},
]
def _writes_allowed() -> bool:
    """Write tools are opt-in per launch (`collie mcp-serve --allow-writes`)."""
    return os.environ.get("COLLIE_MCP_WRITES") in ("1", "on", "true")


if _writes_allowed():
    TOOLS = TOOLS + WRITE_TOOL_DEFS

_BY_NAME = {t["name"]: t for t in TOOLS}


def _instructions() -> str:
    base = (
        "This is one person's Collie, running on their own computer. It answers questions about "
        "what is actually happening there: today's state, goals and their real progress, tasks, "
        "calendar with the goal behind each event, notes, the AI-maintained journal, learned "
        "workflows, and what the person is doing on screen right now. "
    )
    if _WRITE_NAMES.intersection(_BY_NAME):
        return base + (
            "Write tools are enabled for tasks and notes because the operator explicitly started "
            "this server with --allow-writes. Requests to operate the computer still require the "
            "owner's approval in Collie and never execute directly from this connection."
        )
    return base + "All exposed tools are read-only."


def _public_tools() -> list[dict]:
    """Tool descriptors with fully-formed schemas.

    A strict client reads a schema with no ``required`` and no ``additionalProperties`` as
    unfinished and retries the handshake. Saying "this takes nothing, and nothing else is allowed"
    explicitly costs two keys and removes the ambiguity.
    """
    out = []
    for t in TOOLS:
        schema = dict(t["inputSchema"])
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        schema.setdefault("required", [])
        schema.setdefault("additionalProperties", False)
        out.append({"name": t["name"], "title": t.get("title") or t["name"].replace("collie_", "Collie: ").replace("_", " "),
                    "description": t["description"], "inputSchema": schema})
    return out


# --------------------------------------------------------------------------- server
def stored_token(create: bool = True) -> str:
    """The secret path segment, kept between launches.

    A fresh secret every start sounds safer until you use it: the cloud side saved a URL, and
    rotating the path silently breaks that connection with no error anyone will see — which is how
    a "connected" integration becomes a lie. The secret lives in a 0600 file instead, so restarting
    Collie keeps the link alive and `--rotate` is the deliberate way to cut it.
    """
    path = os.path.join(_home(), "mcpserve-token")
    try:
        with open(path, encoding="utf-8") as fh:
            existing = fh.read().strip()
        if existing:
            return existing
    except OSError:
        pass
    if not create:
        return ""
    fresh = secrets.token_urlsafe(24)
    os.makedirs(_home(), exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, fresh.encode("utf-8"))
    finally:
        os.close(fd)
    return fresh


class McpServer:
    def __init__(self, *, token: str = "", name: str = "collie"):
        self.token = token or stored_token()
        self.name = name
        self.calls = 0
        self.rejected = 0
        self.last_call = ""
        self.session_id = ""
        self._lock = threading.Lock()

    def new_session(self) -> str:
        with self._lock:
            self.session_id = secrets.token_urlsafe(18)
            return self.session_id

    # -- audit ------------------------------------------------------------
    def audit(self, event: str, detail: dict) -> bool:
        row = dict(detail)
        row["at"] = _dt.datetime.now().isoformat(timespec="seconds")
        row["event"] = event
        try:
            os.makedirs(_home(), exist_ok=True)
            with open(os.path.join(_home(), _AUDIT), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            return False
        return True

    # -- JSON-RPC ---------------------------------------------------------
    def handle(self, msg: dict) -> dict | None:
        method = msg.get("method") or ""
        mid = msg.get("id")
        if method == "initialize":
            asked = ((msg.get("params") or {}).get("protocolVersion") or "").strip()
            version = asked if asked in PROTOCOL_VERSIONS else _DEFAULT_PROTOCOL
            client = ((msg.get("params") or {}).get("clientInfo") or {}).get("name", "?")
            self.audit("initialize", {"client": _clip(client, 60), "protocol": version})
            self.new_session()
            from . import __version__ as collie_version
            return self._ok(mid, {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.name, "version": str(collie_version)},
                "instructions": _instructions(),
            })
        if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
            return None                                   # notification: no reply
        if method == "ping":
            return self._ok(mid, {})
        if method == "tools/list":
            return self._ok(mid, {"tools": _public_tools()})
        if method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name") or ""
            args = params.get("arguments") or {}
            tool = _BY_NAME.get(name)
            if tool is None:
                self.audit("unknown_tool", {"tool": _clip(name, 60)})
                return self._ok(mid, {"isError": True,
                                      "content": [{"type": "text", "text": "unknown tool: %s" % name}]})
            safe_args = sorted(args)[:8] if isinstance(args, dict) else []
            if name in _WRITE_NAMES and not self.audit("write_attempt", {"tool": name, "args": safe_args}):
                return self._ok(mid, {
                    "isError": True,
                    "content": [{"type": "text", "text":
                                 "write refused: the MCP audit trail is unavailable"}],
                })
            started = time.time()
            try:
                text = tool["fn"](args if isinstance(args, dict) else {})
            except Exception as exc:                      # a broken tool must not kill the connection
                text = "ERROR: %s: %s" % (type(exc).__name__, exc)
                self.audit("tool_error", {"tool": name, "error": _clip(str(exc), 160)})
                return self._ok(mid, {"isError": True, "content": [{"type": "text", "text": text}]})
            text = _clip(text, 24000)
            with self._lock:
                self.calls += 1
                self.last_call = "%s %s" % (_dt.datetime.now().strftime("%H:%M:%S"), name)
            # the answer itself is the person's private state: record its SIZE, never its content
            self.audit("tool_call", {"tool": name, "args": safe_args,
                                     "chars": len(text), "ms": int((time.time() - started) * 1000)})
            return self._ok(mid, {"isError": False, "content": [{"type": "text", "text": text}]})
        if method in ("resources/list", "prompts/list"):
            return self._ok(mid, {"resources": [], "prompts": []})
        return self._err(mid, -32601, "method not found: %s" % method)

    @staticmethod
    def _ok(mid, result) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def _err(mid, code, message) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _handler(server: McpServer):
    path_ok = "/%s/mcp" % server.token

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "collie-mcp"

        def log_message(self, fmt, *a):                  # the URL carries the secret: never log it
            pass

        def _send(self, code, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if server.session_id:
                self.send_header("Mcp-Session-Id", server.session_id)
            self.send_header("MCP-Protocol-Version", _DEFAULT_PROTOCOL)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _deny(self):
            server.rejected += 1
            server.audit("rejected", {"path": _clip(self.path.split("/mcp")[0][:12] + "…", 24),
                                      "peer": self.client_address[0]})
            self._send(404, b'{"error":"not found"}')

        def do_GET(self):
            # Streamable HTTP allows a GET for a server-initiated SSE stream. Collie has nothing to
            # push, and saying so plainly beats holding a socket open forever.
            if self.path != path_ok:
                return self._deny()
            self._send(405, b'{"error":"this server does not offer a server-initiated stream"}')

        def do_DELETE(self):
            if self.path != path_ok:
                return self._deny()
            self._send(200, b'{"ok":true}')

        def do_POST(self):
            if self.path != path_ok:
                return self._deny()
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > _MAX_BODY:
                return self._send(400, b'{"error":"bad content length"}')
            raw = self.rfile.read(length)
            try:
                msg = json.loads(raw.decode("utf-8"))
            except Exception:
                return self._send(400, json.dumps(McpServer._err(None, -32700, "parse error")).encode())
            batch = msg if isinstance(msg, list) else [msg]
            replies = []
            for one in batch:
                if not isinstance(one, dict):
                    continue
                reply = server.handle(one)
                if reply is not None:
                    replies.append(reply)
            if not replies:
                return self._send(202, b"")               # notifications only
            payload = replies if isinstance(msg, list) else replies[0]
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            accept = (self.headers.get("Accept") or "").lower()
            if "text/event-stream" in accept and "application/json" not in accept:
                # some clients ask for SSE only; one framed event is a valid Streamable HTTP reply
                framed = b"event: message\ndata: " + body + b"\n\n"
                return self._send(200, framed, "text/event-stream")
            self._send(200, body)

    return H


# 8791 belongs to the surfaces suite's map-web check; taking it made that suite fail with a
# message about a server that "did not come up" when the real cause was this one holding it.
def serve(port: int = 8789, *, token: str = "", name: str = "collie", block: bool = True):
    """Start the MCP server on loopback. Returns (server, httpd, url_path)."""
    mcp = McpServer(token=token, name=name)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _handler(mcp))
    httpd.daemon_threads = True
    path = "/%s/mcp" % mcp.token
    mcp.audit("listening", {"port": httpd.server_address[1]})
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    if block:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            httpd.shutdown()
    return mcp, httpd, path
