import copy

import pytest
import yaml
from pydantic import ValidationError

from autonomous_collab.config import deep_merge, load_scenario, load_settings, settings_for_scenario
from autonomous_collab.environment import GridWorld
from autonomous_collab.errors import ConfigError
from autonomous_collab.models import TaskSpec, WorldSpec, json_schema_for_plan
from helpers import ALL_SCENARIOS, SCEN, get_scenario

GOOD = dict(task_id="t1", task_type="visual_inspection", target_zone="A", required_capabilities=["visual_inspection"],
            depends_on=[], priority=3, estimated_duration_s=30, acceptance_criteria=["inspection_result_recorded"])


def test_taskspec_round_trip_preserves_fields():
    spec = TaskSpec.model_validate(GOOD)
    again = TaskSpec.model_validate(spec.model_dump())
    assert again == spec and again.model_dump() == spec.model_dump()
    assert spec.retry_policy == "safe_to_retry" and spec.mandatory is True


@pytest.mark.parametrize("change", [
    {"priority": True}, {"priority": "3"}, {"priority": 3.5}, {"estimated_duration_s": "30"}, {"depends_on": "t0"},
    {"required_capabilities": "x"}, {"extra_field": 1}, {"assigned_agent": "agent_1"}, {"mandatory": "yes"},
])
def test_taskspec_rejects_bad_types_and_extras(change):
    with pytest.raises(ValidationError):
        TaskSpec.model_validate({**GOOD, **change})


@pytest.mark.parametrize("missing", ["task_id", "task_type", "target_zone", "required_capabilities", "priority", "estimated_duration_s", "acceptance_criteria"])
def test_taskspec_requires_core_fields(missing):
    bad = {k: v for k, v in GOOD.items() if k != missing}
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(bad)


def test_settings_load_and_digest_is_stable(settings):
    assert settings.simulation.tick_seconds == 5
    assert settings.digest() == load_settings().digest()


def test_settings_reject_unknown_and_invalid_values():
    with pytest.raises(ConfigError):
        load_settings(overrides={"simulation": {"tick_seconds": -1}})
    with pytest.raises(ConfigError):
        load_settings(overrides={"simulation": {"bogus": 1}})
    with pytest.raises(ConfigError):
        load_settings(overrides={"planner_limits": {"injection_policy": "maybe"}})
    with pytest.raises(ConfigError):
        load_settings(overrides={"task_catalog": {"visual_inspection": {"capability": "telepathy", "acceptance_criterion": "x", "energy": 1, "default_duration_s": 1, "default_priority": 3, "default_retry_policy": "safe_to_retry"}}})


def test_missing_and_malformed_files(tmp_path):
    with pytest.raises(ConfigError):
        load_scenario(tmp_path / "nope.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just\n- a list\n")
    with pytest.raises(ConfigError):
        load_scenario(bad)
    bad.write_text("name: x\nmission_text: y\nunknown_key: 1\n")
    with pytest.raises(ConfigError):
        load_scenario(bad)


def test_scenario_extends_merges_and_chained_extends_rejected(tmp_path):
    child = get_scenario("s3_agent_failure")
    parent = get_scenario("s0_nominal")
    assert child.world == parent.world and child.events and not parent.events
    (tmp_path / "a.yaml").write_text(yaml.safe_dump({"extends": "b.yaml", "name": "a"}))
    (tmp_path / "b.yaml").write_text(yaml.safe_dump({"extends": "c.yaml"}))
    (tmp_path / "c.yaml").write_text("name: c\n")
    with pytest.raises(ConfigError):
        load_scenario(tmp_path / "a.yaml")


def test_deep_merge_replaces_lists_and_does_not_mutate():
    base = {"a": {"x": 1, "y": [1, 2]}, "b": 1}
    snapshot = copy.deepcopy(base)
    merged = deep_merge(base, {"a": {"y": [9]}, "c": 2})
    assert merged == {"a": {"x": 1, "y": [9]}, "b": 1, "c": 2} and base == snapshot


@pytest.mark.parametrize("name", ALL_SCENARIOS)
def test_every_scenario_loads_and_is_well_formed(name, settings):
    sc = get_scenario(name)
    world = GridWorld.from_spec(sc.world)
    starts = [tuple(a.start) for a in sc.agents]
    assert len(set(starts)) == len(starts)
    for a in sc.agents:
        assert "navigation" in a.capabilities and world.is_passable(tuple(a.start))
        assert set(a.capabilities) <= set(settings.capabilities)
    settings_for_scenario(sc, settings)
    assert sc.expectation, "every scenario documents its expectation"


@pytest.mark.parametrize("grid,zones,report", [
    (["..", "."], {"HQ": (0, 0)}, "HQ"),
    (["#.", ".."], {"HQ": (0, 0)}, "HQ"),
    (["..", ".."], {"HQ": (0, 0), "A": (0, 0)}, "HQ"),
    (["..", ".."], {"HQ": (5, 5)}, "HQ"),
    (["..", ".."], {"A": (0, 0)}, "HQ"),
    ([], {"HQ": (0, 0)}, "HQ"),
])
def test_world_spec_errors(grid, zones, report):
    with pytest.raises(ConfigError):
        GridWorld.from_spec(WorldSpec(grid=grid, zones=zones, report_zone=report))


def test_plan_json_schema_exposes_strict_envelope():
    schema = json_schema_for_plan()
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"status", "tasks", "questions", "reason"}
