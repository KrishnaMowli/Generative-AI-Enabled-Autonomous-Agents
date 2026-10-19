"""Export JSON Schemas for the planner contracts into schemas/ (generated from the pydantic models)."""
import json
from pathlib import Path

from autonomous_collab.models import json_schema_for_plan, json_schema_for_task

out = Path(__file__).resolve().parents[1] / "schemas"
out.mkdir(exist_ok=True)
(out / "task_spec.schema.json").write_text(json.dumps(json_schema_for_task(), indent=2, sort_keys=True) + "\n")
(out / "candidate_plan.schema.json").write_text(json.dumps(json_schema_for_plan(), indent=2, sort_keys=True) + "\n")
print("wrote", sorted(p.name for p in out.glob("*.json")))
