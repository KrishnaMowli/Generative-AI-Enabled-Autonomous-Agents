"""Compare a freshly generated runs.csv with the committed results/runs.csv.

Usage:  python scripts/verify_reproduction.py results_check
Wall-clock columns (replan compute time, planner latency) are ignored; everything else must be identical.
"""
import csv
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
fresh = Path(sys.argv[1] if len(sys.argv) > 1 else "results_check")
DROP = {"replan_compute_ms_mean", "planner_latency_s"}
load = lambda p: [{k: v for k, v in r.items() if k not in DROP} for r in csv.DictReader(open(p))]
committed, new = load(root / "results" / "runs.csv"), load(fresh / "runs.csv")
if committed == new:
    print(f"OK: {len(new)} freshly generated runs are identical to the committed results (wall-clock columns ignored)")
    sys.exit(0)
diffs = [(a["scenario"], a["variant"], a["seed"]) for a, b in zip(committed, new) if a != b]
print(f"MISMATCH: {len(diffs)} differing rows (first few: {diffs[:5]}); row counts {len(committed)} vs {len(new)}")
sys.exit(1)
