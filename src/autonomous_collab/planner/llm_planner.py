"""Planner implementations: LLMPlanner (any provider) and ManualPlanner (fixture plan)."""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from ..config import PlannerLimits
from ..errors import ProviderError
from ..models import TaskSpec, V, Violation
from ..validation.schema_validator import parse_candidate
from .interface import AttemptRecord, PlanningOutcome, PlanningRequest, PlanValidatorLike
from .prompts import PROMPT_VERSION, build_prompts
from .providers import LLMProvider


class LLMPlanner:
    """Calls a provider, parses and validates the response, with bounded retries.

    Budget: at most ``max_total_calls`` provider calls; at most ``max_provider_retries``
    retries of retryable provider errors; at most ``max_repair_attempts`` repair prompts after
    an invalid response. After that the mission fails closed with diagnostics. The planner never
    edits an invalid plan itself.
    """

    def __init__(self, provider: LLMProvider, limits: PlannerLimits, max_tokens: int = 2000) -> None:
        self.provider = provider
        self.limits = limits
        self.max_tokens = max_tokens

    def plan(self, request: PlanningRequest, validator: PlanValidatorLike) -> PlanningOutcome:
        lim = self.limits
        outcome = PlanningOutcome(status="rejected", provider=self.provider.name, model=self.provider.model, prompt_version=PROMPT_VERSION)
        calls = provider_retries = repairs = 0
        feedback: list[Violation] = []
        while True:
            if calls >= lim.max_total_calls:
                outcome.status = "rejected"
                outcome.violations = outcome.violations or [Violation(V.PROVIDER_FAILURE, f"call budget of {lim.max_total_calls} exhausted")]
                return outcome
            system, user = build_prompts(request, feedback)
            calls += 1
            try:
                resp = self.provider.complete(system, user, max_tokens=self.max_tokens, temperature=0.0)
            except ProviderError as exc:
                violation = Violation(V.PROVIDER_FAILURE, str(exc))
                outcome.attempts.append(AttemptRecord(len(outcome.attempts), "provider_error", [V.PROVIDER_FAILURE],
                                                      provider=self.provider.name, model=self.provider.model))
                outcome.violations = [violation]
                if exc.retryable and provider_retries < lim.max_provider_retries:
                    provider_retries += 1
                    continue
                outcome.status = "provider_error"
                return outcome

            parsed = parse_candidate(resp.text, lim)
            violations = list(parsed.violations)
            if not violations and parsed.status == "plan":
                violations = validator.validate_tasks(parsed.tasks)
            record = AttemptRecord(
                len(outcome.attempts), "accepted", [v.code for v in violations], resp.latency_s,
                resp.input_tokens, resp.output_tokens, resp.text, resp.provider, resp.model,
            )
            outcome.attempts.append(record)
            outcome.model = resp.model
            if not violations:
                if parsed.status == "plan":
                    outcome.status, outcome.tasks, outcome.violations = "plan", parsed.tasks, []
                elif parsed.status == "needs_clarification":
                    record.outcome = "clarification"
                    outcome.status, outcome.questions = "needs_clarification", parsed.questions
                    outcome.violations = [Violation(V.PLANNER_CLARIFICATION, "; ".join(parsed.questions))]
                else:
                    record.outcome = "planner_error"
                    outcome.status = "planner_error"
                    outcome.violations = [Violation(V.PLANNER_ERROR_STATUS, parsed.reason or "planner reported an error")]
                return outcome
            record.outcome = "rejected"
            outcome.violations = violations
            if repairs < lim.max_repair_attempts:
                repairs += 1
                feedback = violations
                continue
            outcome.status = "rejected"
            return outcome


class ManualPlanner:
    """Returns a hand-authored plan from the scenario (variant A reference). Still validated."""

    name = "manual"

    def __init__(self, plan_dicts: list[dict[str, Any]]) -> None:
        self.plan_dicts = plan_dicts

    def plan(self, request: PlanningRequest, validator: PlanValidatorLike) -> PlanningOutcome:
        outcome = PlanningOutcome(status="rejected", provider="manual", model="fixture", prompt_version="manual")
        tasks: list[TaskSpec] = []
        violations: list[Violation] = []
        for i, item in enumerate(self.plan_dicts):
            try:
                tasks.append(TaskSpec.model_validate(item))
            except ValidationError as exc:
                violations.append(Violation(V.SCHEMA_TYPE_ERROR, f"manual_plan[{i}]: {exc.errors()[0]['msg']}"))
        if not violations:
            violations = validator.validate_tasks(tasks)
        outcome.attempts.append(AttemptRecord(0, "rejected" if violations else "accepted", [v.code for v in violations], provider="manual", model="fixture"))
        if violations:
            outcome.violations = violations
        else:
            outcome.status, outcome.tasks = "plan", tasks
        return outcome
