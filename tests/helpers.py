"""Shared test helpers (no network, no API keys)."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from autonomous_collab.config import load_scenario, load_settings
from autonomous_collab.environment import GridWorld
from autonomous_collab.experiments import VARIANTS, run_variant
from autonomous_collab.models import ScenarioSpec, WorldSpec

ROOT = Path(__file__).resolve().parents[1]
SCEN = ROOT / "scenarios"
ALL_SCENARIOS = sorted(p.stem for p in SCEN.glob("s*.yaml"))


def get_scenario(name: str = "s0_nominal", **updates: Any) -> ScenarioSpec:
    sc = load_scenario(SCEN / f"{name}.yaml")
    return sc.model_copy(update=updates) if updates else sc


def run(sc: ScenarioSpec | str = "s0_nominal", variant: str = "D_mock_milp_replan", seed: int = 0, **settings_overrides: Any):
    scenario = get_scenario(sc) if isinstance(sc, str) else sc
    if settings_overrides:
        scenario = scenario.model_copy(update={"settings_overrides": {**scenario.settings_overrides, **settings_overrides}})
    return run_variant(scenario, load_settings(), VARIANTS[variant], seed)


def make_world(rows: list[str], zones: Optional[dict[str, tuple[int, int]]] = None, report_zone: str = "HQ") -> GridWorld:
    zones = zones or {"HQ": (0, 0)}
    return GridWorld.from_spec(WorldSpec(grid=rows, zones=zones, report_zone=report_zone))


def plan_dicts(sc: ScenarioSpec) -> list[dict[str, Any]]:
    return [dict(t) for t in sc.manual_plan]


def check_invariants(result, sc: ScenarioSpec) -> None:
    """Replay the event log and assert the safety/consistency invariants from the guide."""
    recs = result.records
    specs: dict[str, dict[str, Any]] = {t["task_id"]: t for t in sc.manual_plan}
    for ev in sc.events:
        if ev.type == "TASK_ADDED" and ev.task:
            specs[ev.task["task_id"]] = ev.task
    start = next(r for r in recs if r["type"] == "mission_start")
    agents = {a["agent_id"]: a for a in start["agents"]}
    state: dict[str, str] = {}
    battery = {aid: a["battery"] for aid, a in agents.items()}
    failed_at: dict[str, int] = {}
    results: dict[str, int] = {}
    for r in recs:
        t = r["type"]
        if t == "task_transition":
            tid, frm, to = r["task_id"], r["from_state"], r["to_state"]
            assert state.get(tid, "PENDING") == frm, f"inconsistent transition {r}"
            assert frm != "SUCCEEDED", f"SUCCEEDED task {tid} changed state"
            if to == "EXECUTING":
                for dep in specs[tid]["depends_on"]:
                    assert state.get(dep) == "SUCCEEDED", f"{tid} dispatched before dependency {dep} succeeded"
            if to in ("ASSIGNED", "EXECUTING") and r["agent_id"] in failed_at:
                assert r["tick"] < failed_at[r["agent_id"]], f"{r['agent_id']} got work after failing: {r}"
            if to == "EXECUTING":
                caps = set(agents[r["agent_id"]]["capabilities"])
                assert set(specs[tid]["required_capabilities"]) <= caps, f"incapable agent dispatched: {r}"
            state[tid] = to
        elif t == "event" and r["event_type"] in ("AGENT_FAILED", "LOW_BATTERY"):
            failed_at.setdefault(r["agent_id"], r["tick"])
        elif t == "move":
            battery[r["agent_id"]] -= r["energy"]
            assert battery[r["agent_id"]] >= -1e-9, "battery went negative"
        elif t == "work_start":
            battery[r["agent_id"]] -= r["energy"]
            assert battery[r["agent_id"]] >= -1e-9, "battery went negative"
        elif t == "task_result":
            results[r["task_id"]] = results.get(r["task_id"], 0) + 1
        elif t == "collision":
            raise AssertionError(f"collision recorded: {r}")
    assert all(n == 1 for n in results.values()), f"a task result was recorded more than once: {results}"
    assert {tid for tid, n in results.items()} == {tid for tid, s in state.items() if s == "SUCCEEDED"}
