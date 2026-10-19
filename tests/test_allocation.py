import itertools
import math
import random
from dataclasses import replace

import pytest

from autonomous_collab.allocation import AllocationContext, make_allocator
from autonomous_collab.allocation.cost_model import CostModel, explain_unassigned, static_objective
from autonomous_collab.errors import ConfigError
from autonomous_collab.models import TaskSpec, V
from autonomous_collab.views import AgentView
from helpers import make_world

CAPS = {"v": "visual_inspection", "e": "environmental_reading", "r": "reporting"}
TYPES = {"v": "visual_inspection", "e": "environmental_reading", "r": "compile_report"}
CRIT = {"v": "inspection_result_recorded", "e": "reading_recorded", "r": "report_compiled"}


def task(tid, kind="v", zone="A", prio=3, dur=30, deps=()):
    return TaskSpec(task_id=tid, task_type=TYPES[kind], target_zone=zone, required_capabilities=[CAPS[kind]], depends_on=list(deps),
                    priority=prio, estimated_duration_s=dur, acceptance_criteria=[CRIT[kind]])


def agent(aid, caps="ver", pos=(0, 0), battery=100.0, reserve=10.0, **kw):
    return AgentView(aid, frozenset({"navigation"} | {CAPS[c] for c in caps}), pos, battery, reserve, 0.5, **kw)


@pytest.fixture()
def world():
    return make_world(["." * 12] * 8, {"HQ": (0, 0), "A": (0, 11), "B": (7, 6), "C": (7, 0), "D": (3, 5)})


@pytest.fixture()
def cm(world, settings):
    return CostModel(world, settings)


def ctx_of(cm, tasks, agents, **kw):
    return AllocationContext(tasks, agents, cm, **kw)


# ------------------------------------------------------------------ cost model / feasibility
def test_pair_feasibility_reasons(cm, world):
    t = task("t", "v", "A")
    assert cm.pair(agent("a"), t).feasible
    assert V.CAPABILITY_MISMATCH in cm.pair(agent("a", caps="e"), t).reasons
    assert V.AGENT_NOT_AVAILABLE in cm.pair(agent("a", available=False), t).reasons
    assert V.EXCLUDED_AFTER_REJECTION in cm.pair(agent("a"), t, excluded={"a"}).reasons
    assert V.BATTERY_RESERVE in cm.pair(agent("a", battery=12), t).reasons
    pc = cm.pair(agent("a", battery=12), t)
    assert not pc.feasible and pc.cost > 0  # cost still computed, but feasibility is a separate hard flag
    world.block_cell((0, 11))
    unreachable = cm.pair(agent("a"), t)
    assert V.NO_PATH in unreachable.reasons and math.isinf(unreachable.cost) and not unreachable.feasible


def test_cost_terms_are_nonnegative_monotone_and_normalised(cm):
    near, far = agent("n", pos=(0, 10)), agent("f", pos=(7, 0))
    t = task("t", "v", "A")
    pn, pf = cm.pair(near, t), cm.pair(far, t)
    assert all(v >= 0 for v in pn.parts.values()) and pn.cost < pf.cost
    assert pn.travel_cells < pf.travel_cells and pn.energy < pf.energy
    assert cm.pair(replace(near, committed_ticks=20), t).cost > pn.cost
    assert cm.pair(near, task("hp", "v", "A", prio=5)).parts["priority"] >= cm.pair(near, task("lp", "v", "A", prio=1)).parts["priority"]
    assert pn.cost < 10  # normalised terms: no component dominates by raw scale


def test_ignore_constraints_scores_infeasible_pairs_for_the_common_objective(cm):
    pc = cm.pair(agent("a", caps="e"), task("t", "v", "A"), ignore_constraints=True)
    assert pc.feasible is False or pc.cost > 0
    assert math.isfinite(pc.cost)
    objective = static_objective(cm, {"a": agent("a")}, {"t": task("t")}, {"t": "a", "ghost": "a"})
    assert objective == pytest.approx(cm.pair(agent("a"), task("t")).cost)


def test_explain_unassigned_precedence(cm):
    t = task("t", "v", "A")
    assert explain_unassigned(t, [agent("a", caps="e")], cm) == V.NO_CAPABLE_AGENT
    assert explain_unassigned(t, [agent("a", available=False)], cm) == V.NO_AVAILABLE_AGENT
    assert explain_unassigned(t, [agent("a")], cm, {"a"}) == V.EXCLUDED_AFTER_REJECTION
    assert explain_unassigned(t, [agent("a", battery=11)], cm) == V.BATTERY_LIMIT
    cm.world.block_cell((0, 11))
    assert explain_unassigned(t, [agent("a")], cm) == V.NO_REACHABLE_PATH


# ------------------------------------------------------------------ brute-force references
def brute_force_milp(cm, tasks, agents):
    """Enumerate every task->agent/unassigned map; lexicographic (max #assigned, min c + w_m * Z)."""
    best = None
    options = [[None] + [a for a in agents if cm.pair(a, t).feasible] for t in tasks]
    for combo in itertools.product(*options):
        load = {a.agent_id: a.committed_ticks for a in agents}
        energy = {a.agent_id: 0.0 for a in agents}
        total = 0.0
        for t, a in zip(tasks, combo):
            if a is None:
                continue
            pc = cm.pair(a, t)
            total += pc.cost
            load[a.agent_id] += pc.total_ticks
            energy[a.agent_id] += pc.energy
        if any(energy[a.agent_id] > a.battery - a.reserve + 1e-9 for a in agents):
            continue
        z = max(load.values()) / cm.t_ref
        key = (-sum(1 for a in combo if a is not None), total + cm.weights.makespan * z)
        if best is None or key < best:
            best = key
    return best


def random_instance(rng, n_tasks, n_agents):
    zones = ["A", "B", "C", "D", "HQ"]
    tasks = [task(f"t{i}", rng.choice("vve"), rng.choice(zones), prio=rng.randint(1, 5), dur=rng.choice([15, 30, 45])) for i in range(n_tasks)]
    agents = [agent(f"a{i}", caps=rng.choice(["ve", "v", "e", "ver"]), pos=(rng.randint(0, 7), rng.randint(0, 11)),
                    battery=rng.choice([100, 100, 40, 25]), committed_ticks=rng.choice([0, 0, 6])) for i in range(n_agents)]
    return tasks, agents


@pytest.mark.parametrize("seed", range(12))
def test_milp_matches_exhaustive_enumeration(cm, seed):
    rng = random.Random(seed)
    tasks, agents = random_instance(rng, rng.randint(2, 5), rng.randint(2, 3))
    res = make_allocator("milp").allocate(ctx_of(cm, tasks, agents))
    by_id = {a.agent_id: a for a in agents}
    load = {a.agent_id: a.committed_ticks for a in agents}
    total = 0.0
    for tid, aid in res.assignments.items():
        t = next(x for x in tasks if x.task_id == tid)
        pc = cm.pair(by_id[aid], t)
        assert pc.feasible
        total += pc.cost
        load[aid] += pc.total_ticks
    achieved = (-len(res.assignments), total + cm.weights.makespan * max(load.values()) / cm.t_ref)
    expected = brute_force_milp(cm, tasks, agents)
    assert achieved[0] == expected[0] and achieved[1] == pytest.approx(expected[1], abs=1e-4)


@pytest.mark.parametrize("seed", range(10))
def test_hungarian_is_optimal_for_the_selected_one_to_one_cycle(cm, seed):
    rng = random.Random(100 + seed)
    tasks, agents = random_instance(rng, rng.randint(2, 4), rng.randint(2, 4))
    agents = [replace(a, committed_ticks=0.0) for a in agents]
    res = make_allocator("hungarian").allocate(ctx_of(cm, tasks, agents))
    assert len(set(res.assignments.values())) == len(res.assignments)  # one-to-one
    ordered = sorted(tasks, key=lambda t: (-t.priority, t.task_id))
    cand = [t for t in ordered if any(cm.pair(a, t).feasible for a in agents)][: len(agents)]
    best = None
    for k in range(len(cand), 0, -1):
        for subset_assign in itertools.permutations(agents, k):
            pairs = list(zip(cand, subset_assign))
            if all(cm.pair(a, t).feasible for t, a in pairs):
                c = sum(cm.pair(a, t).cost for t, a in pairs)
                best = c if best is None or c < best else best
        if best is not None:
            break  # max feasible cardinality first, then min cost
    got = sum(cm.pair(next(a for a in agents if a.agent_id == aid), next(t for t in tasks if t.task_id == tid)).cost for tid, aid in res.assignments.items())
    if best is None:
        assert res.assignments == {}
    else:
        assert len(res.assignments) == k and got == pytest.approx(best, abs=1e-9)


# ------------------------------------------------------------------ hard constraints, all allocators
@pytest.mark.parametrize("name", ["greedy", "milp", "nearest", "hungarian"])
def test_no_allocator_ever_assigns_an_infeasible_pair(cm, name):
    rng = random.Random(5)
    for _ in range(40):
        tasks, agents = random_instance(rng, rng.randint(2, 6), rng.randint(1, 4))
        agents = [replace(a, idle=True) for a in agents]
        res = make_allocator(name).allocate(ctx_of(cm, tasks, agents))
        by_id = {a.agent_id: a for a in agents}
        for tid, aid in res.assignments.items():
            pc = cm.pair(by_id[aid], next(t for t in tasks if t.task_id == tid))
            assert pc.feasible, (name, tid, aid, pc.reasons)
        assert set(res.assignments) & set(res.unassigned) == set()


@pytest.mark.parametrize("name", ["greedy", "milp"])
def test_batch_allocators_assign_every_placeable_task_exactly_once(cm, name):
    tasks = [task("t1", "v", "A"), task("t2", "e", "B"), task("t3", "v", "C"), task("t4", "r", "HQ", deps=["t1"])]
    agents = [agent("a1", "vr", (0, 0)), agent("a2", "e", (7, 5)), agent("a3", "ver", (7, 11))]
    res = make_allocator(name).allocate(ctx_of(cm, tasks, agents))
    assert sorted(res.assignments) == ["t1", "t2", "t3", "t4"] and res.unassigned == {}
    assert res.assignments["t2"] in {"a2", "a3"} and res.assignments["t4"] in {"a1", "a3"}


@pytest.mark.parametrize("name", ["greedy", "milp", "nearest", "hungarian"])
def test_infeasible_tasks_reported_with_reason_not_assigned(cm, name):
    tasks = [task("ok", "v", "A"), task("nocap", "r", "HQ")]
    agents = [agent("a1", "v", (0, 9))]
    res = make_allocator(name).allocate(ctx_of(cm, tasks, agents))
    assert res.assignments == {"ok": "a1"}
    if make_allocator(name).mode == "batch":
        assert res.unassigned == {"nocap": V.NO_CAPABLE_AGENT}


def test_milp_respects_joint_energy_budget_and_workload_limit(cm):
    # each task costs 11 cells * 0.5 + 3 = 8.5 energy; battery 24 - reserve 10 = 14 => one task per agent, not two
    tasks = [task("t1", "v", "A"), task("t2", "v", "A")]
    a = [agent("a1", "v", (0, 0), battery=24), agent("a2", "v", (0, 0), battery=24)]
    res = make_allocator("milp").allocate(ctx_of(cm, tasks, a))
    assert sorted(res.assignments.values()) == ["a1", "a2"]  # spread: both can't go to one agent
    one = [agent("a1", "v", (0, 0), battery=24)]
    res = make_allocator("milp").allocate(ctx_of(cm, tasks, one))
    assert len(res.assignments) == 1 and list(res.unassigned.values()) == [V.BATTERY_LIMIT]
    res = make_allocator("milp").allocate(ctx_of(cm, tasks, [agent("a1", "v", (0, 0))], workload_limit_ticks=20))
    assert len(res.assignments) == 1 and list(res.unassigned.values()) == [V.WORKLOAD_LIMIT]


def test_milp_balances_load_through_the_makespan_term(cm):
    tasks = [task(f"t{i}", "v", "D", dur=60) for i in range(4)]
    agents = [agent("a1", "v", (3, 5)), agent("a2", "v", (3, 4))]
    res = make_allocator("milp").allocate(ctx_of(cm, tasks, agents))
    counts = sorted(list(res.assignments.values()).count(a) for a in ("a1", "a2"))
    assert counts == [2, 2]


def test_greedy_uses_virtual_battery_so_it_cannot_overcommit(cm):
    tasks = [task("t1", "v", "A"), task("t2", "v", "A"), task("t3", "v", "A")]
    res = make_allocator("greedy").allocate(ctx_of(cm, tasks, [agent("a1", "v", (0, 5), battery=20)]))  # 6.0 + 3.0 fits (reserve 10), a third 3.0 does not
    energy = sum(cm.pair(agent("a1", "v", (0, 5)), t).energy for t in tasks if t.task_id in res.assignments)
    assert len(res.assignments) == 2 and len(res.unassigned) == 1
    assert set(res.unassigned.values()) == {V.BATTERY_LIMIT}


def test_hungarian_does_not_use_penalty_pairs_even_when_they_are_numerically_cheapest(cm):
    t1, t2 = task("only_a1", "v", "A", prio=5), task("any", "e", "HQ", prio=1)
    a1 = agent("a1", "ve", (0, 11))
    a2 = agent("a2", "e", (0, 0))  # cannot do the visual task but sits on the cheap spot
    res = make_allocator("hungarian").allocate(ctx_of(cm, [t1, t2], [a1, a2]))
    assert res.assignments["only_a1"] == "a1" and res.assignments.get("any") == "a2"
    lone = make_allocator("hungarian").allocate(ctx_of(cm, [t1], [a2]))
    assert lone.assignments == {}


def test_nearest_picks_closest_feasible_ready_task(cm):
    tasks = [task("far", "v", "C"), task("near", "v", "A")]
    res = make_allocator("nearest").allocate(ctx_of(cm, tasks, [agent("a1", "v", (0, 10))]))
    assert res.assignments == {"near": "a1"}


@pytest.mark.parametrize("name", ["greedy", "milp", "nearest", "hungarian"])
def test_allocation_is_deterministic_including_ties(cm, name):
    tasks = [task("t1", "v", "D"), task("t2", "v", "D"), task("t3", "v", "D")]
    agents = [agent("a1", "v", (3, 4)), agent("a2", "v", (3, 4)), agent("a3", "v", (3, 4))]  # fully symmetric agents
    first = make_allocator(name).allocate(ctx_of(cm, tasks, agents)).assignments
    for _ in range(5):
        assert make_allocator(name).allocate(ctx_of(cm, list(reversed(tasks)), list(reversed(agents)))).assignments == first


def test_unknown_allocator_name():
    with pytest.raises(ConfigError):
        make_allocator("simulated_annealing")


def test_empty_inputs(cm):
    for name in ("greedy", "milp", "nearest", "hungarian"):
        res = make_allocator(name).allocate(ctx_of(cm, [], [agent("a1")]))
        assert res.assignments == {} and res.unassigned == {}
        res = make_allocator(name).allocate(ctx_of(cm, [task("t")], []))
        assert res.assignments == {}
