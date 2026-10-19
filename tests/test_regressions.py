"""One test per defect discovered while building the system (see docs/PROJECT_STATUS.md)."""
import pytest

from autonomous_collab.models import InjectedEvent, TaskSpec
from autonomous_collab.config import load_settings
from autonomous_collab.experiments import VARIANTS, run_variant
from helpers import check_invariants, get_scenario, run
from test_safety_simulator import aspec, make_sim, run_ticks, spec


def test_r1_queued_assignment_does_not_make_agent_look_busy_to_the_monitor():
    """Defect: monitor 'idle' mixed allocation queue with physical state, so every dispatch was rejected."""
    res = run("s0_nominal", "A_manual_greedy")
    assert res.status == "COMPLETE" and res.metrics["dispatch_rejections"] == 0


@pytest.mark.parametrize("variant", ["C_mock_milp", "E_mock_nearest_replan", "F_mock_hungarian_replan", "B_mock_greedy"])
def test_r2_r3_r4_alcove_deadlock_and_livelock_are_resolved(variant):
    """Defects: idle agent boxed in a one-cell alcove by a visitor (deadlock); retreat/yield livelock; head-on moving deadlock."""
    sc = get_scenario("s0_nominal")
    for seed in range(10):
        res = run(sc, variant, seed)
        check_invariants(res, sc)
        assert res.status == "COMPLETE", (variant, seed, res.summary_lines())
        assert not [r for r in res.records if r["type"] == "event" and r["event_type"] == "DEADLOCK"]
        assert res.metrics["terminal_s"] < 1000


def test_r5_cleared_failed_agent_does_not_occupy_its_cell():
    """Defect: with failed_agents_block_cell=false the dead agent still blocked its cell in the occupancy map."""
    sc = get_scenario("s3_agent_failure")
    assert all(run(sc, "D_mock_milp_replan", seed).status == "COMPLETE" for seed in range(10))


def test_r6_failed_agent_inside_a_zone_blocks_the_zone_by_default_and_not_when_cleared():
    ev = [InjectedEvent(type="AGENT_FAILED", agent_id="agent_1", when_agent_executing="agent_1", when_task_id="inspect_zone_C_visual", when_progress=0.3)]
    base = get_scenario("s0_nominal")
    base = base.model_copy(update={"events": ev, "agents": [a.model_copy(update={"battery": 100.0}) for a in base.agents],
                                   "randomization": base.randomization.model_copy(update={"battery_jitter": 0.0})})
    blocked = run_variant(base, load_settings(), VARIANTS["D_mock_milp_replan"], 0)
    assert blocked.status == "INCOMPLETE" and any("NO_REACHABLE_PATH" in t["reason"] or "UNREACHABLE" in t["reason"] for t in blocked.tasks)
    cleared = run_variant(base.model_copy(update={"settings_overrides": {"simulation": {"failed_agents_block_cell": False}}}), load_settings(), VARIANTS["D_mock_milp_replan"], 0)
    assert cleared.status == "COMPLETE"


def test_r7_added_task_is_required_work_even_when_replanning_is_off():
    """Defect: with replanning off the injected task was silently dropped so the baseline 'succeeded'."""
    res = run("s7_new_task", "C_mock_milp")
    assert not res.success and any(t["task_id"] == "inspect_zone_D_visual" for t in res.tasks)


def test_r8_low_battery_event_is_emitted_exactly_once():
    """Defect: LOW_BATTERY was emitted twice (explicit emit + disable emit)."""
    sim, world, log = make_sim([aspec("a", (0, 0), battery=10.9, reserve=10.0)])
    sim.start_task("a", spec("t", zone="Z1"), world.shortest_path((0, 0), (0, 4)), 0)
    run_ticks(sim, 10)
    assert len([r for r in log.of_type("event") if r["event_type"] == "LOW_BATTERY"]) == 1


def test_r9_bool_is_not_accepted_as_integer_priority():
    with pytest.raises(Exception):
        TaskSpec.model_validate({"task_id": "t", "task_type": "visual_inspection", "target_zone": "A", "required_capabilities": ["visual_inspection"],
                                 "priority": True, "estimated_duration_s": 1, "acceptance_criteria": ["inspection_result_recorded"]})


def test_r10_exclusion_cause_is_preserved_in_final_reason():
    sc = get_scenario("s0_nominal").model_copy(update={"events": [InjectedEvent(type="AGENT_FAILED", agent_id="agent_1", tick=4)],
                                                      "settings_overrides": {"simulation": {"failed_agents_block_cell": False}}})
    res = run_variant(sc, load_settings(), VARIANTS["D_mock_milp_replan"], 0)
    assert any("EXCLUDED_AFTER_REJECTION(" in t["reason"] for t in res.tasks)


def test_r11_parallel_experiments_use_spawn_not_fork():
    """Defect: ProcessPoolExecutor with the default fork start method deadlocked on a multi-core Linux machine."""
    import inspect
    from autonomous_collab import experiments
    assert 'get_context("spawn")' in inspect.getsource(experiments.run_all)
