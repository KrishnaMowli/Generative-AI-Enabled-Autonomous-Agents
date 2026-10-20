# START HERE

You opened the ZIP (or the GitHub repo) and want to know **which file to open and what to do**. This page is the map. It assumes nothing: not Python experience beyond running commands, not robotics, not LLMs.

## 0. What is this project, in 60 seconds
You type a mission such as *"Inspect zones A, B and C, take readings, then compile a report."* A language model **proposes** a list of tasks with dependencies. Ordinary, deterministic code then **checks** that list (rejecting anything invalid), **assigns** the tasks to simulated robots with different abilities, **simulates** them moving on a grid, **blocks unsafe actions**, and **replans** if a robot fails or the map changes. The model never controls a robot.

Honest scope: it is a **simulation**; the planner used for all results is an offline **mock** (not a real LLM); a real-LLM adapter exists but was never run live; **ROS 2 / Gazebo is not implemented** (only a plan). Details in the README.

## 1. Which file do I open? (find yourself below)

| You want to… | Open this | Time |
|---|---|---|
| **See that it works** | run `bash scripts/quickstart.sh` (steps in §2) | 3–4 min |
| **Read the overview and results** | [`README.md`](README.md) | 10 min |
| **Check every claim yourself** | [`README.md`](README.md) → "Verify the claims" table | 5 min – 10 min |
| **Understand how it is built** | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 15 min |
| **Rebuild the whole project from scratch, phase by phase** | [`docs/BUILD_GUIDE.md`](docs/BUILD_GUIDE.md) (maps the original roadmap's 10 phases to files, tests and exit-gate commands) | a few days of work |
| **See the original roadmap** | [`docs/reference/GenAI_Autonomous_Collaboration_Project_Guide.pdf`](docs/reference/GenAI_Autonomous_Collaboration_Project_Guide.pdf) | 30 min |
| **Understand the maths of allocation** | [`docs/ALLOCATION.md`](docs/ALLOCATION.md) | 15 min |
| **Understand what the safety layer does and does not guarantee** | [`docs/SAFETY.md`](docs/SAFETY.md) | 10 min |
| **Understand the metrics and experiments** | [`docs/EXPERIMENTS.md`](docs/EXPERIMENTS.md) | 15 min |
| **See which test proves which requirement** | [`docs/TRACEABILITY.md`](docs/TRACEABILITY.md) | 10 min |
| **See what is finished, what is not, known defects** | [`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md) | 10 min |
| **See the results report with plots** | [`results/report.md`](results/report.md) and [`results/figures/`](results/figures) | 10 min |
| **Read what comes next (ROS 2 etc.)** | [`docs/ROS2_ROADMAP.md`](docs/ROS2_ROADMAP.md), README "Novelty and contributions" | 10 min |

## 2. Do this, step by step (first run)

Open a terminal **in the folder that contains `README.md` and `pyproject.toml`** (the project root).

**Step 1 — Check prerequisites.**
```bash
python3 --version          # must print 3.10 or higher
git --version
```
Ubuntu/Debian, if something is missing: `sudo apt install -y python3 python3-venv python3-pip git`.
Windows: use WSL (Ubuntu). macOS: `brew install python`.

**Step 2 — Get the code.**
- From the ZIP: unzip it, then `cd` into the extracted folder (the one containing `README.md`).
- From GitHub: `git clone https://github.com/KrishnaMowli/Generative-AI-Enabled-Autonomous-Agents.git` then `cd Generative-AI-Enabled-Autonomous-Agents`.
- Check you are in the right place: `ls` must show `README.md  pyproject.toml  src  tests  scenarios  scripts ...`.

**Step 3 — Run the one-command check.**
```bash
bash scripts/quickstart.sh
```
It creates a private virtual environment (`.venv/`), installs the dependencies, and runs: config validation → the failure demo → all 535 tests → trace verification. **Success ends with `ALL CHECKS PASSED.`**
You should see in the middle: `COMPLETE ... tasks 7/7` *with* replanning and `INCOMPLETE ... tasks 4/7` *without* it, and `535 passed`.

**Step 4 — Activate the environment for everything else.**
```bash
source .venv/bin/activate
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1     # only needed if your shell has ROS sourced
```

**Step 5 — Look at what it does.**
```bash
autocollab demo                                                        # nominal mission
autocollab run --scenario scenarios/s0_nominal.yaml --seed 0 --trace out.jsonl
autocollab run --mission "Inspect zones A and B, then compile a report."   # your own mission
autocollab run --scenario scenarios/s5_invalid_plan.yaml               # bad plan: rejected, nothing runs
```
`out.jsonl` is the complete event log of the run (one JSON record per event). Everything in `results/` is computed from such logs.

**Step 6 — Verify the big claims** (optional, ~2 minutes for the long one). Use the table in the README ("Verify the claims"). The strongest check regenerates all 2,940 simulated missions and confirms they equal the committed results:
```bash
autocollab experiments --seeds 30 --jobs 2 --out results_check
python scripts/verify_reproduction.py results_check
```

**Step 7 — If something fails:** README → "Troubleshooting" (python vs python3, venv missing, ROS plugin crash, wrong folder).

## 3. How to read the code (suggested order)
1. `scenarios/s0_nominal.yaml` — a complete scenario: mission text, map, agents, the hand-written reference plan.
2. `src/autonomous_collab/models.py` — the data contracts (what a task is; every rejection code).
3. `src/autonomous_collab/validation/semantic_validator.py` — how a proposed plan is accepted or rejected.
4. `src/autonomous_collab/allocation/cost_model.py` then `milp.py` — who does which task, and why.
5. `src/autonomous_collab/validation/safety_monitor.py` — the gate every action must pass.
6. `src/autonomous_collab/simulator.py` — the grid world and robot motion.
7. `src/autonomous_collab/coordinator.py` — scheduling, dispatch, retries, replanning.
8. `src/autonomous_collab/runner.py` — ties everything into one mission run.
9. `tests/` — start with `tests/test_coordinator_replan.py` to see the behaviours stated as checks.

## 4. Glossary (plain language)
| Term | Meaning |
|---|---|
| **Mission** | the natural-language request |
| **Task / TaskSpec** | one unit of work: type, zone, required ability, dependencies, priority, duration |
| **Dependency graph (DAG)** | "task B must wait for task A"; cycles (A waits for B waits for A) are rejected |
| **Mock planner** | offline keyword program that plays the role of the LLM so everything runs without internet or a key |
| **Validator** | deterministic code that accepts or rejects a proposed plan; it never "fixes" it |
| **Allocator** | decides which agent does which task (MILP = exact integer optimisation; Hungarian = one-to-one matching; greedy/nearest = simple baselines) |
| **SafetyMonitor** | the gate that approves or refuses each dispatch and each move |
| **Replanning** | recomputing assignments for unfinished work after a fault; finished work is never redone |
| **Fail closed** | when in doubt (bad plan, no provider), do nothing and report why |
| **Seed** | number that fixes all randomness so runs are reproducible and variants can be compared fairly |
| **Makespan** | simulated time until the last task finishes |

## 5. Complete file index
```
START_HERE.md               this file
README.md                   overview, quick start, results, verification table, novelty, roadmap
LICENSE                     MIT
pyproject.toml              package definition (command `autocollab`)
requirements.txt            dependency ranges           requirements-lock.txt   exact versions the project was tested with
.env.example                template for optional API settings (never commit a real .env)
config/default.yaml         all tunable settings (cost weights, limits, task catalogue)
scenarios/                  s0..s11 scenario files (14) + planner_prompts.yaml (14 labelled prompts)
prompts/                    versioned planner prompt template
schemas/                    JSON Schemas of the planner's output (generated)
src/autonomous_collab/      the code (see §3 and docs/ARCHITECTURE.md)
tests/                      535 automated tests
scripts/                    quickstart.sh, run_all.sh, run_demo.sh, headline_numbers.py, verify_reproduction.py,
                            check_traceability.py, export_schemas.py
docs/                       ARCHITECTURE, ALLOCATION, SAFETY, EXPERIMENTS, TRACEABILITY, PROJECT_STATUS,
                            ROS2_ROADMAP, BUILD_GUIDE, reference/ (original roadmap PDF)
results/                    runs.csv, summary.csv, paired.csv, manifest.json, report.md, figures/, traces/, planner_eval/
```
