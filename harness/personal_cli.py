"""CLI for the personal layer: `collie today | note | task | journal | sauna | state`.

Thin wrappers over harness/executive.py, personal_state.py and sauna.py so the same state is
reachable from a terminal, a script, or an SSH session — the executive view is not a web-only thing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time


def _ex():
    from .executive import default_executive
    return default_executive()


def _sauna():
    from .sauna import default_client
    return default_client(_ex().state)


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


# ------------------------------------------------------------------------------ commands
def cmd_today(args) -> int:
    ex = _ex()
    if getattr(args, "json", False):
        b = ex.brief()
        b["sauna"] = _sauna().status()
        _print_json(b)
        return 0
    print(ex.answer(getattr(args, "query", "") or ""))
    st = _sauna().status()
    print("Sauna: %s" % ("connected as %s · last sync %s" % (st["account"], _ago(st["last_sync"])) if st["connected"]
                         else "not connected (local only)"))
    return 0


def cmd_note(args) -> int:
    ex = _ex()
    s = ex.state
    text = " ".join(args.text or []).strip()
    if not text:
        print("usage: collie note \"text\" [--title T] [--project P] [--goal G] [--append-to NOTE] [--decision]")
        return 2
    project_id = ""
    if args.project:
        p = s.find_project(args.project) or s.upsert_project(args.project)
        project_id = p["id"]
    goal_id = ""
    if args.goal:
        for g in s.goals(None):
            if args.goal.lower() in g["title"].lower():
                goal_id = g["id"]
                break
    if args.append_to:
        target = s.find_note(args.append_to)
        if target is None:
            for n in s.notes(limit=300):
                if args.append_to.lower() in n["title"].lower():
                    target = n
                    break
        if target is None:
            print("no note matches %r" % args.append_to)
            return 1
        n = s.append_note(target["id"], text)
        print("appended to \"%s\" (%s)" % (n["title"], n["id"]))
    else:
        n = s.add_note(text, title=args.title or "", project_id=project_id, goal_id=goal_id)
        print("saved \"%s\" (%s)" % (n["title"], n["id"]))
    if args.decision:
        s.record_decision(text, project_id=project_id, goal_id=goal_id)
        print("recorded as a decision")
    s.render_views()
    return 0


def cmd_task(args) -> int:
    ex = _ex()
    s = ex.state
    action = args.task_action
    if action == "ls":
        rows = s.tasks(include_done=args.all)
        focus = s.get_meta("focus_task")
        for t in rows:
            mark = {"done": "✓", "doing": "→", "next": "→", "dropped": "×"}.get(t["status"], "○")
            g = s.goal(t["goal_id"]) if t.get("goal_id") else None
            print("%s %s %s%s%s" % (mark, t["id"], t["title"], (" · " + g["title"]) if g else "",
                                    "  (focus)" if t["id"] == focus else ""))
        if not rows:
            print("no tasks")
        return 0
    title = " ".join(args.title or []).strip()
    if action == "add":
        if not title:
            print("usage: collie task add \"title\" [--goal G] [--project P]")
            return 2
        goal_id = ""
        if args.goal:
            for g in s.goals(None):
                if args.goal.lower() in g["title"].lower():
                    goal_id = g["id"]
                    break
        project_id = ""
        if args.project:
            p = s.find_project(args.project) or s.upsert_project(args.project)
            project_id = p["id"]
        t = s.add_task(title, goal_id=goal_id, project_id=project_id)
        print("added %s %s" % (t["id"], t["title"]))
        return 0
    t = s.task(title) if title.startswith("tsk_") else None
    if t is None and title:
        t, _ = s.match_task(title, min_score=0.34)
    if t is None:
        print("no task matches %r" % title)
        return 1
    if action == "done":
        done = s.complete_task(t["id"], actor="user")
        try:
            ex.workflows.observe_task_completion(done)
            sug = ex.workflows.suggest_after(done)
        except Exception:
            sug = None
        s.build_journal(); s.render_views()
        g = s.goal(done["goal_id"]) if done.get("goal_id") else None
        print("done: %s%s" % (done["title"], (" · goal \"%s\" %d%%" % (g["title"], round(g["progress"] * 100))) if g else ""))
        if sug:
            print("next: %s — %s" % (sug["title"], sug["body"]))
        return 0
    if action == "focus":
        s.set_meta("focus_task", t["id"])
        if t["status"] in ("open", "next"):
            s.update_task(t["id"], status="doing")
        print("focus: %s" % t["title"])
        return 0
    if action == "drop":
        s.update_task(t["id"], status="dropped")
        print("dropped: %s" % t["title"])
        return 0
    print("unknown task action")
    return 2


def cmd_journal(args) -> int:
    ex = _ex()
    s = ex.state
    if args.build:
        entry = s.build_journal(args.day or None, narrator=ex.narrator)
        s.render_views()
    else:
        from .personal_state import day_key
        entry = s.journal_entry(args.day or day_key())
        if entry is None:
            entry = s.build_journal(args.day or None, narrator=ex.narrator)
    if getattr(args, "json", False):
        _print_json(entry)
        return 0
    from .personal_state import _journal_markdown
    print(_journal_markdown(entry))
    if args.week:
        from .personal_state import week_key
        w = s.weekly_summary(week_key())
        print("Week %s: %s" % (w["key"], w["body"]))
        for h in w["happened"][:12]:
            print("- " + h)
    return 0


def cmd_sauna(args) -> int:
    c = _sauna()
    action = args.sauna_action
    if action == "status":
        st = c.status()
        if getattr(args, "json", False):
            _print_json(st); return 0
        print("Sauna (%s): %s" % (st["mode"], "connected as %s" % st["account"] if st["connected"] else "not connected"))
        print("device: %s (%s)" % (st["device_name"], st["device_id"]))
        if st["connected"]:
            print("last sync: %s · credential: %s" % (_ago(st["last_sync"]), st["credential"]))
        print("sync: " + ", ".join("%s=%s" % (k, "on" if v else "off") for k, v in st["sync"].items()))
        print("cloud tasks: %s" % st["cloud"])
        return 0
    if action == "connect":
        st = c.connect(args.account or "")
        print("connected as %s · synced %s" % (st["account"], _ago(st["last_sync"])))
        return 0
    if action == "disconnect":
        c.disconnect(forget_cloud_copy=args.forget)
        print("disconnected — Collie keeps working locally")
        return 0
    if action == "sync":
        r = c.sync()
        _print_json(r); return 0 if r.get("synced") else 1
    if action == "context":
        print(c.person_context(" ".join(args.query or [])) or "(not connected — local only)")
        return 0
    if action == "devices":
        for d in c.devices():
            print("%s%s · %s · %s · %s" % ("* " if d.get("this_device") else "  ", d["name"], d["kind"], d.get("platform", ""), d.get("status", "")))
        return 0
    if action == "handoff":
        text = " ".join(args.query or []).strip()
        if not text:
            print("usage: collie sauna handoff \"research X tonight, report tomorrow morning\""); return 2
        ct = c.handoff(text)
        print("scheduled on Sauna Cloud (%s): %s" % (ct["id"], ct["title"]))
        if ct.get("deliver_at"):
            print("deliver by: %s" % time.strftime("%a %H:%M", time.localtime(ct["deliver_at"])))
        print("(prototype: cloud execution is mocked — run it here with `collie run` if you need it now)")
        return 0
    if action == "export":
        print(c.export_snapshot(args.path or None)); return 0
    if action == "restore":
        r = c.restore(args.path or None)
        print("welcome back — restored from %s: %s" % (r["from"], json.dumps(r["welcome"], ensure_ascii=False)))
        return 0
    if action == "push":
        text = " ".join(args.query or []).strip()
        if not text:
            print("usage: collie sauna push \"tell Sauna this\" [--transport browser|email]")
            return 2
        try:
            out = c.push(text, transport=args.transport, wait=float(args.wait))
        except Exception as exc:
            print("push failed: %s" % exc, file=sys.stderr)
            return 1
        if not out.get("ok"):
            print(out.get("why") or "not sent")
            print("  to:   %s" % out.get("to"))
            print("  from: %s (must be the address on the account)" % out.get("from_required"))
            return 1
        print("pushed via %s in %.1fs" % (out["transport"], out["seconds"]))
        print()
        print(out.get("reply") or out.get("note") or "(no reply yet)")
        return 0
    if action == "route":
        _print_json(c.route(" ".join(args.query or []))); return 0
    print("unknown sauna action"); return 2


def cmd_mcpserve(args) -> int:
    """Offer this Collie to a person-level cloud as an MCP server.

    Sauna's `Add MCP or API` connector takes a URL and probes it *from the cloud*, so the device has
    to be the server and needs a public address. `--tunnel` gets one from cloudflared; without it
    you are on loopback and it is on you to expose it.
    """
    import subprocess
    import sys as _sys
    from . import mcpserve

    if getattr(args, "rotate", False):
        try:
            os.remove(os.path.join(mcpserve._home(), "mcpserve-token"))
            print("collie mcp · secret rotated — every saved URL is now dead")
        except OSError:
            pass
    if getattr(args, "allow_writes", False):
        os.environ["COLLIE_MCP_WRITES"] = "1"
        import importlib
        importlib.reload(mcpserve)
    mcp, httpd, path = mcpserve.serve(port=args.port, block=False, name=args.name)
    port = httpd.server_address[1]
    local = "http://127.0.0.1:%d%s" % (port, path)
    writes = getattr(args, "allow_writes", False)
    print("collie mcp · %d tools (%s) · %s" % (
        len(mcpserve.TOOLS), "read + WRITE" if writes else "read-only", local))
    print("  tools: " + ", ".join(t["name"] for t in mcpserve.TOOLS))
    proc = None
    if args.tunnel:
        print("  opening a public tunnel (cloudflared)…")
        try:
            from . import plat
            proc = subprocess.Popen(["npx", "-y", "cloudflared", "tunnel", "--url",
                                     "http://127.0.0.1:%d" % port, "--no-autoupdate"],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    shell=_sys.platform.startswith("win"),
                                    **plat.no_window_kwargs())   # no console flash on Windows
        except FileNotFoundError:
            print("  cloudflared not available (needs node/npx). Expose port %d yourself." % port,
                  file=_sys.stderr)
        if proc is not None:
            import re
            public = ""
            deadline = time.time() + 90
            while time.time() < deadline and not public:
                line = proc.stdout.readline()
                if not line:
                    break
                hit = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
                if hit:
                    public = hit.group(0)
            if public:
                print()
                print("  PUBLIC URL — paste this into Sauna → Connections → Add MCP or API → MCP Server:")
                print("  %s%s" % (public, path))
                print()
                print("  The path carries your Collie's secret (kept in ~/.collie/mcpserve-token, so a")
                print("  restart does not break the connection). `--rotate` invalidates it.")
                print("  NOTE: a free cloudflared tunnel gets a NEW hostname every time it starts, so")
                print("  the cloud side must be re-pointed after a restart. For a URL that survives,")
                print("  use a named Cloudflare tunnel or your own domain.")
            else:
                print("  tunnel did not report a URL; see cloudflared output", file=_sys.stderr)
    print("  Ctrl+C to stop. Every call is logged to ~/.collie/mcpserve-audit.log")
    try:
        while True:
            time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
    print("collie mcp · stopped · %d call(s) served, %d rejected" % (mcp.calls, mcp.rejected))
    return 0


def cmd_state(args) -> int:
    ex = _ex()
    s = ex.state
    action = args.state_action
    if action == "render":
        files = s.render_views(profile_lines=ex._profile_lines())
        for k, v in files.items():
            print("%s -> %s" % (k, v))
        return 0
    if action == "path":
        print(s.path); return 0
    if action == "seed-demo":
        from . import demo_seed
        out = demo_seed.seed(s, ex, _sauna(), connect_sauna=args.connect)
        print(json.dumps(out, ensure_ascii=False)); return 0
    if action == "reset-demo":
        from . import demo_seed
        print(json.dumps(demo_seed.reset(s), ensure_ascii=False)); return 0
    if action == "learn":
        print(json.dumps([w["name"] for w in ex.workflows.learn_from_history()], ensure_ascii=False)); return 0
    if action == "activity":
        for a in s.recent_activity(limit=args.limit):
            print("%s [%s] %s" % (time.strftime("%m-%d %H:%M", time.localtime(a["at"])), a["actor"], a["summary"]))
        return 0
    print("unknown state action"); return 2


def cmd_demo(args) -> int:
    """Prepare, verify, or reset the isolated interview demonstration."""
    from . import demo_ready
    if args.demo_action == "prepare":
        return demo_ready.prepare(state_dir=args.state_dir, port=args.port,
                                  launch=not args.no_launch, desktop=not args.app_only)
    if args.demo_action == "check":
        return demo_ready.check(state_dir=args.state_dir)
    return demo_ready.reset()


# ------------------------------------------------------------------------------ parsers
def add_parsers(sub) -> None:
    pt = sub.add_parser("today", help="the executive view: upcoming, goals, tasks, suggestions, recent")
    pt.add_argument("query", nargs="?", default="", help="focus the answer, e.g. 'Sauna interview'")
    pt.add_argument("--json", action="store_true")
    pt.set_defaults(fn=cmd_today)

    pn = sub.add_parser("note", help="save a note into personal state: collie note \"text\"")
    pn.add_argument("text", nargs="*")
    pn.add_argument("--title", default="")
    pn.add_argument("--project", default="")
    pn.add_argument("--goal", default="")
    pn.add_argument("--append-to", dest="append_to", default="", help="title of an existing note to append to")
    pn.add_argument("--decision", action="store_true", help="also record it as a decision (and in memory)")
    pn.set_defaults(fn=cmd_note)

    pk = sub.add_parser("task", help="tasks: ls | add \"title\" | done <title|id> | focus <title|id> | drop <title|id>")
    pk.add_argument("task_action", choices=["ls", "add", "done", "focus", "drop"])
    pk.add_argument("title", nargs="*")
    pk.add_argument("--goal", default="")
    pk.add_argument("--project", default="")
    pk.add_argument("--all", action="store_true", help="ls: include done/dropped")
    pk.set_defaults(fn=cmd_task)

    pj = sub.add_parser("journal", help="the AI-maintained journal (today by default)")
    pj.add_argument("day", nargs="?", default="", help="YYYY-MM-DD")
    pj.add_argument("--build", action="store_true", help="(re)compress the day's activity into the entry")
    pj.add_argument("--week", action="store_true", help="also print this week's roll-up")
    pj.add_argument("--json", action="store_true")
    pj.set_defaults(fn=cmd_journal)

    psa = sub.add_parser("sauna", help="the person-level layer: status | connect | disconnect | sync | context | devices | handoff | export | restore | route")
    psa.add_argument("sauna_action", choices=["status", "connect", "disconnect", "sync", "context", "devices", "handoff",
                                              "export", "restore", "route", "push"])
    psa.add_argument("query", nargs="*")
    psa.add_argument("--account", default="")
    psa.add_argument("--path", default="")
    psa.add_argument("--forget", action="store_true", help="disconnect: also delete the cloud copy")
    psa.add_argument("--transport", default="browser", choices=["browser", "email"],
                     help="push: browser drives your signed-in Sauna; email reports the address to forward to")
    psa.add_argument("--wait", default="60", help="push: seconds to wait for Sauna's reply")
    psa.add_argument("--json", action="store_true")
    psa.set_defaults(fn=cmd_sauna)

    pms = sub.add_parser("mcp-serve", help="offer this Collie's personal state to a cloud (e.g. Sauna) as a "
                                           "read-only MCP server; --tunnel gets a public URL")
    pms.add_argument("--port", type=int, default=8789)
    pms.add_argument("--name", default="collie")
    pms.add_argument("--tunnel", action="store_true", help="open a public HTTPS tunnel via cloudflared")
    pms.add_argument("--allow-writes", dest="allow_writes", action="store_true",
                     help="also expose the write tools: add/complete a task, save a note, and ask the owner "
                          "to run something (the last one never executes on its own)")
    pms.add_argument("--rotate", action="store_true",
                     help="mint a new secret first, invalidating every URL already given out")
    pms.set_defaults(fn=cmd_mcpserve)

    pst = sub.add_parser("state", help="personal state plumbing: render | path | activity | learn | seed-demo | reset-demo")
    pst.add_argument("state_action", choices=["render", "path", "activity", "learn", "seed-demo", "reset-demo"])
    pst.add_argument("--limit", type=int, default=30)
    pst.add_argument("--connect", action="store_true", help="seed-demo: also connect Sauna (prototype)")
    pst.set_defaults(fn=cmd_state)

    pdemo = sub.add_parser("demo", help="prepare a clean, isolated Collie × Sauna interview demo")
    pdemo.add_argument("demo_action", nargs="?", default="prepare",
                       choices=["prepare", "check", "reset"])
    pdemo.add_argument("--state-dir", default="",
                       help="use this isolated profile (default: a new ~/.collie/demos/interview-* profile)")
    pdemo.add_argument("--port", type=int, default=8878,
                       help="preferred loopback port; a free port is chosen if busy")
    pdemo.add_argument("--no-launch", action="store_true",
                       help="seed and validate data without replacing or opening native surfaces")
    pdemo.add_argument("--app-only", action="store_true",
                       help="open the isolated native app without replacing the ambient desktop")
    pdemo.set_defaults(fn=cmd_demo)


def _ago(ts) -> str:
    if not ts:
        return "never"
    d = int(time.time()) - int(ts)
    if d < 90:
        return "%ds ago" % d
    if d < 5400:
        return "%dm ago" % (d // 60)
    if d < 172800:
        return "%dh ago" % (d // 3600)
    return "%dd ago" % (d // 86400)
