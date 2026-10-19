"""Metrics computed ONLY from the saved event log (never from live simulator state).

Definitions (also in docs/EXPERIMENTS.md):
  mission_success         1 iff mission_end.status == COMPLETE (all mandatory tasks SUCCEEDED)
  task_completion_rate    mandatory SUCCEEDED / mandatory tasks in the validated mission
                          (0.0 if no plan was accepted: omitted tasks are never counted as done)
  makespan_s              simulated time of the last SUCCEEDED transition (None if nothing succeeded)
  travel_cells            number of executed moves (includes yield/avoidance moves)
  energy_total            sum of move energy + task energy actually spent
  allocation_objective    sum of static c_ij of the (task -> executing agent) pairs from the
                          initial snapshot; identical formula/weights for every allocator
  recovery_ticks          per replan: first event tick -> first EXECUTING/SUCCEEDED transition
                          afterwards (0 if nothing was left to do; None if never resumed)
  invalid_plan_rejections planner attempts rejected by the validator + rejected added tasks
  collisions              same-cell / edge-swap occurrences found by the independent audit
"""
from __future__ import annotations

from statistics import mean
from typing import Any, Optional


def compute_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    start = next(r for r in records if r["type"] == "mission_start")
    end = next((r for r in reversed(records) if r["type"] == "mission_end"), None)
    tick_s = float(start["tick_seconds"])
    final = end["tasks_final"] if end else []
    mandatory = [t for t in final if t["mandatory"]]
    succeeded = [t for t in mandatory if t["state"] == "SUCCEEDED"]
    transitions = [r for r in records if r["type"] == "task_transition"]
    last_success = [r["tick"] for r in transitions if r["to_state"] == "SUCCEEDED"]
    attempts = [r for r in records if r["type"] == "plan_attempt"]
    replans = [r for r in records if r["type"] == "replan"]

    recovery: list[Optional[int]] = []
    for rp in replans:
        if rp["unfinished"] == 0:
            recovery.append(0)
            continue
        resumed = next((t["tick"] for t in transitions if t["seq"] > rp["seq"] and t["to_state"] in ("EXECUTING", "SUCCEEDED")), None)
        recovery.append(None if resumed is None else resumed - rp["first_event_tick"])
    recovered = [x for x in recovery if x is not None]
    states = [t["state"] for t in final]

    return {
        "scenario": start["scenario"],
        "seed": start["seed"],
        "status": end["status"] if end else "NO_END",
        "mission_success": int(bool(end) and end["status"] == "COMPLETE"),
        "plan_accepted": int(any(r["type"] == "plan_accepted" for r in records)),
        "n_tasks": len(final),
        "n_mandatory": len(mandatory),
        "n_succeeded": len(succeeded),
        "task_completion_rate": (len(succeeded) / len(mandatory)) if mandatory else 0.0,
        "makespan_s": (max(last_success) * tick_s) if last_success else None,
        "terminal_s": (end["tick"] * tick_s) if end else None,
        "travel_cells": sum(1 for r in records if r["type"] == "move"),
        "energy_total": round(sum(r["energy"] for r in records if r["type"] in ("move", "work_start")), 6),
        "allocation_objective": next((r["value"] for r in reversed(records) if r["type"] == "objective_realized"), None),
        "n_allocations": sum(1 for r in records if r["type"] == "allocation"),
        "n_replans": len(replans),
        "replan_compute_ms_mean": round(mean(r["compute_ms"] for r in replans), 3) if replans else None,
        "recovery_ticks_mean": round(mean(recovered), 3) if recovered else None,
        "n_unrecovered_replans": len(recovery) - len(recovered),
        "invalid_plan_rejections": sum(1 for r in attempts if r["outcome"] == "rejected") + sum(1 for r in records if r["type"] == "plan_rejected"),
        "planner_calls": len(attempts),
        "planner_latency_s": round(sum(r["latency_s"] for r in attempts), 6),
        "collisions": sum(1 for r in records if r["type"] == "collision"),
        "monitor_rejections": sum(1 for r in records if r["type"] == "move_rejected"),
        "dispatch_rejections": sum(1 for r in records if r["type"] == "dispatch_rejected"),
        "tasks_not_succeeded": len(mandatory) - len(succeeded),
        "tasks_blocked": states.count("BLOCKED"),
        "tasks_failed": states.count("FAILED"),
        "tasks_added": sum(1 for r in records if r["type"] == "task_added"),
        "provider": start.get("provider", ""),
    }
