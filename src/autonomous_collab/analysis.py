"""Aggregation and statistics for experiment results (stdlib + numpy only)."""
from __future__ import annotations

import csv
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import numpy as np

METRICS = ["makespan_s", "travel_cells", "energy_total", "allocation_objective", "recovery_ticks_mean",
           "replan_compute_ms_mean", "n_replans", "invalid_plan_rejections", "collisions", "tasks_not_succeeded", "task_completion_rate"]
# makespan / objective are only meaningful for missions that actually finished their work
SUCCESS_ONLY = {"makespan_s", "allocation_objective"}


def load_runs(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(path, newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            row: dict[str, Any] = {}
            for k, v in raw.items():
                if v == "" or v is None:
                    row[k] = None
                else:
                    try:
                        row[k] = int(v)
                    except ValueError:
                        try:
                            row[k] = float(v)
                        except ValueError:
                            row[k] = v
            rows.append(row)
    return rows


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion."""
    if n == 0:
        return (float("nan"), float("nan"))
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def bootstrap_ci(values: Sequence[float], n_resamples: int = 2000, level: float = 0.95, seed: int = 12345) -> tuple[float, float]:
    """Percentile bootstrap CI of the mean; resampling unit = one run (seed)."""
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        return (float("nan"), float("nan"))
    if arr.size == 1:
        return (float(arr[0]), float(arr[0]))
    rng = np.random.default_rng(seed)
    means = rng.choice(arr, size=(n_resamples, arr.size), replace=True).mean(axis=1)
    lo, hi = np.percentile(means, [(1 - level) / 2 * 100, (1 + level) / 2 * 100])
    return float(lo), float(hi)


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[(r["scenario"], r["variant"])].append(r)
    out: list[dict[str, Any]] = []
    for (scenario, variant), grp in sorted(groups.items()):
        n = len(grp)
        k = sum(int(r["mission_success"]) for r in grp)
        lo, hi = wilson_interval(k, n)
        row: dict[str, Any] = {"scenario": scenario, "variant": variant, "n_runs": n, "n_success": k, "success_rate": k / n,
                               "success_ci_lo": lo, "success_ci_hi": hi,
                               "plan_accepted_rate": sum(int(r["plan_accepted"]) for r in grp) / n}
        for m in METRICS:
            pool = grp if m not in SUCCESS_ONLY else [r for r in grp if r["mission_success"]]
            vals = [float(r[m]) for r in pool if r.get(m) is not None]
            row[f"{m}_n"] = len(vals)
            if vals:
                ci = bootstrap_ci(vals)
                row[f"{m}_mean"], row[f"{m}_sd"] = float(np.mean(vals)), float(np.std(vals, ddof=1)) if len(vals) > 1 else 0.0
                row[f"{m}_ci_lo"], row[f"{m}_ci_hi"] = ci
            else:
                for suffix in ("mean", "sd", "ci_lo", "ci_hi"):
                    row[f"{m}_{suffix}"] = None
        out.append(row)
    return out


COMPARISONS = [("B_mock_greedy", "C_mock_milp"), ("C_mock_milp", "D_mock_milp_replan"), ("G_mock_greedy_replan", "D_mock_milp_replan"),
               ("E_mock_nearest_replan", "D_mock_milp_replan"), ("F_mock_hungarian_replan", "D_mock_milp_replan"),
               ("A_manual_greedy", "B_mock_greedy")]
PAIRED_METRICS = ["mission_success", "makespan_s", "travel_cells", "energy_total", "allocation_objective"]


def paired_differences(rows: list[dict[str, Any]], comparisons: Iterable[tuple[str, str]] = COMPARISONS,
                       metrics: Sequence[str] = PAIRED_METRICS) -> list[dict[str, Any]]:
    """Within-seed differences (y - x) with a bootstrap CI; seeds where either value is missing are dropped."""
    index: dict[tuple[str, str, int], dict[str, Any]] = {(r["scenario"], r["variant"], r["seed"]): r for r in rows}
    scenarios = sorted({r["scenario"] for r in rows})
    out: list[dict[str, Any]] = []
    for sc in scenarios:
        seeds = sorted({r["seed"] for r in rows if r["scenario"] == sc})
        for x, y in comparisons:
            for m in metrics:
                diffs = []
                for s in seeds:
                    rx, ry = index.get((sc, x, s)), index.get((sc, y, s))
                    if rx is None or ry is None or rx.get(m) is None or ry.get(m) is None:
                        continue
                    if m in SUCCESS_ONLY and not (rx["mission_success"] and ry["mission_success"]):
                        continue
                    diffs.append(float(ry[m]) - float(rx[m]))
                if not diffs:
                    continue
                lo, hi = bootstrap_ci(diffs)
                out.append({"scenario": sc, "metric": m, "x": x, "y": y, "n_pairs": len(diffs), "mean_diff_y_minus_x": float(np.mean(diffs)),
                            "ci_lo": lo, "ci_hi": hi})
    return out


def write_dicts(rows: list[dict[str, Any]], path: Path) -> None:
    if not rows:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for r in rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        w = csv.DictWriter(handle, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
