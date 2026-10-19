import csv
import json
import math
from pathlib import Path

import numpy as np
import pytest

from autonomous_collab import cli
from autonomous_collab.analysis import bootstrap_ci, load_runs, paired_differences, summarize, wilson_interval
from autonomous_collab.experiments import VARIANTS, run_all
from autonomous_collab.logging_utils import read_jsonl
from autonomous_collab.metrics import compute_metrics
from autonomous_collab.reporting import build_report, verify_traces
from helpers import SCEN, get_scenario, run, run_variant, load_settings  # noqa: F401


def rec(type_, tick=0, **kw):
    return {"type": type_, "tick": tick, **kw}


def synthetic(**over):
    recs = [rec("mission_start", scenario="x", seed=1, tick_seconds=5.0, provider="mock", agents=[]),
            rec("move", 1, agent_id="a", energy=0.5), rec("move", 2, agent_id="a", energy=0.5),
            rec("work_start", 3, agent_id="a", task_id="t", energy=3.0),
            rec("task_transition", 4, task_id="t", from_state="EXECUTING", to_state="SUCCEEDED"),
            rec("plan_accepted"), rec("objective_realized", 4, value=1.25),
            rec("mission_end", 5, status="COMPLETE", tasks_final=[{"task_id": "t", "state": "SUCCEEDED", "mandatory": True, "agent_id": "a", "reason": "", "attempts": 1}])]
    for i, r in enumerate(recs):
        r["seq"] = i
    return recs


def test_metric_definitions_on_hand_made_log():
    m = compute_metrics(synthetic())
    assert m["mission_success"] == 1 and m["task_completion_rate"] == 1.0 and m["makespan_s"] == 20.0 and m["terminal_s"] == 25.0
    assert m["travel_cells"] == 2 and m["energy_total"] == pytest.approx(4.0) and m["allocation_objective"] == 1.25
    assert m["collisions"] == 0 and m["n_replans"] == 0 and m["invalid_plan_rejections"] == 0


def test_rejected_plan_counts_zero_completion_not_vacuous_success():
    recs = [rec("mission_start", seed=0, scenario="x", tick_seconds=5.0), rec("plan_attempt", outcome="rejected", latency_s=0.1),
            rec("plan_attempt", outcome="rejected", latency_s=0.2), rec("mission_end", status="PLAN_REJECTED", tasks_final=[])]
    for i, r in enumerate(recs):
        r["seq"] = i
    m = compute_metrics(recs)
    assert m["mission_success"] == 0 and m["task_completion_rate"] == 0.0 and m["plan_accepted"] == 0
    assert m["invalid_plan_rejections"] == 2 and m["planner_calls"] == 2 and m["planner_latency_s"] == pytest.approx(0.3) and m["makespan_s"] is None


def test_recovery_ticks_definition():
    recs = synthetic()[:-1]
    recs += [rec("replan", 10, first_event_tick=8, unfinished=3, compute_ms=1.0),
             rec("task_transition", 12, task_id="u", from_state="ASSIGNED", to_state="EXECUTING"),
             rec("replan", 20, first_event_tick=19, unfinished=0, compute_ms=3.0),
             rec("replan", 30, first_event_tick=29, unfinished=2, compute_ms=2.0),
             rec("mission_end", 40, status="INCOMPLETE", tasks_final=[])]
    for i, r in enumerate(recs):
        r["seq"] = i
    m = compute_metrics(recs)
    assert m["n_replans"] == 3 and m["n_unrecovered_replans"] == 1
    assert m["recovery_ticks_mean"] == pytest.approx((4 + 0) / 2)  # 12-8, nothing-to-recover=0, last never resumed
    assert m["replan_compute_ms_mean"] == pytest.approx(2.0)


def test_metrics_recomputed_from_saved_jsonl_equal_live_metrics(tmp_path):
    sc = get_scenario("s3_agent_failure")
    path = tmp_path / "trace.jsonl"
    res = run_variant(sc, load_settings(), VARIANTS["D_mock_milp_replan"], 2, log_path=path)
    again = compute_metrics(read_jsonl(path))
    assert again == res.metrics
    assert res.metrics["n_replans"] >= 1 and res.metrics["recovery_ticks_mean"] is not None


# ------------------------------------------------------------------ statistics
def test_wilson_interval_known_values():
    lo, hi = wilson_interval(5, 10)
    assert (lo, hi) == (pytest.approx(0.2366, abs=1e-3), pytest.approx(0.7634, abs=1e-3))
    assert wilson_interval(0, 10)[0] == 0.0 and wilson_interval(0, 10)[1] == pytest.approx(0.2775, abs=1e-3)
    assert wilson_interval(10, 10)[1] == 1.0 and wilson_interval(10, 10)[0] == pytest.approx(0.7225, abs=1e-3)
    assert math.isnan(wilson_interval(0, 0)[0])


def test_bootstrap_ci_properties():
    vals = list(np.random.default_rng(0).normal(10, 2, 40))
    lo, hi = bootstrap_ci(vals)
    assert lo < np.mean(vals) < hi and (lo, hi) == bootstrap_ci(vals)  # deterministic
    assert bootstrap_ci([3.0] * 10) == (3.0, 3.0) and bootstrap_ci([4.0]) == (4.0, 4.0)
    assert all(math.isnan(x) for x in bootstrap_ci([]))
    wide = bootstrap_ci(vals[:5])
    assert wide[1] - wide[0] > hi - lo  # fewer samples -> wider interval


def synth_rows():
    rows = []
    for seed in range(5):
        for variant, base, ok in (("B_mock_greedy", 100.0, 1), ("C_mock_milp", 90.0, 1), ("D_mock_milp_replan", 90.0, 1)):
            rows.append({"scenario": "s", "variant": variant, "seed": seed, "mission_success": ok if not (variant == "C_mock_milp" and seed < 2) else 0,
                         "plan_accepted": 1, "makespan_s": base + seed, "travel_cells": 50 + seed, "energy_total": 40.0, "allocation_objective": 5.0,
                         "recovery_ticks_mean": None, "replan_compute_ms_mean": None, "n_replans": 0, "invalid_plan_rejections": 0, "collisions": 0,
                         "tasks_not_succeeded": 0, "task_completion_rate": 1.0})
    return rows


def test_summarize_uses_successful_runs_for_makespan_and_reports_n():
    s = {(r["variant"]): r for r in summarize(synth_rows())}
    c = s["C_mock_milp"]
    assert c["n_runs"] == 5 and c["n_success"] == 3 and c["makespan_s_n"] == 3  # failed runs excluded from makespan
    assert c["makespan_s_mean"] == pytest.approx(np.mean([90 + 2, 90 + 3, 90 + 4]))
    assert c["travel_cells_n"] == 5 and s["B_mock_greedy"]["success_ci_hi"] == 1.0


def test_paired_differences_are_within_seed_and_drop_missing_pairs():
    out = {(p["x"], p["y"], p["metric"]): p for p in paired_differences(synth_rows())}
    p = out[("B_mock_greedy", "C_mock_milp", "makespan_s")]
    assert p["mean_diff_y_minus_x"] == pytest.approx(-10.0) and p["ci_lo"] == p["ci_hi"] == pytest.approx(-10.0) and p["n_pairs"] == 3
    s = out[("C_mock_milp", "D_mock_milp_replan", "mission_success")]
    assert s["n_pairs"] == 5 and s["mean_diff_y_minus_x"] == pytest.approx(0.4)


# ------------------------------------------------------------------ experiment runner / report
@pytest.fixture(scope="module")
def results(tmp_path_factory):
    out = tmp_path_factory.mktemp("results")
    scen = [SCEN / "s0_nominal.yaml", SCEN / "s3_agent_failure.yaml", SCEN / "s7_new_task.yaml"]
    run_all(scen, [0, 1, 2], out, progress=False)
    return out


def test_run_all_writes_complete_grid_and_manifest(results):
    rows = load_runs(results / "runs.csv")
    assert len(rows) == 3 * len(VARIANTS) * 3
    assert {(r["scenario"], r["variant"], r["seed"]) for r in rows} == {(s, v, k) for s in ("s0_nominal", "s3_agent_failure", "s7_new_task") for v in VARIANTS for k in range(3)}
    manifest = json.loads((results / "manifest.json").read_text())
    assert manifest["n_runs"] == len(rows) and "mock" in manifest["provider"]
    traces = sorted(p.name for p in (results / "traces").glob("*.jsonl"))
    assert len(traces) == 3 * len(VARIANTS) and all("seed0" in t for t in traces)  # traces=first -> seed 0 only


def test_saved_traces_reproduce_the_csv_metrics_and_tampering_is_detected(results):
    assert verify_traces(results) == []
    with open(results / "runs.csv", newline="") as h:
        rows = list(csv.DictReader(h))
    fields = list(rows[0].keys())
    target = next(r for r in rows if r["scenario"] == "s0_nominal" and r["variant"] == "D_mock_milp_replan" and r["seed"] == "0")
    target["makespan_s"] = "999999.0"
    tampered = results / "tampered"
    (tampered / "traces").mkdir(parents=True)
    for p in (results / "traces").glob("*.jsonl"):
        (tampered / "traces" / p.name).write_text(p.read_text())
    with open(tampered / "runs.csv", "w", newline="") as h:
        w = csv.DictWriter(h, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    problems = verify_traces(tampered)
    assert len(problems) == 1 and "makespan_s" in problems[0] and "s0_nominal__D_mock_milp_replan__seed0" in problems[0]


def test_experiments_are_deterministic_across_invocations_and_job_counts(results, tmp_path):
    scen = [SCEN / "s3_agent_failure.yaml"]
    run_all(scen, [0, 1], tmp_path / "a", variants=["D_mock_milp_replan", "E_mock_nearest_replan"], traces="none", progress=False)
    run_all(scen, [0, 1], tmp_path / "b", variants=["D_mock_milp_replan", "E_mock_nearest_replan"], traces="none", jobs=2, progress=False)
    drop = {"replan_compute_ms_mean", "planner_latency_s"}
    a, b = load_runs(tmp_path / "a" / "runs.csv"), load_runs(tmp_path / "b" / "runs.csv")
    assert [{k: v for k, v in r.items() if k not in drop} for r in a] == [{k: v for k, v in r.items() if k not in drop} for r in b]


def test_nominal_results_are_complete_and_fault_ablation_is_visible(results):
    rows = load_runs(results / "runs.csv")
    nominal = [r for r in rows if r["scenario"] == "s0_nominal"]
    assert all(r["mission_success"] == 1 and r["collisions"] == 0 for r in nominal)
    s3 = {v: [r["mission_success"] for r in rows if r["scenario"] == "s3_agent_failure" and r["variant"] == v] for v in VARIANTS}
    assert sum(s3["C_mock_milp"]) == 0 and sum(s3["D_mock_milp_replan"]) == 3  # replanning ablation: measured, not assumed


def test_plots_and_report_generation(results):
    from autonomous_collab.planner import MockProvider
    from autonomous_collab.planner_eval import run_planner_eval
    from autonomous_collab.experiments import catalog_dicts
    summary = run_planner_eval(MockProvider(catalog_dicts(load_settings())), results / "planner_eval")
    assert summary["provider"] == "mock" and summary["fault_modes_executed_when_persistent"] == 0
    assert cli.main(["plots", "--results", str(results)]) == 0
    figs = results / "figures"
    for name in ("success_heatmap.png", "nominal_metrics.png", "recovery.png", "timeline_nominal.png", "timeline_failure.png", "trajectories_nominal.png", "planner_eval.png"):
        assert (figs / name).stat().st_size > 5000, name
    report = build_report(results, SCEN).read_text()
    for needle in ("## 1. Provenance", "mock", "## 5. Faults", "Wilson", "figures/success_heatmap.png", "Limitations"):
        assert needle in report
    summ = {(r["scenario"], r["variant"]): r for r in csv.DictReader(open(results / "summary.csv"))}
    r = summ[("s3_agent_failure", "D_mock_milp_replan")]
    assert f"{r['n_success']}/{r['n_runs']}" in report  # every reported number comes from the saved summary


def test_planner_eval_results_match_labels(results):
    rows = list(csv.DictReader(open(results / "planner_eval" / "prompts.csv")))
    assert len(rows) >= 12
    assert all(r["correct"] == "1" for r in rows if r["category"] in ("adversarial", "ambiguous"))
    miss = [r["prompt_id"] for r in rows if r["correct"] == "0"]
    assert miss == ["p07_long_colloquial"]  # honest: the keyword mock misses the colloquial phrasing
    faults = list(csv.DictReader(open(results / "planner_eval" / "fault_modes.csv")))
    assert all(r["executed"] == "0" for r in faults if r["persistent"] == "1" and r["fault"] != "markdown_wrapped")
    assert all(r["executed"] == "1" for r in faults if r["persistent"] == "0" and r["fault"] not in ("provider_fatal",))
