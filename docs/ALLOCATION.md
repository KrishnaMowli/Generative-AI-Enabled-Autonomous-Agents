# Task allocation: mathematics, assumptions, limits

## Problem
Given pending tasks J, agents I (capabilities, position, battery, reserve, committed work), decide task→agent assignment. The LLM never assigns.

## Cost (units: dimensionless after normalisation)
For agent *i*, task *j* (`allocation/cost_model.py`):

```
c_ij = w_d·d_ij/D_ref + w_t·(T_travel_ij + T_work_j)/T_ref + w_e·E_ij/E_ref + w_p·(p_j/P_max)·T_travel_ij/T_ref + w_l·L_i/T_ref
```
d_ij: cells from the agent's (virtual) position (A*); T in ticks (1 cell/tick; work = ⌈duration/tick⌉); E_ij = 0.5·d_ij + task energy; p_j priority (1–5); L_i ticks already committed. Normalisers (`auto`): D_ref = rows+cols, T_ref = D_ref + longest task ticks, E_ref = D_ref·e_move + largest task energy. Weights (default `config/default.yaml`): w_d=1, w_t=1, w_e=0.5, w_p=1, w_l=0.5, w_m=1 — fixed *before* evaluation (no tuning on results).

## Hard constraints (never a penalty)
Pair (i,j) is **feasible** iff: required capabilities ⊆ capabilities(i); agent available; j reachable from i; battery_i − E_ij ≥ reserve_i; i not excluded for j after a rejected dispatch. Infeasible pairs have no decision variable (MILP), are discarded after solving (Hungarian), or are skipped (greedy/nearest). Infeasible tasks are returned with a reason: `NO_CAPABLE_AGENT`, `NO_AVAILABLE_AGENT`, `NO_REACHABLE_PATH`, `BATTERY_LIMIT`, `EXCLUDED_AFTER_REJECTION(cause)`, `WORKLOAD_LIMIT`.

## Allocators
| Name | Mode | Method | Optimal for |
|---|---|---|---|
| `milp` | batch | binary MILP (`scipy.optimize.milp`/HiGHS) | the encoded static model (below) |
| `hungarian` | online | priority-first selection of ≤ #idle tasks, then `linear_sum_assignment` on c_ij | the one-to-one matching of that cycle |
| `greedy` | batch | topological order; each task to the feasible agent with lowest *incremental* cost from the agent's virtual position/battery/load | nothing (baseline; route-aware) |
| `nearest` | online | each idle agent takes the nearest feasible ready task | nothing (rule-based baseline) |

### MILP
Variables x_ij∈{0,1} (feasible pairs only), u_j∈{0,1} (unassigned), Z≥0 (makespan proxy).
```
min  Σ (c_ij + ε_i) x_ij + U·Σ u_j + w_m·Z
s.t. Σ_i x_ij + u_j = 1                       ∀ j
     L_i/T_ref + Σ_j (t_ij/T_ref) x_ij ≤ Z     ∀ i      (max agent finish-time proxy)
     Σ_j E_ij x_ij ≤ battery_i − reserve_i     ∀ i      (energy budget)
     Σ_j t_ij x_ij ≤ limit − L_i               ∀ i      (optional workload limit)
```
U exceeds every achievable cost so the order is lexicographic: maximise #assigned, then minimise cost + w_m·Z. ε_i = 1e-7·(i+1) gives deterministic tie-breaking. *Tested against exhaustive enumeration* on 12 random instances (`test_milp_matches_exhaustive_enumeration`).

**Assumptions under which it is optimal:** static assignment approximation — each c_ij and E_ij is evaluated from the agent's position at decision time, so travel *between* an agent's several tasks is not modelled; dependencies are not in the model (the scheduler enforces them); Z is a proxy for makespan, not a schedule. **Consequences observed in the results:** the energy budget can both under- and over-estimate chained routes (the dispatch gate catches the former; the latter leaves tasks unassigned, e.g. s6a at low battery), and greedy (route-aware) can beat MILP on some metrics. Do **not** read "MILP" as "globally optimal multi-robot routing".

### Common comparison objective
`allocation_objective` (metrics) = Σ c_ij over (task → executing agent) using each agent's **initial** snapshot and the same weights for every allocator — comparable across variants; not the solver's internal objective.

## Complexity
MILP: NP-hard in general; instances here are ≤ ~10 tasks × 3 agents (ms). Hungarian: O(n³). Greedy: O(|J|·|I|) path queries (A* with caching per map version).

## Not modelled
Deadline penalties, agent-to-agent handover, task splitting, travel between sequenced tasks inside the MILP.
