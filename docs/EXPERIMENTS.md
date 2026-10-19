# Experiment design, metric definitions, interpretation

Reproduce everything: `scripts/run_all.sh` (≈ 2 min on one core). Outputs in `results/`: `runs.csv` (one row/run), `summary.csv` (per scenario×variant), `paired.csv` (within-seed differences), `manifest.json`, `traces/*.jsonl` (seed-0 event logs), `planner_eval/`, `figures/`, `report.md` (auto-generated; every number traceable to the CSVs). `autocollab verify-traces` recomputes metrics from the raw traces and compares them with `runs.csv`.

## Design
- **Variants** (planner → allocator, replanning): A manual→greedy, off · B mock→greedy, off · C mock→MILP, off · D mock→MILP, **on** · E mock→nearest (online), on · F mock→Hungarian (online), on · G mock→greedy, on. (A–D follow the project guide; E–G add the rule-based baseline, the Hungarian method and a greedy+replanning ablation.)
- **Scenarios (14):** s0 nominal · s1 scarce capability · s2 blocked route · s3 agent failure · s4a malformed output repaired · s4b malformed persistent · s5 cyclic plan · s6a low battery (partial) · s6b all agents below energy needed · s7 new task · s8 failure during non-idempotent task · s9 provider down · s10 only reporter fails · s11 zone becomes unreachable.
- **Paired design:** the same seed gives identical agent starts, battery and duration jitter and failure times across variants (draws keyed by `(seed, label)`, independent of execution order — tested). 30 seeds (0–29). Randomness: start jitter (≤2 cells), battery ±5 %, task-duration ±20 %, failure tick windows.
- **Statistics:** success = Wilson 95 % interval; continuous metrics = mean, sd, percentile-bootstrap 95 % CI (2000 resamples, resampling unit = seed, fixed RNG seed); paired differences y − x with bootstrap CI over seeds where both runs have the metric. Failed/timed-out runs are **never dropped**; makespan/objective are defined only for successful runs and the report prints `n`.
- No tuning on results: weights and thresholds were fixed in `config/default.yaml` before the full run. (Scenario designs were adjusted during development after diagnosing defects; see `PROJECT_STATUS.md`.)

## Metric definitions (all computed from the JSONL log only)
| Metric | Definition | Runs contributing |
|---|---|---|
| mission_success | 1 iff final status `COMPLETE` (all mandatory tasks SUCCEEDED by postcondition) | all |
| task_completion_rate | mandatory SUCCEEDED / mandatory in the validated mission (0 if no plan accepted; omitted tasks never count as done) | all |
| makespan_s | simulated time of the last SUCCEEDED transition | successful runs |
| travel_cells | executed moves, incl. yield/avoidance moves | all |
| energy_total | move energy + task energy actually spent (proxy, not battery physics) | all |
| allocation_objective | Σ c_ij of executing-agent pairs from the **initial** snapshot, same weights for all allocators | successful runs |
| recovery_ticks | per replan: first triggering event → first EXECUTING/SUCCEEDED transition afterwards (0 if nothing left; unrecovered counted separately) | runs with replans |
| replan_compute_ms | wall-clock of the replan routine (not deterministic; excluded from reproducibility checks) | runs with replans |
| invalid_plan_rejections | planner attempts rejected by validators + rejected added tasks | all |
| collisions | same-cell or edge-swap found by the independent per-tick audit | all |
| monitor_rejections | proposed moves refused/held by the SafetyMonitor (prevented conflicts) | all |
| tasks_not_succeeded / blocked / failed | final counts; every non-success carries a reason | all |
| planner_latency_s | Σ provider latency (≈0 for mock; meaningful only for a real provider) | runs with planner calls |

## How to read the results (as measured in the committed run)
These statements are read off `results/report.md`; regenerate to confirm.
- **Nominal (s0), paired, 30 seeds:** MILP vs greedy: makespan −64 s [−96, −30], travel −6.5 cells, energy −3.3, static objective −2.1 (all CIs exclude 0). The online nearest-feasible baseline (E) has the *shortest* makespan (247 s) but a worse static objective than MILP (6.49 vs 6.01): optimising the static objective is not the same as minimising makespan. Hungarian-online (F) is worst (340 s). A and B are identical (the mock reproduces the manual plan exactly), so A→B isolates the planner path only.
- **Replanning ablation (s3, s7, s8):** without replanning 0–1/30 succeed; with it D reaches 25/30 (s3), 30/30 (s7), 30/30 (s8). All 5 D failures in s3 are `EXCLUDED_AFTER_REJECTION(BATTERY_RESERVE)`: the survivor lacked battery for all remaining work, the static MILP energy budget did not foresee it, and the dispatch gate refused it. The route-aware greedy+replan (G) and online variants reach 30/30 in s3.
- **Fail-closed (s4b, s5, s9):** 0/30 plans executed for every LLM-path variant; the manual reference plan (A) is unaffected by LLM faults (30/30) by construction. s4a: one repair succeeds (2 calls, 1 rejection).
- **Honest-failure scenarios (s6b, s10, s11):** 0/30 success for all variants with explicit reasons (`BATTERY_LIMIT`, report blocked after loss of the only reporter, unreachable zone). A correct run here is a *reported* failure.
- **Safety:** 0 collisions in all 2,940 runs; the monitor held/refused 9,033 proposed moves. Simulated evidence only.
- **Planner harness (mock):** 14 labelled prompts, accuracy 0.93; all 3 ambiguous prompts → clarification, all 3 injection prompts rejected; the colloquial prompt p07 is (correctly) a *miss* for the keyword parser; 0 of the persistent corruptions executed.

## What these results do **not** show
Anything about real LLM quality (no real LLM was run), real-robot behaviour, larger maps/fleets, or statistical significance beyond the stated CIs/seeds. Do not infer causation beyond the controlled ablations (same seed, one factor changed).

## Running with a real provider
`autocollab planner-eval --provider anthropic` writes `results/planner_eval_anthropic/` with provider and model recorded; the simulation experiments always use the mock planner so allocation comparisons stay deterministic. Treat LLM nondeterminism as measured behaviour (re-run and report variance), not noise.
