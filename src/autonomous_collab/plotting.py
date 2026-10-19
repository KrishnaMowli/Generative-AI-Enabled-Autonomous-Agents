"""Matplotlib figures generated from saved results/traces (headless Agg backend)."""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from .logging_utils import read_jsonl  # noqa: E402

VARIANT_ORDER = ["A_manual_greedy", "B_mock_greedy", "C_mock_milp", "D_mock_milp_replan",
                 "E_mock_nearest_replan", "F_mock_hungarian_replan", "G_mock_greedy_replan"]


def _short(v: str) -> str:
    return v.split("_")[0]


def plot_success_heatmap(summary: list[dict[str, Any]], path: Path) -> None:
    scenarios = sorted({r["scenario"] for r in summary})
    variants = [v for v in VARIANT_ORDER if any(r["variant"] == v for r in summary)]
    grid = np.full((len(scenarios), len(variants)), np.nan)
    for r in summary:
        grid[scenarios.index(r["scenario"]), variants.index(r["variant"])] = r["success_rate"]
    fig, ax = plt.subplots(figsize=(9, 6))
    im = ax.imshow(grid, vmin=0, vmax=1, cmap="RdYlGn", aspect="auto")
    ax.set_xticks(range(len(variants)), [_short(v) for v in variants])
    ax.set_yticks(range(len(scenarios)), scenarios)
    for i in range(len(scenarios)):
        for j in range(len(variants)):
            if not np.isnan(grid[i, j]):
                ax.text(j, i, f"{grid[i, j]:.2f}", ha="center", va="center", fontsize=8)
    ax.set_title("Mission success rate by scenario and variant")
    fig.colorbar(im, ax=ax, label="success rate")
    fig.tight_layout(); fig.savefig(path, dpi=130); plt.close(fig)


def plot_metric_bars(summary: list[dict[str, Any]], path: Path, scenario: str = "s0_nominal") -> None:
    rows = {r["variant"]: r for r in summary if r["scenario"] == scenario}
    variants = [v for v in VARIANT_ORDER if v in rows]
    metrics = [("makespan_s", "Makespan (s, successful runs)"), ("travel_cells", "Total travel (cells)"),
               ("energy_total", "Energy consumed (units)"), ("allocation_objective", "Static allocation objective")]
    fig, axes = plt.subplots(1, 4, figsize=(16, 3.8))
    for ax, (m, title) in zip(axes, metrics):
        means = [rows[v][f"{m}_mean"] or 0 for v in variants]
        err = [[(rows[v][f"{m}_mean"] or 0) - (rows[v][f"{m}_ci_lo"] or 0) for v in variants],
               [(rows[v][f"{m}_ci_hi"] or 0) - (rows[v][f"{m}_mean"] or 0) for v in variants]]
        ax.bar(range(len(variants)), means, yerr=err, capsize=3, color="#4c72b0")
        ax.set_xticks(range(len(variants)), [_short(v) for v in variants]); ax.set_title(title, fontsize=9)
    fig.suptitle(f"{scenario}: mean with 95% bootstrap CI (resampling unit = seed)")
    fig.tight_layout(); fig.savefig(path, dpi=130); plt.close(fig)


def plot_recovery(summary: list[dict[str, Any]], path: Path, scenarios: tuple[str, ...] = ("s3_agent_failure", "s7_new_task", "s8_uncertain_task")) -> None:
    variants = [v for v in VARIANT_ORDER if any(r["variant"] == v for r in summary)]
    fig, axes = plt.subplots(1, 2, figsize=(12, 4))
    width = 0.8 / len(scenarios)
    for k, sc in enumerate(scenarios):
        rows = {r["variant"]: r for r in summary if r["scenario"] == sc}
        xs = np.arange(len(variants)) + k * width
        rate = [rows[v]["success_rate"] if v in rows else 0 for v in variants]
        err = [[rows[v]["success_rate"] - rows[v]["success_ci_lo"] if v in rows else 0 for v in variants],
               [rows[v]["success_ci_hi"] - rows[v]["success_rate"] if v in rows else 0 for v in variants]]
        axes[0].bar(xs, rate, width, yerr=err, capsize=2, label=sc)
        rec = [(rows[v]["recovery_ticks_mean_mean"] or 0) * 5 if v in rows else 0 for v in variants]
        axes[1].bar(xs, rec, width, label=sc)
    for ax, title in zip(axes, ["Mission success rate (Wilson 95% CI)", "Mean replan recovery time (s; 0 = no replan)"]):
        ax.set_xticks(np.arange(len(variants)) + 0.4 - width / 2, [_short(v) for v in variants]); ax.set_title(title, fontsize=10)
    axes[0].legend(fontsize=7)
    fig.tight_layout(); fig.savefig(path, dpi=130); plt.close(fig)


def plot_planner_eval(prompts_csv: Path, faults_csv: Optional[Path], path: Path) -> None:
    with open(prompts_csv, newline="") as h:
        prompts = list(csv.DictReader(h))
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.5))
    cats = sorted({r["category"] for r in prompts})
    acc = [np.mean([int(r["correct"]) for r in prompts if r["category"] == c]) for c in cats]
    axes[0].bar(cats, acc, color="#55a868"); axes[0].set_ylim(0, 1.05); axes[0].set_title("Prompt-set accuracy by category", fontsize=10)
    axes[0].tick_params(axis="x", rotation=30)
    if faults_csv and Path(faults_csv).exists():
        with open(faults_csv, newline="") as h:
            faults = list(csv.DictReader(h))
        names = sorted({r["fault"] for r in faults})
        trans = [int(next(r for r in faults if r["fault"] == n and r["persistent"] == "0")["executed"]) for n in names]
        pers = [int(next(r for r in faults if r["fault"] == n and r["persistent"] == "1")["executed"]) for n in names]
        y = np.arange(len(names))
        axes[1].barh(y - 0.2, trans, 0.4, label="fault on 1st call only (repair allowed)")
        axes[1].barh(y + 0.2, pers, 0.4, label="persistent fault")
        axes[1].set_yticks(y, names, fontsize=7); axes[1].set_title("Plan executed? (1 = executed)", fontsize=10); axes[1].legend(fontsize=7)
    fig.tight_layout(); fig.savefig(path, dpi=130); plt.close(fig)


def plot_timeline(trace_path: Path, path: Path, title: str) -> None:
    rec = read_jsonl(trace_path)
    tick_s = next(r for r in rec if r["type"] == "mission_start")["tick_seconds"]
    agents = sorted(a["agent_id"] for a in next(r for r in rec if r["type"] == "mission_start")["agents"])
    open_: dict[str, tuple[int, str]] = {}
    bars: list[tuple[str, str, int, int, str]] = []
    for r in rec:
        if r["type"] != "task_transition":
            continue
        tid, to = r["task_id"], r["to_state"]
        if to == "EXECUTING":
            open_[tid] = (r["tick"], r["agent_id"])
        elif tid in open_ and to in ("SUCCEEDED", "FAILED", "BLOCKED"):
            t0, ag = open_.pop(tid)
            bars.append((ag, tid, t0, r["tick"], to))
    colours = {"SUCCEEDED": "#55a868", "FAILED": "#c44e52", "BLOCKED": "#dd8452"}
    fig, ax = plt.subplots(figsize=(11, 3.2 + 0.2 * len(agents)))
    for ag, tid, t0, t1, st in bars:
        ax.barh(agents.index(ag), (t1 - t0 + 1) * tick_s, left=t0 * tick_s, color=colours[st], edgecolor="k", height=0.6)
        ax.text(t0 * tick_s + 1, agents.index(ag), tid.replace("inspect_zone_", "V-").replace("read_zone_", "R-").replace("compile_", ""), fontsize=6, va="center")
    for r in rec:
        if r["type"] == "event" and r["event_type"] in ("AGENT_FAILED", "CELL_BLOCKED", "TASK_ADDED", "LOW_BATTERY"):
            ax.axvline(r["tick"] * tick_s, color="red", ls="--", lw=1); ax.text(r["tick"] * tick_s, len(agents) - 0.4, r["event_type"], fontsize=6, color="red", rotation=90, va="top")
        if r["type"] == "replan":
            ax.axvline(r["tick"] * tick_s, color="purple", ls=":", lw=1); ax.text(r["tick"] * tick_s, -0.45, f"replan v{r['plan_version']}", fontsize=6, color="purple")
    ax.set_yticks(range(len(agents)), agents); ax.set_xlabel("simulated time (s)  [bar = dispatch -> terminal state; green ok, red failed, orange blocked]")
    ax.set_title(title, fontsize=10); fig.tight_layout(); fig.savefig(path, dpi=130); plt.close(fig)


def plot_trajectories(trace_path: Path, scenario_path: Path, path: Path, title: str) -> None:
    from .config import load_scenario
    from .environment import GridWorld
    sc = load_scenario(scenario_path); world = GridWorld.from_spec(sc.world)
    rec = read_jsonl(trace_path)
    fig, ax = plt.subplots(figsize=(9, 6))
    img = np.zeros((world.rows, world.cols))
    for (r, c) in world.obstacles:
        img[r, c] = 1
    ax.imshow(img, cmap="Greys", vmin=0, vmax=1.6)
    for name, (r, c) in world.zones.items():
        ax.text(c, r, name, ha="center", va="center", fontsize=9, color="navy", fontweight="bold")
    paths: dict[str, list[tuple[int, int]]] = {}
    for r in rec:
        if r["type"] == "mission_start":
            for a in r["agents"]:
                paths[a["agent_id"]] = [tuple(a["start"])]
        elif r["type"] == "move":
            paths[r["agent_id"]].append(tuple(r["to_cell"]))
    for i, (ag, pts) in enumerate(sorted(paths.items())):
        ys, xs = zip(*pts)
        ax.plot(np.array(xs) + 0.08 * i, np.array(ys) + 0.08 * i, lw=1.5, label=ag, alpha=0.85)
        ax.plot(xs[0], ys[0], "o", ms=6, color=ax.lines[-1].get_color())
    ax.legend(fontsize=7, loc="upper right"); ax.set_title(title, fontsize=10)
    fig.tight_layout(); fig.savefig(path, dpi=130); plt.close(fig)
