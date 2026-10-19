#!/usr/bin/env bash
# One-command check for first-time visitors. Run from anywhere: bash scripts/quickstart.sh
# Creates .venv, installs dependencies, then runs: config check, failure demo, full test suite, trace verification.
# Needs: Python >= 3.10 with venv. No API key, no network except `pip install`.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"

command -v "$PY" >/dev/null || { echo "ERROR: $PY not found. Install Python 3.10+ (Ubuntu: sudo apt install python3 python3-venv python3-pip)."; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || { echo "ERROR: Python >= 3.10 required (found $("$PY" --version))."; exit 1; }
"$PY" -m venv .venv 2>/dev/null || { echo "ERROR: could not create a virtual environment. Ubuntu/Debian: sudo apt install python3-venv"; exit 1; }

# shellcheck disable=SC1091
source .venv/bin/activate
unset PYTHONPATH                       # avoids ROS / other Python paths leaking into the test run
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1  # stops third-party pytest plugins (e.g. ROS) from loading

echo "== [1/5] Installing dependencies (needs internet, ~1-2 min) =="
pip install -q -r requirements.txt && pip install -q -e .

echo; echo "== [2/5] Validating settings and scenarios =="
autocollab validate-config

echo; echo "== [3/5] Offline demo: agent failure, replanning ON vs OFF =="
autocollab demo --failure --out results/demo

echo; echo "== [4/5] Running the full test suite (~20-60 s) =="
python -m pytest -q

echo; echo "== [5/5] Recomputing saved metrics from the raw event logs =="
autocollab verify-traces --results results

echo
echo "ALL CHECKS PASSED."
echo "Next: 'source .venv/bin/activate' then see the README section 'Verify the claims' for the full-reproduction commands."
