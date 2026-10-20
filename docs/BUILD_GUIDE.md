# BUILD GUIDE — rebuilding this project from scratch, phase by phase

This guide maps the original roadmap ([`reference/GenAI_Autonomous_Collaboration_Project_Guide.pdf`](reference/GenAI_Autonomous_Collaboration_Project_Guide.pdf)) onto **this repository**: for each of the 10 phases it says what to build, which files hold the finished version, how to prove the phase is done (an *exit gate* you can run), and which mistakes were actually made and fixed while building it.

**Rules the roadmap insists on (and this build followed):**
1. Build the **non-LLM system first**; connect the language model only after execution, allocation and safety work with a hand-written mission.
2. The LLM may only **propose** tasks. Validators, allocator, scheduler and safety gate are deterministic code.
3. **Do not move to the next phase until the exit gate passes.**
4. Never claim more than the experiments establish (simulation only; no hardware; mock planner).

**How to use this guide.** Option A — *study*: read each phase, open the listed files, run the gate. Option B — *rebuild*: create an empty project, write the files of each phase yourself (the "Interfaces" lines say what each must provide), and use the gate (tests in this repo) to check your version; the finished tests are the specification. Set up first: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && pip install -e .` and `export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` if ROS is sourced.

| Phase | Roadmap title | Guide's own effort estimate (independent hours, *not measured here*) |
|---|---|---|
| 0 | Executable specification | 2–4 |
| 1 | Repository + environment | 2–4 |
| 2 | Data contracts + validators | 4–7 |
| 3 | World simulator | 6–10 |
| 4 | Dependencies, scheduling, deterministic execution | 4–7 |
| 5 | Costs, allocation, baselines | 6–10 |
| 6 | Generative-AI decomposer | 5–9 |
| 7 | Semantic validation + safety boundary | 5–8 |
| 8 | Integration + event-driven replanning | 6–10 |
| 9 | Automated regression + experiments | 6–10 |
| 10 | Evaluation report + demo | 4–8 |

---

## Phase 0 — Convert the idea into an executable specification
**Goal:** remove ambiguity before coding: scenario, allowed actions, success criteria, safety rules.
**Decide and write down:** one mission; 3 named zones; task types and their simulated effect (`visual_inspection`, `environmental_reading`, `compile_report`); agent capabilities (at least one agent *lacks* a capability some task needs); mandatory vs optional tasks; priority scale 1–5; durations and energy; what "task success" and "collision" mean (success = postcondition holds; collision = same cell or swapped edge in one tick); non-goals (no hardware, no LLM motor commands, no safety certification).
**Finished version:** [`scenarios/s0_nominal.yaml`](../scenarios/s0_nominal.yaml) (mission, map, agents, the hand-written `manual_plan`), [`config/default.yaml`](../config/default.yaml) (task catalogue, limits, weights).
**Exit gate:** you can execute the mission *on paper* from `manual_plan`: every task has a legal zone, a required capability, a dependency list and an unambiguous success condition.

## Phase 1 — Repository and reproducible environment
**Build:** `pyproject.toml`, `requirements.txt`, `.gitignore`, `.env.example`, `src/autonomous_collab/config.py` (loads and validates YAML at startup), `cli.py` command `validate-config`.
**Decisions:** keep settings in YAML, not in code; secrets only in an ignored `.env`; tests must need no API key.
**Exit gate:**
```bash
autocollab validate-config          # OK: settings (digest 4c5c4f6813c4) and 14 scenarios are valid
python -m pytest tests/test_models_config.py        # 43 passed
```
**Pitfall met:** `bash_tool`/`sh` has no brace expansion (`mkdir a/{b,c}` creates a folder literally named `{b,c}`) — use explicit paths.

## Phase 2 — Data contracts and validators
**Build (in this order):** `models.py` (strict `TaskSpec`, plan envelope, agent/world/scenario models, the stable rejection codes class `V`) → `task_graph.py` (cycle detection, topological order, depth) → `task_state.py` (the task state machine) → `validation/schema_validator.py` (raw text → typed plan) → `validation/semantic_validator.py`.
**Interfaces:** `TaskSpec` forbids extra fields and does not coerce types (`True` is not an integer priority). The state machine: `PENDING→ASSIGNED→READY→EXECUTING→SUCCEEDED`; `SUCCEEDED` is terminal; `FAILED/BLOCKED→PENDING` only via the replanner.
**Exit gate:**
```bash
python -m pytest tests/test_task_graph_state.py tests/test_plan_validation.py     # 101 passed
```
(every invalid example is rejected with a *specific* code; all 49 state-pair transitions tested).
**Pitfalls met:** schema strictness (bool-as-int); parse exactly one JSON document (optionally in one code fence) — never guess which part of a response was "the plan".

## Phase 3 — Deterministic multi-agent world simulator
**Build:** `environment.py` (grid, obstacles, dynamic blocked cells, A\*), `executor.py` (simulated task effects + postconditions), `simulator.py` (agents, battery, one move per tick, collision audit), `rng.py` (seeded streams keyed by `(seed, label)`), `logging_utils.py` (JSONL event log).
**Decisions:** conservative move rule (destination must be free at the *start* of the tick); randomness only from keyed streams so different variants see identical draws; the simulator owns truth and never trusts a "success" claim.
**Exit gate:**
```bash
python -m pytest tests/test_environment.py tests/test_safety_simulator.py          # 38 passed
```
(same seed → same trace; invalid routes and low battery handled explicitly; A\* agrees with BFS on random grids).
**Pitfalls met (real defects, each now has a regression test):** idle agent trapped in a one-cell alcove by a visitor (deadlock); retreat/yield livelock (fixed by giving yield moves higher priority); head-on meeting of two moving agents; "cleared" failed agent still occupying its cell.

## Phase 4 — Dependencies, scheduling, deterministic execution
**Build:** the dispatch/scheduler part of `coordinator.py` and `ManualPlanner` in `planner/llm_planner.py`; `runner.py` runs a hand-written plan with no LLM (this is **variant A**).
**Rules:** a task is dispatchable only when every prerequisite is `SUCCEEDED`; handlers are looked up in a finite table by allowlisted `task_type` (never from model text); a task is `SUCCEEDED` only after its postcondition passes.
**Exit gate:**
```bash
python -m pytest tests/test_integration.py -k A_manual_greedy                       # 14 passed
autocollab run --scenario scenarios/s0_nominal.yaml --planner manual --allocator greedy --no-replan
```
**Pitfall met:** the safety monitor's "idle" flag was confused with "has queued work", so *every* dispatch was rejected.

## Phase 5 — Path costs, allocation, baselines
**Build:** `allocation/cost_model.py` (cost *and* hard feasibility), `greedy.py`, `nearest.py`, `hungarian.py`, `milp.py`, `base.py`.
**Interfaces:** an allocator receives tasks + agent views + cost model and returns `assignments`, `unassigned` (with a reason code) and an objective. **Hard constraints are never costs:** capability, availability, reachability, battery reserve. MILP omits infeasible variables; Hungarian uses a big penalty only to let the solver run, then **discards** penalty pairs.
**Exit gate:**
```bash
python -m pytest tests/test_allocation.py                                          # 47 passed
```
(MILP equals exhaustive enumeration on random small instances; no allocator ever assigns an infeasible pair; deterministic including ties).
**Pitfalls met:** the MILP is a *static assignment approximation* (travel between one agent's several tasks is not modelled) — say so, never call it globally optimal; its energy budget is optimistic after failures (see Phase 8).

## Phase 6 — Generative-AI task decomposer
**Build:** `planner/interface.py` (provider-independent contracts), `planner/providers.py` (`MockProvider`, `FaultInjectingProvider`, `AnthropicProvider` over plain HTTPS with an injectable transport), `planner/mission_text.py` (mission screening + the keyword decomposer behind the mock), `planner/prompts.py` + `prompts/task_decomposer_v1.txt`, `planner/llm_planner.py` (`LLMPlanner`).
**Rules:** mission text goes into the prompt as JSON-escaped *data*; ask for tasks and dependencies only, never agents; at most 1 repair prompt, 2 provider retries, 4 calls — then **fail closed**; the repair prompt carries violation codes, **not** the rejected raw output; save prompt version, model, latency, raw output.
**Exit gate:**
```bash
python -m pytest tests/test_planner.py                                             # 97 passed
autocollab planner-eval --provider mock --out results_check/planner_eval            # adversarial_rejected [1,1,1]; persistent faults executed: 0
```
**No live LLM call was ever made in this project** — the Anthropic adapter is tested only with a fake transport.

## Phase 7 — Semantic validation and the safety boundary
**Build:** finish `validation/semantic_validator.py` (allowlists; unknown/self/cyclic dependencies; depth and size limits; report-after-inspections; capability and reachability feasibility; **mission coverage both ways**) and `validation/safety_monitor.py` (action allowlist, dispatch gate, central move resolution).
**Exit gate:** the Phase 2/3 test files above plus `tests/test_planner.py::test_adversarial_model_output_cannot_reach_execution`. All malformed/invalid examples are rejected *before* allocation; a valid plan still cannot bypass the per-action monitor.
**Pitfall met:** an independent per-tick collision audit (not using the monitor) is shown by test to catch an approve-everything monitor.

## Phase 8 — Integration and event-driven replanning
**Build:** replanning in `coordinator.py` (debounce, plan versions, retry policies incl. `verify_before_retry`, release of unstarted work), fault injection in `runner.py` (`FaultSchedule`), the full pipeline `runner.run_mission`, scenarios `s2`–`s11`.
**Rules:** completed work is immutable; replan only unfinished work; no LLM call on ordinary events; every replan has a reason and a new plan version; if nothing is feasible, report it — never claim success.
**Exit gate:**
```bash
python -m pytest tests/test_coordinator_replan.py tests/test_integration.py        # 28 + 155 passed
autocollab demo --failure                                                           # tasks 7/7 with replanning, 4/7 without
```
**Pitfalls met:** an injected task was silently dropped when replanning was off, so the baseline "succeeded" (regression test r7); the allocator's static energy budget let an over-long task through after a failure — the dispatch gate refused it and the mission now reports `EXCLUDED_AFTER_REJECTION(BATTERY_RESERVE)`.

## Phase 9 — Automated regression and experiments
**Build:** `metrics.py` (metrics computed *only* from the event log), `experiments.py` (scenario × variant × seed grid, parallel workers), `analysis.py` (Wilson and bootstrap intervals, paired differences), `planner_eval.py`; `tests/test_metrics_experiments.py`, `tests/test_regressions.py`.
**Design:** 7 variants (A manual+greedy … G greedy+replan), 14 scenarios, 30 seeds, paired by seed.
**Exit gate:**
```bash
python -m pytest tests/test_metrics_experiments.py tests/test_regressions.py       # 26 passed
autocollab experiments --seeds 30 --jobs 2 --out results_check                    # 2940 runs, ~2 min
python scripts/verify_reproduction.py results_check                                # identical to committed results
autocollab verify-traces --results results                                        # metrics recomputed from raw logs match
```
**Pitfalls met:** parallel workers started with `fork` deadlocked on a multi-core machine → use `spawn`; a tamper test must edit one CSV row via a parser (string replace hit the wrong row); a mutation check (15 deliberate breakages) found one untested rule (postcondition) → new test.

## Phase 10 — Evaluation report and reproducible demo
**Build:** `plotting.py`, `reporting.py` (writes `results/report.md` from saved CSVs only), `cli.py` commands `demo`, `plots`, `report`; scripts; the docs.
**Exit gate:** a reader can reproduce the nominal run and a fault-recovery run from the README, and every reported number points to a saved file:
```bash
bash scripts/quickstart.sh
python scripts/headline_numbers.py
python scripts/check_traceability.py                                               # 86 referenced tests, none missing
SEEDS=30 JOBS=2 scripts/run_all.sh                                                 # whole pipeline, exits 0
```

---

## Completion checklist (from the roadmap) and where each item lives
| Roadmap item | Where |
|---|---|
| Spec, capabilities, task types, acceptance criteria version-controlled | `scenarios/`, `config/default.yaml`, `docs/` |
| Simulator runs without any external LLM call | all tests and demos (mock planner) |
| Task schema and state transitions validated and tested | `tests/test_models_config.py`, `tests/test_task_graph_state.py` |
| Manually authored mission completes end to end | variant A, `tests/test_integration.py -k A_manual_greedy` |
| Allocators reject ineligible assignments and report infeasibility | `tests/test_allocation.py` |
| LLM produces candidates only; every candidate passes local checks | `tests/test_planner.py`, `tests/test_plan_validation.py` |
| Every move and dispatch passes the deterministic SafetyMonitor | `validation/safety_monitor.py`, `tests/test_safety_simulator.py` |
| Faults trigger replanning while preserving finished work | `tests/test_coordinator_replan.py` |
| Same scenario/seed set for all variants; metrics traceable to logs | `experiments.py`, `autocollab verify-traces` |
| README reproduces the demo; report states limitations and failures | `README.md`, `results/report.md`, `docs/PROJECT_STATUS.md` |
| No secret committed; no claim beyond the evidence | `.gitignore`, `.env.example`; scope boxes in README |
| **Optional:** ROS 2 / Gazebo extension | **not implemented** — see [`ROS2_ROADMAP.md`](ROS2_ROADMAP.md) |

## Where to go after finishing
Run a real model (`README` → "Optional: real LLM provider"), replace the static MILP by a route-aware one, add ROS 2 stage S0 (`ROS2_ROADMAP.md`), or add your own scenario: copy `scenarios/s0_nominal.yaml`, change the map/agents/events, run `autocollab validate-config`, then `autocollab run --scenario <your file>`.
