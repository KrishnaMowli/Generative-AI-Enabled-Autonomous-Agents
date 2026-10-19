#!/usr/bin/env bash
# Offline demonstration: no API key, no network. Run from the repository root.
set -euo pipefail
export PYTHONPATH="${PYTHONPATH:-}:src"
python -m autonomous_collab demo --out results/demo
python -m autonomous_collab demo --failure --out results/demo
