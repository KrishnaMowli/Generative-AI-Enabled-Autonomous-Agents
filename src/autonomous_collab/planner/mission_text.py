"""Mission-text hygiene and a deterministic rule-based decomposer.

``sanitize_mission`` is a first, *weak* defence against prompt injection (pattern matching
is easy to evade). The real boundary is structural: model output is parsed as data, schema
and semantically validated, and the model has no execution authority.

``decompose_rule_based`` is the engine of the offline MockProvider. It is a keyword parser,
not a language model: results obtained with it measure the pipeline, not LLM quality.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..models import RequiredOutcome, V, Violation

_INJECTION_PATTERNS = [
    r"ignore\s+(?:all\s+|any\s+|the\s+|your\s+)?(?:previous|prior|above|earlier)\s+(?:instructions|rules|constraints|prompts?)",
    r"disregard\s+.{0,40}(?:instructions|rules|constraints|safety)",
    r"(?:reveal|print|show|repeat)\s+.{0,30}(?:system\s+prompt|your\s+instructions)",
    r"\bsystem\s+prompt\b",
    r"\byou\s+are\s+now\b",
    r"\b(?:override|bypass|disable|skip|turn\s+off)\s+(?:the\s+)?(?:safety|validator|validation|constraints|checks|monitor)",
    r"\b(?:developer|jailbreak|god|admin)\s+mode\b",
    r"\brm\s+-rf\b|\bsudo\b|\bchmod\b|/etc/passwd",
    r"\b(?:execute|run|eval)\s+(?:this\s+|the\s+)?(?:shell|bash|python|code|command|script)",
    r"\bassigned_agent\b|\bassign\s+(?:this|all|every)\b.{0,30}\bto\s+agent",
    r"\b(?:set|send)\s+(?:motor|velocity|wheel|actuator)",
    r"\bmove\s+(?:agent|robot)\w*\s+to\b",
    r"<\s*/?\s*(?:system|assistant|user|mission_input)\s*>",
]
_INJECTION_RE = re.compile("|".join(f"(?:{p})" for p in _INJECTION_PATTERNS), re.IGNORECASE | re.DOTALL)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_mission(text: str, max_chars: int) -> tuple[str, list[Violation]]:
    """Strip control characters and flag empty / oversize / injection-like text."""
    clean = _CONTROL.sub("", text if isinstance(text, str) else "").strip()
    problems: list[Violation] = []
    if not clean:
        problems.append(Violation(V.MISSION_EMPTY, "mission text is empty"))
    if len(clean) > max_chars:
        problems.append(Violation(V.MISSION_TOO_LONG, f"mission has {len(clean)} chars (limit {max_chars})"))
        clean = clean[:max_chars]
    match = _INJECTION_RE.search(clean)
    if match:
        problems.append(Violation(V.MISSION_INJECTION_SUSPECTED, f"mission text contains instruction-like content: {match.group(0)[:60]!r}"))
    return clean, problems


# ---------------------------------------------------------------------------
_TOKEN = r"[A-Z0-9][A-Za-z0-9_]*"
_ZONE_LIST = re.compile(
    rf"\bzones?\s+({_TOKEN}(?:\s*(?:,\s*(?:and\s+)?|\s+and\s+|\s*&\s*|\s+or\s+){_TOKEN})*)", re.IGNORECASE
)
_ZONE_TOKEN = re.compile(_TOKEN)
_SPLIT = re.compile(r"\s*(?:,|&|\band\b|\bor\b)\s*", re.IGNORECASE)
_ALL_ZONES = re.compile(r"\b(?:all|every)\s+(?:the\s+)?(?:zones|areas|locations)\b", re.IGNORECASE)
_VISUAL = re.compile(r"\b(?:inspect\w*|visual\w*|survey\w*|examin\w*)\b", re.IGNORECASE)
_READING = re.compile(r"\b(?:readings?|measure\w*|sensor\w*|environmental|temperature|humidity|air\s+quality|sampl\w*)\b", re.IGNORECASE)
_REPORT = re.compile(r"\b(?:report|summar\w+|compile)\b", re.IGNORECASE)


@dataclass
class ParsedMission:
    zones: list[str] = field(default_factory=list)
    unknown_zones: list[str] = field(default_factory=list)
    visual: bool = False
    reading: bool = False
    report: bool = False
    questions: list[str] = field(default_factory=list)


def parse_mission(text: str, zones: list[str], report_zone: str) -> ParsedMission:
    out = ParsedMission()
    lookup = {z.lower(): z for z in zones}
    found: list[str] = []
    for match in _ZONE_LIST.finditer(text):
        for token in _SPLIT.split(match.group(1)):
            token = token.strip()
            if not token or not _ZONE_TOKEN.fullmatch(token):
                continue
            canon = lookup.get(token.lower())
            if canon is None:
                out.unknown_zones.append(token)
            elif canon not in found:
                found.append(canon)
    if _ALL_ZONES.search(text):
        found = [z for z in zones if z != report_zone]
    out.zones = found
    out.visual = bool(_VISUAL.search(text))
    out.reading = bool(_READING.search(text))
    out.report = bool(_REPORT.search(text))

    if out.unknown_zones:
        out.questions.append(f"Zone(s) {sorted(set(out.unknown_zones))} are not in the catalogue {sorted(zones)}. Which zones do you mean?")
    if not (out.visual or out.reading or out.report):
        out.questions.append("What should be done: visual inspection, environmental readings and/or a report?")
    elif (out.visual or out.reading) and not out.zones and not out.unknown_zones:
        out.questions.append(f"Which zone(s) should be covered? Known zones: {sorted(zones)}")
    return out


def derive_required_outcomes(text: str, zones: list[str], report_zone: str) -> Optional[list[RequiredOutcome]]:
    """Independent keyword checklist used when a scenario provides no explicit one."""
    parsed = parse_mission(text, zones, report_zone)
    if parsed.questions:
        return None
    outcomes: list[RequiredOutcome] = []
    for zone in parsed.zones:
        if parsed.visual:
            outcomes.append(RequiredOutcome(task_type="visual_inspection", zone=zone))
        if parsed.reading:
            outcomes.append(RequiredOutcome(task_type="environmental_reading", zone=zone))
    if parsed.report:
        outcomes.append(RequiredOutcome(task_type="compile_report", zone=None))
    return outcomes


def decompose_rule_based(
    text: str,
    zones: list[str],
    report_zone: str,
    catalog: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Return a planner response envelope (plan or needs_clarification) for ``text``."""
    parsed = parse_mission(text, zones, report_zone)
    if parsed.questions:
        return {"status": "needs_clarification", "questions": parsed.questions}

    def make(task_id: str, task_type: str, zone: str, deps: list[str]) -> dict[str, Any]:
        c = catalog[task_type]
        return {
            "task_id": task_id,
            "task_type": task_type,
            "target_zone": zone,
            "required_capabilities": [c["capability"]],
            "depends_on": deps,
            "priority": c["default_priority"],
            "estimated_duration_s": c["default_duration_s"],
            "acceptance_criteria": [c["acceptance_criterion"]],
            "retry_policy": c["default_retry_policy"],
        }

    tasks: list[dict[str, Any]] = []
    for zone in parsed.zones:
        if parsed.visual:
            tasks.append(make(f"inspect_zone_{zone}_visual", "visual_inspection", zone, []))
        if parsed.reading:
            tasks.append(make(f"read_zone_{zone}_env", "environmental_reading", zone, []))
    if parsed.report:
        tasks.append(make("compile_report", "compile_report", report_zone, [t["task_id"] for t in tasks]))
    return {"status": "plan", "tasks": tasks}
