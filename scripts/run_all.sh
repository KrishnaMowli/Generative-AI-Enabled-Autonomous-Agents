#!/usr/bin/env bash
# Reproduce every committed result: tests, experiments (30 seeds), planner eval, plots, report, trace verification.
set -euo pipefail
export PYTHONPATH="${PYTHONPATH:-}:src"
SEEDS="${SEEDS:-30}"; JOBS="${JOBS:-1}"
python -m pytest -q
python -m autonomous_collab validate-config
python -m autonomous_collab experiments --seeds "$SEEDS" --jobs "$JOBS" --out results
python -m autonomous_collab planner-eval --provider mock --out results/planner_eval
python -m autonomous_collab plots --results results
python -m autonomous_collab report --results results
python -m autonomous_collab verify-traces --results results
