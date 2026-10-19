"""Prompt assembly from ``prompts/task_decomposer_v1.txt``."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Sequence

from ..config import PROMPTS_DIR
from ..models import Violation, json_schema_for_plan
from .interface import PlanningRequest

PROMPT_VERSION = "task_decomposer_v1"


def _escape(obj: object) -> str:
    # '<' is escaped so mission text can never close the <mission_input> delimiter.
    return json.dumps(obj, ensure_ascii=True, sort_keys=True).replace("<", "\\u003c")


def build_prompts(
    request: PlanningRequest,
    repair_violations: Optional[Sequence[Violation]] = None,
    prompts_dir: Path = PROMPTS_DIR,
) -> tuple[str, str]:
    raw = (Path(prompts_dir) / f"{PROMPT_VERSION}.txt").read_text(encoding="utf-8")
    system_part, user_part = raw.split("### USER", 1)
    system = system_part.replace("### SYSTEM", "", 1).strip()
    mission_json = _escape(
        {
            "mission_id": request.mission_id,
            "mission_text": request.mission_text,
            "allowed_task_types": request.allowed_task_types,
            "allowed_zones": sorted(request.zones),
            "report_zone": request.report_zone,
            "capability_catalogue": sorted(request.capability_catalogue),
            "required_outcomes": request.required_outcomes,
        }
    )
    repair_block = ""
    if repair_violations:
        lines = "\n".join(f"- [{v.code}] {v.message}" for v in repair_violations[:20])
        repair_block = (
            "\n<validation_errors>\nYour previous response was rejected by the deterministic validator. "
            "Produce a corrected response that fixes these problems without adding unrequested tasks:\n"
            f"{lines}\n</validation_errors>"
        )
    user = (
        user_part.strip()
        .replace("{mission_json}", mission_json)
        .replace("{schema_json}", json.dumps(json_schema_for_plan(), sort_keys=True))
        .replace("{task_limit}", str(request.task_limit))
        .replace("{repair_block}", repair_block)
    )
    return system, user
