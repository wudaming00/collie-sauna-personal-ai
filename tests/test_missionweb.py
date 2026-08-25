"""Pin MissionService (harness.missionweb) — the NL-front-door service behind
`collie web`'s mission commands. Deterministic ($0): a scripted decider stands in
for the model, so this tests the goal-in / status-out marshalling and the
confirm/resume plumbing, not the model.

Run: python tests/test_missionweb.py   (exit 0 = all green)
"""
import json
import os
import sys
import tempfile
import threading
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.jobs import (clear_registry, NEEDS_YOU, WAITING, DONE_ACCEPTED, FAILED_S,
                          PAUSED, CANCELLED, QUEUED, RECONCILING, DONE_VERIFIED,
                          RECOVERY_REQUIRED)  # noqa: E402
from harness.missionweb import MissionService  # noqa: E402
from harness.tasktree import TaskTreeStore  # noqa: E402
from harness.verifier import Verdict, VERIFIED  # noqa: E402

_fails = []


@pytest.fixture(autouse=True)
def _fail_pytest_on_new_check_failures():
    """Make every script-style ``check`` failure fail its collected pytest test."""
    start = len(_fails)
    yield
    failures = _fails[start:]
    if failures:
        pytest.fail("; ".join(failures))


def check(cond, msg):
    if not cond:
        _fails.append(msg)
        print("  FAIL:", msg)


class Scripted:
    def __init__(self, decisions):
        self.decisions, self.i = list(decisions), 0

    def __call__(self, goal, case, primitives):
        if self.i >= len(self.decisions):
            return {"action": "done", "reason": "end"}
        d = self.decisions[self.i]
        self.i += 1
        return d


R = {"action": "research", "args": {"query": "price"}, "reason": "price"}
C = {"action": "compose", "args": {"facts": "car"}, "reason": "draft"}
P = {"action": "web.submit", "args": {"what": "listing"}, "reason": "publish"}
H = {"action": "needs_human", "args": {"summary": "buyer ready"}, "reason": "hand off"}
W = {"action": "wait", "args": {"seconds": 3600}, "reason": "later"}


def _svc(decisions):
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    clear_registry()
    # a scripted decider is a controlled scenario -> force the canned stub primitives
    # (independent of whatever real provider the host env has configured).
    return MissionService(base=p, decider=Scripted(decisions), stub=True)


def test_start_gate_confirm_handoff():
    print("test_start_gate_confirm_handoff")
    svc = _svc([R, C, P, H])
    st = svc.start("sell my car", autonomous=False)
    check(st["state"] == QUEUED, "start persists and returns the id before model work")
    st = svc.run(st["mission_id"])

    check(st["state"] == NEEDS_YOU, f"publish should park (needs_you), got {st['state']}")
    check(st["case"].get("researched") and st["case"].get("composed"),
          "reversible steps ran and show in the returned case")
    check("_case" not in st["case"], "the injected _case context is stripped from the UI payload")
    check(st["inbox"] and st["inbox"]["capability"] == "web.submit",
          "a Confirm item is surfaced for the parked publish")
    check(st["needs_human"] is False, "a gated confirm is not a hand-off")

    mid, nonce = st["mission_id"], st["inbox"]["nonce"]
    st2 = svc.confirm(mid, nonce)
    check(st2["state"] == NEEDS_YOU and st2["needs_human"] is True,
          "after confirm+publish it hands off to the human")
    check(st2["case"].get("submitted") is True, "publish fired after confirm")
    check(any(r["capability"] == "web.submit" and r["fired"] for r in st2["receipts"]),
          "the publish receipt is attributed to this mission")

    st3 = svc.accept(mid)
    check(st3["state"] == DONE_ACCEPTED, "accept explicitly takes over the hand-off")
    svc.close()


def test_bad_confirm_is_soft_error():
    print("test_bad_confirm_is_soft_error")
    svc = _svc([R, C, P, H])
    st = svc.start("sell my car", autonomous=False)
    st = svc.run(st["mission_id"])
    out = svc.confirm(st["mission_id"], "not-a-real-nonce")
    check("error" in out and st["state"] == NEEDS_YOU,
          "a bad nonce returns a soft error, not a crash, and leaves the mission parked")
    svc.close()


def test_missions_listing():
    print("test_missions_listing")
    svc = _svc([R, C, P, H])
    svc.start("sell my car", autonomous=True)
    ms = svc.missions()
    check(len(ms) == 1 and ms[0]["goal"] == "sell my car", "the mission is listed for the UI")
    svc.close()


def test_plain_mission_uses_saved_autonomy_default():
    print("test_plain_mission_uses_saved_autonomy_default")
    svc = _svc([])
    with patch("harness.settings.get", return_value="smart"):
        hands_off = svc.start("run the campaign")
    with patch("harness.settings.get", return_value="review"):
        review = svc.start("draft before publishing")
    check(svc.store.get(hands_off["mission_id"]).leash["irreversible"] == "allow",
          "plain Mission uses saved Hands-off mode")
    check(svc.store.get(review["mission_id"]).leash["irreversible"] == "confirm",
          "plain Mission uses saved Review mode")
    svc.close()


def test_pause_resume_check_and_cancel():
    print("test_pause_resume_check_and_cancel")
    svc = _svc([W, H])
    st = svc.start("watch for a reply", autonomous=True)
    st = svc.run(st["mission_id"]); mid = st["mission_id"]
    check(st["state"] == WAITING and "check" in st["controls"], "waiting is manageable")
    check(svc.pause(mid)["state"] == PAUSED, "pause is durable")
    check(svc.tick(now=10**11).get("advanced") == 0,
          "daemon does not consume a paused wake")
    check(svc.resume(mid)["state"] == WAITING, "resume restores waiting")
    check(svc.check(mid)["state"] == NEEDS_YOU, "check now wakes only this mission")
    check(svc.cancel(mid)["state"] == CANCELLED, "cancel is terminal")
    check(svc.cancel(mid)["state"] == CANCELLED, "cancel is idempotent")
    svc.close()


def test_wrong_mission_nonce_and_cancelled_nonce_are_refused():
    print("test_wrong_mission_nonce_and_cancelled_nonce_are_refused")
    svc = _svc([P])
    one = svc.start("publish one", autonomous=False)
    one = svc.run(one["mission_id"])
    nonce = one["inbox"]["nonce"]
    two = svc.start("publish two", autonomous=False)
    bad = svc.confirm(two["mission_id"], nonce)
    check("error" in bad and svc.actions.get(nonce).state == "pending",
          "a nonce cannot be confirmed through another mission id")
    killed = svc.cancel(one["mission_id"])
    check(killed["state"] == CANCELLED and svc.actions.get(nonce).state == "refused",
          "cancel revokes the parked action")
    check("error" in svc.confirm(one["mission_id"], nonce),
          "a cancelled nonce remains unusable")
    svc.close()


def test_read_surfaces_do_not_require_a_provider():
    print("test_read_surfaces_do_not_require_a_provider")
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    svc = MissionService(base=p, provider="")
    st = svc.start("persist only", autonomous=False)
    check(st["state"] == QUEUED and len(svc.missions()) == 1,
          "create/status/list work without constructing a model provider")
    svc.close()


def test_fresh_install_can_queue_before_connecting_a_provider():
    print("test_fresh_install_can_queue_before_connecting_a_provider")
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    unavailable = ValueError(
        "Auto found no currently authenticated model; connect a provider or choose one explicitly")
    with patch("harness.router.resolve_run_decision", side_effect=unavailable):
        svc = MissionService(base=p, provider="auto")
        st = svc.start("persist before setup", autonomous=False)
    pending = st.get("case", {}).get("brain_route_pending", {})
    check(st["state"] == QUEUED and pending.get("requested_provider") == "auto",
          "a fresh install queues ordinary work and records that its route is pending")
    svc.close()


def test_human_assist_can_continue_without_ending_the_mission():
    print("test_human_assist_can_continue_without_ending_the_mission")
    svc = _svc([H, {"action": "done", "reason": "finished after MFA"}])
    st = svc.start("finish signup", autonomous=False)
    st = svc.run(st["mission_id"]); mid = st["mission_id"]
    check(st["needs_human"] and "continue" in st["controls"],
          "a temporary human hand-off offers continue separately from accept")
    resumed = svc.continue_after_human(mid, "MFA completed")
    check(resumed["state"] == QUEUED and
          resumed["case"]["human_updates"][-1]["note"] == "MFA completed",
          "human completion note is durable and returns control to Collie")
    reported = svc.run(mid)
    check(reported["state"] == NEEDS_YOU and reported["needs_human"],
          "Collie continues after the human assist but its done self-report awaits review")
    check(svc.accept(mid)["state"] == DONE_ACCEPTED,
          "the user explicitly accepts reported completion")
    svc.close()


def test_mock_provider_never_fakes_durable_progress():
    print("test_mock_provider_never_fakes_durable_progress")
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    svc = MissionService(base=p, provider="mock")
    created = svc.start("real-world task", autonomous=False)
    out = svc.run(created["mission_id"])
    check(out["state"] == QUEUED and "error" in out,
          "the canned mock provider cannot advance a durable real-world mission")
    svc.close()


def test_refused_parked_action_does_not_deadlock_the_mission():
    print("test_refused_parked_action_does_not_deadlock_the_mission")
    svc = _svc([P, H])
    st = svc.start("publish safely", autonomous=False)
    st = svc.run(st["mission_id"]); mid, nonce = st["mission_id"], st["inbox"]["nonce"]
    assert svc.actions.refuse(nonce, "approval expired")
    repaired = svc.status(mid)
    check(repaired["inbox"] is None and repaired["needs_human"],
          "refused/expired parked action becomes a replannable hand-off")
    continued = svc.continue_after_human(mid, "re-prepare a fresh target")
    check(continued["state"] == QUEUED,
          "stale awaiting row no longer makes continue/accept impossible")
    svc.close()


def test_reconcile_wrong_state_has_no_side_effects():
    print("test_reconcile_wrong_state_has_no_side_effects")
    svc = _svc([P])
    st = svc.start("publish safely", autonomous=False)
    st = svc.run(st["mission_id"]); mid, nonce = st["mission_id"], st["inbox"]["nonce"]
    before = svc.actions.get(nonce)
    out = svc.reconcile(mid, "this is not a recovery state")
    after = svc.actions.get(nonce)
    check("error" in out and out["state"] == NEEDS_YOU,
          "reconcile is rejected outside recovery_required")
    check(before.state == after.state == "pending" and svc.status(mid)["inbox"],
          "a rejected reconcile does not revoke or detach the pending action")
    svc.close()


def test_reconcile_fences_cleanup_before_requeue():
    print("test_reconcile_fences_cleanup_before_requeue")
    svc = _svc([])
    st = svc.start("recover safely", autonomous=False); mid = st["mission_id"]
    nonce = svc.actions.propose(
        "web.submit", {"what": "old"}, job_id=mid, leash_id=mid)
    svc.store.set_state(mid, RECOVERY_REQUIRED, "inspect first")
    seen = []
    original = svc.actions.refuse

    def inspect_fence(*args, **kwargs):
        seen.append(svc.store.get(mid).state)
        return original(*args, **kwargs)

    svc.actions.refuse = inspect_fence
    out = svc.reconcile(mid, "receipts checked")
    check(seen == [RECONCILING] and out["state"] == QUEUED and
          svc.actions.get(nonce).state == "refused",
          "cross-database cleanup runs behind a persistent non-runnable fence")
    svc.close()


def test_stale_reconciler_cannot_revoke_a_fresh_action():
    print("test_stale_reconciler_cannot_revoke_a_fresh_action")
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    one = MissionService(base=p, decider=Scripted([]), stub=True)
    two = MissionService(base=p, decider=Scripted([]), stub=True)
    st = one.start("recover without crossing generations"); mid = st["mission_id"]
    old = one.actions.propose(
        "web.submit", {"what": "old"}, job_id=mid, leash_id=mid)
    one.store.set_state(mid, RECOVERY_REQUIRED, "old run crashed")

    entered, release = threading.Event(), threading.Event()
    original_begin = one.store.begin_reconcile

    def stalled_begin(mission_id, note="", lease_s=300):
        token = original_begin(mission_id, note, lease_s=0)
        entered.set(); release.wait(3)
        return token

    one.store.begin_reconcile = stalled_begin
    stale_result = []
    t = threading.Thread(
        target=lambda: stale_result.append(one.reconcile(mid, "old inspection")))
    t.start(); check(entered.wait(1), "first reconciler acquired its cleanup lease")
    winner = two.reconcile(mid, "take over expired cleanup")
    check(winner["state"] == QUEUED and two.actions.get(old).state == "refused",
          "replacement owner safely completed the old cleanup")

    leash = two.store.get(mid).leash
    fresh_run = two.store.claim_run(mid)
    ok, _why, _retry = two.store.reserve_action(
        mid, "fresh-key", True, leash, "web.submit", {"fresh": True}, fresh_run)
    fresh = two.actions.propose(
        "web.submit", {"what": "fresh"}, job_id=mid, leash_id=mid)
    bound = two.store.bind_action_key(mid, "fresh-key", fresh, fresh_run)
    parked = two.store.park_for_confirm(
        mid, fresh_run, "web.submit", fresh, "fresh confirmation")
    release.set(); t.join(3)
    key = two.store.db.execute(
        "SELECT state FROM mission_action_keys WHERE mission_id=? AND nonce=?",
        (mid, fresh)).fetchone()
    check(ok and bound and parked and stale_result and "error" in stale_result[0] and
          two.actions.get(fresh).state == "pending" and key is not None and
          two.store.last_parked(mid)[1] == fresh,
          "expired cleanup owner cannot revoke or detach a post-recovery action")
    one.close(); two.close()


def test_reconcile_resolves_old_inbox_but_preserves_executed_key():
    print("test_reconcile_resolves_old_inbox_but_preserves_executed_key")
    svc = _svc([P])
    st = svc.start("publish exactly once", autonomous=False)
    st = svc.run(st["mission_id"]); mid, nonce = st["mission_id"], st["inbox"]["nonce"]
    svc.actions.confirm(nonce)
    svc.actions.execute(
        nonce, side_effect_fn=lambda _r: {"submitted": True},
        donecheck_fn=lambda _r, _x: Verdict(VERIFIED, "done"))
    svc.store.set_state(mid, RECOVERY_REQUIRED, "crashed before folding receipt")
    out = svc.reconcile(mid, "receipt and external target inspected")
    row = svc.store.db.execute(
        "SELECT state FROM mission_action_keys WHERE mission_id=? AND nonce=?",
        (mid, nonce)).fetchone()
    check(out["state"] == QUEUED and svc.store.last_parked(mid)[1] is None and
          row is not None,
          "reconcile retires the stale inbox while retaining executed idempotency")
    reported = svc.run(mid)
    check(reported["state"] == NEEDS_YOU and not reported["action_in_flight"] and
          "accept" in reported["controls"],
          "old executed inbox cannot permanently suppress completion review")
    svc.close()


def test_reconcile_clears_only_unmaterialized_reserved_keys():
    print("test_reconcile_clears_only_unmaterialized_reserved_keys")
    svc = _svc([])
    st = svc.start("recover reservation crash", autonomous=False,
                   max_irreversible_actions=1)
    mid = st["mission_id"]
    leash = svc.store.get(mid).leash
    old_run = svc.store.claim_run(mid)
    ok, _why, _retry = svc.store.reserve_action(
        mid, "orphan-key", True, leash, "web.submit", {"old": True}, old_run)
    with svc.store._lock:
        svc.store.db.execute(
            "UPDATE missions SET state=?,run_token='',lease_until=0 WHERE mission_id=?",
            (RECOVERY_REQUIRED, mid))
        svc.store.db.commit()
    out = svc.reconcile(mid, "no matching action or external effect exists")
    fresh_run = svc.store.claim_run(mid)
    again, _why2, _retry2 = svc.store.reserve_action(
        mid, "orphan-key", True, leash, "web.submit", {"new": True}, fresh_run)
    check(ok and out["state"] == QUEUED and again,
          "an empty reservation returns both its key and max-action quota")
    svc.close()


def test_reconcile_releases_a_previously_refused_materialized_key():
    print("test_reconcile_releases_a_previously_refused_materialized_key")
    svc = _svc([])
    st = svc.start("retry an action proven not fired", autonomous=False); mid = st["mission_id"]
    leash = svc.store.get(mid).leash
    old_run = svc.store.claim_run(mid)
    ok, _why, _retry = svc.store.reserve_action(
        mid, "refused-key", True, leash, "web.submit", {"old": True}, old_run)
    nonce = svc.actions.propose(
        "web.submit", {"what": "old"}, job_id=mid, leash_id=mid)
    bound = svc.store.bind_action_key(mid, "refused-key", nonce, old_run)
    refused = svc.actions.refuse(nonce, "old worker lost ownership before firing")
    with svc.store._lock:
        svc.store.db.execute(
            "UPDATE missions SET state=?,run_token='',lease_until=0 WHERE mission_id=?",
            (RECOVERY_REQUIRED, mid))
        svc.store.db.commit()
    out = svc.reconcile(mid, "refusal proves the action never fired")
    key = svc.store.db.execute(
        "SELECT 1 FROM mission_action_keys WHERE mission_id=? AND action_key=?",
        (mid, "refused-key")).fetchone()
    fresh_run = svc.store.claim_run(mid)
    retried, _why2, _retry2 = svc.store.reserve_action(
        mid, "refused-key", True, leash, "web.submit", {"new": True}, fresh_run)
    check(ok and bound and refused and out["state"] == QUEUED and key is None and retried,
          "reconcile releases exact REFUSED/EXPIRED nonces without touching uncertain ones")
    svc.close()


def test_status_never_releases_an_executed_action_key():
    print("test_status_never_releases_an_executed_action_key")
    svc = _svc([P])
    st = svc.start("publish exactly once", autonomous=False)
    st = svc.run(st["mission_id"]); mid, nonce = st["mission_id"], st["inbox"]["nonce"]
    svc.actions.confirm(nonce)
    svc.actions.execute(
        nonce, side_effect_fn=lambda _r: {"submitted": True},
        donecheck_fn=lambda _r, _x: Verdict(VERIFIED, "done"))
    raced = svc.status(mid)
    row = svc.store.db.execute(
        "SELECT state FROM mission_action_keys WHERE mission_id=? AND nonce=?",
        (mid, nonce)).fetchone()
    check(raced["action_in_flight"] and raced["controls"] == ["cancel"],
          "a stale NEEDS_YOU view surfaces an uncertain/in-flight action conservatively")
    check(row and row["state"] == "executed",
          "status preserves and hardens the semantic key after ActionStore execution")
    svc.close()


def test_reconcile_waits_for_old_execution_latch():
    print("test_reconcile_waits_for_old_execution_latch")
    svc = _svc([P])
    st = svc.start("publish after crash", autonomous=False)
    st = svc.run(st["mission_id"]); mid, nonce = st["mission_id"], st["inbox"]["nonce"]
    svc.actions.confirm(nonce)
    old_run = svc.store.claim_run(mid, expected=(NEEDS_YOU,))
    resource, execution_token = svc.store.claim_execution(nonce, mid, old_run)
    entered, release = __import__("threading").Event(), __import__("threading").Event()

    def old_side_effect(_rec):
        entered.set(); release.wait(2); return {"submitted": True}

    thread = __import__("threading").Thread(target=lambda: svc.actions.execute(
        nonce, side_effect_fn=old_side_effect,
        donecheck_fn=lambda _r, _x: Verdict(VERIFIED, "done")))
    thread.start(); check(entered.wait(1), "old worker reached EXECUTING")
    with svc.store._lock:
        svc.store.db.execute(
            "UPDATE missions SET state=?,run_token='',lease_until=0 WHERE mission_id=?",
            (RECOVERY_REQUIRED, mid))
        svc.store.db.commit()
    blocked = svc.reconcile(mid, "site inspected while old worker was still live")
    row = svc.store.db.execute(
        "SELECT state FROM mission_action_keys WHERE mission_id=? AND nonce=?",
        (mid, nonce)).fetchone()
    check(blocked["state"] == RECONCILING and "still executing" in blocked["error"],
          "reconcile remains fenced while an old side effect is live")
    check(svc.actions.get(nonce).state == "executing" and row is not None and
          svc.store.active_resources(mid),
          "live action key and execution/resource latch are preserved")
    release.set(); thread.join(2)
    svc.store.release_resource(resource, mid, execution_token)
    done = svc.reconcile(mid, "old worker settled; receipt inspected")
    row2 = svc.store.db.execute(
        "SELECT state FROM mission_action_keys WHERE mission_id=? AND nonce=?",
        (mid, nonce)).fetchone()
    check(done["state"] == QUEUED and row2 is not None,
          "retry completes only after the old action settles, without deleting its key")
    svc.close()


def test_terminal_takeover_can_return_to_a_deduplicated_successor():
    print("test_terminal_takeover_can_return_to_a_deduplicated_successor")
    svc = _svc([P, H])
    old = svc.start("finish the launch", autonomous=True)
    old = svc.run(old["mission_id"]); old_mid = old["mission_id"]
    check(old["summary"]["completed"] and old["summary"]["current"],
          "Mission status contains a deterministic current-task summary")
    accepted = svc.accept(old_mid)
    check(accepted["state"] == DONE_ACCEPTED and "continue" in accepted["controls"],
          "terminal takeover clearly offers a return-to-Collie recovery")
    successor = svc.continue_after_human(old_mid, "continue the remaining launch work")
    inherited = svc.store.db.execute(
        "SELECT action_key,state FROM mission_action_keys WHERE mission_id=?",
        (successor["mission_id"],)).fetchall()
    check(successor["state"] == QUEUED and successor["mission_id"] != old_mid and
          svc.store.get(old_mid).state == DONE_ACCEPTED,
          "returning creates a queued successor without rewriting terminal audit history")
    check(inherited and all(row["state"] not in ("reserved", "materialized")
                            for row in inherited),
          "the successor inherits completed semantic keys so fired work cannot repeat")
    repeated = svc.continue_after_human(
        old_mid, "a duplicate browser request must converge")
    check(repeated.get("mission_id") == successor.get("mission_id"),
          "duplicate return-to-Collie requests reuse one durable successor")
    svc.close()


def test_unfinished_successor_setup_cannot_be_reconciled_around_fences():
    print("test_unfinished_successor_setup_cannot_be_reconciled_around_fences")
    svc = _svc([])
    mid = svc.start("retry exactly once", autonomous=True)["mission_id"]
    svc.store.set_state(mid, FAILED_S, "synthetic failure")
    child = "msn_crash_window"
    svc.store.create_successor_once(
        mid, "retry", child, "retry exactly once", case={"_retry_of": mid},
        expected_state=FAILED_S)
    blocked = svc.reconcile(child, "generic recovery must not publish this row")
    check(blocked["state"] == RECONCILING and not blocked.get("controls") and
          "original retry command" in blocked.get("error", ""),
          "generic reconcile cannot bypass unfinished successor setup")
    resumed = svc.retry(mid, "resume the interrupted setup")
    check(resumed.get("mission_id") == child and resumed.get("state") == QUEUED,
          "the original predecessor transition safely resumes and publishes the same child")
    svc.close()


def test_successor_workspace_binding_failure_is_resumable():
    print("test_successor_workspace_binding_failure_is_resumable")
    svc = _svc([])
    with tempfile.TemporaryDirectory() as workspace:
        mid = svc.start("resume successor setup", autonomous=True)["mission_id"]
        case = dict(svc.store.get(mid).case)
        case["_isolated_workspace"] = workspace
        svc.store.set_case(mid, case)
        svc.store.set_state(mid, FAILED_S, "synthetic failure")
        with patch.object(svc, "_ensure_agent_root", return_value=""):
            first = svc.retry(mid, "first binding attempt")
        child = first.get("mission_id")
        check(child and first.get("state") == RECONCILING and
              svc.store.successor_setup(child),
              "a failed workspace bind leaves the unique successor safely resumable")
        resumed = svc.retry(mid, "binding service recovered")
        check(resumed.get("mission_id") == child and resumed.get("state") == QUEUED,
              "a later predecessor command completes the same successor setup")
    svc.close()


def test_verification_conflict_recovery_gets_a_fresh_tasktree_root():
    print("test_verification_conflict_recovery_gets_a_fresh_tasktree_root")
    with tempfile.TemporaryDirectory() as td:
        workspace = os.path.join(td, "repo")
        os.makedirs(workspace)
        tree = TaskTreeStore(os.path.join(td, "tasktree.db"))
        svc = MissionService(
            base=os.path.join(td, "svc"), decider=Scripted([]), stub=True,
            run_tree=tree)
        started = svc.start(
            "finish every required branch", autonomous=True, may=["code"])
        mid = started["mission_id"]
        svc.create_run_tree(
            mid, [{"kind": "file", "id": workspace, "mode": "write"}],
            workspace=workspace)
        original = svc.store.get(mid)
        old_run_id = original.case["_run_id"]
        svc.store.record_event(
            mid, "goal_verification", "done",
            payload={"verdict": VERIFIED,
                     "evidence": [{"channel": "test", "ok": True, "at": 1}]})
        svc.store.set_state(mid, DONE_VERIFIED, "initially complete")
        check(svc._sync_terminal_mission_tree(mid) and
              tree.get(old_run_id)["status"] == "completed",
              "the predecessor root is terminal before integrity recovery")
        case = dict(svc.store.get(mid).case)
        case["_campaign_coverage"] = [
            {"branch": "remaining", "status": "pending", "required": True}]
        svc.store.set_case(mid, case)
        conflicted = svc.status(mid)
        check(conflicted["state"] == RECOVERY_REQUIRED,
              "late contradictory coverage demotes the previous green state")
        recovered = svc.reconcile(mid, "complete the missing branch")
        successor = svc.store.get(recovered.get("mission_id"))
        new_run_id = (successor.case or {}).get("_run_id") if successor else ""
        check(recovered.get("state") == QUEUED and successor and
              successor.mission_id != mid and new_run_id and new_run_id != old_run_id,
              "integrity recovery creates a queued audit successor with fresh authority")
        check(tree.get(old_run_id)["status"] == "completed" and
              tree.get(new_run_id)["status"] not in
              ("completed", "failed", "cancelled"),
              "the old terminal TaskTree remains immutable while the new root can run")
        repeated = svc.reconcile(mid, "duplicate recovery request")
        check(repeated.get("mission_id") == successor.mission_id,
              "duplicate verification recovery requests reuse one successor")
        svc.close()
        tree.close()


def test_progress_report_is_structured_stable_and_redacted():
    print("test_progress_report_is_structured_stable_and_redacted")
    svc = _svc([])
    started = svc.start("publish a careful product launch", autonomous=True)
    mid = started["mission_id"]
    case = dict(svc.store.get(mid).case)
    case["private_token"] = "must-never-enter-report"
    case["_campaign_coverage"] = [
        {"branch": "X launch", "status": "completed", "required": True,
         "summary": "Public launch receipt verified", "updated_at": 10},
        {"branch": "Reddit launch", "status": "blocked", "required": True,
         "summary": "Current route is ineligible", "blocker_kind": "policy",
         "updated_at": 20},
    ]
    case["pending_authorizations"] = [{
        "id": "auth_one", "domain": "example.com", "kind": "account_identity",
        "summary": "Choose an authorized work identity", "blocking": False,
    }]
    svc.store.set_case(mid, case)
    svc.store.record_event(
        mid, "result", "browse", payload={"verdict": "verified",
                                            "reason": "rules inspected"})

    status = svc.status(mid)
    report = status["report"]
    encoded = json.dumps(report)
    check(report["format_version"] == 1 and report["mission_id"] == mid,
          "status exposes a versioned Mission progress report")
    check(report["coverage"]["completed"] == 1 and
          report["coverage"]["open"] == 1 and
          report["coverage"]["branches"][1]["blocker_kind"] == "policy",
          "the report distinguishes completed coverage from an open blocked branch")
    check(report["needs_you"][0]["blocking"] is False and
          report["log"][-1]["summary"] == "rules inspected",
          "the report contains non-blocking asks and the compact activity ledger")
    check("must-never-enter-report" not in encoded and "private_token" not in encoded and
          "## Channel coverage" in report["markdown"],
          "the integration report omits raw case values and includes copyable Markdown")
    check(svc.report(mid) == report,
          "the dedicated integration report matches the status report revision")
    svc.close()


def test_verified_state_fails_closed_when_coverage_remains_open():
    print("test_verified_state_fails_closed_when_coverage_remains_open")
    svc = _svc([])
    started = svc.start("finish every required launch branch", autonomous=True)
    mid = started["mission_id"]
    case = dict(svc.store.get(mid).case)
    case["_campaign_coverage"] = [
        {"branch": "GitHub", "status": "completed", "required": True},
        {"branch": "Indie Hackers", "status": "pending", "required": True},
    ]
    svc.store.set_case(mid, case)
    svc.store.set_state(mid, DONE_VERIFIED, "legacy false-green result")

    status = svc.status(mid)
    check(status["state"] == RECOVERY_REQUIRED,
          "contradictory verified state is migrated to recovery_required")
    check(status["integrity"]["verification_conflict"] and
          "coverage" in status["integrity"]["reason"],
          "status explains the verification integrity conflict")
    check(any(event["kind"] == "integrity" and
              event["name"] == "verification_conflict"
              for event in status["recent_events"]),
          "the fail-closed migration leaves a durable integrity event")
    listed = {row["mission_id"]: row for row in svc.missions()}[mid]
    check(listed["state"] == RECOVERY_REQUIRED and
          listed["integrity"]["verification_conflict"],
          "Mission index cannot reintroduce the false-green state")
    svc.close()


def test_verified_state_requires_well_formed_independent_evidence():
    print("test_verified_state_requires_well_formed_independent_evidence")
    for suffix, event in (
            ("missing", None),
            ("empty", {}),
            ("string-ok", {"verdict": VERIFIED, "evidence": [
                {"channel": "test", "at": 1, "ok": "true"}]})):
        svc = _svc([])
        mid = svc.start("prove the whole outcome %s" % suffix, autonomous=True)["mission_id"]
        if event is not None:
            svc.store.record_event(
                mid, "goal_verification", "done", payload=event)
        svc.store.set_state(mid, DONE_VERIFIED, "synthetic legacy green")
        status = svc.status(mid)
        check(status["state"] == RECOVERY_REQUIRED and
              status["integrity"]["verification_conflict"],
              "verified state fails closed for %s goal evidence" % suffix)
        svc.close()


def test_verified_state_never_tail_drops_required_work():
    print("test_verified_state_never_tail_drops_required_work")
    scenarios = {
        "coverage": {
            "_campaign_coverage": ([{
                "branch": "oldest-open", "status": "pending", "required": True,
            }] + [{
                "branch": "complete-%02d" % i, "status": "completed",
                "required": True,
            } for i in range(40)]),
        },
        "authorization": {
            "pending_authorizations": [{
                "id": "auth-%02d" % i, "summary": "approval %02d" % i,
            } for i in range(21)],
        },
        "followup": {
            "pending_followups": [{
                "id": "followup-%02d" % i, "branch": "branch-%02d" % i,
                "due_at": 9999999999,
            } for i in range(21)],
        },
    }
    reason_words = {"coverage": "coverage", "authorization": "authorization",
                    "followup": "follow-up"}
    for name, case in scenarios.items():
        svc = _svc([])
        mid = svc.start("retain every required %s" % name, autonomous=True,
                        case=case)["mission_id"]
        svc.store.record_event(
            mid, "goal_verification", "done",
            payload={"verdict": VERIFIED,
                     "evidence": [{"channel": "test", "ok": True, "at": 1}]})
        svc.store.set_state(mid, DONE_VERIFIED, "synthetic legacy green")
        status = svc.status(mid)
        check(status["state"] == RECOVERY_REQUIRED and
              reason_words[name] in status["integrity"]["reason"],
              "verified state retains and blocks on oversized %s work" % name)
        svc.close()


def test_pending_work_writers_do_not_evict_older_obligations():
    print("test_pending_work_writers_do_not_evict_older_obligations")
    svc = _svc([])
    mid = svc.start("retain every pending obligation", autonomous=True)["mission_id"]
    token = svc.store.claim_run(mid)
    driver = svc._driver()
    for i in range(21):
        mission = svc.store.get(mid)
        routed = driver._handle_authorization(
            mid, token, mission,
            {"kind": "account", "domain": "site-%02d.example" % i,
             "operation": "authorize", "summary": "approval %02d" % i,
             "blocking": False}, "authorization required")
        check(routed == "_continue", "distinct deferred authorization remains nonblocking")
    for i in range(21):
        mission = svc.store.get(mid)
        routed = driver._handle_wait(
            mid, token, mission,
            {"branch": "watch-%02d" % i, "seconds": 3600,
             "summary": "watch branch %02d" % i}, "monitor")
        check(routed == "_continue", "distinct follow-up remains independently scheduled")
    case = svc.store.get(mid).case
    check(len(case.get("pending_authorizations") or []) == 21 and
          len(case.get("pending_followups") or []) == 21,
          "the 21st pending item no longer evicts the first durable obligation")
    svc.close()


def test_mission_index_uses_batch_overview_not_full_status_per_row():
    print("test_mission_index_uses_batch_overview_not_full_status_per_row")
    svc = _svc([])
    ids = [svc.start("overview mission %d" % i, autonomous=True)["mission_id"]
           for i in range(12)]

    def forbidden(_mid):
        raise AssertionError("Mission index called full status()")

    svc.status = forbidden
    listed = svc.missions()
    check({row["mission_id"] for row in listed} == set(ids),
          "batch overview returns every root Mission without N full status calls")
    check(all(row.get("summary") and row.get("controls") for row in listed),
          "batch overview retains actionable summaries and lifecycle controls")
    svc.close()


def test_mission_index_fails_closed_before_detail_is_opened():
    print("test_mission_index_fails_closed_before_detail_is_opened")
    svc = _svc([])
    mid = svc.start("never publish a false green overview", autonomous=True)["mission_id"]
    svc.store.set_state(mid, DONE_VERIFIED, "legacy green without evidence")
    listed = {row["mission_id"]: row for row in svc.missions()}[mid]
    check(listed["state"] == RECOVERY_REQUIRED and
          listed["integrity"]["verification_conflict"],
          "Mission index demotes contradictory green state before detail status runs")
    svc.close()


def test_mission_index_is_bounded_and_cursor_stable():
    print("test_mission_index_is_bounded_and_cursor_stable")
    svc = _svc([])
    for i in range(45):
        svc.start("paged Mission %02d" % i, autonomous=True)
    first = svc.missions(limit=20)
    second = svc.missions(limit=20, before=svc.mission_cursor(first[-1]))
    first_ids = {row["mission_id"] for row in first}
    second_ids = {row["mission_id"] for row in second}
    check(len(first) == 20 and len(second) == 20 and not first_ids & second_ids,
          "Mission overview uses a bounded, non-overlapping cursor page")
    try:
        svc.missions(limit=20, before="not-a-valid-cursor")
        invalid_refused = False
    except ValueError:
        invalid_refused = True
    check(invalid_refused, "malformed Mission cursors fail closed")
    svc.close()


def test_mission_cursor_uses_pre_integrity_page_bucket():
    print("test_mission_cursor_uses_pre_integrity_page_bucket")
    svc = _svc([])
    ids = []
    for suffix in ("x", "y", "z"):
        mid = "msn_cursor_" + suffix
        svc.store.create(mid, "cursor " + suffix,
                         leash={"may": ["research"]}, case={})
        ids.append(mid)
    svc.store.set_state(ids[0], DONE_ACCEPTED, "accepted")
    svc.store.set_state(ids[1], DONE_VERIFIED, "legacy green without evidence")
    svc.store.set_state(ids[2], DONE_ACCEPTED, "accepted")
    first = svc.missions(limit=2, include_cursors=True)
    check([row["mission_id"] for row in first] == [ids[2], ids[1]] and
          first[-1]["state"] == RECOVERY_REQUIRED,
          "the page-tail verified row is demoted after its terminal page snapshot")
    second = svc.missions(limit=2, before=first[-1]["_page_cursor"])
    check([row["mission_id"] for row in second] == [ids[0]],
          "the next page neither duplicates nor skips rows after bucket migration")
    svc.close()


def test_failed_retry_retires_only_stale_reversible_execution():
    print("test_failed_retry_retires_only_stale_reversible_execution")
    svc = _svc([])
    st = svc.start("recover a failed browser preparation", autonomous=True); mid = st["mission_id"]
    original_case = dict(svc.store.get(mid).case)
    original_case["_campaign_coverage"] = [
        {"branch": "DEV Community launch", "status": "pending", "required": True}]
    original_case["pending_authorizations"] = [
        {"id": "auth_email", "kind": "missing_fact", "domain": "medium.com"}]
    svc.store.set_case(mid, original_case)
    with svc.store._lock:
        svc.store.db.execute("UPDATE missions SET state=?,run_token='',lease_until=0 WHERE mission_id=?",
                             (FAILED_S, mid))
        svc.store.db.commit()
    old = svc.actions.propose("browse", {"goal": "inspect"}, risk="read", job_id=mid)
    svc.actions.confirm(old)
    with svc.actions._lock:
        svc.actions.db.execute(
            "UPDATE pending_actions SET state='executing',attempted_at=? WHERE nonce=?",
            (1, old))
        svc.actions.db.commit()
    retried = svc.retry(mid, "independent inspection proved no final submit fired")
    receipt = svc.actions.receipts(old)
    check(not retried.get("error") and retried.get("mission_id") != mid,
          "a stale reversible latch no longer dead-ends an ordinary failed-Mission retry")
    successor_case = svc.store.get(retried["mission_id"]).case
    check(successor_case.get("_campaign_coverage") == original_case["_campaign_coverage"] and
          successor_case.get("pending_authorizations") ==
          original_case["pending_authorizations"],
          "failed-Mission retry preserves durable coverage and authorization contracts")
    check(svc.actions.get(old).state == "executed" and receipt and
          receipt[-1]["verdict"] == "inconclusive" and receipt[-1]["fired"],
          "retiring the stale latch leaves an honest inconclusive fired receipt")
    repeated = svc.retry(mid, "the same retry request was delivered twice")
    check(repeated.get("mission_id") == retried.get("mission_id"),
          "repeating retry returns the same durable successor")

    blocked_mid = svc.start("never retire an uncertain publish", autonomous=True)["mission_id"]
    with svc.store._lock:
        svc.store.db.execute("UPDATE missions SET state=?,run_token='',lease_until=0 WHERE mission_id=?",
                             (FAILED_S, blocked_mid))
        svc.store.db.commit()
    dangerous = svc.actions.propose(
        "browse.submit", {"button": "Post"}, risk="publish", job_id=blocked_mid)
    svc.actions.confirm(dangerous)
    with svc.actions._lock:
        svc.actions.db.execute(
            "UPDATE pending_actions SET state='executing',attempted_at=? WHERE nonce=?",
            (1, dangerous))
        svc.actions.db.commit()
    blocked = svc.retry(blocked_mid, "do not guess")
    check("outcome is uncertain" in blocked.get("error", "") and
          svc.actions.get(dangerous).state == "executing",
          "a stale consequential latch still blocks retry and remains untouched")
    svc.close()


def test_failed_retry_is_idempotent_under_concurrent_requests():
    print("test_failed_retry_is_idempotent_under_concurrent_requests")
    svc = _svc([])
    mid = svc.start("one successor only", autonomous=True)["mission_id"]
    svc.store.set_state(mid, FAILED_S, "synthetic failure")
    barrier = threading.Barrier(3)
    results = []

    def retry():
        barrier.wait()
        results.append(svc.retry(mid).get("mission_id"))

    threads = [threading.Thread(target=retry) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(5)
    check(len(results) == 2 and results[0] and len(set(results)) == 1,
          "concurrent retry requests converge on one successor")
    successors = [m for m in svc.store.list()
                  if (m.case or {}).get("_retry_of") == mid]
    check(len(successors) == 1 and successors[0].state == QUEUED,
          "exactly one retry successor becomes runnable")
    svc.close()


def test_retry_inherits_action_fired_before_mission_key_projection():
    print("test_retry_inherits_action_fired_before_mission_key_projection")
    svc = _svc([])
    mid = svc.start("never publish twice after a crash", autonomous=True)["mission_id"]
    token = svc.store.claim_run(mid)
    mission = svc.store.get(mid)
    action_key = "semantic-publish-once"
    reserved, reason, _wake = svc.store.reserve_action(
        mid, action_key, True, mission.leash, "web.submit",
        {"target": "launch"}, token)
    check(reserved and not reason, "predecessor semantic action key is reserved")
    nonce = svc.actions.propose(
        "web.submit", {"target": "launch"}, risk="publish",
        job_id=mid, leash_id=mid)
    check(svc.store.bind_action_key(mid, action_key, nonce, token),
          "ActionStore nonce is bound into the Mission key ledger")
    svc.actions.confirm(nonce)
    svc.actions.execute(
        nonce, side_effect_fn=lambda _record: {"published": True},
        donecheck_fn=lambda _record, _result: Verdict(
            VERIFIED, "external launch receipt verified"))
    # Simulate process death after ActionStore atomically wrote EXECUTED+receipt,
    # but before MissionDriver.complete_action_key projected that fact.
    with svc.store._lock:
        svc.store.db.execute(
            "UPDATE missions SET state=?,run_token='',lease_until=0 WHERE mission_id=?",
            (FAILED_S, mid))
        svc.store.db.commit()
    retried = svc.retry(mid, "resume after the projection crash")
    child = retried.get("mission_id")
    inherited = svc.store.db.execute(
        "SELECT state,nonce FROM mission_action_keys WHERE mission_id=? "
        "AND action_key=?", (child, action_key)).fetchone()
    check(retried.get("state") == QUEUED and inherited and
          inherited["nonce"] == nonce and inherited["state"] == VERIFIED,
          "the fired receipt repairs and inherits the predecessor semantic fence")
    child_token = svc.store.claim_run(child)
    duplicate, duplicate_reason, _wake = svc.store.reserve_action(
        child, action_key, True, svc.store.get(child).leash, "web.submit",
        {"target": "launch"}, child_token)
    check(not duplicate and "duplicate external action" in duplicate_reason,
          "the successor cannot reserve the already-fired publish key")
    svc.close()


def test_successor_status_reports_campaign_budget_usage():
    print("test_successor_status_reports_campaign_budget_usage")
    svc = _svc([])
    started = svc.start(
        "finish within one campaign budget", autonomous=True,
        max_model_tokens=10, max_model_calls=10)
    predecessor = started["mission_id"]
    token = svc.store.claim_run(predecessor)
    check(bool(token) and svc.store.account_runtime(
        predecessor, token, input_tokens=7, model_calls=1),
        "the predecessor records campaign usage before failure")
    check(svc.store.finish_run(predecessor, token, FAILED_S, "retry safely"),
          "the predecessor enters the retryable terminal state")
    child = svc.retry(predecessor, "continue remaining work").get("mission_id")
    status = svc.status(child)
    check(status.get("aggregate_runtime", {}).get("input_tokens") == 0,
          "the successor execution subtree remains a distinct local view")
    check(status.get("budget_root_mission_id") == predecessor and
          status.get("budget_runtime", {}).get("input_tokens") == 7 and
          status.get("budget_runtime", {}).get("model_calls") == 1,
          "status explains the cumulative predecessor budget charged to the successor")
    svc.close()


def main():
    test_start_gate_confirm_handoff()
    test_bad_confirm_is_soft_error()
    test_missions_listing()
    test_plain_mission_uses_saved_autonomy_default()
    test_pause_resume_check_and_cancel()
    test_wrong_mission_nonce_and_cancelled_nonce_are_refused()
    test_read_surfaces_do_not_require_a_provider()
    test_fresh_install_can_queue_before_connecting_a_provider()
    test_human_assist_can_continue_without_ending_the_mission()
    test_mock_provider_never_fakes_durable_progress()
    test_refused_parked_action_does_not_deadlock_the_mission()
    test_reconcile_wrong_state_has_no_side_effects()
    test_reconcile_fences_cleanup_before_requeue()
    test_stale_reconciler_cannot_revoke_a_fresh_action()
    test_reconcile_resolves_old_inbox_but_preserves_executed_key()
    test_reconcile_clears_only_unmaterialized_reserved_keys()
    test_reconcile_releases_a_previously_refused_materialized_key()
    test_status_never_releases_an_executed_action_key()
    test_reconcile_waits_for_old_execution_latch()
    test_unfinished_successor_setup_cannot_be_reconciled_around_fences()
    test_successor_workspace_binding_failure_is_resumable()
    test_failed_retry_retires_only_stale_reversible_execution()
    test_failed_retry_is_idempotent_under_concurrent_requests()
    test_retry_inherits_action_fired_before_mission_key_projection()
    test_successor_status_reports_campaign_budget_usage()
    test_verification_conflict_recovery_gets_a_fresh_tasktree_root()
    test_progress_report_is_structured_stable_and_redacted()
    test_verified_state_fails_closed_when_coverage_remains_open()
    test_verified_state_requires_well_formed_independent_evidence()
    test_verified_state_never_tail_drops_required_work()
    test_pending_work_writers_do_not_evict_older_obligations()
    test_mission_index_uses_batch_overview_not_full_status_per_row()
    test_mission_index_fails_closed_before_detail_is_opened()
    test_mission_index_is_bounded_and_cursor_stable()
    test_mission_cursor_uses_pre_integrity_page_bucket()
    if _fails:
        print(f"\n{len(_fails)} FAILED")
        sys.exit(1)
    print("\nall green")


if __name__ == "__main__":
    main()
