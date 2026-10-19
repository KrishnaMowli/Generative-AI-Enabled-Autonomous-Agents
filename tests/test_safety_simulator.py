import pytest

from autonomous_collab import executor
from autonomous_collab.config import load_settings
from autonomous_collab.errors import PreconditionError
from autonomous_collab.logging_utils import EventLog, canonical_trace
from autonomous_collab.models import AgentSpec, AgentStatus, TaskSpec, TaskState, V
from autonomous_collab.rng import stable_rng, stable_seed
from autonomous_collab.simulator import Simulator, detect_collisions
from autonomous_collab.validation.safety_monitor import MoveProposal, SafetyMonitor
from autonomous_collab.views import AgentView
from helpers import make_world

ROW = ["....."]
ZONES = {"HQ": (0, 0), "Z1": (0, 4), "Z2": (0, 2)}


def spec(tid="t", kind="visual_inspection", zone="Z1", deps=(), prio=3, dur=30):
    crit = {"visual_inspection": "inspection_result_recorded", "environmental_reading": "reading_recorded", "compile_report": "report_compiled"}[kind]
    cap = {"visual_inspection": "visual_inspection", "environmental_reading": "environmental_reading", "compile_report": "reporting"}[kind]
    return TaskSpec(task_id=tid, task_type=kind, target_zone=zone, required_capabilities=[cap], depends_on=list(deps), priority=prio,
                    estimated_duration_s=dur, acceptance_criteria=[crit])


def aspec(aid, start, battery=100.0, reserve=10.0, caps=("navigation", "visual_inspection", "environmental_reading", "reporting")):
    return AgentSpec(agent_id=aid, capabilities=list(caps), start=start, battery=battery, battery_reserve=reserve)


def make_sim(agents, rows=ROW, zones=None, seed=0, settings=None, monitor_cls=SafetyMonitor, **kw):
    settings = settings or load_settings()
    world = make_world(rows, zones or ZONES)
    log = EventLog()
    return Simulator(world, agents, settings, seed, log, monitor=monitor_cls(world, settings), **kw), world, log


def run_ticks(sim, n, start=0):
    events = []
    for t in range(start, start + n):
        events += sim.step(t)
    return events


def prop(aid, a, b, prio=3, battery=100.0, energy=0.5, version=1):
    return MoveProposal(aid, a, b, prio, battery, energy, version)


@pytest.fixture()
def monitor(settings):
    return SafetyMonitor(make_world(["....."], ZONES), settings)


# ---------------------------------------------------------------- move resolution
def test_simple_move_accepted(monitor):
    acc, rej = monitor.resolve_moves([prop("a", (0, 0), (0, 1))], {"a": (0, 0)})
    assert len(acc) == 1 and not rej


def test_same_destination_goes_to_higher_priority_then_agent_id(monitor):
    pos = {"a": (0, 0), "b": (0, 2)}
    acc, rej = monitor.resolve_moves([prop("a", (0, 0), (0, 1), 2), prop("b", (0, 2), (0, 1), 4)], pos)
    assert [p.agent_id for p in acc] == ["b"] and rej[0][1].code == V.NODE_RESERVED
    acc, _ = monitor.resolve_moves([prop("b", (0, 2), (0, 1), 3), prop("a", (0, 0), (0, 1), 3)], pos)
    assert [p.agent_id for p in acc] == ["a"]  # tie -> lower agent id


def test_edge_swap_is_rejected_for_both_agents(monitor):
    pos = {"a": (0, 0), "b": (0, 1)}
    acc, rej = monitor.resolve_moves([prop("a", (0, 0), (0, 1)), prop("b", (0, 1), (0, 0))], pos)
    assert acc == [] and {v.code for _, v in rej} == {V.EDGE_SWAP}


def test_occupied_destination_even_if_occupant_is_leaving_is_rejected(monitor):
    pos = {"a": (0, 0), "b": (0, 1)}
    _, rej = monitor.resolve_moves([prop("a", (0, 0), (0, 1))], pos)
    assert rej[0][1].code == V.NODE_OCCUPIED and rej[0][1].data["blocker"] == "b"
    acc, rej = monitor.resolve_moves([prop("a", (0, 0), (0, 1)), prop("b", (0, 1), (0, 2))], pos)
    assert [p.agent_id for p in acc] == ["b"] and rej[0][1].code == V.NODE_OCCUPIED  # no follow-the-leader


@pytest.mark.parametrize("p,code", [
    (prop("a", (0, 0), (0, 2)), V.ILLEGAL_MOVE),
    (prop("a", (0, 1), (0, 2)), V.ILLEGAL_MOVE),
    (prop("a", (0, 0), (0, 1), version=99), V.STALE_STATE),
    (prop("a", (0, 0), (0, 1), battery=0.2), V.BATTERY_EXHAUSTED),
    (prop("a", (0, 0), (0, -1)), V.ROUTE_BLOCKED),  # out of bounds = not passable
])
def test_unsafe_single_moves_rejected(monitor, p, code):
    _, rej = monitor.resolve_moves([p], {"a": (0, 0)})
    assert rej and rej[0][1].code == code


def test_blocked_cell_is_not_enterable(settings):
    world = make_world(["....."], ZONES)
    world.block_cell((0, 1))
    m = SafetyMonitor(world, settings)
    _, rej = m.resolve_moves([prop("a", (0, 0), (0, 1), version=world.version)], {"a": (0, 0)})
    assert rej[0][1].code == V.ROUTE_BLOCKED


def test_resolution_is_independent_of_proposal_order(monitor):
    pos = {"a": (0, 0), "b": (0, 2), "c": (0, 4)}
    props = [prop("a", (0, 0), (0, 1), 3), prop("b", (0, 2), (0, 1), 3), prop("c", (0, 4), (0, 3), 5)]
    ref = [p.agent_id for p in monitor.resolve_moves(props, pos)[0]]
    for perm in (props[::-1], [props[1], props[2], props[0]]):
        assert [p.agent_id for p in monitor.resolve_moves(perm, pos)[0]] == ref


# ---------------------------------------------------------------- dispatch checks
def view(**kw):
    base = dict(agent_id="a", capabilities=frozenset({"navigation", "visual_inspection"}), position=(0, 0), battery=100.0, reserve=10.0, energy_per_move=0.5)
    base.update(kw)
    return AgentView(**base)


def dispatch(monitor, t=None, v=None, deps=None, assigned=1, plan=1, path="auto"):
    t = t or spec()
    v = v or view()
    if path == "auto":
        path = monitor.world.shortest_path(v.position, monitor.world.zone_cell(t.target_zone))
    return {x.code for x in monitor.check_dispatch(t, v, deps or {}, assigned_version=assigned, plan_version=plan, path=path)}


def test_dispatch_checks_each_rule(monitor):
    assert dispatch(monitor) == set()
    assert V.CAPABILITY_MISMATCH in dispatch(monitor, v=view(capabilities=frozenset({"navigation"})))
    assert V.DEPENDENCY_NOT_SATISFIED in dispatch(monitor, t=spec(deps=["x"]), deps={"x": TaskState.EXECUTING})
    assert dispatch(monitor, t=spec(deps=["x"]), deps={"x": TaskState.SUCCEEDED}) == set()
    assert V.STALE_STATE in dispatch(monitor, assigned=1, plan=2)
    assert V.NO_PATH in dispatch(monitor, path=None)
    assert V.ROUTE_BLOCKED in dispatch(monitor, path=[(0, 0), (0, 3)])
    assert V.BATTERY_RESERVE in dispatch(monitor, v=view(battery=14))
    assert V.AGENT_NOT_AVAILABLE in dispatch(monitor, v=view(available=False))
    assert V.AGENT_NOT_AVAILABLE in dispatch(monitor, v=view(idle=False))


def test_action_allowlist(monitor):
    assert monitor.check_action("move", "") == [] and monitor.check_action("task", "visual_inspection") == []
    for kind, name in [("shell", "ls"), ("task", "rm_rf"), ("python", "eval"), ("task", "")]:
        assert monitor.check_action(kind, name)[0].code == V.ACTION_NOT_ALLOWED


def test_battery_reserve_boundary(monitor):
    # path 4 cells * 0.5 + 3.0 task energy = 5.0 -> exactly at reserve is allowed, below is not
    assert dispatch(monitor, v=view(battery=15.0)) == set()
    assert V.BATTERY_RESERVE in dispatch(monitor, v=view(battery=14.99))


# ---------------------------------------------------------------- independent collision audit
def test_detect_collisions():
    assert detect_collisions({"a": (0, 0), "b": (0, 2)}, {"a": (0, 1), "b": (0, 2)}) == []
    same = detect_collisions({"a": (0, 0), "b": (0, 2)}, {"a": (0, 1), "b": (0, 1)})
    assert same[0]["kind"] == "same_cell"
    swap = detect_collisions({"a": (0, 0), "b": (0, 1)}, {"a": (0, 1), "b": (0, 0)})
    assert any(c["kind"] == "edge_swap" for c in swap)
    assert detect_collisions({"a": (0, 0), "b": (0, 0)}, {"a": (0, 0), "b": (0, 0)})[0]["kind"] == "same_cell"


class ApproveAll(SafetyMonitor):
    def resolve_moves(self, proposals, positions):
        return list(proposals), []


def head_on(monitor_cls):
    sim, world, log = make_sim([aspec("a", (0, 0)), aspec("b", (0, 2))], monitor_cls=monitor_cls)
    sim.start_task("a", spec("ta", zone="Z1"), world.shortest_path((0, 0), (0, 4)), 0)
    sim.start_task("b", spec("tb", zone="HQ"), world.shortest_path((0, 2), (0, 0)), 0)
    run_ticks(sim, 12)
    return log


def test_audit_catches_collisions_when_the_monitor_is_bypassed():
    log = head_on(ApproveAll)
    assert log.of_type("collision"), "the independent audit must detect unsafe motion"


def test_real_monitor_prevents_all_collisions_in_the_same_situation():
    log = head_on(SafetyMonitor)
    assert not log.of_type("collision") and log.of_type("move_rejected")


# ---------------------------------------------------------------- simulator mechanics
def test_movement_energy_task_effect_and_postcondition():
    sim, world, log = make_sim([aspec("a", (0, 0))])
    sim.start_task("a", spec("t", dur=20), world.shortest_path((0, 0), (0, 4)), 0)
    events = run_ticks(sim, 12)
    done = [e for e in events if e.type == "TASK_COMPLETED"]
    assert len(done) == 1 and done[0].task_id == "t"
    a = sim.agents["a"]
    assert a.position == (0, 4) and a.status == AgentStatus.IDLE
    assert a.battery == pytest.approx(100 - 4 * 0.5 - 3.0)  # 4 moves + visual task energy
    assert sim.registry["t"]["zone"] == "Z1" and len(log.of_type("move")) == 4


def test_task_not_succeeded_until_work_finished():
    sim, world, _ = make_sim([aspec("a", (0, 0))])
    sim.start_task("a", spec("t", zone="HQ", dur=50), [(0, 0)], 0)  # already at the zone: work starts at once
    assert sim.agents["a"].status == AgentStatus.EXECUTING
    events = run_ticks(sim, 9)
    assert not [e for e in events if e.type == "TASK_COMPLETED"] and "t" not in sim.registry
    events = run_ticks(sim, 2, start=9)
    assert [e.type for e in events] == ["TASK_COMPLETED"]


def test_battery_never_negative_and_low_battery_disables_agent():
    sim, world, log = make_sim([aspec("a", (0, 0), battery=10.9, reserve=10.0)])
    sim.start_task("a", spec("t", zone="Z1"), world.shortest_path((0, 0), (0, 4)), 0)
    events = run_ticks(sim, 10)
    assert any(e.type == "LOW_BATTERY" for e in events)
    assert sim.agents["a"].status == AgentStatus.UNAVAILABLE and sim.agents["a"].battery >= 0
    assert world.blocked  # default: a disabled agent blocks its cell


def test_exhausted_battery_cannot_move_at_all():
    sim, world, _ = make_sim([aspec("a", (0, 0), battery=0.2, reserve=0.0)])
    sim.start_task("a", spec("t", zone="Z1"), world.shortest_path((0, 0), (0, 4)), 0)
    events = run_ticks(sim, 5)
    assert sim.agents["a"].position == (0, 0) and sim.agents["a"].battery >= 0
    assert any(e.type == "LOW_BATTERY" for e in events)


def test_agent_failure_blocks_cell_or_not_per_setting():
    sim, world, _ = make_sim([aspec("a", (0, 1)), aspec("b", (0, 3))])
    sim.fail_agent("a", 0)
    assert sim.agents["a"].status == AgentStatus.FAILED and not world.is_passable((0, 1)) and (0, 1) in sim.positions().values()
    settings = load_settings(overrides={"simulation": {"failed_agents_block_cell": False}})
    sim2, world2, _ = make_sim([aspec("a", (0, 1)), aspec("b", (0, 3))], settings=settings)
    sim2.fail_agent("a", 0)
    assert world2.is_passable((0, 1)) and "a" not in sim2.positions()
    assert sim.fail_agent("a", 1) == []  # idempotent


def test_failure_mid_task_reports_task_and_work_started_flag():
    sim, world, _ = make_sim([aspec("a", (0, 0))])
    sim.start_task("a", spec("t", zone="HQ", dur=50), [(0, 0)], 0)
    run_ticks(sim, 2)
    ev = sim.fail_agent("a", 2)[0]
    assert ev.type == "AGENT_FAILED" and ev.task_id == "t" and ev.payload["work_started"] is True


def test_unreachable_goal_after_world_change():
    sim, world, _ = make_sim([aspec("a", (0, 0))])
    sim.start_task("a", spec("t", zone="Z1"), world.shortest_path((0, 0), (0, 4)), 0)
    sim.step(0)
    world.block_cell((0, 2))
    events = run_ticks(sim, 2, start=1)
    assert any(e.type == "ROUTE_UNREACHABLE" and e.task_id == "t" for e in events) and sim.agents["a"].status == AgentStatus.IDLE


def test_start_task_guards_and_precondition_failure():
    sim, world, _ = make_sim([aspec("a", (0, 0))])
    sim.start_task("a", spec("t", zone="Z1"), world.shortest_path((0, 0), (0, 4)), 0)
    with pytest.raises(ValueError):
        sim.start_task("a", spec("u"), [(0, 4)], 0)
    sim2, _, _ = make_sim([aspec("a", (0, 0))])
    ev = sim2.start_task("a", spec("rep", "compile_report", "HQ", deps=["missing"]), [(0, 0)], 0)
    assert ev[0].type == "TASK_FAILED" and ev[0].payload["code"] == "PRECONDITION_FAILED"


def test_agents_must_start_on_distinct_cells():
    with pytest.raises(ValueError):
        make_sim([aspec("a", (0, 0)), aspec("b", (0, 0))])


def test_simulated_execution_failure_probability():
    settings = load_settings(overrides={"simulation": {"task_failure_probability": 1.0}})
    sim, _, _ = make_sim([aspec("a", (0, 0))], settings=settings)
    sim.start_task("a", spec("t", zone="HQ", dur=5), [(0, 0)], 0)
    events = run_ticks(sim, 3)
    assert events[-1].type == "TASK_FAILED" and events[-1].payload["code"] == "EXEC_FAILURE" and "t" not in sim.registry


def test_same_seed_same_trace_different_seed_different_results():
    def go(seed):
        sim, world, log = make_sim([aspec("a", (0, 0))], seed=seed, duration_jitter=0.3)
        sim.start_task("a", spec("e1", "environmental_reading", "HQ", dur=100), [(0, 0)], 0)
        run_ticks(sim, 40)
        return log, sim.registry["e1"]
    (l0, r0), (l0b, r0b), (l1, r1) = go(0), go(0), go(1)
    assert canonical_trace(l0.records) == canonical_trace(l0b.records) and r0 == r0b
    assert r0 != r1  # seeded readings differ between seeds


def test_duration_jitter_is_keyed_by_task_not_by_execution_order():
    def work_ticks(order):
        sim, world, log = make_sim([aspec("a", (0, 0)), aspec("b", (0, 4))], seed=3, duration_jitter=0.4)
        for aid, t in order:
            sim.start_task(aid, spec(t, zone="HQ" if aid == "a" else "Z1", dur=100), [sim.agents[aid].position], 0)
        return {r["task_id"]: r["work_ticks"] for r in log.of_type("work_start")}
    assert work_ticks([("a", "t1"), ("b", "t2")]) == work_ticks([("b", "t2"), ("a", "t1")])


def test_rng_streams_are_stable_and_independent():
    assert stable_seed(1, "a") == stable_seed(1, "a") != stable_seed(1, "b") != stable_seed(2, "b")
    assert stable_rng(5, "x").random() == stable_rng(5, "x").random()


# ---------------------------------------------------------------- executor
def test_executor_postconditions_reject_tampered_results():
    rng = stable_rng(0)
    reg = {}
    v = spec("v", zone="Z1")
    res = executor.run_handler(v, "a", rng, reg)
    assert executor.check_postcondition(v, res, reg)
    assert not executor.check_postcondition(v, {**res, "zone": "other"}, reg)
    assert not executor.check_postcondition(v, {}, reg)
    e = spec("e", "environmental_reading", "Z1")
    assert not executor.check_postcondition(e, {"zone": "Z1"}, reg)
    reg["v"] = res
    r = spec("r", "compile_report", "HQ", deps=["v"])
    rep = executor.run_handler(r, "a", rng, reg)
    assert executor.check_postcondition(r, rep, reg) and not executor.check_postcondition(r, {**rep, "n_inputs": 0}, reg)
    with pytest.raises(PreconditionError):
        executor.check_precondition(spec("r2", "compile_report", "HQ", deps=["nope"]), reg)
    assert set(executor.HANDLERS) == {"visual_inspection", "environmental_reading", "compile_report"}


def test_task_with_unsatisfied_postcondition_never_succeeds(monkeypatch):
    """A handler that returns a wrong/forged result must yield TASK_FAILED, not a recorded success."""
    monkeypatch.setitem(executor.HANDLERS, "visual_inspection", lambda s, a, r, reg: {"zone": "somewhere_else", "observation_id": "x"})
    sim, _, _ = make_sim([aspec("a", (0, 0))])
    sim.start_task("a", spec("t", zone="HQ", dur=5), [(0, 0)], 0)
    events = run_ticks(sim, 3)
    assert events[-1].type == "TASK_FAILED" and events[-1].payload["code"] == "POSTCONDITION_FAILED"
    assert "t" not in sim.registry and sim.agents["a"].status == AgentStatus.IDLE
