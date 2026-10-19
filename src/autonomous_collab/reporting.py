"""Generate results/report.md from saved experiment outputs ONLY (no hand-typed numbers)."""
from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path
from typing import Any, Optional

from .analysis import load_runs, paired_differences, summarize, write_dicts
from .config import load_settings, load_scenario
from .experiments import VARIANTS


def _fmt(x: Optional[float], nd: int = 1) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def _ci(row: dict[str, Any], m: str, nd: int = 1) -> str:
    if row.get(f"{m}_mean") is None:
        return "n/a"
    return f"{row[f'{m}_mean']:.{nd}f} [{row[f'{m}_ci_lo']:.{nd}f}, {row[f'{m}_ci_hi']:.{nd}f}] (n={row[f'{m}_n']})"


def build_summaries(results_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    rows = load_runs(results_dir / "runs.csv")
    summary, paired = summarize(rows), paired_differences(rows)
    write_dicts(summary, results_dir / "summary.csv")
    write_dicts(paired, results_dir / "paired.csv")
    return rows, summary, paired


def build_report(results_dir: Path, scenarios_dir: Path) -> Path:
    results_dir = Path(results_dir)
    rows, summary, paired = build_summaries(results_dir)
    manifest = json.loads((results_dir / "manifest.json").read_text())
    settings = load_settings()
    by = {(r["scenario"], r["variant"]): r for r in summary}
    scenarios = sorted({r["scenario"] for r in summary})
    variants = [v for v in VARIANTS if any(k[1] == v for k in by)]
    L: list[str] = []
    A = L.append
    A("# Experiment report (auto-generated from saved results)\n")
    A(f"Generated {date.today().isoformat()} by `autocollab report` from `{results_dir.name}/runs.csv`. Every number below is computed from that file "
      "(which is computed from per-run event logs); nothing is typed by hand.\n")
    A("## 1. Provenance\n")
    A(f"- Runs: **{manifest['n_runs']}** = {len(manifest['scenarios'])} scenarios x {len(manifest['variants'])} variants x {len(manifest['seeds'])} seeds "
      f"(seeds {manifest['seeds'][0]}..{manifest['seeds'][-1]}). Wall time {manifest['wall_seconds']} s.")
    A(f"- Planner source: **{manifest['provider']}**. These results say nothing about the quality of a real LLM; see section 6 for the planner harness.")
    A(f"- Settings digest: `{settings.digest()}`; allocation weights: {settings.allocation.weights.model_dump()}.")
    A("- Randomness (seeded, label-keyed so paired variants see identical draws): agent start jitter, battery jitter, task-duration jitter, failure-time windows.\n")
    A("## 2. Variants and scenarios\n")
    A("| variant | planner | allocator | replanning |\n|---|---|---|---|")
    for v in variants:
        A(f"| {v} | {VARIANTS[v].planner} | {VARIANTS[v].allocator} | {'on' if VARIANTS[v].replan else 'off'} |")
    A("\n| scenario | description |\n|---|---|")
    for sc in scenarios:
        A(f"| {sc} | {load_scenario(Path(scenarios_dir) / (sc + '.yaml')).description} |")
    A("\n## 3. Mission success rate (successes/runs, Wilson 95% CI)\n")
    A("| scenario | " + " | ".join(v.split("_")[0] for v in variants) + " |\n|---|" + "---|" * len(variants))
    for sc in scenarios:
        cells = []
        for v in variants:
            r = by.get((sc, v))
            cells.append("-" if r is None else f"{r['n_success']}/{r['n_runs']} [{r['success_ci_lo']:.2f}, {r['success_ci_hi']:.2f}]")
        A(f"| {sc} | " + " | ".join(cells) + " |")
    A("\n![success heatmap](figures/success_heatmap.png)\n")
    A("## 4. Nominal operation (s0_nominal): allocation comparison\n")
    A("Mean [95% bootstrap CI over seeds]. Makespan and objective use successful runs only. `allocation_objective` is the common static c_ij sum of the "
      "executing agent assignments from the initial snapshot (identical formula for all allocators).\n")
    A("| variant | makespan (s) | travel (cells) | energy | alloc. objective |\n|---|---|---|---|---|")
    for v in variants:
        r = by.get(("s0_nominal", v))
        if r:
            A(f"| {v} | {_ci(r, 'makespan_s')} | {_ci(r, 'travel_cells')} | {_ci(r, 'energy_total')} | {_ci(r, 'allocation_objective', 2)} |")
    A("\n![nominal metrics](figures/nominal_metrics.png)\n")
    A("Paired within-seed differences (y - x), s0_nominal:\n")
    A("| x -> y | metric | n pairs | mean diff | 95% CI |\n|---|---|---|---|---|")
    for p in paired:
        if p["scenario"] == "s0_nominal" and p["metric"] != "mission_success":
            A(f"| {p['x'].split('_')[0]} -> {p['y'].split('_')[0]} | {p['metric']} | {p['n_pairs']} | {p['mean_diff_y_minus_x']:.2f} | [{p['ci_lo']:.2f}, {p['ci_hi']:.2f}] |")
    A("\n## 5. Faults, replanning and fail-closed behaviour\n")
    A("| scenario | variant | success | tasks done (mean completion rate) | recovery ticks (mean) | replans | invalid-plan rejections | collisions |\n|---|---|---|---|---|---|---|---|")
    for sc in scenarios:
        if sc in ("s0_nominal", "s1_heterogeneity", "s2_blocked_route"):
            continue
        for v in ("C_mock_milp", "D_mock_milp_replan", "B_mock_greedy", "E_mock_nearest_replan"):
            r = by.get((sc, v))
            if r:
                A(f"| {sc} | {v.split('_')[0]} | {r['n_success']}/{r['n_runs']} | {_fmt(r['task_completion_rate_mean'], 2)} | {_fmt(r['recovery_ticks_mean_mean'], 2)} | "
                  f"{_fmt(r['n_replans_mean'], 2)} | {_fmt(r['invalid_plan_rejections_mean'], 2)} | {_fmt(r['collisions_mean'], 2)} |")
    A("\n![recovery](figures/recovery.png)\n\n![timeline nominal](figures/timeline_nominal.png)\n\n![timeline failure](figures/timeline_failure.png)\n\n![trajectories](figures/trajectories_nominal.png)\n")
    total_col = sum(int(r["collisions"]) for r in rows)
    total_rej = sum(int(r["monitor_rejections"]) for r in rows)
    A(f"**Safety audit (simulated):** {total_col} collisions (same-cell or edge-swap) observed by the independent per-tick audit across {len(rows)} runs; "
      f"the SafetyMonitor rejected/held {total_rej} proposed moves. This is evidence for the tested scenarios in a discrete simulator only, not a safety guarantee.\n")
    pe = results_dir / "planner_eval" / "summary.json"
    A("## 6. Planner evaluation (separate from the simulation runs)\n")
    if pe.exists():
        s = json.loads(pe.read_text())
        A(f"- Provider: **{s['provider']}** / model `{s['model']}`. {s['note']}.")
        A(f"- Labelled prompt set: {s['n_prompts']} prompts, overall accuracy {s['accuracy_overall']:.2f}; plan-validity rate on prompts that should yield a plan "
          f"{s['plan_validity_rate']:.2f} (Wilson 95% CI [{s['plan_validity_ci95'][0]:.2f}, {s['plan_validity_ci95'][1]:.2f}], n={s['plan_prompts']}).")
        A("- Per-prompt outcomes: `planner_eval/prompts.csv`; corruption matrix: `planner_eval/fault_modes.csv`.")
        if "fault_modes_executed_when_persistent" in s:
            A(f"- Fault-mode matrix: plans executed under a *persistent* corruption (excluding the benign markdown-fence case): **{s['fault_modes_executed_when_persistent']}**.")
        A("\n![planner eval](figures/planner_eval.png)\n")
    else:
        A("_Planner evaluation has not been run (`autocollab planner-eval`)._\n")
    A("## 7. Limitations of this report\n")
    for line in [
        "Planner = deterministic keyword mock; identical task graphs for A and B are expected, so A-vs-B differences reflect only the planner path, not LLM variability.",
        "Static MILP assignment ignores sequence-dependent travel; greedy is route-aware, so MILP is not guaranteed to beat greedy on distance/makespan.",
        "Discrete grid, 4-connected unit-cost moves; safety results do not transfer to physical robots.",
        "Failure scenarios s3/s8/s10 assume the failed agent is cleared from the grid (configurable).",
        "Confidence intervals treat seeds as independent draws; the 30-seed design gives limited power for small effects.",
    ]:
        A(f"- {line}")
    A("\n## 8. Reproduce\n\n```bash\nautocollab experiments --seeds 30 --jobs 4 --out results\nautocollab planner-eval --provider mock --out results/planner_eval\nautocollab plots --results results\nautocollab report --results results\n```\n")
    out = results_dir / "report.md"
    out.write_text("\n".join(L), encoding="utf-8")
    return out


_WALL_CLOCK = {"replan_compute_ms_mean", "planner_latency_s"}


def verify_traces(results_dir: Path) -> list[str]:
    """Recompute metrics from every saved JSONL trace and compare with runs.csv. Returns mismatches."""
    from .logging_utils import read_jsonl
    from .metrics import compute_metrics

    results_dir = Path(results_dir)
    rows = {(r["scenario"], r["variant"], r["seed"]): r for r in load_runs(results_dir / "runs.csv")}
    problems: list[str] = []
    traces = sorted((results_dir / "traces").glob("*.jsonl"))
    for path in traces:
        scenario, variant, seedpart = path.stem.split("__")
        seed = int(seedpart.replace("seed", ""))
        row = rows.get((scenario, variant, seed))
        if row is None:
            problems.append(f"{path.name}: no matching row in runs.csv")
            continue
        recomputed = compute_metrics(read_jsonl(path))
        for key, value in recomputed.items():
            if key in _WALL_CLOCK:
                continue
            stored = row.get(key)
            same = (stored is None and value is None) or (
                isinstance(value, (int, float)) and isinstance(stored, (int, float)) and abs(float(value) - float(stored)) < 1e-6) or stored == value
            if not same:
                problems.append(f"{path.name}: {key} recomputed={value!r} stored={stored!r}")
    if not traces:
        problems.append("no traces found to verify")
    return problems
