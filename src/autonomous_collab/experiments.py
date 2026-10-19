"""Experiment runner: scenario x variant x seed, paired by seed, results as CSV/JSONL.

Variants (see docs/EXPERIMENTS.md). A-D follow the project guide; E-G add the rule-based and
Hungarian online baselines and a greedy+replanning ablation:
  A manual plan + greedy, replanning OFF      E mock plan + nearest-feasible (online), replanning ON
  B mock plan  + greedy, replanning OFF       F mock plan + Hungarian (online),        replanning ON
  C mock plan  + MILP,   replanning OFF       G mock plan + greedy,                    replanning ON
  D mock plan  + MILP,   replanning ON
"""
from __future__ import annotations

import csv
import json
import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import Settings, load_scenario, load_settings, settings_for_scenario
from .models import ScenarioSpec
from .planner import FaultInjectingProvider, LLMPlanner, ManualPlanner, MockProvider
from .runner import MissionResult, run_mission


@dataclass(frozen=True)
class Variant:
    name: str
    planner: str      # "manual" | "mock"
    allocator: str
    replan: bool


VARIANTS: dict[str, Variant] = {v.name: v for v in [
    Variant("A_manual_greedy", "manual", "greedy", False),
    Variant("B_mock_greedy", "mock", "greedy", False),
    Variant("C_mock_milp", "mock", "milp", False),
    Variant("D_mock_milp_replan", "mock", "milp", True),
    Variant("E_mock_nearest_replan", "mock", "nearest", True),
    Variant("F_mock_hungarian_replan", "mock", "hungarian", True),
    Variant("G_mock_greedy_replan", "mock", "greedy", True),
]}


def catalog_dicts(settings: Settings) -> dict[str, dict[str, Any]]:
    return {k: v.model_dump() for k, v in settings.task_catalog.items()}


def build_planner(kind: str, scenario: ScenarioSpec, settings: Settings, provider: Optional[Any] = None):
    """``manual`` -> scenario fixture plan; ``mock`` -> LLMPlanner over the (fault-injected) mock provider."""
    if kind == "manual":
        return ManualPlanner(scenario.manual_plan)
    prov = provider or MockProvider(catalog_dicts(settings))
    if scenario.planner_faults and provider is None:
        prov = FaultInjectingProvider(prov, scenario.planner_faults, scenario.planner_faults_persistent)
    return LLMPlanner(prov, settings.planner_limits)


def run_variant(scenario: ScenarioSpec, base_settings: Settings, variant: Variant, seed: int,
                *, log_path: Optional[Path] = None) -> MissionResult:
    settings = settings_for_scenario(scenario, base_settings)
    planner = build_planner(variant.planner, scenario, settings)
    label = "manual" if variant.planner == "manual" else "mock"
    return run_mission(scenario, settings, planner=planner, allocator_name=variant.allocator, replan=variant.replan,
                       seed=seed, variant=variant.name, provider_label=label, log_path=log_path)


def _job(args: tuple[str, str, int, Optional[str]]) -> dict[str, Any]:
    scenario_path, variant_name, seed, trace_path = args
    scenario = load_scenario(Path(scenario_path))
    result = run_variant(scenario, load_settings(), VARIANTS[variant_name], seed, log_path=Path(trace_path) if trace_path else None)
    row = dict(result.metrics)
    row.update(variant=variant_name, allocator=VARIANTS[variant_name].allocator, replan=int(VARIANTS[variant_name].replan),
               planner_source=VARIANTS[variant_name].planner)
    return row


def run_all(scenario_paths: Iterable[Path], seeds: list[int], out_dir: Path, *, variants: Optional[list[str]] = None,
            jobs: int = 1, traces: str = "first", progress: bool = True) -> list[dict[str, Any]]:
    """Run every scenario x variant x seed. ``traces``: 'first' (seed 0 only), 'all' or 'none'."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    names = variants or list(VARIANTS)
    tasks: list[tuple[str, str, int, Optional[str]]] = []
    for sp in sorted(scenario_paths):
        stem = Path(sp).stem
        for vn in names:
            for seed in seeds:
                keep = traces == "all" or (traces == "first" and seed == seeds[0])
                trace = str(out_dir / "traces" / f"{stem}__{vn}__seed{seed}.jsonl") if keep else None
                tasks.append((str(sp), vn, seed, trace))
    started = time.perf_counter()
    rows: list[dict[str, Any]] = []
    if jobs > 1:
        # 'spawn' (not fork): forking after numpy/scipy/HiGHS have started threads can deadlock on multi-core Linux
        with ProcessPoolExecutor(max_workers=jobs, mp_context=multiprocessing.get_context("spawn")) as pool:
            for i, row in enumerate(pool.map(_job, tasks, chunksize=8), 1):
                rows.append(row)
                if progress and i % 200 == 0:
                    print(f"  {i}/{len(tasks)} runs", flush=True)
    else:
        for i, t in enumerate(tasks, 1):
            rows.append(_job(t))
            if progress and i % 200 == 0:
                print(f"  {i}/{len(tasks)} runs", flush=True)
    rows.sort(key=lambda r: (r["scenario"], r["variant"], r["seed"]))
    write_runs_csv(rows, out_dir / "runs.csv")
    manifest = {"n_runs": len(rows), "seeds": seeds, "variants": names, "scenarios": sorted(Path(p).stem for p in scenario_paths),
                "provider": "mock (offline, deterministic keyword planner) - no real LLM involved",
                "wall_seconds": round(time.perf_counter() - started, 2), "traces": traces}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return rows


def write_runs_csv(rows: list[dict[str, Any]], path: Path) -> None:
    if not rows:
        return
    fields = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
