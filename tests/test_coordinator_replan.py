import copy

import pytest

from autonomous_collab.config import load_settings
from autonomous_collab.experiments import VARIANTS, build_planner, run_variant
from autonomous_collab.models import InjectedEvent
from autonomous_collab.runner import run_mission
from helpers import check_invariants, get_scenario, plan_dicts, run


def events_of(result, kind):
    return [r for r in result.records if r["type"] == "event" and r["event_type"] == kind]


def states(result):
    return {t["task_id"]: t for t in result.tasks}


def with_events(sc, *events):
    return sc.model_copy(update={"events": [InjectedEvent(**e) for e in events]})


def charged(sc):
    """All agents fully charged and no battery jitter: isolates replanning logic from energy limits."""
    agents = [a.model_copy(update={"battery": 100.0}) for a in sc.agents]
    return sc.model_copy(update={"agents": agents, "randomization": sc.randomization.model_copy(update={"battery_jitter": 0.0})})


FAIL_NO_BLOCK = {"simulation": {"failed_agents_block_cell": False}}


def run_sc(sc, variant="D_mock_milp_replan", seed=0, overrides=None):
    sc = sc.model_copy(update={"settings_overrides": overrides or {}})
    return run_variant(sc, load_settings(), VARIANTS[variant], seed), sc


# --------------------------------------------------------------------- agent failure timing
@pytest.mark.parametrize("fail_event", [
    {"type": "AGENT_FAILED", "agent_id": "agent_1", "tick": 0},                       # before any task starts
    {"type": "AGENT_FAILED", "agent_id": "agent_1", "tick": 4},                       # while navigating
    {"type": "AGENT_FAILED", "agent_id": "agent_3", "when_agent_executing": "agent_3", "when_progress": 0.5},  # during execution
    {"type": "AGENT_FAILED", "agent_id": "agent_2", "tick": 60},                      # late, after most work is done
], ids=["before_start", "during_navigation", "during_execution", "late"])
def test_agent_failure_at_any_time_preserves_completed_work_and_recovers(fail_event):
    sc = with_events(charged(get_scenario("s0_nominal")), fail_event)
    res, sc2 = run_sc(sc, overrides=FAIL_NO_BLOCK)
    check_invariants(res, sc2)
    assert res.status == "COMPLETE", res.summary_lines()
    # each task has exactly one success (checked in invariants) and the failed agent never got new work after failing
    fired = events_of(res, "AGENT_FAILED")
    if fired:
        assert res.metrics["n_replans"] >= 1


def test_completed_tasks_are_never_reassigned_or_redone_after_replan():
    sc = with_events(charged(get_scenario("s0_nominal")), {"type": "AGENT_FAILED", "agent_id": "agent_1", "tick": 14})
    res, sc2 = run_sc(sc, overrides=FAIL_NO_BLOCK)
    replan_tick = next(r["tick"] for r in res.records if r["type"] == "replan")
    done_before = {r["task_id"]: r["agent_id"] for r in res.records if r["type"] == "task_transition" and r["to_state"] == "SUCCEEDED" and r["tick"] < replan_tick}
    assert done_before, "scenario must have completed work before the failure"
    after = [r for r in res.records if r["type"] == "task_transition" and r["tick"] >= replan_tick and r["task_id"] in done_before]
    assert after == []  # no transition at all touches a completed task
    for tid, agent in done_before.items():
        assert states(res)[tid]["agent_id"] == agent and states(res)[tid]["state"] == "SUCCEEDED"


def test_recovery_limited_by_battery_is_reported_and_the_monitor_gate_catches_it():
    """agent_3 (battery ~60) cannot absorb all of agent_1's work: the static MILP budget was optimistic,
    the SafetyMonitor's dispatch check rejected the over-long task, and the cause is reported."""
    sc = with_events(get_scenario("s0_nominal"), {"type": "AGENT_FAILED", "agent_id": "agent_1", "tick": 4})
    res, sc2 = run_sc(sc, overrides=FAIL_NO_BLOCK)
    check_invariants(res, sc2)
    assert res.status == "INCOMPLETE"
    assert any(r["type"] == "dispatch_rejected" and "BATTERY_RESERVE" in r["codes"] for r in res.records)
    reasons = [t["reason"] for t in res.tasks if t["state"] != "SUCCEEDED"]
    assert any("BATTERY_RESERVE" in r or "BATTERY_LIMIT" in r for r in reasons), reasons


def test_without_replanning_the_mission_stalls_and_says_why():
    sc = with_events(get_scenario("s0_nominal"), {"type": "AGENT_FAILED", "agent_id": "agent_1", "tick": 6})
    res, sc2 = run_sc(sc, variant="C_mock_milp", overrides=FAIL_NO_BLOCK)
    check_invariants(res, sc2)
    assert res.status == "INCOMPLETE" and not res.success and res.metrics["n_replans"] == 0
    reasons = " ".join(t["reason"] for t in res.tasks)
    assert "ASSIGNED_TO_UNAVAILABLE_AGENT" in reasons or "AGENT_FAILED" in reasons


def test_two_simultaneous_failures_are_debounced_into_one_replan():
    sc = with_events(get_scenario("s0_nominal"), {"type": "AGENT_FAILED", "agent_id": "agent_1", "tick": 10},
                     {"type": "AGENT_FAILED", "agent_id": "agent_2", "tick": 10})
    res, _ = run_sc(sc, overrides=FAIL_NO_BLOCK)
    replans = [r for r in res.records if r["type"] == "replan"]
    assert len(replans) == 1 and replans[0]["triggers"] == ["AGENT_FAILED"]
    assert [r["plan_version"] for r in replans] == [2]


def test_replan_interval_limits_replan_frequency():
    sc = with_events(get_scenario("s0_nominal"), {"type": "AGENT_FAILED", "agent_id": "agent_1", "tick": 10}, {"type": "AGENT_FAILED", "agent_id": "agent_2", "tick": 11})
    res, _ = run_sc(sc, overrides={**FAIL_NO_BLOCK, "simulation": {"failed_agents_block_cell": False, "min_replan_interval_ticks": 5}})
    ticks = [r["tick"] for r in res.records if r["type"] == "replan"]
    assert all(b - a >= 5 for a, b in zip(ticks, ticks[1:]))


def test_dispatch_is_frozen_between_event_and_replan():
    sc = with_events(get_scenario("s0_nominal"), {"type": "AGENT_FAILED", "agent_id": "agent_1", "tick": 10})
    res, _ = run_sc(sc, overrides={"simulation": {"failed_agents_block_cell": False, "min_replan_interval_ticks": 6}})
    replan = next(r for r in res.records if r["type"] == "replan")
    dispatches = [r for r in res.records if r["type"] == "task_transition" and r["to_state"] == "EXECUTING" and 10 <= r["tick"] < replan["tick"]]
    assert dispatches == []


# ------------------------------------------------------------------- uncertain tasks / retry policy
def manual_variant(sc, mutate):
    plan = copy.deepcopy(plan_dicts(sc))
    for p in plan:
        mutate(p)
    return sc.model_copy(update={"manual_plan": plan})


def test_verify_before_retry_is_logged_and_work_is_not_double_counted():
    res = run("s8_uncertain_task")
    check_invariants(res, get_scenario("s8_uncertain_task"))
    verifies = [r for r in res.records if r["type"] == "verify_before_retry"]
    assert verifies and all(v["completion_recorded"] is False for v in verifies)
    assert res.status == "COMPLETE"


def test_no_automatic_retry_leaves_interrupted_task_failed():
    sc = manual_variant(get_scenario("s8_uncertain_task"), lambda p: p.update(retry_policy="no_automatic_retry") if p["task_type"] == "environmental_reading" else None)
    res = run_variant(sc, load_settings(), VARIANTS["A_manual_greedy"].__class__("X", "manual", "milp", True), 0)
    denied = [r for r in res.records if r["type"] == "retry_denied"]
    assert denied and denied[0]["reason"] == "NO_AUTOMATIC_RETRY"
    assert res.status == "INCOMPLETE" and any(t["state"] == "FAILED" and t["reason"] == "NO_AUTOMATIC_RETRY" for t in res.tasks)


def test_failure_before_work_started_is_safe_to_retry_even_with_no_automatic_retry():
    sc = manual_variant(charged(get_scenario("s0_nominal")), lambda p: p.update(retry_policy="no_automatic_retry"))
    sc = with_events(sc, {"type": "AGENT_FAILED", "agent_id": "agent_1", "tick": 3})  # agent_1 is still travelling: no side effects yet
    sc = sc.model_copy(update={"settings_overrides": FAIL_NO_BLOCK})
    res = run_variant(sc, load_settings(), VARIANTS["A_manual_greedy"].__class__("X", "manual", "milp", True), 0)
    assert res.status == "COMPLETE" and not [r for r in res.records if r["type"] == "retry_denied"]


def test_attempts_are_bounded_when_every_execution_fails():
    sc = get_scenario("s0_nominal").model_copy(update={"settings_overrides": {"simulation": {"task_failure_probability": 1.0}}})
    res = run_variant(sc, load_settings(), VARIANTS["D_mock_milp_replan"], 0)
    assert res.status == "INCOMPLETE" and not res.success
    assert all(t["attempts"] <= 3 for t in res.tasks)
    assert any(t["reason"] == "RETRY_EXHAUSTED" for t in res.tasks)
    assert res.metrics["terminal_s"] < 600 * 5  # terminated by exhaustion, not by hitting max_ticks


# ------------------------------------------------------------------- new tasks, blocked routes
def test_new_task_is_validated_allocated_and_completed():
    res = run("s7_new_task")
    check_invariants(res, get_scenario("s7_new_task"))
    assert res.status == "COMPLETE" and states(res)["inspect_zone_D_visual"]["state"] == "SUCCEEDED"
    assert res.metrics["tasks_added"] == 1 and res.metrics["n_replans"] >= 1


def test_new_task_without_replanning_is_still_required_work_so_batch_baseline_does_not_claim_success():
    res = run("s7_new_task", variant="C_mock_milp")
    added = states(res)["inspect_zone_D_visual"]
    assert res.status == "INCOMPLETE" and not res.success and added["state"] == "PENDING" and added["reason"] == "NOT_DISPATCHED"
    assert res.metrics["tasks_added"] == 1 and res.metrics["n_replans"] == 0
    assert any(r["type"] == "event_ignored" for r in res.records)
    online = run("s7_new_task", variant="E_mock_nearest_replan")  # online allocators pick up ready tasks every cycle
    assert online.status == "COMPLETE"


@pytest.mark.parametrize("bad,code", [
    ({"target_zone": "Z9"}, "UNKNOWN_ZONE"),
    ({"depends_on": ["ghost"]}, "UNKNOWN_DEPENDENCY"),
    ({"task_type": "hack"}, "UNKNOWN_TASK_TYPE"),
    ({"task_id": "compile_report"}, "DUPLICATE_TASK_ID"),
    ({"priority": 99}, "PRIORITY_RANGE"),
    ({"assigned_agent": "agent_1"}, "SCHEMA_TYPE_ERROR"),
])
def test_invalid_added_tasks_are_rejected_and_mission_continues(bad, code):
    sc = get_scenario("s7_new_task")
    task = {**sc.events[0].task, **bad}
    sc = sc.model_copy(update={"events": [InjectedEvent(type="TASK_ADDED", tick=10, task=task)]})
    res = run_variant(sc, load_settings(), VARIANTS["D_mock_milp_replan"], 0)
    rejected = [r for r in res.records if r["type"] == "plan_rejected" and r["source"] == "task_added"]
    assert rejected and (code in rejected[0]["codes"] or code == "SCHEMA_TYPE_ERROR")
    assert res.status == "COMPLETE" and res.metrics["invalid_plan_rejections"] >= 1 and res.metrics["tasks_added"] == 0


def test_blocked_route_is_never_entered_after_the_block_and_mission_completes():
    sc = get_scenario("s2_blocked_route")
    res = run(sc)
    check_invariants(res, sc)
    assert res.status == "COMPLETE"
    entered = [r for r in res.records if r["type"] == "move" and r["tick"] > 3 and r["to_cell"] == [3, 7]]
    assert entered == []


def test_unreachable_zone_after_validation_is_reported_not_hidden():
    for variant in ("D_mock_milp_replan", "C_mock_milp", "E_mock_nearest_replan"):
        res = run("s11_unreachable_zone", variant=variant)
        assert res.status == "INCOMPLETE" and not res.success, variant
        c_tasks = [t for t in res.tasks if "zone_C" in t["task_id"]]
        assert any(t["state"] != "SUCCEEDED" for t in c_tasks)
        assert all(t["reason"] for t in res.tasks if t["state"] != "SUCCEEDED")
        assert res.metrics["tasks_not_succeeded"] >= 2  # at least the C visual and the report


def test_no_feasible_allocation_is_reported_with_reasons_and_nothing_moves():
    res = run("s6b_resources_insufficient")
    assert res.status == "INCOMPLETE" and not res.success
    assert res.metrics["travel_cells"] == 0 and res.metrics["n_succeeded"] == 0
    assert {t["state"] for t in res.tasks} == {"BLOCKED"} and {t["reason"] for t in res.tasks} == {"BATTERY_LIMIT"}


def test_loss_of_the_only_capable_agent_blocks_the_dependent_task_with_reason():
    res = run("s10_no_feasible_recovery")
    check_invariants(res, get_scenario("s10_no_feasible_recovery"))
    assert res.status == "INCOMPLETE"
    report = states(res)["compile_report"]
    assert report["state"] != "SUCCEEDED" and ("NO_AVAILABLE_AGENT" in report["reason"] or "BLOCKED_BY_DEPENDENCY" in report["reason"] or report["reason"])
    others = [t for t in res.tasks if t["task_id"] != "compile_report"]
    assert sum(t["state"] == "SUCCEEDED" for t in others) >= 4  # all feasible work is still completed


def test_low_battery_assignment_goes_to_capable_agent_with_enough_battery():
    res = run("s6a_low_battery_partial")
    check_invariants(res, get_scenario("s6a_low_battery_partial"))
    assert res.status == "COMPLETE"
    # agent_1 has battery 13, reserve 10 (budget 3.0): it can only take the short, cheap report, never a visual task
    assert not any(t["agent_id"] == "agent_1" and "visual" in t["task_id"] for t in res.tasks)
    energy_by_agent = sum(r["energy"] for r in res.records if r["type"] in ("move", "work_start") and r.get("agent_id") == "agent_1")
    assert energy_by_agent <= 3.0 + 1e-9
    assert res.metrics["dispatch_rejections"] == 0  # the allocator itself excluded infeasible pairs; the monitor never had to


def test_dispatch_rejection_without_replanning_blocks_task_instead_of_executing_it(settings):
    sc = get_scenario("s0_nominal")
    # tamper: assigned battery is fine at allocation time but agent_3 loses battery before dispatch via a huge reserve
    agents = [a.model_copy(update={"battery_reserve": 99.0}) if a.agent_id == "agent_3" else a for a in sc.agents]
    res = run_variant(sc.model_copy(update={"agents": agents}), settings, VARIANTS["B_mock_greedy"], 0)
    assert not any(r["type"] == "work_start" and r["agent_id"] == "agent_3" for r in res.records)
