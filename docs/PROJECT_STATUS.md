# PROJECT STATUS — FINAL CHECKPOINT (2026-10-18)

**State: all phases implemented, tested and packaged. Items that remain unverified are listed explicitly below.**

## Phase checklist
| Phase | Status |
|---|---|
| 1 Spec + repo | DONE (`docs/TRACEABILITY.md`, verified by `scripts/check_traceability.py`) |
| 2 Deterministic foundation | DONE (environment, simulator, executor, task FSM) |
| 3 Dependencies + allocation | DONE (greedy, nearest, Hungarian, MILP; brute-force-verified) |
| 4 Safety validation | DONE (SafetyMonitor, validators, independent collision audit) |
| 5 GenAI planning | DONE (mock, fault-injecting, Anthropic adapter via fake-transport tests) |
| 6 Integration | DONE (`runner.run_mission`, coordinator) |
| 7 Replanning | DONE (events, debounce, retry policies, verify-before-retry) |
| 8 Experiments | DONE: 14 scenarios × 7 variants × 30 seeds = 2,940 runs; planner eval; plots; report |
| 9 Hardening | DONE: fresh-venv install + full tests; mutation check; trace verification |
| 10 Delivery | DONE: ZIP `/mnt/user-data/outputs/autonomous-collab.zip` |

## Commands executed and ACTUAL results
- Fresh venv (`python -m venv`, `pip install -r requirements.txt`, `pip install -e .`), Python 3.12.3, no `ANTHROPIC_API_KEY`: `autocollab validate-config` OK (14 scenarios); `autocollab demo` COMPLETE; **`pytest`: 535 passed in 18.9 s**.
- `autocollab experiments --seeds 30 --jobs 1 --out results`: 2,940 runs in ~82 s; `planner-eval --provider mock`; `plots`; `report`; **`verify-traces`: OK** (every saved seed-0 trace reproduces its runs.csv row).
- Observed in results: 0 collisions in 2,940 runs; 0 persistent planner corruptions executed; replanning ablation measured (s3: C 0/30 vs D 25/30; s7 and s8: 0/30 vs 30/30).
- Mutation check: 15 deliberate breakages of safety/validation/allocation/replanning code — 14 caught initially, the survivor (postcondition check) led to a new test and is now caught.
- `scripts/check_traceability.py`: 86 referenced tests, none missing.
- **Final recheck (fresh copy, fresh venv, every command in the README verification table executed literally):** 535 passed; validate-config OK; failure demo 7/7 vs 4/7; verify-traces OK; headline numbers; traceability OK; **2,940 regenerated runs identical to committed results** (`scripts/verify_reproduction.py`, with `--jobs 2`); planner-eval; `scripts/run_all.sh` exit 0.

## NOT run / NOT verified (do not assume)
1. **No live LLM call was ever made** (no API key). `AnthropicProvider` request/response/error handling is unit-tested with a fake transport only; the default model string `claude-sonnet-5-5` and live behaviour are unverified. Run `autocollab planner-eval --provider anthropic` yourself.
2. I tested Python 3.12.3 on a 1-CPU Linux sandbox only (package declares ≥3.10). Multi-core behaviour was first exercised on the user's machine, where defect r11 appeared.
3. ROS 2 / Gazebo extension: **not implemented**.
4. No hardware; no formal safety claim. Zero collisions = evidence for tested scenarios in a discrete simulator.
5. Real-LLM planner quality: unmeasured (the mock is a keyword parser; its 0.93 prompt accuracy says nothing about LLMs).

## Known limitations / unresolved issues (documented, not hidden)
- Static MILP energy budget is a model approximation: can over-/under-estimate chained-route energy (visible in s3 D 25/30 and s6a C 25/30; dispatch gate prevents unsafe execution, task is then reported `EXCLUDED_AFTER_REJECTION(BATTERY_RESERVE)`).
- Recovery scenarios s3/s8/s10 assume failed agents are cleared from the grid (`failed_agents_block_cell: false`); default (true) is physically stricter and tested (r6).
- Added tasks are not retro-fitted into an existing report's prerequisites.
- Re-decomposition by the LLM on mission change is not implemented (events use the deterministic allocator only); `COMMUNICATION_DELAY` events and deadline penalties not implemented.
- 1-wide corridor head-on with no free neighbour for either agent ends in `DEADLOCK` → replan, not guaranteed resolution.
- Scenario designs were adjusted during development after diagnosing defects (documented below), so results are not a blind test.

## Defects found and fixed (each has a regression test)
r1 monitor idle semantics · r2–r4 alcove deadlock / retreat livelock / head-on deadlock · r5 cleared agent still occupying cell · r6 failure-in-zone blocking (modelling choice) · r7 added task silently ignored without replanning (would have inflated baseline success) · r8 duplicate LOW_BATTERY events · r9 bool accepted as int priority (schema strictness verified) · r10 exclusion cause lost in reported reason · **r11 parallel experiment workers used fork and deadlocked on the user's multi-core Linux machine (Ubuntu 22.04, Python 3.10, ROS Humble sourced); fixed with the spawn start method, found only after user testing** · plus test-quality fixes found by review/mutation (weak assertions, tamper test, postcondition test).

## Resume instructions (if work continues)
1. `pip install -e . && pytest` (expect 535 passed) and `scripts/run_all.sh` to regenerate `results/`.
2. Optional next steps, in order: (a) run `planner-eval --provider anthropic` with a real key and add a section to the report; (b) route-aware MILP (sequence-dependent travel) and re-run the allocation comparison; (c) LLM re-decomposition path on mission change; (d) ROS 2 mission-layer bridge with the same SafetyMonitor gate.
