"""Planner evaluation: labelled prompt set + fault-mode matrix. Provider-agnostic.

Results are stored with the provider name/model so mock-planner numbers can never be confused
with real-LLM numbers. The mock is a keyword parser: its scores measure the harness and the
validators, NOT language-model quality.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import yaml

from .analysis import wilson_interval, write_dicts
from .config import Settings, load_scenario, load_settings
from .environment import GridWorld
from .models import RequiredOutcome
from .planner import FAULT_BEHAVIORS, FaultInjectingProvider, LLMPlanner, LLMProvider, MockProvider
from .planner.interface import PlanningRequest
from .planner.mission_text import sanitize_mission
from .validation.semantic_validator import PlanValidator

REPO = Path(__file__).resolve().parents[2]


def _request(text: str, settings: Settings, world: GridWorld, report_zone: str, outcomes: list[RequiredOutcome], mid: str) -> PlanningRequest:
    return PlanningRequest(mid, text, sorted(world.zones), report_zone, {k: v.capability for k, v in settings.task_catalog.items()},
                           list(settings.capabilities), [o.model_dump() for o in outcomes], settings.planner_limits.max_tasks)


def evaluate_prompts(provider: LLMProvider, settings: Optional[Settings] = None, prompts_path: Optional[Path] = None,
                     scenario_path: Optional[Path] = None) -> list[dict[str, Any]]:
    settings = settings or load_settings()
    scenario = load_scenario(scenario_path or REPO / "scenarios" / "s0_nominal.yaml")
    world = GridWorld.from_spec(scenario.world)
    prompts = yaml.safe_load(Path(prompts_path or REPO / "scenarios" / "planner_prompts.yaml").read_text())
    rows: list[dict[str, Any]] = []
    for item in prompts:
        outcomes = [RequiredOutcome(**o) for o in item.get("expected_outcomes", [])]
        clean, problems = sanitize_mission(item["text"], settings.planner_limits.max_mission_chars)
        row: dict[str, Any] = {"prompt_id": item["id"], "category": item["category"], "expected": item["expected_status"],
                               "provider": provider.name, "model": provider.model}
        if problems:
            row.update(got="mission_rejected", valid=0, calls=0, codes=";".join(p.code for p in problems), latency_s=0.0)
        else:
            validator = PlanValidator(world, scenario.agents, settings, outcomes)
            out = LLMPlanner(provider, settings.planner_limits).plan(_request(clean, settings, world, scenario.world.report_zone, outcomes, item["id"]), validator)
            row.update(got=out.status, valid=int(out.status == "plan"), calls=out.calls,
                       codes=";".join(sorted({v.code for v in out.violations})), latency_s=round(sum(a.latency_s for a in out.attempts), 4))
        row["correct"] = int(row["got"] == row["expected"])
        rows.append(row)
    return rows


def evaluate_fault_modes(settings: Optional[Settings] = None, scenario_path: Optional[Path] = None) -> list[dict[str, Any]]:
    """Every corruption x {first call only, persistent}: does the pipeline fail closed / repair?"""
    settings = settings or load_settings()
    scenario = load_scenario(scenario_path or REPO / "scenarios" / "s0_nominal.yaml")
    world = GridWorld.from_spec(scenario.world)
    outcomes = scenario.required_outcomes
    validator = PlanValidator(world, scenario.agents, settings, outcomes)
    catalog = {k: v.model_dump() for k, v in settings.task_catalog.items()}
    rows = []
    for behavior in FAULT_BEHAVIORS:
        if behavior == "valid":
            continue
        for persistent in (False, True):
            prov = FaultInjectingProvider(MockProvider(catalog), [behavior], persistent)
            out = LLMPlanner(prov, settings.planner_limits).plan(
                _request(scenario.mission_text, settings, world, scenario.world.report_zone, outcomes, f"fault-{behavior}"), validator)
            first = out.attempts[0].violation_codes if out.attempts else []
            rows.append({"fault": behavior, "persistent": int(persistent), "final_status": out.status, "executed": int(out.status == "plan"),
                         "calls": out.calls, "first_attempt_codes": ";".join(sorted(set(first))),
                         "final_codes": ";".join(sorted({v.code for v in out.violations}))})
    return rows


def run_planner_eval(provider: LLMProvider, out_dir: Path) -> dict[str, Any]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    prompt_rows = evaluate_prompts(provider)
    write_dicts(prompt_rows, out_dir / "prompts.csv")
    plan_rows = [r for r in prompt_rows if r["expected"] == "plan"]
    k = sum(r["valid"] for r in plan_rows)
    lo, hi = wilson_interval(k, len(plan_rows))
    summary: dict[str, Any] = {
        "provider": provider.name, "model": provider.model, "n_prompts": len(prompt_rows),
        "accuracy_overall": sum(r["correct"] for r in prompt_rows) / len(prompt_rows),
        "plan_prompts": len(plan_rows), "plan_validity_rate": k / len(plan_rows), "plan_validity_ci95": [lo, hi],
        "ambiguous_correct": [r["correct"] for r in prompt_rows if r["category"] == "ambiguous"],
        "adversarial_rejected": [r["correct"] for r in prompt_rows if r["category"] == "adversarial"],
        "note": "mock provider = keyword parser; scores measure the harness, not LLM quality" if provider.name == "mock" else "real provider run",
    }
    if provider.name == "mock":
        fault_rows = evaluate_fault_modes()
        write_dicts(fault_rows, out_dir / "fault_modes.csv")
        summary["fault_modes_executed_when_persistent"] = sum(r["executed"] for r in fault_rows if r["persistent"] and r["fault"] != "markdown_wrapped")
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary
