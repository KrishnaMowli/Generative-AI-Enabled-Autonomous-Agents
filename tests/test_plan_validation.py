import copy
import json

import pytest

from autonomous_collab.models import TaskSpec, V
from autonomous_collab.validation.schema_validator import extract_json, parse_candidate
from autonomous_collab.validation.semantic_validator import PlanValidator
from helpers import get_scenario, make_world, plan_dicts


@pytest.fixture()
def env(settings):
    sc = get_scenario("s0_nominal")
    from autonomous_collab.environment import GridWorld
    world = GridWorld.from_spec(sc.world)
    return sc, world, PlanValidator(world, sc.agents, settings, sc.required_outcomes)


def tasks_of(sc, mutate=None):
    plan = copy.deepcopy(plan_dicts(sc))
    if mutate:
        mutate(plan)
    return [TaskSpec.model_validate(p) for p in plan]


def codes(violations):
    return {v.code for v in violations}


def test_valid_reference_plan_is_accepted(env):
    sc, _, validator = env
    assert validator.validate_tasks(tasks_of(sc)) == []


def idx(plan, tid):
    return next(i for i, p in enumerate(plan) if p["task_id"] == tid)


CASES = [
    ("unknown_zone", lambda p: p[0].update(target_zone="Z9"), V.UNKNOWN_ZONE),
    ("unknown_type", lambda p: p[0].update(task_type="move_to_xy"), V.UNKNOWN_TASK_TYPE),
    ("unknown_capability", lambda p: p[0].update(required_capabilities=["telepathy"]), V.UNKNOWN_CAPABILITY),
    ("capability_type_mismatch", lambda p: p[0].update(required_capabilities=["reporting"]), V.CAPABILITY_TYPE_MISMATCH),
    ("priority_high", lambda p: p[0].update(priority=6), V.PRIORITY_RANGE),
    ("priority_low", lambda p: p[0].update(priority=0), V.PRIORITY_RANGE),
    ("duration_zero", lambda p: p[0].update(estimated_duration_s=0), V.DURATION_RANGE),
    ("duration_negative", lambda p: p[0].update(estimated_duration_s=-5), V.DURATION_RANGE),
    ("duration_huge", lambda p: p[0].update(estimated_duration_s=10**6), V.DURATION_RANGE),
    ("duration_inf", lambda p: p[0].update(estimated_duration_s=float("inf")), V.DURATION_RANGE),
    ("retry_invalid", lambda p: p[0].update(retry_policy="always"), V.RETRY_POLICY_INVALID),
    ("acceptance_unknown", lambda p: p[0].update(acceptance_criteria=["rm -rf /"]), V.ACCEPTANCE_UNRECOGNIZED),
    ("acceptance_empty", lambda p: p[0].update(acceptance_criteria=[]), V.ACCEPTANCE_UNRECOGNIZED),
    ("acceptance_wrong_for_type", lambda p: p[0].update(acceptance_criteria=["report_compiled"]), V.ACCEPTANCE_UNRECOGNIZED),
    ("duplicate_id", lambda p: p[1].update(task_id=p[0]["task_id"]), V.DUPLICATE_TASK_ID),
    ("bad_id_chars", lambda p: p[0].update(task_id="a b;rm"), V.TASK_ID_INVALID),
    ("unknown_dependency", lambda p: p[0].update(depends_on=["ghost"]), V.UNKNOWN_DEPENDENCY),
    ("self_dependency", lambda p: p[0].update(depends_on=[p[0]["task_id"]]), V.SELF_DEPENDENCY),
    ("two_cycle", lambda p: (p[0].update(depends_on=[p[1]["task_id"]]), p[1].update(depends_on=[p[0]["task_id"]])), V.DEPENDENCY_CYCLE),
    ("report_without_prereqs", lambda p: p[idx(p, "compile_report")].update(depends_on=[]), V.REPORT_DEPENDENCY_MISSING),
    ("report_missing_one_prereq", lambda p: p[idx(p, "compile_report")]["depends_on"].pop(), V.REPORT_DEPENDENCY_MISSING),
    ("task_depends_on_report", lambda p: p[0].update(depends_on=["compile_report"]), V.DEPENDENCY_CYCLE),
    ("missing_coverage", lambda p: p.pop(1), V.COVERAGE_MISSING),
]


@pytest.mark.parametrize("name,mutate,code", CASES, ids=[c[0] for c in CASES])
def test_invalid_plans_are_rejected_with_specific_code(env, name, mutate, code):
    sc, _, validator = env
    got = validator.validate_tasks(tasks_of(sc, mutate))
    assert code in codes(got), f"{name}: expected {code}, got {[str(v) for v in got]}"


def test_three_task_cycle_reports_the_loop(env):
    sc, _, validator = env
    def mutate(p):
        p[0]["depends_on"] = [p[1]["task_id"]]
        p[1]["depends_on"] = [p[2]["task_id"]]
        p[2]["depends_on"] = [p[0]["task_id"]]
    v = [x for x in validator.validate_tasks(tasks_of(sc, mutate)) if x.code == V.DEPENDENCY_CYCLE]
    assert v and v[0].message.count("->") >= 3


def test_unrequested_task_is_rejected(env):
    sc, _, validator = env
    extra = copy.deepcopy(plan_dicts(sc)[0])
    extra.update(task_id="bonus", target_zone="D")
    got = validator.validate_tasks(tasks_of(sc, lambda p: p.append(extra)))
    assert V.UNREQUESTED_TASK in codes(got)


def test_report_must_not_be_a_prerequisite_of_inspection(env):
    sc, _, validator = env
    def mutate(p):
        p[0]["depends_on"] = ["compile_report"]
    got = codes(validator.validate_tasks(tasks_of(sc, mutate)))
    assert V.DEPENDENCY_CYCLE in got  # report depends on it -> cycle; also never terminal


def test_depth_and_count_limits(env, settings):
    sc, world, validator = env
    chain = []
    for i in range(7):
        t = copy.deepcopy(plan_dicts(sc)[0])
        t.update(task_id=f"c{i}", depends_on=[f"c{i-1}"] if i else [])
        chain.append(TaskSpec.model_validate(t))
    assert V.GRAPH_DEPTH_EXCEEDED in codes(validator.validate_tasks(chain, check_coverage=False))
    many = [TaskSpec.model_validate({**plan_dicts(sc)[0], "task_id": f"m{i}", "depends_on": []}) for i in range(settings.planner_limits.max_tasks + 1)]
    assert codes(validator.validate_tasks(many, check_coverage=False)) == {V.TASK_LIMIT_EXCEEDED}
    assert codes(validator.validate_tasks([])) == {V.EMPTY_PLAN}


def test_no_capable_agent_and_unreachable_zone(settings):
    sc = get_scenario("s0_nominal")
    from autonomous_collab.environment import GridWorld
    world = GridWorld.from_spec(sc.world)
    no_reporter = [a.model_copy(update={"capabilities": [c for c in a.capabilities if c != "reporting"]}) for a in sc.agents]
    got = PlanValidator(world, no_reporter, settings, sc.required_outcomes).validate_tasks(tasks_of(sc))
    assert V.NO_CAPABLE_AGENT in codes(got)
    for cell in [(9, 2)]:
        world.block_cell(cell)  # zone C (10,2) is a dead-end alcove entered only from (9,2)
    got = PlanValidator(world, sc.agents, settings, sc.required_outcomes).validate_tasks(tasks_of(sc))
    assert V.UNREACHABLE_ZONE in codes(got)


def test_added_task_validated_against_existing_graph(env):
    sc, _, validator = env
    existing = tasks_of(sc)
    ok = TaskSpec.model_validate({**plan_dicts(sc)[0], "task_id": "late", "target_zone": "D", "depends_on": []})
    assert validator.validate_tasks([ok], existing=existing, check_coverage=False) == []
    dup = ok.model_copy(update={"task_id": existing[0].task_id})
    assert V.DUPLICATE_TASK_ID in codes(validator.validate_tasks([dup], existing=existing, check_coverage=False))
    cyc = ok.model_copy(update={"depends_on": ["compile_report"]})
    bad_existing = [t if t.task_id != "compile_report" else t.model_copy(update={"depends_on": ["late"]}) for t in existing]
    assert V.DEPENDENCY_CYCLE in codes(validator.validate_tasks([cyc], existing=bad_existing, check_coverage=False))


# ----------------------------------------------------------------- schema-level parsing
def envelope(sc, **kw):
    return json.dumps({"status": "plan", "tasks": plan_dicts(sc), **kw})


def test_parse_accepts_plain_and_single_fenced_json(settings):
    sc = get_scenario()
    lim = settings.planner_limits
    assert parse_candidate(envelope(sc), lim).ok
    assert parse_candidate("```json\n" + envelope(sc) + "\n```", lim).ok
    assert not parse_candidate("Here you go:\n" + envelope(sc), lim).ok
    assert not parse_candidate("```json\n" + envelope(sc) + "\n```\nthanks", lim).ok


@pytest.mark.parametrize("text,code", [
    ("not json", V.MALFORMED_JSON), ("", V.MALFORMED_JSON), ("{'status': 'plan'}", V.MALFORMED_JSON),
    ("[1,2]", V.SCHEMA_NOT_OBJECT), ('{"status":"done"}', V.SCHEMA_BAD_STATUS), ('{"tasks":[]}', V.SCHEMA_BAD_STATUS),
    ('{"status":"plan","tasks":[]}', V.EMPTY_PLAN), ('{"status":"plan","tasks":"x"}', V.SCHEMA_TYPE_ERROR),
    ('{"status":"plan","tasks":[1]}', V.SCHEMA_TYPE_ERROR), ('{"status":"plan","tasks":[{}]}', V.SCHEMA_MISSING_FIELD),
    ('{"status":"plan","tasks":[],"admin":true}', V.SCHEMA_EXTRA_FIELD), ('{"status":"needs_clarification"}', V.SCHEMA_TYPE_ERROR),
    ('{"status":"needs_clarification","questions":[]}', V.SCHEMA_TYPE_ERROR), ("x" * 25000, V.OUTPUT_TOO_LARGE),
])
def test_parse_rejects_with_stable_code(settings, text, code):
    assert code in {v.code for v in parse_candidate(text, settings.planner_limits).violations}


def test_parse_task_limit_and_statuses(settings):
    sc = get_scenario()
    big = {"status": "plan", "tasks": [plan_dicts(sc)[0]] * (settings.planner_limits.max_tasks + 1)}
    assert V.TASK_LIMIT_EXCEEDED in {v.code for v in parse_candidate(json.dumps(big), settings.planner_limits).violations}
    clar = parse_candidate('{"status":"needs_clarification","questions":["which zone?"]}', settings.planner_limits)
    assert clar.ok and clar.questions == ["which zone?"] and not clar.tasks
    err = parse_candidate('{"status":"error","reason":"cannot"}', settings.planner_limits)
    assert err.ok and err.reason == "cannot"


def test_extract_json_never_raises_on_garbage():
    for junk in ["\x00\x01", "{" * 1000, "null", "true", '"s"', "```\n```", None, 5]:
        obj, viol = extract_json(junk, 20000)  # type: ignore[arg-type]
        assert isinstance(viol, list)
