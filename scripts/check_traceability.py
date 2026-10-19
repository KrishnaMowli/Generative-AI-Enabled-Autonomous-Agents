"""Verify that every test referenced in docs/TRACEABILITY.md exists (via pytest --collect-only)."""
import re
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
collected = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-o", "addopts="], cwd=root, capture_output=True, text=True).stdout
ids = {re.sub(r"\[.*\]$", "", line.strip()) for line in collected.splitlines() if "::" in line}
refs = set(re.findall(r"`(tests/[\w./]+::\w+)`", (root / "docs" / "TRACEABILITY.md").read_text()))
missing = sorted(r for r in refs if r not in ids)
print(f"{len(refs)} referenced tests, {len(ids)} collected test functions; missing: {missing or 'none'}")
sys.exit(1 if missing else 0)
