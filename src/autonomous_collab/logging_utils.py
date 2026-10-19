"""Structured event log: an in-memory list of JSON-serialisable records, writable as JSONL.

All metrics are computed from these records only (see ``metrics.py``).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("autonomous_collab")

# Fields that legitimately differ between otherwise identical runs (wall-clock measurements).
NONDETERMINISTIC_FIELDS = frozenset({"compute_ms", "latency_s", "wall_ms"})


class EventLog:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def append(self, type_: str, tick: int, **fields: Any) -> dict[str, Any]:
        record = {"seq": len(self.records), "type": type_, "tick": tick, **fields}
        self.records.append(record)
        return record

    def of_type(self, *types: str) -> list[dict[str, Any]]:
        wanted = set(types)
        return [r for r in self.records if r["type"] in wanted]

    def write_jsonl(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            for record in self.records:
                handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def canonical_trace(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip wall-clock fields so two deterministic runs compare equal."""
    return [{k: v for k, v in r.items() if k not in NONDETERMINISTIC_FIELDS} for r in records]
