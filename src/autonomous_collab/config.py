"""Settings and scenario loading with startup validation."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import ConfigError
from .models import ScenarioSpec

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "default.yaml"
PROMPTS_DIR = REPO_ROOT / "prompts"


class SimulationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tick_seconds: float = Field(gt=0)
    max_ticks: int = Field(gt=0)
    stall_limit_ticks: int = Field(gt=0)
    min_replan_interval_ticks: int = Field(ge=0)
    max_hold_ticks: int = Field(gt=0)
    failed_agents_block_cell: bool
    max_task_attempts: int = Field(gt=0)
    energy_per_move: float = Field(gt=0)
    task_failure_probability: float = Field(ge=0, le=1)


class TaskTypeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capability: str
    acceptance_criterion: str
    energy: float = Field(ge=0)
    default_duration_s: float = Field(gt=0)
    default_priority: int
    default_retry_policy: str


class PlannerLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_tasks: int = Field(gt=0)
    min_priority: int
    max_priority: int
    max_duration_s: float = Field(gt=0)
    max_graph_depth: int = Field(gt=0)
    max_repair_attempts: int = Field(ge=0)
    max_provider_retries: int = Field(ge=0)
    max_total_calls: int = Field(gt=0)
    max_mission_chars: int = Field(gt=0)
    max_output_chars: int = Field(gt=0)
    injection_policy: str = "reject"


class Weights(BaseModel):
    model_config = ConfigDict(extra="forbid")
    distance: float = Field(ge=0)
    time: float = Field(ge=0)
    energy: float = Field(ge=0)
    priority: float = Field(ge=0)
    load: float = Field(ge=0)
    makespan: float = Field(ge=0)


class AllocationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    weights: Weights
    norms: dict[str, Any]
    workload_limit_ticks: Optional[float] = None


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    simulation: SimulationSettings
    capabilities: list[str]
    task_catalog: dict[str, TaskTypeSpec]
    planner_limits: PlannerLimits
    allocation: AllocationSettings

    def digest(self) -> str:
        blob = json.dumps(self.model_dump(mode="json"), sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:12]

    def validate_consistency(self) -> None:
        problems: list[str] = []
        for name, spec in self.task_catalog.items():
            if spec.capability not in self.capabilities:
                problems.append(f"task_catalog[{name}] uses unknown capability {spec.capability!r}")
            if not (self.planner_limits.min_priority <= spec.default_priority <= self.planner_limits.max_priority):
                problems.append(f"task_catalog[{name}] default_priority outside planner priority range")
        if self.planner_limits.injection_policy not in ("reject", "warn"):
            problems.append("planner_limits.injection_policy must be 'reject' or 'warn'")
        if problems:
            raise ConfigError("invalid settings: " + "; ".join(problems))


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursive dict merge; lists and scalars in ``override`` replace those in ``base``."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    return data


def load_settings(path: Optional[Path] = None, overrides: Optional[dict[str, Any]] = None) -> Settings:
    raw = _read_yaml(Path(path) if path else DEFAULT_CONFIG_PATH)
    if overrides:
        raw = deep_merge(raw, overrides)
    try:
        settings = Settings.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid settings: {exc}") from exc
    settings.validate_consistency()
    return settings


def load_scenario(path: Path) -> ScenarioSpec:
    """Load a scenario YAML; supports ``extends: other.yaml`` (deep-merged, child wins)."""
    path = Path(path)
    raw = _read_yaml(path)
    parent_ref = raw.pop("extends", None)
    if parent_ref:
        parent_path = (path.parent / parent_ref).resolve()
        parent_raw = _read_yaml(parent_path)
        if "extends" in parent_raw:
            raise ConfigError(f"{parent_path}: chained 'extends' is not supported")
        raw = deep_merge(parent_raw, raw)
    try:
        return ScenarioSpec.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid scenario {path}: {exc}") from exc


def settings_for_scenario(scenario: ScenarioSpec, base: Optional[Settings] = None) -> Settings:
    """Settings after applying the scenario's ``settings_overrides``."""
    base = base or load_settings()
    if not scenario.settings_overrides:
        return base
    merged = deep_merge(base.model_dump(mode="json"), scenario.settings_overrides)
    try:
        settings = Settings.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(f"invalid settings_overrides in scenario {scenario.name}: {exc}") from exc
    settings.validate_consistency()
    return settings
