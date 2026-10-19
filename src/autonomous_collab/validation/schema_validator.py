"""Syntactic validation of raw planner output (JSON text -> typed candidate)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import ValidationError

from ..config import PlannerLimits
from ..models import TaskSpec, V, Violation

_FENCE = re.compile(r"^\s*```(?:json|JSON)?[ \t]*\n(.*?)\n[ \t]*```\s*$", re.DOTALL)
_ENVELOPE_KEYS = {"status", "tasks", "questions", "reason"}
_STATUSES = {"plan", "needs_clarification", "error"}


@dataclass
class ParsedCandidate:
    status: Optional[str] = None
    tasks: list[TaskSpec] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    reason: Optional[str] = None
    violations: list[Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations and self.status is not None


def extract_json(raw_text: str, max_chars: int) -> tuple[Any, list[Violation]]:
    """Accept exactly one JSON document, optionally wrapped in a single code fence.

    Free text around the JSON is rejected (fail-closed): we never guess which part of a
    response was meant to be the plan.
    """
    if not isinstance(raw_text, str):
        return None, [Violation(V.MALFORMED_JSON, f"provider returned {type(raw_text).__name__}, expected text")]
    if len(raw_text) > max_chars:
        return None, [Violation(V.OUTPUT_TOO_LARGE, f"output has {len(raw_text)} chars (limit {max_chars})")]
    text = raw_text.strip()
    match = _FENCE.match(text)
    if match:
        text = match.group(1).strip()
    try:
        return json.loads(text), []
    except json.JSONDecodeError as exc:
        return None, [Violation(V.MALFORMED_JSON, f"output is not valid JSON: {exc.msg} (line {exc.lineno}, col {exc.colno})")]


def _map_pydantic_errors(exc: ValidationError, index: int) -> list[Violation]:
    out: list[Violation] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        where = f"tasks[{index}]" + (f".{loc}" if loc else "")
        kind = err["type"]
        if kind == "missing":
            code = V.SCHEMA_MISSING_FIELD
        elif kind == "extra_forbidden":
            code = V.SCHEMA_EXTRA_FIELD
        else:
            code = V.SCHEMA_TYPE_ERROR
        out.append(Violation(code, f"{where}: {err['msg']}"))
    return out


def parse_candidate(raw_text: str, limits: PlannerLimits) -> ParsedCandidate:
    """Parse and structurally validate a planner response. Never raises on bad model output."""
    obj, violations = extract_json(raw_text, limits.max_output_chars)
    if violations:
        return ParsedCandidate(violations=violations)
    if not isinstance(obj, dict):
        return ParsedCandidate(violations=[Violation(V.SCHEMA_NOT_OBJECT, "top-level JSON value must be an object")])

    out = ParsedCandidate()
    extra = sorted(set(obj) - _ENVELOPE_KEYS)
    if extra:
        out.violations.append(Violation(V.SCHEMA_EXTRA_FIELD, f"unexpected top-level field(s): {extra}"))
    status = obj.get("status")
    if status not in _STATUSES:
        out.violations.append(Violation(V.SCHEMA_BAD_STATUS, f"status must be one of {sorted(_STATUSES)}, got {status!r}"))
        return out
    out.status = status

    if status == "needs_clarification":
        questions = obj.get("questions")
        if not isinstance(questions, list) or not questions or not all(isinstance(q, str) for q in questions):
            out.violations.append(Violation(V.SCHEMA_TYPE_ERROR, "questions must be a non-empty list of strings"))
        else:
            out.questions = questions
        return out
    if status == "error":
        reason = obj.get("reason")
        out.reason = reason if isinstance(reason, str) else "planner reported an error"
        return out

    tasks_raw = obj.get("tasks")
    if not isinstance(tasks_raw, list):
        out.violations.append(Violation(V.SCHEMA_TYPE_ERROR, "tasks must be a list when status is 'plan'"))
        return out
    if not tasks_raw:
        out.violations.append(Violation(V.EMPTY_PLAN, "plan contains no tasks"))
        return out
    if len(tasks_raw) > limits.max_tasks:
        out.violations.append(Violation(V.TASK_LIMIT_EXCEEDED, f"{len(tasks_raw)} tasks exceed limit {limits.max_tasks}"))
        return out
    for idx, item in enumerate(tasks_raw):
        if not isinstance(item, dict):
            out.violations.append(Violation(V.SCHEMA_TYPE_ERROR, f"tasks[{idx}] must be an object"))
            continue
        try:
            out.tasks.append(TaskSpec.model_validate(item))
        except ValidationError as exc:
            out.violations.extend(_map_pydantic_errors(exc, idx))
    return out
