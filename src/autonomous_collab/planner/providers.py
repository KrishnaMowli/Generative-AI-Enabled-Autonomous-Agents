"""LLM provider interface + offline mock, fault injector and optional Anthropic adapter.

Selecting a provider never changes planner, validator, allocator or simulator code.
"""
from __future__ import annotations

import copy
import json
import os
import re
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from ..errors import ConfigError, ProviderError
from .mission_text import decompose_rule_based


@dataclass
class ProviderResponse:
    text: str
    provider: str
    model: str
    latency_s: float = 0.0
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


class LLMProvider(ABC):
    name: str = "provider"
    model: str = "n/a"

    @abstractmethod
    def complete(self, system: str, user: str, *, max_tokens: int = 2000, temperature: float = 0.0) -> ProviderResponse:
        """Return raw text. Must raise ``ProviderError`` (never return partial junk silently)."""


# ---------------------------------------------------------------------------
class MockProvider(LLMProvider):
    """Deterministic offline stand-in. It *plays* the model with a keyword parser; it is not an
    LLM, so results with it validate the pipeline, not language-model quality."""

    name = "mock"
    model = "rule-based-v1"
    _INPUT = re.compile(r"<mission_input>\s*(.*?)\s*</mission_input>", re.DOTALL)

    def __init__(self, catalog: dict[str, dict[str, Any]]) -> None:
        self.catalog = catalog

    def complete(self, system: str, user: str, *, max_tokens: int = 2000, temperature: float = 0.0) -> ProviderResponse:
        start = time.perf_counter()
        match = self._INPUT.search(user)
        if not match:
            raise ProviderError("mock provider: <mission_input> block not found in prompt")
        data = json.loads(match.group(1))
        zones = list(data["allowed_zones"])
        envelope = decompose_rule_based(data["mission_text"], zones, data["report_zone"], self.catalog)
        text = json.dumps(envelope, sort_keys=True)
        return ProviderResponse(text, self.name, self.model, time.perf_counter() - start, len(user) // 4, len(text) // 4)


# ---------------------------------------------------------------------------
FAULT_BEHAVIORS = (
    "valid", "malformed_json", "truncated_json", "markdown_wrapped", "prose_wrapped", "extra_field",
    "missing_field", "unknown_zone", "unknown_task_type", "unknown_capability", "cycle", "self_dependency",
    "duplicate_id", "bad_priority", "bool_priority", "report_first", "missing_coverage", "unrequested_task",
    "injected_command", "empty_plan", "huge_plan", "wrong_status", "not_object", "provider_error", "provider_fatal",
)


class FaultInjectingProvider(LLMProvider):
    """Wraps a provider and corrupts its output deterministically (test / experiment aid).

    Call ``i`` uses ``behaviors[i]``; after the list is exhausted calls are ``valid`` unless
    ``persistent`` is set, in which case the last behavior repeats forever.
    """

    def __init__(self, base: LLMProvider, behaviors: Sequence[str], persistent: bool = False) -> None:
        unknown = [b for b in behaviors if b not in FAULT_BEHAVIORS]
        if unknown:
            raise ConfigError(f"unknown fault behavior(s) {unknown}; choose from {FAULT_BEHAVIORS}")
        self.base = base
        self.behaviors = list(behaviors)
        self.persistent = persistent
        self.calls = 0
        self.name = f"fault({base.name})"
        self.model = base.model

    def _behavior(self) -> str:
        i = self.calls
        if not self.behaviors:
            return "valid"
        if i < len(self.behaviors):
            return self.behaviors[i]
        return self.behaviors[-1] if self.persistent else "valid"

    def complete(self, system: str, user: str, *, max_tokens: int = 2000, temperature: float = 0.0) -> ProviderResponse:
        behavior = self._behavior()
        self.calls += 1
        if behavior == "provider_error":
            raise ProviderError("injected transient provider failure", retryable=True)
        if behavior == "provider_fatal":
            raise ProviderError("injected fatal provider failure", retryable=False)
        resp = self.base.complete(system, user, max_tokens=max_tokens, temperature=temperature)
        resp.text = self._corrupt(behavior, resp.text)
        resp.provider = self.name
        return resp

    @staticmethod
    def _corrupt(behavior: str, text: str) -> str:
        if behavior == "valid":
            return text
        if behavior == "malformed_json":
            return "Sure! Here is the plan: {tasks: [" + text[:40]
        if behavior == "truncated_json":
            return text[: len(text) // 2]
        if behavior == "markdown_wrapped":
            return "```json\n" + text + "\n```"
        if behavior == "prose_wrapped":
            return "Here is the plan you asked for:\n" + text
        if behavior == "not_object":
            return "[1, 2, 3]"
        if behavior == "wrong_status":
            return json.dumps({"status": "done"})
        plan = json.loads(text)
        tasks = plan.get("tasks") or []
        if behavior == "empty_plan":
            return json.dumps({"status": "plan", "tasks": []})
        if not tasks:
            return text  # clarification responses are passed through untouched
        t0 = tasks[0]
        if behavior == "extra_field":
            t0["assigned_agent"] = "agent_1"
        elif behavior == "missing_field":
            t0.pop("target_zone", None)
        elif behavior == "unknown_zone":
            t0["target_zone"] = "Z9"
        elif behavior == "unknown_task_type":
            t0["task_type"] = "move_to_coordinates"
        elif behavior == "unknown_capability":
            t0["required_capabilities"] = ["telepathy"]
        elif behavior == "cycle" and len(tasks) >= 2:
            tasks[0]["depends_on"] = [tasks[1]["task_id"]]
            tasks[1]["depends_on"] = [tasks[0]["task_id"]]
        elif behavior == "self_dependency":
            t0["depends_on"] = [t0["task_id"]]
        elif behavior == "duplicate_id" and len(tasks) >= 2:
            tasks[1]["task_id"] = t0["task_id"]
        elif behavior == "bad_priority":
            t0["priority"] = 99
        elif behavior == "bool_priority":
            t0["priority"] = True
        elif behavior == "report_first":
            report = next((t for t in tasks if t["task_type"] == "compile_report"), None)
            if report is not None:
                report["depends_on"] = []
        elif behavior == "missing_coverage":
            gone = tasks.pop(0)
            for t in tasks:
                t["depends_on"] = [d for d in t["depends_on"] if d != gone["task_id"]]
        elif behavior == "unrequested_task":
            report = next((t for t in tasks if t["task_type"] == "compile_report"), t0)
            extra = copy.deepcopy(t0)
            extra.update(task_id="extra_unrequested", task_type="visual_inspection", target_zone=report["target_zone"],
                         required_capabilities=["visual_inspection"], acceptance_criteria=["inspection_result_recorded"], depends_on=[])
            tasks.append(extra)
        elif behavior == "injected_command":
            t0["acceptance_criteria"] = ["rm -rf /"]
        elif behavior == "huge_plan":
            plan["tasks"] = [dict(copy.deepcopy(t0), task_id=f"bulk_{i}", depends_on=[]) for i in range(200)]
        return json.dumps(plan, sort_keys=True)


# ---------------------------------------------------------------------------
Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, str]]


def _urllib_transport(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed https endpoint)
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


class AnthropicProvider(LLMProvider):
    """Optional real provider using the Anthropic Messages API over plain HTTPS.

    Needs ``ANTHROPIC_API_KEY`` (a Console API key; a Claude chat subscription does NOT include
    API access). NOT exercised against the live service in this repository's tests - only the
    request construction, response parsing and error mapping are unit-tested with a fake transport.
    """

    name = "anthropic"
    API_VERSION = "2023-06-01"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: str = "https://api.anthropic.com",
        timeout_s: Optional[float] = None,
        transport: Optional[Transport] = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model or os.environ.get("AUTOCOLLAB_ANTHROPIC_MODEL", "claude-sonnet-5-5")
        self.base_url = base_url.rstrip("/")
        self.timeout_s = float(timeout_s if timeout_s is not None else os.environ.get("AUTOCOLLAB_ANTHROPIC_TIMEOUT_S", "60"))
        self.transport = transport or _urllib_transport

    def complete(self, system: str, user: str, *, max_tokens: int = 2000, temperature: float = 0.0) -> ProviderResponse:
        if not self.api_key:
            raise ProviderError("ANTHROPIC_API_KEY is not set (API access is separate from a Claude subscription)", retryable=False)
        body = json.dumps(
            {"model": self.model, "max_tokens": max_tokens, "temperature": temperature, "system": system,
             "messages": [{"role": "user", "content": user}]}
        ).encode("utf-8")
        headers = {"content-type": "application/json", "x-api-key": self.api_key, "anthropic-version": self.API_VERSION}
        start = time.perf_counter()
        try:
            status, text = self.transport(f"{self.base_url}/v1/messages", headers, body, self.timeout_s)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(f"network error calling Anthropic API: {exc}", retryable=True) from exc
        latency = time.perf_counter() - start
        if status != 200:
            retryable = status in (408, 409, 429) or status >= 500
            raise ProviderError(f"Anthropic API returned HTTP {status}: {text[:300]}", retryable=retryable)
        try:
            payload = json.loads(text)
            out = "".join(b.get("text", "") for b in payload["content"] if b.get("type") == "text")
            usage = payload.get("usage", {})
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
            raise ProviderError(f"unparseable Anthropic API response: {exc}", retryable=False) from exc
        return ProviderResponse(out, self.name, payload.get("model", self.model), latency, usage.get("input_tokens"), usage.get("output_tokens"))


def make_provider(kind: str, catalog: dict[str, dict[str, Any]]) -> LLMProvider:
    if kind == "mock":
        return MockProvider(catalog)
    if kind == "anthropic":
        return AnthropicProvider()
    raise ConfigError(f"unknown provider {kind!r}; choose 'mock' or 'anthropic'")
