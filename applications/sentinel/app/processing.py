# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import json
import logging
import math
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import BaseModel, ValidationError

from app.schemas import ReviewerOutput

logger = logging.getLogger(__name__)

LENS_PREFIX = {
    "medical": "MED",
    "legal": "LEG",
    "regulatory": "REG",
    "editorial": "ED",
    "custom": "CUSTOM",
}
REVIEWER_STATE_KEYS = {
    "medical": "medical_findings",
    "legal": "legal_findings",
    "regulatory": "regulatory_findings",
    "editorial": "editorial_findings",
    "custom": "rules_findings",
}
LOCATION_FIELDS = ("page", "section", "bbox", "quote")

EMPTY_RULES_OUTPUT: dict[str, Any] = {
    "lens": "custom",
    "reviewer_summary": "No custom rules file was supplied for this review pass.",
    "findings": [],
}


def as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def normalize_bbox(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        numbers = [float(v) for v in value]
    except (TypeError, ValueError):
        return None
    if any(math.isnan(n) or math.isinf(n) for n in numbers):
        return None
    x_min, y_min, x_max, y_max = (min(1.0, max(0.0, n)) for n in numbers)
    if x_min > x_max:
        x_min, x_max = x_max, x_min
    if y_min > y_max:
        y_min, y_max = y_max, y_min
    if x_max - x_min <= 0 or y_max - y_min <= 0:
        return None
    return [round(n, 4) for n in (x_min, y_min, x_max, y_max)]


def bbox_union(boxes: list[list[float]]) -> list[float] | None:
    valid = [b for b in (normalize_bbox(box) for box in boxes) if b]
    if not valid:
        return None
    return [
        min(b[0] for b in valid),
        min(b[1] for b in valid),
        max(b[2] for b in valid),
        max(b[3] for b in valid),
    ]


def bbox_iou(first: Any, second: Any) -> float:
    a, b = normalize_bbox(first), normalize_bbox(second)
    if not a or not b:
        return 0.0
    inter_w = min(a[2], b[2]) - max(a[0], b[0])
    inter_h = min(a[3], b[3]) - max(a[1], b[1])
    if inter_w <= 0 or inter_h <= 0:
        return 0.0
    inter = inter_w * inter_h
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def normalize_inventory(inventory: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(inventory)
    items = result.get("items")
    if not isinstance(items, list):
        result["items"] = []
        return result
    used = {
        item.get("item_id")
        for item in items
        if isinstance(item, dict) and item.get("item_id")
    }
    seen: set[str] = set()
    next_number = 1
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("item_id")
        if not item_id or item_id in seen:
            while f"C{next_number}" in used or f"C{next_number}" in seen:
                next_number += 1
            item_id = f"C{next_number}"
            used.add(item_id)
            item["item_id"] = item_id
        seen.add(item_id)
        location = item.get("location")
        if isinstance(location, dict) and "bbox" in location:
            location["bbox"] = normalize_bbox(location.get("bbox"))
    return result


def rules_reviewer_gate(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> LlmResponse | None:
    if callback_context.state.get("custom_rules"):
        return None
    return LlmResponse(
        content=types.Content(
            role="model",
            parts=[types.Part(text=json.dumps(EMPTY_RULES_OUTPUT))],
        )
    )


def confidence_band(confidence: float) -> str:
    if confidence >= 0.8:
        return "high"
    if confidence >= 0.5:
        return "medium"
    return "low"


def _clamp_confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.5
    if math.isnan(number):
        return 0.5
    return min(1.0, max(0.0, number))


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if v is not None]


def items_by_id(inventory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["item_id"]: item
        for item in inventory.get("items") or []
        if isinstance(item, dict) and item.get("item_id")
    }


def derive_location(
    finding: dict[str, Any], items: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    related = [
        items[item_id]
        for item_id in _string_list(finding.get("related_item_ids"))
        if item_id in items
    ]
    locations = [
        item["location"] for item in related if isinstance(item.get("location"), dict)
    ]
    location: dict[str, Any] = dict.fromkeys(LOCATION_FIELDS)
    if len(locations) == 1:
        for field in LOCATION_FIELDS:
            location[field] = locations[0].get(field)
    elif locations:
        sections: list[str] = []
        for entry in locations:
            section = entry.get("section")
            if section and section not in sections:
                sections.append(section)
        location["page"] = next(
            (e.get("page") for e in locations if e.get("page") is not None), None
        )
        location["section"] = ", ".join(sections[:3]) or None
        location["bbox"] = bbox_union([e["bbox"] for e in locations if e.get("bbox")])
    legacy = finding.get("location")
    if isinstance(legacy, dict):
        for field in LOCATION_FIELDS:
            if legacy.get(field) is not None:
                location[field] = legacy[field]
    explicit = normalize_bbox(finding.get("bbox"))
    if explicit:
        location["bbox"] = explicit
    location["bbox"] = normalize_bbox(location.get("bbox"))
    if all(location.get(field) is None for field in LOCATION_FIELDS):
        return None
    return location


def expand_reviewer_output(
    lens: str, output: Any, items: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    data = as_dict(output)
    prefix = LENS_PREFIX[lens]
    raw_findings = [f for f in data.get("findings") or [] if isinstance(f, dict)]
    findings = []
    for number, raw in enumerate(raw_findings, start=1):
        confidence = _clamp_confidence(raw.get("confidence"))
        findings.append(
            {
                "finding_id": f"F-{prefix}-{number}",
                "review_lens": lens,
                "category": raw.get("category") or "other",
                "severity": raw.get("severity") or "informational",
                "confidence": confidence,
                "confidence_band": confidence_band(confidence),
                "evidence_depth": raw.get("evidence_depth") or "surface",
                "related_item_ids": _string_list(raw.get("related_item_ids")),
                "quoted_content": raw.get("quoted_content"),
                "observation": str(raw.get("observation") or ""),
                "mlr_principle": str(raw.get("mlr_principle") or ""),
                "discussion": str(raw.get("discussion") or ""),
                "suggested_questions": _string_list(raw.get("suggested_questions")),
                "suggested_actions": _string_list(raw.get("suggested_actions")),
                "location": derive_location(raw, items),
            }
        )
    expanded = {
        "lens": lens,
        "reviewer_summary": str(data.get("reviewer_summary") or ""),
        "findings": findings,
    }
    try:
        return ReviewerOutput.model_validate(expanded).model_dump(mode="json")
    except ValidationError:
        logger.exception("Expanded %s findings failed schema validation", lens)
        return expanded


def normalize_reviewer_state(state: Any) -> dict[str, dict[str, Any]]:
    items = items_by_id(as_dict(state.get("intake_findings")))
    expanded: dict[str, dict[str, Any]] = {}
    for lens, key in REVIEWER_STATE_KEYS.items():
        if state.get(key) is not None:
            expanded[key] = expand_reviewer_output(lens, state.get(key), items)
    return expanded
