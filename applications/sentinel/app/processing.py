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
import re
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import BaseModel, ValidationError

from app.schemas import (
    CriticAssessment,
    EvidenceDepth,
    FinalReport,
    IssueCategory,
    ReviewerOutput,
    Severity,
)

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


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


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
        item["item_id"]
        for item in items
        if isinstance(item, dict)
        and isinstance(item.get("item_id"), str)
        and item["item_id"]
    }
    seen: set[str] = set()
    next_number = 1
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("item_id")
        if not isinstance(item_id, str) or not item_id or item_id in seen:
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
        for item in as_list(inventory.get("items"))
        if isinstance(item, dict) and isinstance(item.get("item_id"), str)
    }


def _clean_location(raw: Any) -> dict[str, Any]:
    data = as_dict(raw)
    page, section, quote = data.get("page"), data.get("section"), data.get("quote")
    return {
        "page": page if isinstance(page, int) and not isinstance(page, bool) else None,
        "section": section if isinstance(section, str) and section.strip() else None,
        "bbox": normalize_bbox(data.get("bbox")),
        "quote": quote if isinstance(quote, str) and quote else None,
    }


def derive_location(
    finding: dict[str, Any], items: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    related = [
        items[item_id]
        for item_id in _string_list(finding.get("related_item_ids"))
        if item_id in items
    ]
    cleaned = [_clean_location(item.get("location")) for item in related]
    locations = [
        entry for entry in cleaned if any(v is not None for v in entry.values())
    ]
    location: dict[str, Any] = dict.fromkeys(LOCATION_FIELDS)
    if len(locations) == 1:
        location.update(locations[0])
    elif locations:
        sections: list[str] = []
        for entry in locations:
            if entry["section"] and entry["section"] not in sections:
                sections.append(entry["section"])
        location["page"] = next(
            (e["page"] for e in locations if e["page"] is not None), None
        )
        location["section"] = ", ".join(sections[:3]) or None
        location["bbox"] = bbox_union([e["bbox"] for e in locations if e["bbox"]])
    legacy = _clean_location(finding.get("location"))
    for field in LOCATION_FIELDS:
        if legacy[field] is not None:
            location[field] = legacy[field]
    explicit = normalize_bbox(finding.get("bbox"))
    if explicit:
        location["bbox"] = explicit
    if all(location.get(field) is None for field in LOCATION_FIELDS):
        return None
    return location


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _enum_value(raw: Any, enum_class: Any, default: str) -> str:
    if isinstance(raw, str):
        try:
            return enum_class(raw.strip().lower()).value
        except ValueError:
            return default
    return default


def expand_finding(
    raw: dict[str, Any],
    lens: str,
    finding_id: str,
    items: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    confidence = _clamp_confidence(raw.get("confidence"))
    return {
        "finding_id": finding_id,
        "review_lens": lens,
        "category": _enum_value(raw.get("category"), IssueCategory, "other"),
        "severity": _enum_value(raw.get("severity"), Severity, "medium"),
        "confidence": confidence,
        "confidence_band": confidence_band(confidence),
        "evidence_depth": _enum_value(
            raw.get("evidence_depth"), EvidenceDepth, "surface"
        ),
        "related_item_ids": _string_list(raw.get("related_item_ids")),
        "quoted_content": _optional_text(raw.get("quoted_content")),
        "observation": str(raw.get("observation") or ""),
        "mlr_principle": str(raw.get("mlr_principle") or ""),
        "discussion": str(raw.get("discussion") or ""),
        "suggested_questions": _string_list(raw.get("suggested_questions")),
        "suggested_actions": _string_list(raw.get("suggested_actions")),
        "location": derive_location(raw, items),
    }


def expand_reviewer_output(
    lens: str, output: Any, items: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    data = as_dict(output)
    prefix = LENS_PREFIX[lens]
    raw_findings = [f for f in as_list(data.get("findings")) if isinstance(f, dict)]
    expanded = {
        "lens": lens,
        "reviewer_summary": str(data.get("reviewer_summary") or ""),
        "findings": [
            expand_finding(raw, lens, f"F-{prefix}-{number}", items)
            for number, raw in enumerate(raw_findings, start=1)
        ],
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


EXPECTED_ITEM_KINDS = (
    "important_safety_info",
    "safety_statement",
    "citation",
    "footnote",
    "disclaimer",
    "call_to_action",
    "indication_statement",
)
DEFENSE_ARGUMENT_LIMIT = 320


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def all_reviewer_findings(state: Any) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for key in REVIEWER_STATE_KEYS.values():
        output = as_dict(state.get(key))
        findings.extend(
            f for f in as_list(output.get("findings")) if isinstance(f, dict)
        )
    return findings


def finding_digest(finding: dict[str, Any]) -> dict[str, Any]:
    location = as_dict(finding.get("location"))
    return {
        "id": finding.get("finding_id"),
        "lens": finding.get("review_lens"),
        "severity": finding.get("severity"),
        "confidence": finding.get("confidence"),
        "category": finding.get("category"),
        "observation": finding.get("observation"),
        "principle": finding.get("mlr_principle"),
        "items": _string_list(finding.get("related_item_ids")),
        "region": location.get("section"),
        "bbox": location.get("bbox"),
        "quote": finding.get("quoted_content"),
    }


MAX_BOX_PAIRS = 200
MAX_UNCOVERED_ITEMS = 60


def candidate_duplicates(
    findings: list[dict[str, Any]], iou_threshold: float = 0.7
) -> dict[str, Any]:
    """Hints for the dedupe critic whose size stays linear in the number of findings.

    Findings with exactly the same related items are listed as one group each.
    Other pairs are listed only when their boxes overlap strongly, strongest
    first, and capped at MAX_BOX_PAIRS.
    """
    groups: dict[frozenset[str], list[Any]] = {}
    item_sets: list[frozenset[str]] = []
    for finding in findings:
        items = frozenset(_string_list(finding.get("related_item_ids")))
        item_sets.append(items)
        if items:
            groups.setdefault(items, []).append(finding.get("finding_id"))
    boxes = [as_dict(f.get("location")).get("bbox") for f in findings]
    scored: list[tuple[float, int, int]] = []
    for i in range(len(findings)):
        if not boxes[i]:
            continue
        for j in range(i + 1, len(findings)):
            if item_sets[i] and item_sets[i] == item_sets[j]:
                continue
            overlap = bbox_iou(boxes[i], boxes[j])
            if overlap >= iou_threshold:
                scored.append((overlap, i, j))
    scored.sort(key=lambda entry: -entry[0])
    return {
        "same_item_groups": [
            {"items": sorted(items), "finding_ids": ids}
            for items, ids in groups.items()
            if len(ids) > 1
        ],
        "overlapping_box_pairs": [
            [findings[i].get("finding_id"), findings[j].get("finding_id")]
            for _, i, j in scored[:MAX_BOX_PAIRS]
        ],
        "box_pairs_truncated": len(scored) > MAX_BOX_PAIRS,
    }


def coverage_hints(
    inventory: dict[str, Any], findings: list[dict[str, Any]]
) -> dict[str, Any]:
    items = [i for i in as_list(inventory.get("items")) if isinstance(i, dict)]
    referenced = {
        item_id
        for f in findings
        for item_id in (_string_list(f.get("related_item_ids")))
    }
    uncovered = [
        {
            "item_id": i.get("item_id"),
            "kind": i.get("kind"),
            "text": str(i.get("text") or "")[:80],
        }
        for i in items
        if i.get("item_id") not in referenced
    ]
    present_kinds = {str(i.get("kind")) for i in items}
    per_lens = dict.fromkeys(LENS_PREFIX, 0)
    for finding in findings:
        lens = finding.get("review_lens")
        if isinstance(lens, str) and lens in per_lens:
            per_lens[lens] += 1
    return {
        "items_without_findings": uncovered[:MAX_UNCOVERED_ITEMS],
        "items_without_findings_total": len(uncovered),
        "findings_per_lens": per_lens,
        "categories_present": sorted(
            {str(f.get("category")) for f in findings if f.get("category")}
        ),
        "absent_item_kinds": [k for k in EXPECTED_ITEM_KINDS if k not in present_kinds],
        "image_submission": any(as_dict(i.get("location")).get("bbox") for i in items),
    }


def defense_by_item(
    brief: dict[str, Any], findings: list[dict[str, Any]]
) -> dict[str, Any]:
    result = []
    for defense in as_list(brief.get("defenses")):
        if not isinstance(defense, dict):
            continue
        items = set(_string_list(defense.get("related_item_ids")))
        result.append(
            {
                "topic": defense.get("addresses_topic"),
                "items": sorted(items),
                "argument": str(defense.get("argument") or "")[:DEFENSE_ARGUMENT_LIMIT],
                "compromise": defense.get("proposed_compromise"),
                "likely_to_dispute": as_list(defense.get("likely_to_dispute")),
                "related_findings": [
                    f.get("finding_id")
                    for f in findings
                    if items & set(_string_list(f.get("related_item_ids")))
                ],
            }
        )
    return {"overall_framing": brief.get("overall_framing"), "defenses": result}


def prepare_critic_inputs_state(state: Any) -> dict[str, str]:
    findings = all_reviewer_findings(state)
    inventory = as_dict(state.get("intake_findings"))
    return {
        "findings_digest": compact_json([finding_digest(f) for f in findings]),
        "dup_candidates": compact_json(candidate_duplicates(findings)),
        "coverage_hints": compact_json(coverage_hints(inventory, findings)),
        "defense_by_item": compact_json(
            defense_by_item(as_dict(state.get("submitter_defense")), findings)
        ),
    }


_ID_LENS_ALIASES = {"EDT": "ED", "EDIT": "ED", "CRIT": "GAP"}


def normalize_finding_id(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    match = re.match(r"^\s*F-([A-Za-z]+)-0*(\d+)\s*$", raw, re.IGNORECASE)
    if not match:
        return None
    token = match.group(1).upper()
    token = _ID_LENS_ALIASES.get(token, token)
    return f"F-{token}-{int(match.group(2))}"


def expand_gap_findings(
    raw_findings: Any, items: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    expanded = []
    for number, raw in enumerate(
        [f for f in as_list(raw_findings) if isinstance(f, dict)], start=1
    ):
        lens = raw.get("review_lens")
        if not isinstance(lens, str) or lens not in LENS_PREFIX:
            lens = "regulatory"
        expanded.append(expand_finding(raw, lens, f"F-GAP-{number}", items))
    return expanded


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _text(value: Any) -> str:
    return str(value).strip() if value else ""


def merge_critic_outputs(state: Any) -> dict[str, Any]:
    items = items_by_id(as_dict(state.get("intake_findings")))
    dedupe = as_dict(state.get("dedupe_critic_output"))
    severity = as_dict(state.get("severity_critic_output"))
    gap = as_dict(state.get("gap_critic_output"))
    duplicate_groups = [g for g in as_list(dedupe.get("duplicate_groups")) if g]
    themes = [t for t in as_list(dedupe.get("cross_lens_themes")) if t]
    adjustments = [a for a in as_list(severity.get("severity_adjustments")) if a]
    gaps = [str(g) for g in as_list(gap.get("gaps_identified"))]
    iterations_done = _safe_int(state.get("review_iteration_count"))
    wants_another_pass = iterations_done == 0 and (
        len(gaps) >= 2 or len(adjustments) >= 3
    )
    assessment = " ".join(
        part
        for part in (
            _text(dedupe.get("rationale")),
            _text(severity.get("rationale")),
            _text(gap.get("completeness_assessment")),
        )
        if part
    )
    merged = {
        "overall_assessment": assessment or "No critic commentary was produced.",
        "duplicate_groups": duplicate_groups,
        "cross_lens_themes": themes,
        "severity_adjustments": adjustments,
        "gaps_identified": gaps,
        "additional_findings": expand_gap_findings(
            gap.get("additional_findings"), items
        ),
        "iteration_recommendation": (
            "another_pass_would_help"
            if wants_another_pass
            else "reviewers_have_converged"
        ),
    }
    try:
        return CriticAssessment.model_validate(merged).model_dump(mode="json")
    except ValidationError:
        logger.exception("Merged critic assessment failed schema validation")
        return merged


SEVERITIES = ("critical", "high", "medium", "low", "informational")
SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}
LENS_ORDER = tuple(LENS_PREFIX)


def _theme_text(theme: dict[str, Any]) -> str:
    label = _text(theme.get("theme"))
    discussion = _text(theme.get("discussion"))
    return f"{label}: {discussion}" if label and discussion else label or discussion


def assemble_findings(state: Any) -> dict[str, Any]:
    critic = as_dict(state.get("critic_review"))
    gap_findings = [
        f for f in as_list(critic.get("additional_findings")) if isinstance(f, dict)
    ]
    ordered = [*all_reviewer_findings(state), *gap_findings]
    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for finding in ordered:
        key = normalize_finding_id(finding.get("finding_id"))
        if key and key not in by_id:
            by_id[key] = copy.deepcopy(finding)
            order.append(key)

    replaced_by: dict[str, str] = {}

    def find(key: str) -> str:
        seen = {key}
        while key in replaced_by and replaced_by[key] not in seen:
            key = replaced_by[key]
            seen.add(key)
        return key

    groups = [as_dict(g) for g in as_list(critic.get("duplicate_groups"))]
    explicit_keeps = {
        keep
        for keep in (normalize_finding_id(g.get("keep")) for g in groups)
        if keep in by_id
    }
    unresolved: list[str] = []
    for group in groups:
        raw_ids = as_list(group.get("finding_ids"))
        members = [normalize_finding_id(i) for i in raw_ids]
        unresolved.extend(
            str(raw)
            for raw, key in zip(raw_ids, members, strict=True)
            if key not in by_id
        )
        members = [m for m in members if m in by_id]
        keep = normalize_finding_id(group.get("keep"))
        if keep not in by_id:
            keep = members[0] if members else None
        if keep is None:
            continue
        for member in members:
            if (
                member != keep
                and member not in explicit_keeps
                and member not in replaced_by
                and find(keep) != member
            ):
                replaced_by[member] = keep

    for adjustment in as_list(critic.get("severity_adjustments")):
        adjustment = as_dict(adjustment)
        key = normalize_finding_id(adjustment.get("finding_id"))
        recommended = adjustment.get("recommended_severity")
        if key not in by_id:
            unresolved.append(str(adjustment.get("finding_id")))
        elif (
            find(key) == key
            and isinstance(recommended, str)
            and recommended in SEVERITY_RANK
        ):
            by_id[key]["severity"] = recommended

    kept = [key for key in order if find(key) == key]
    position = {key: index for index, key in enumerate(order)}

    def sort_key(key: str) -> tuple[int, int, int]:
        finding = by_id[key]
        lens = finding.get("review_lens")
        lens_rank = (
            LENS_ORDER.index(lens)
            if isinstance(lens, str) and lens in LENS_ORDER
            else len(LENS_ORDER)
        )
        severity = finding.get("severity")
        severity_rank = (
            SEVERITY_RANK[severity]
            if isinstance(severity, str) and severity in SEVERITY_RANK
            else len(SEVERITIES)
        )
        return (severity_rank, lens_rank, position[key])

    kept.sort(key=sort_key)
    counters: dict[str, int] = {}
    new_ids: dict[str, str] = {}
    findings = []
    for key in kept:
        finding = by_id[key]
        lens = finding.get("review_lens")
        lens = lens if isinstance(lens, str) else ""
        counters[lens] = counters.get(lens, 0) + 1
        new_id = f"F-{LENS_PREFIX.get(lens, 'GAP')}-{counters[lens]}"
        new_ids[key] = new_id
        finding["finding_id"] = new_id
        findings.append(finding)
    themes = [
        text
        for text in (
            _theme_text(as_dict(t)) for t in as_list(critic.get("cross_lens_themes"))
        )
        if text
    ]
    return {
        "findings": findings,
        "themes": themes,
        "counts_by_lens": {
            lens: sum(1 for f in findings if f.get("review_lens") == lens)
            for lens in LENS_ORDER
        },
        "counts_by_severity": {
            severity: sum(1 for f in findings if f.get("severity") == severity)
            for severity in SEVERITIES
        },
        "id_map": {key: new_ids[find(key)] for key in order},
        "dropped": sorted(key for key in order if find(key) != key),
        "unresolved_ids": sorted(set(unresolved)),
    }


def narrative_inputs_state(state: Any, assembled: dict[str, Any]) -> dict[str, str]:
    inventory = as_dict(state.get("intake_findings"))
    findings = assembled["findings"]
    summary_keys = (
        "content_summary",
        "promotional_intent",
        "intended_audience",
        "audience_rationale",
        "product_or_topic",
        "open_observations",
    )
    return {
        "intake_summary": compact_json({k: inventory.get(k) for k in summary_keys}),
        "final_findings_digest": compact_json([finding_digest(f) for f in findings]),
        "report_themes": compact_json(assembled["themes"]),
        "final_counts": compact_json(
            {
                "by_lens": assembled["counts_by_lens"],
                "by_severity": assembled["counts_by_severity"],
            }
        ),
        "final_defense_by_item": compact_json(
            defense_by_item(as_dict(state.get("submitter_defense")), findings)
        ),
    }


def _fallback_summary(inventory: dict[str, Any], assembled: dict[str, Any]) -> str:
    counts = assembled["counts_by_severity"]
    breakdown = ", ".join(f"{counts[s]} {s}" for s in SEVERITIES if counts[s])
    subject = _text(inventory.get("content_summary")) or "The submission."
    detail = f" ({breakdown})" if breakdown else ""
    return (
        f"{subject} The review produced {len(assembled['findings'])} findings{detail}. "
        "The narrative summary could not be generated for this run; see the "
        "findings and themes."
    )


def compose_final_report(state: Any, assembled: dict[str, Any]) -> dict[str, Any]:
    inventory = as_dict(state.get("intake_findings"))
    narrative = as_dict(state.get("report_narrative"))
    report = {
        "content_summary": _text(inventory.get("content_summary")),
        "intended_audience": inventory.get("intended_audience") or "mixed_or_unclear",
        "promotional_intent": _text(inventory.get("promotional_intent")),
        "executive_summary": _text(narrative.get("executive_summary"))
        or _fallback_summary(inventory, assembled),
        "themes": assembled["themes"],
        "findings": assembled["findings"],
        "open_questions_for_reviewers": _string_list(
            narrative.get("open_questions_for_reviewers")
        ),
        "recommended_discussion_topics": _string_list(
            narrative.get("recommended_discussion_topics")
        ),
        "counts_by_lens": assembled["counts_by_lens"],
        "counts_by_severity": assembled["counts_by_severity"],
    }
    try:
        return FinalReport.model_validate(report).model_dump(mode="json")
    except ValidationError:
        logger.exception("Final report failed schema validation")
        return report


CRITIC_INPUT_DEFAULTS = {
    "findings_digest": "[]",
    "dup_candidates": "{}",
    "coverage_hints": "{}",
    "defense_by_item": "{}",
}
NARRATIVE_INPUT_DEFAULTS = {
    "intake_summary": "{}",
    "final_findings_digest": "[]",
    "report_themes": "[]",
    "final_counts": "{}",
    "final_defense_by_item": "{}",
}


def empty_critic_assessment() -> dict[str, Any]:
    return {
        "overall_assessment": "No critic commentary was produced.",
        "duplicate_groups": [],
        "cross_lens_themes": [],
        "severity_adjustments": [],
        "gaps_identified": [],
        "additional_findings": [],
        "iteration_recommendation": "reviewers_have_converged",
    }


def _empty_counts() -> tuple[dict[str, int], dict[str, int]]:
    return dict.fromkeys(LENS_ORDER, 0), dict.fromkeys(SEVERITIES, 0)


def fallback_assembly(state: Any) -> dict[str, Any]:
    """Every reviewer and gap finding, unmerged, if the normal assembly fails."""
    findings: list[dict[str, Any]] = []
    try:
        gap = as_list(as_dict(state.get("critic_review")).get("additional_findings"))
        for finding in [*all_reviewer_findings(state), *gap]:
            if isinstance(finding, dict):
                findings.append(finding)
    except Exception:
        logger.exception("fallback assembly could not read the findings")
    by_lens, by_severity = _empty_counts()
    for finding in findings:
        if finding.get("review_lens") in by_lens:
            by_lens[finding["review_lens"]] += 1
        if finding.get("severity") in by_severity:
            by_severity[finding["severity"]] += 1
    return {
        "findings": findings,
        "themes": [],
        "counts_by_lens": by_lens,
        "counts_by_severity": by_severity,
        "id_map": {},
        "dropped": [],
        "unresolved_ids": [],
    }


def minimal_report(assembled: dict[str, Any]) -> dict[str, Any]:
    by_lens, by_severity = _empty_counts()
    return {
        "content_summary": "",
        "intended_audience": "mixed_or_unclear",
        "promotional_intent": "",
        "executive_summary": (
            "The report could not be fully composed for this run; the findings "
            "below are the unprocessed review output."
        ),
        "themes": [],
        "findings": as_list(assembled.get("findings")),
        "open_questions_for_reviewers": [],
        "recommended_discussion_topics": [],
        "counts_by_lens": assembled.get("counts_by_lens") or by_lens,
        "counts_by_severity": assembled.get("counts_by_severity") or by_severity,
    }
