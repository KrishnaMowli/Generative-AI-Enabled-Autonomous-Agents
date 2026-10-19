"""Print the headline numbers quoted in README.md, computed from results/runs.csv (no hand-typed values).

Usage:  python scripts/headline_numbers.py [results_dir]
"""
import csv
import statistics as st
import sys
from pathlib import Path

results = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "results")
rows = list(csv.DictReader(open(results / "runs.csv")))
num = lambda r, k: float(r[k]) if r[k] not in ("", None) else None


def succ(scn, var):
    sel = [r for r in rows if r["scenario"] == scn and r["variant"].startswith(var + "_")]
    return f"{sum(int(r['mission_success']) for r in sel)}/{len(sel)}"


print(f"runs: {len(rows)}   scenarios: {len({r['scenario'] for r in rows})}   variants: {len({r['variant'] for r in rows})}   seeds: {len({r['seed'] for r in rows})}")
print(f"collisions observed (independent audit), all runs: {sum(int(r['collisions']) for r in rows)}")
print(f"monitor-prevented move conflicts, all runs: {sum(int(r['monitor_rejections']) for r in rows)}")
print()
print("Mission success (successes/runs)           C=MILP no replan  D=MILP+replan  G=greedy+replan  E=nearest+replan")
for scn in ("s3_agent_failure", "s7_new_task", "s8_uncertain_task"):
    print(f"  {scn:26} {succ(scn, 'C'):>16} {succ(scn, 'D'):>14} {succ(scn, 'G'):>16} {succ(scn, 'E'):>17}")
print()
print("Fail-closed scenarios, LLM-path variants B..G (plans executed = successes):")
for scn in ("s4b_malformed_persistent", "s5_invalid_plan", "s9_provider_down"):
    ex = sum(int(r["mission_success"]) for r in rows if r["scenario"] == scn and not r["variant"].startswith("A_"))
    n = sum(1 for r in rows if r["scenario"] == scn and not r["variant"].startswith("A_"))
    print(f"  {scn:26} executed {ex}/{n}")
print("Honest-failure scenarios (must NOT report success):")
for scn in ("s6b_resources_insufficient", "s10_no_feasible_recovery", "s11_unreachable_zone"):
    print(f"  {scn:26} successes {sum(int(r['mission_success']) for r in rows if r['scenario'] == scn)}/{sum(1 for r in rows if r['scenario'] == scn)}")
print()
print("Nominal scenario s0 (mean makespan in s, successful runs only):")
for r0 in sorted({r["variant"] for r in rows if r["scenario"] == "s0_nominal"}):
    v = [num(r, "makespan_s") for r in rows if r["scenario"] == "s0_nominal" and r["variant"] == r0 and r["mission_success"] == "1"]
    print(f"  {r0:26} {st.mean(v):7.1f}   (n={len(v)})")
