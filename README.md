# autonomous-collab

**GenAI-enabled task decomposition and dynamic allocation for heterogeneous autonomous agents** — a simulation-first Python project.

A natural-language mission goes in. A language model (an offline mock by default, or a real LLM through an optional adapter) only **proposes** a structured task graph. Deterministic code then **validates** it, **allocates** tasks to capable agents (MILP, Hungarian, greedy, nearest-feasible), **schedules** by dependencies, passes every action through a **SafetyMonitor**, **simulates** execution on a grid, and **replans** after faults. The model never issues movement commands.

> **Honest scope.** Everything is verified in a *discrete grid simulator* only: no hardware, no formal safety claim. All reported experiments use a **keyword mock planner, not a real LLM**; the real-LLM adapter has never been called against the live API (no key was available). See [What is and is not implemented](#6-what-is-and-is-not-implemented).

## Contents
1. [Requirements](#1-requirements) · 2. [Setup](#2-setup-which-folder-to-run-from) · 3. [Try it in 1 minute](#3-try-it-in-1-minute) · 4. [Verify every claim](#4-verify-the-claims-in-this-repository) · 5. [Results snapshot](#5-results-snapshot) · 6. [Implemented / not implemented](#6-what-is-and-is-not-implemented) · 7. [Repository layout](#7-repository-layout) · 8. [Real LLM provider (optional)](#8-optional-real-llm-provider) · 9. [Troubleshooting](#9-troubleshooting)

---

## 1. Requirements
- Linux or macOS terminal (Windows: use WSL, or adapt the activate command).
- **Python ≥ 3.10** with `venv`. Tested by the author: Python 3.12 (Linux sandbox, 1 CPU) and the demo on Python 3.10 / Ubuntu 22.04. Exact tested package versions: [`requirements-lock.txt`](requirements-lock.txt).
- Internet only for `pip install`. **No API key, no network and no paid service is needed** for anything in sections 3–5.

## 2. Setup (which folder to run from)

**Run every command from the repository root** — the folder that contains `README.md`, `pyproject.toml`, `src/` and `tests/`.

```bash
git clone https://github.com/KrishnaMowli/Generative-AI-Enabled-Autonomous-Agents.git
cd Generative-AI-Enabled-Autonomous-Agents      # the repository root: contains README.md and pyproject.toml
ls                                 # you should see: README.md  config  docs  pyproject.toml  results  scenarios  scripts  src  tests ...

sudo apt install -y python3-venv python3-pip      # Ubuntu/Debian only, if venv is missing
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .                   # provides the `autocollab` command
```

If you use ROS 2 and your shell sources it, run this once in the terminal (it stops ROS's pytest plugins from loading; see [Troubleshooting](#9-troubleshooting)):
```bash
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
```

## 3. Try it in 1 minute
```bash
autocollab demo --failure
```
Runs the same seeded mission twice, offline. Expected: with replanning, `COMPLETE`, `tasks 7/7`, one `REPLAN v2`; without replanning, `INCOMPLETE`, `tasks 4/7`, and a reason for every unfinished task (e.g. `ASSIGNED_TO_UNAVAILABLE_AGENT:agent_1`).

Other quick commands:
```bash
autocollab demo                                                     # nominal mission, no fault
autocollab run --scenario scenarios/s0_nominal.yaml --seed 0 --trace out.jsonl     # one mission + JSONL event log
autocollab run --mission "Inspect zones A and B, then compile a report."            # your own mission text (mock planner)
autocollab run --scenario scenarios/s5_invalid_plan.yaml            # planner proposes a cyclic plan -> rejected, nothing executes (exit code 1)
```

## 4. Verify the claims in this repository

Each row: a claim, the command that checks it, and what you should see. All run offline.

| # | Claim | Command (from repo root) | Expected result | Time |
|---|---|---|---|---|
| 1 | The test suite passes (unit, component, integration, fault injection, regression) | `python -m pytest` | `535 passed` | ~25 s |
| 2 | Settings and all 14 scenarios are valid | `autocollab validate-config` | `OK: settings (digest 4c5c4f6813c4) and 14 scenarios are valid` | 1 s |
| 3 | Core system runs offline; replanning recovers from agent failure | `autocollab demo --failure` | `COMPLETE` 7/7 with replanning, `INCOMPLETE` 4/7 without | 2 s |
| 4 | Every number in `results/` can be recomputed from the raw event logs | `autocollab verify-traces --results results` | `OK: every saved trace reproduces its metrics row in runs.csv` | 1 s |
| 5 | Headline numbers quoted below come from the saved data | `python scripts/headline_numbers.py` | table in [section 5](#5-results-snapshot) | 1 s |
| 6 | Requirements are traced to real tests | `python scripts/check_traceability.py` | `86 referenced tests, 167 collected test functions; missing: none` | 3 s |
| 7 | **Full experiments reproduce**: 14 scenarios × 7 variants × 30 seeds = 2,940 runs regenerate identically | `autocollab experiments --seeds 30 --jobs 2 --out results_check` then `python scripts/verify_reproduction.py results_check` | `OK: 2940 freshly generated runs are identical to the committed results (wall-clock columns ignored)` | ~2 min |
| 8 | Planner harness: injections rejected, every persistent bad output rejected | `autocollab planner-eval --provider mock --out results_check/planner_eval` | `adversarial_rejected: [1,1,1]`, `fault_modes_executed_when_persistent: 0` | 2 s |
| 9 | Everything end to end (tests + experiments + plots + report + trace check) | `SEEDS=30 JOBS=2 scripts/run_all.sh` (regenerates `results/`) | exits 0; last line `OK: every saved trace reproduces its metrics row in runs.csv` | ~2 min |
| 10 | The tests are not vacuous (mutation check) | see [`docs/TRACEABILITY.md`](docs/TRACEABILITY.md#mutation-check) — 15 deliberate code breakages were each caught | documented, script not shipped | — |

Where to read the evidence: [`results/report.md`](results/report.md) (auto-generated, every number traceable to `results/runs.csv`), [`results/figures/`](results/figures), per-run event logs in [`results/traces/`](results/traces), requirement→test matrix in [`docs/TRACEABILITY.md`](docs/TRACEABILITY.md), metric definitions in [`docs/EXPERIMENTS.md`](docs/EXPERIMENTS.md), honest status and defect log in [`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md).

## 5. Results snapshot

Produced by `python scripts/headline_numbers.py` from `results/runs.csv` (mock planner; 30 seeds; simulation only):

```
runs: 2940   scenarios: 14   variants: 7   seeds: 30
collisions observed (independent audit), all runs: 0
monitor-prevented move conflicts, all runs: 9033

Mission success (successes/runs)           C=MILP no replan  D=MILP+replan  G=greedy+replan  E=nearest+replan
  s3_agent_failure                       0/30          25/30            30/30             30/30
  s7_new_task                            0/30          30/30            30/30             30/30
  s8_uncertain_task                      0/30          30/30            29/30             30/30

Fail-closed scenarios, LLM-path variants B..G (plans executed = successes):
  s4b_malformed_persistent   executed 0/180
  s5_invalid_plan            executed 0/180
  s9_provider_down           executed 0/180
Honest-failure scenarios (must NOT report success):
  s6b_resources_insufficient successes 0/210
  s10_no_feasible_recovery   successes 0/210
  s11_unreachable_zone       successes 0/210

Nominal scenario s0 (mean makespan in s, successful runs only):
  A_manual_greedy              357.3   (n=30)
  B_mock_greedy                357.3   (n=30)
  C_mock_milp                  293.3   (n=30)
  D_mock_milp_replan           293.3   (n=30)
  E_mock_nearest_replan        247.5   (n=30)
  F_mock_hungarian_replan      340.2   (n=30)
  G_mock_greedy_replan         357.3   (n=30)
```

How to read it:
- **Replanning matters**: after an agent failure, no-replan variants complete 0/30 missions; with replanning 25–30/30.
- **MILP + replan recovers only 25/30 in s3** — every failure is a battery limit: the MILP's *static* energy budget under-estimates chained routes, the SafetyMonitor's dispatch gate refuses the over-long task, and the mission reports `EXCLUDED_AFTER_REJECTION(BATTERY_RESERVE)`. The route-aware greedy and online variants reach 30/30.
- **MILP is not "best at everything"**: it beats greedy on nominal makespan (paired difference −64 s, 95 % CI [−96, −30]), but the online nearest-feasible baseline is faster still (247.5 s) while having a worse static objective.
- **Some runs must fail**: s6b/s10/s11 have no feasible solution; a correct system reports failure with reasons (0 successes is the right answer).
- **Zero collisions** is evidence for these scenarios in a discrete simulator, not a safety guarantee.

## 6. What is and is not implemented

| Implemented and tested | Not implemented / unverified |
|---|---|
| Strict LLM output schema; semantic validation (cycles, allowlists, coverage both ways, feasibility); bounded repair; fail-closed | **Live LLM calls** — adapter exists, unit-tested with a fake transport only; model name `claude-sonnet-5-5` unverified |
| Prompt-injection screening + structural defence (model output is data; extra/unrequested fields rejected) | Robust injection defence (pattern screening is evadable; the real boundary is validation + no execution authority) |
| MILP, Hungarian, greedy, nearest allocators; hard battery/capability/reachability constraints | Route-aware (sequence-dependent) MILP; deadline penalties |
| Grid simulator, A*, hold/yield/retreat conflict handling, independent collision audit | Continuous dynamics, localisation, hardware validation |
| Replanning for agent failure, blocked cell, new task, low battery, deadlock; retry policies incl. verify-before-retry | `COMMUNICATION_DELAY` events; automatic LLM re-decomposition on mission change |
| 14 scenarios × 7 variants × 30 seeds, CIs, paired differences, plots, auto-report, trace verification | Real-LLM planner evaluation results |
| — | **ROS 2 / Gazebo** (optional extension; not implemented) |

Modelling assumptions worth knowing: failure scenarios s3/s8/s10 assume the failed agent is cleared from the grid (`failed_agents_block_cell: false`; the stricter default is tested separately); added tasks are not retro-fitted into an existing report's prerequisites. Full list: [`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md).

## 7. Repository layout
```
config/default.yaml        settings: cost weights, limits, task catalog          scenarios/        14 scenario YAMLs + labelled planner prompts
prompts/                   versioned planner prompt                               schemas/          JSON Schemas generated from the models
src/autonomous_collab/     models, config, environment, simulator, coordinator, runner, metrics, experiments, analysis, plotting, reporting, cli
  planner/                 provider interface, mock / fault-injecting / Anthropic providers, mission-text screening, LLMPlanner
  validation/              schema validator, semantic validator, safety monitor
  allocation/              cost model, MILP, Hungarian, greedy, nearest
tests/                     535 tests            scripts/   run_demo.sh, run_all.sh, headline_numbers.py, verify_reproduction.py, check_traceability.py, export_schemas.py
docs/                      ARCHITECTURE (Mermaid), ALLOCATION (maths), SAFETY, EXPERIMENTS (metric definitions), TRACEABILITY, PROJECT_STATUS
results/                   runs.csv, summary.csv, paired.csv, manifest.json, report.md, figures/, traces/, planner_eval/
```
Pipeline: mission text → sanitize → planner (mock/LLM) → schema + semantic validation → allocator → coordinator (dependency scheduler, replanner) → SafetyMonitor → simulator → JSONL log → metrics. Details and diagrams: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md); allocation maths and assumptions: [`docs/ALLOCATION.md`](docs/ALLOCATION.md).

## 8. Optional: real LLM provider
Not needed for anything above, and **a Claude chat subscription does not include API access** — create an API key in the Anthropic Console (billed separately).
```bash
cp .env.example .env               # git-ignored; put ANTHROPIC_API_KEY=... in it (never commit it)
autocollab planner-eval --provider anthropic              # writes results/planner_eval_anthropic/ with provider+model recorded
autocollab run --provider anthropic --scenario scenarios/s0_nominal.yaml
```
Behaviour with a real model is **unverified**; results are written to a separate folder so they are never mixed with mock results. Without a key the run fails closed with `PLANNER_UNAVAILABLE` and executes nothing.

## 9. Troubleshooting
| Symptom | Fix |
|---|---|
| `python: command not found` | Ubuntu has `python3`; use `python3 -m venv .venv`, then `source .venv/bin/activate` (inside the venv `python` works) |
| `autocollab: command not found` / `pytest: command not found` | the venv is not active (`source .venv/bin/activate`) or `pip install -e .` was skipped. Fallback: `PYTHONPATH=src python -m autonomous_collab <command>` and `python -m pytest` |
| pytest crashes with a traceback through `/opt/ros/...` (e.g. `No module named 'lark'`) | ROS's pytest plugins auto-load. Run `export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` first |
| `requirements.txt` not found | you are not in the repository root (`cd` into the folder that contains `README.md`) |
| tests appear to hang | fixed in this version (parallel workers use `spawn`). If you still see it, run `python -m pytest -v -x` and open an issue with the last test name |
| `PLAN_REJECTED` / `NEEDS_CLARIFICATION` | read the printed codes; the mock only understands `zones A, B`, action words (inspect / readings / report) |
| Mission `INCOMPLETE` | the summary lists a reason per task (`BATTERY_LIMIT`, `NO_REACHABLE_PATH`, `BLOCKED_BY_DEPENDENCY:<id>` …) — this is intended behaviour, not a crash |
| `results_check/` appeared | output of verification command 7/8; safe to delete |

## License
MIT — see [`LICENSE`](LICENSE).
