# Safety and validation layer — scope and limits

**What this is:** deterministic gates between *proposed* actions and simulator state. **What it is not:** a proof of collision-freedom or any real-world safety claim. Continuous robots need geometry, localisation uncertainty, dynamics, braking distance and an independent low-level safety controller.

## Layers
1. **Mission screen** (weak): control chars, length, instruction-like patterns → `MISSION_REJECTED`. Evadable; not the boundary.
2. **Schema validator:** exactly one JSON document (optionally one code fence); strict types (no bool-as-int, no extras); size caps.
3. **Semantic validator:** task-type/zone/capability/acceptance allowlists; priority/duration/depth/count limits; unique IDs; unknown/self/cyclic dependencies; report after all inspections and never a prerequisite; ≥1 capable and reachable agent per task; **coverage** both ways (every checklist outcome present, no unrequested task). Never repairs; returns stable codes.
4. **Planner budget:** ≤ 1 repair prompt, ≤ 2 provider retries, ≤ 4 calls; repair prompts carry violation codes, never the rejected model text. Then fail closed.
5. **SafetyMonitor – dispatch:** action allowlist, capability, dependencies SUCCEEDED, plan-version freshness, path legality, battery ≥ reserve after route+task, agent physically idle.
6. **SafetyMonitor – moves:** legal one-cell step from the agent's cell, current world version, passable, battery, no edge swap, destination unoccupied at tick start, one mover per cell, deterministic priority order.
7. **Simulator postconditions:** success only if the handler result satisfies the acceptance criterion; completed results immutable.
8. **Independent audit:** per-tick same-cell / edge-swap detection that does not call the monitor (shown by test to catch an approve-everything monitor).

## Evidence in this repo
Adversarial planner outputs (extra fields such as `assigned_agent`, unknown types/zones/capabilities, command-like criteria, cycles, oversized plans, prose-wrapped JSON) are rejected in every persistent-corruption case (0 executed; `results/planner_eval/fault_modes.csv`). 15 deliberate code mutations of the safety/validation logic were each caught by the suite. Simulated collisions across the 2,940 experiment runs: see `results/report.md` §5.

## Known limits
Keyword screening is bypassable; the mock "planner" is not adversarial; conservative move semantics trade throughput for safety; deadlocks in 1-wide corridors with two non-yielding agents end in `DEADLOCK`→replan, not guaranteed resolution; failure scenarios assume failed agents are cleared (`failed_agents_block_cell: false`); no communication delay/loss modelling.
