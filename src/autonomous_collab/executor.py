"""Deterministic simulated task effects, keyed by task_type (finite dispatch table).

Handlers never receive free-form text from a model: they are looked up in ``HANDLERS`` by an
allowlisted task_type. A task only counts as SUCCEEDED when its postcondition holds.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

import numpy as np

from .errors import PreconditionError
from .models import TaskSpec

Registry = Mapping[str, dict[str, Any]]


def _visual(spec: TaskSpec, agent_id: str, rng: np.random.Generator, registry: Registry) -> dict[str, Any]:
    return {
        "zone": spec.target_zone,
        "agent_id": agent_id,
        "defects_found": int(rng.integers(0, 4)),
        "observation_id": f"obs-{spec.task_id}-{int(rng.integers(0, 10**6)):06d}",
    }


def _reading(spec: TaskSpec, agent_id: str, rng: np.random.Generator, registry: Registry) -> dict[str, Any]:
    return {
        "zone": spec.target_zone,
        "agent_id": agent_id,
        "temperature_c": round(float(rng.normal(22.0, 2.0)), 2),
        "humidity_pct": round(float(np.clip(rng.normal(45.0, 8.0), 0, 100)), 2),
    }


def _report(spec: TaskSpec, agent_id: str, rng: np.random.Generator, registry: Registry) -> dict[str, Any]:
    inputs = {dep: registry[dep] for dep in spec.depends_on}
    return {
        "agent_id": agent_id,
        "n_inputs": len(inputs),
        "inputs": sorted(inputs),
        "zones_covered": sorted({r["zone"] for r in inputs.values() if "zone" in r}),
    }


HANDLERS: dict[str, Callable[[TaskSpec, str, np.random.Generator, Registry], dict[str, Any]]] = {
    "visual_inspection": _visual,
    "environmental_reading": _reading,
    "compile_report": _report,
}


def check_precondition(spec: TaskSpec, registry: Registry) -> None:
    """Report handlers require every prerequisite result to exist (defence in depth)."""
    if spec.task_type not in HANDLERS:
        raise PreconditionError(f"no handler for task_type {spec.task_type!r}")
    missing = [d for d in spec.depends_on if d not in registry]
    if missing:
        raise PreconditionError(f"task {spec.task_id}: prerequisite results missing: {missing}")


def run_handler(spec: TaskSpec, agent_id: str, rng: np.random.Generator, registry: Registry) -> dict[str, Any]:
    return HANDLERS[spec.task_type](spec, agent_id, rng, registry)


def check_postcondition(spec: TaskSpec, result: dict[str, Any], registry: Registry) -> bool:
    if spec.task_type == "visual_inspection":
        return result.get("zone") == spec.target_zone and "observation_id" in result
    if spec.task_type == "environmental_reading":
        return result.get("zone") == spec.target_zone and "temperature_c" in result and "humidity_pct" in result
    if spec.task_type == "compile_report":
        return result.get("n_inputs") == len(spec.depends_on) and all(d in registry for d in spec.depends_on)
    return False
