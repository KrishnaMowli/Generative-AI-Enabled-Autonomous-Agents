"""Command-line entry point: ``autocollab <command>`` (or ``python -m autonomous_collab``)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional, Sequence

from .config import REPO_ROOT, load_scenario, load_settings, settings_for_scenario
from .errors import AutoCollabError
from .experiments import VARIANTS, build_planner, catalog_dicts, run_all
from .logging_utils import logger
from .planner import AnthropicProvider, LLMPlanner, MockProvider, make_provider
from .runner import run_mission

DEFAULT_SCENARIO = REPO_ROOT / "scenarios" / "s0_nominal.yaml"


def load_dotenv(path: Path = REPO_ROOT / ".env") -> None:
    """Minimal .env loader (KEY=VALUE lines); never overrides variables already set."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _cmd_run(args: argparse.Namespace) -> int:
    scenario = load_scenario(Path(args.scenario))
    if args.mission:  # a custom mission has no hand-written checklist: derive it from the text
        scenario = scenario.model_copy(update={"required_outcomes": []})
    settings = settings_for_scenario(scenario, load_settings())
    provider_kind = args.provider or os.environ.get("AUTOCOLLAB_PROVIDER", "mock")
    if args.planner == "manual":
        planner, label = build_planner("manual", scenario, settings), "manual"
    elif provider_kind == "mock":
        planner, label = build_planner("mock", scenario, settings), "mock"
    else:
        provider = make_provider(provider_kind, catalog_dicts(settings))
        planner, label = LLMPlanner(provider, settings.planner_limits), provider_kind
        print(f"Using REAL provider {provider.name} / model {provider.model} (requires API key; calls are billed)")
    result = run_mission(scenario, settings, planner=planner, allocator_name=args.allocator, replan=not args.no_replan, seed=args.seed,
                         mission_text=args.mission, variant="cli", provider_label=label, log_path=Path(args.trace) if args.trace else None)
    print("\n".join(result.summary_lines()))
    if args.trace:
        print(f"event log: {args.trace}")
    return 0 if result.success else 1


def _cmd_demo(args: argparse.Namespace) -> int:
    name = "s3_agent_failure.yaml" if args.failure else "s0_nominal.yaml"
    scenario = load_scenario(REPO_ROOT / "scenarios" / name)
    settings = settings_for_scenario(scenario, load_settings())
    out = Path(args.out)
    print(f"=== Demo: {scenario.name} ===\n{scenario.description}\nMission: {scenario.mission_text}\n(offline mock planner; no API key, no network)\n")
    codes = 0
    configs = [("with replanning (variant D: MILP + replan)", "milp", True)]
    if args.failure:
        configs.append(("WITHOUT replanning (variant C: MILP, replan off)", "milp", False))
    for title, alloc, replan in configs:
        print(f"--- {title}, seed {args.seed} ---")
        res = run_mission(scenario, settings, planner=build_planner("mock", scenario, settings), allocator_name=alloc, replan=replan,
                          seed=args.seed, variant="demo", provider_label="mock", log_path=out / f"{scenario.name}_{'replan' if replan else 'noreplan'}.jsonl")
        print("\n".join(res.summary_lines()))
        for r in res.records:
            if r["type"] in ("replan",):
                print(f"  [t={r['tick']}] REPLAN v{r['plan_version']} triggers={r['triggers']} unfinished={r['unfinished']} compute={r['compute_ms']} ms")
            elif r["type"] == "event" and r["event_type"] in ("AGENT_FAILED", "CELL_BLOCKED", "TASK_ADDED", "LOW_BATTERY"):
                print(f"  [t={r['tick']}] EVENT {r['event_type']} agent={r.get('agent_id')}")
        codes |= 0 if res.success else 1
        print()
    print(f"Event logs written to {out}/")
    return 0


def _cmd_experiments(args: argparse.Namespace) -> int:
    from .reporting import build_summaries
    scen = sorted(Path(args.scenarios).glob(args.pattern))
    out = Path(args.out)
    print(f"Running {len(scen)} scenarios x {len(args.variants or VARIANTS)} variants x {args.seeds} seeds ...")
    rows = run_all(scen, list(range(args.seeds)), out, variants=args.variants, jobs=args.jobs, traces=args.traces)
    build_summaries(out)
    print(f"{len(rows)} runs -> {out}/runs.csv, summary.csv, paired.csv, manifest.json")
    return 0


def _cmd_planner_eval(args: argparse.Namespace) -> int:
    from .planner_eval import run_planner_eval
    settings = load_settings()
    kind = args.provider or os.environ.get("AUTOCOLLAB_PROVIDER", "mock")
    provider = make_provider(kind, catalog_dicts(settings))
    out = Path(args.out) if kind == "mock" else Path(args.out + "_" + kind)
    summary = run_planner_eval(provider, out)
    print(json.dumps(summary, indent=2))
    print(f"-> {out}/")
    return 0


def _cmd_plots(args: argparse.Namespace) -> int:
    from . import plotting as P
    from .reporting import build_summaries
    res = Path(args.results)
    figs = res / "figures"
    figs.mkdir(parents=True, exist_ok=True)
    _, summary, _ = build_summaries(res)
    P.plot_success_heatmap(summary, figs / "success_heatmap.png")
    P.plot_metric_bars(summary, figs / "nominal_metrics.png")
    P.plot_recovery(summary, figs / "recovery.png")
    tr = res / "traces"
    pairs = [("s0_nominal__D_mock_milp_replan__seed0.jsonl", "timeline_nominal.png", "Nominal run (D, seed 0)"),
             ("s3_agent_failure__D_mock_milp_replan__seed0.jsonl", "timeline_failure.png", "Agent failure + replanning (D, seed 0)")]
    for trace, fig, title in pairs:
        if (tr / trace).exists():
            P.plot_timeline(tr / trace, figs / fig, title)
    if (tr / pairs[0][0]).exists():
        P.plot_trajectories(tr / pairs[0][0], Path(args.scenarios) / "s0_nominal.yaml", figs / "trajectories_nominal.png", "Agent trajectories, nominal run (D, seed 0)")
    pe = res / "planner_eval"
    if (pe / "prompts.csv").exists():
        P.plot_planner_eval(pe / "prompts.csv", pe / "fault_modes.csv", figs / "planner_eval.png")
    print(f"figures -> {figs}/")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    from .reporting import build_report
    print(f"report -> {build_report(Path(args.results), Path(args.scenarios))}")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    from .reporting import verify_traces
    problems = verify_traces(Path(args.results))
    for line in problems:
        print("MISMATCH:", line)
    if not problems:
        print("OK: every saved trace reproduces its metrics row in runs.csv")
    return 1 if problems else 0


def _cmd_validate(args: argparse.Namespace) -> int:
    settings = load_settings()
    n = 0
    for path in sorted(Path(args.scenarios).glob("s*.yaml")):
        from .environment import GridWorld
        sc = load_scenario(path)
        world = GridWorld.from_spec(sc.world)
        for a in sc.agents:
            if "navigation" not in a.capabilities:
                raise AutoCollabError(f"{path.name}: agent {a.agent_id} lacks 'navigation'")
            if not world.is_passable(tuple(a.start)):
                raise AutoCollabError(f"{path.name}: agent {a.agent_id} starts on an impassable cell")
        settings_for_scenario(sc, settings)
        n += 1
    print(f"OK: settings (digest {settings.digest()}) and {n} scenarios are valid")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autocollab", description="GenAI task planning + dynamic allocation for heterogeneous agents (simulation).")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="run one mission")
    r.add_argument("--scenario", default=str(DEFAULT_SCENARIO)); r.add_argument("--mission", help="override the scenario's mission text")
    r.add_argument("--provider", choices=["mock", "anthropic"], help="default: $AUTOCOLLAB_PROVIDER or mock")
    r.add_argument("--planner", choices=["llm", "manual"], default="llm")
    r.add_argument("--allocator", choices=["milp", "greedy", "nearest", "hungarian"], default="milp")
    r.add_argument("--no-replan", action="store_true"); r.add_argument("--seed", type=int, default=0); r.add_argument("--trace", help="write JSONL event log here")
    r.set_defaults(fn=_cmd_run)
    d = sub.add_parser("demo", help="offline demonstration (nominal, or --failure for failure + replanning)")
    d.add_argument("--failure", action="store_true"); d.add_argument("--seed", type=int, default=0); d.add_argument("--out", default="results/demo")
    d.set_defaults(fn=_cmd_demo)
    e = sub.add_parser("experiments", help="run scenario x variant x seed grid and write CSV summaries")
    e.add_argument("--seeds", type=int, default=30); e.add_argument("--jobs", type=int, default=1); e.add_argument("--out", default="results")
    e.add_argument("--scenarios", default=str(REPO_ROOT / "scenarios")); e.add_argument("--pattern", default="s*.yaml")
    e.add_argument("--variants", nargs="*", choices=list(VARIANTS)); e.add_argument("--traces", choices=["first", "all", "none"], default="first")
    e.set_defaults(fn=_cmd_experiments)
    pe = sub.add_parser("planner-eval", help="evaluate a planner provider on the labelled prompt set + fault-mode matrix")
    pe.add_argument("--provider", choices=["mock", "anthropic"]); pe.add_argument("--out", default="results/planner_eval")
    pe.set_defaults(fn=_cmd_planner_eval)
    pl = sub.add_parser("plots", help="generate figures from saved results")
    pl.add_argument("--results", default="results"); pl.add_argument("--scenarios", default=str(REPO_ROOT / "scenarios")); pl.set_defaults(fn=_cmd_plots)
    rp = sub.add_parser("report", help="generate results/report.md from saved results")
    rp.add_argument("--results", default="results"); rp.add_argument("--scenarios", default=str(REPO_ROOT / "scenarios")); rp.set_defaults(fn=_cmd_report)
    vt = sub.add_parser("verify-traces", help="recompute metrics from saved traces and compare with runs.csv")
    vt.add_argument("--results", default="results"); vt.set_defaults(fn=_cmd_verify)
    v = sub.add_parser("validate-config", help="validate settings and every scenario file")
    v.add_argument("--scenarios", default=str(REPO_ROOT / "scenarios")); v.set_defaults(fn=_cmd_validate)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except AutoCollabError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
