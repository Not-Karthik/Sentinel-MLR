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

import json
import pathlib

import pytest

from app import processing
from app.schemas import ReviewerOutput

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "vitalynx"
REVIEWER_FILES = {
    "medical": "medical_reviewer",
    "legal": "legal_reviewer",
    "regulatory": "regulatory_reviewer",
    "editorial": "editorial_reviewer",
}
SLIM_FIELDS = (
    "category",
    "severity",
    "confidence",
    "evidence_depth",
    "related_item_ids",
    "quoted_content",
    "observation",
    "mlr_principle",
    "discussion",
    "suggested_questions",
    "suggested_actions",
)
ITEMS = {
    "C1": {
        "item_id": "C1",
        "location": {
            "page": 1,
            "section": "Header",
            "bbox": [0.1, 0.1, 0.4, 0.2],
            "quote": "Claim",
        },
    },
    "C2": {
        "item_id": "C2",
        "location": {
            "page": None,
            "section": "Footer",
            "bbox": [0.2, 0.8, 0.9, 0.95],
            "quote": None,
        },
    },
    "C3": {"item_id": "C3", "location": None},
}


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _slim(finding: dict) -> dict:
    return {key: finding.get(key) for key in SLIM_FIELDS}


def _raw(**overrides) -> dict:
    raw = {
        "category": "clarity",
        "severity": "low",
        "confidence": 0.9,
        "evidence_depth": "surface",
        "related_item_ids": ["C1"],
        "observation": "o",
        "mlr_principle": "p",
        "discussion": "d",
    }
    raw.update(overrides)
    return raw


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.0, "high"),
        (0.8, "high"),
        (0.79, "medium"),
        (0.5, "medium"),
        (0.49, "low"),
        (0.0, "low"),
    ],
)
def test_confidence_band_thresholds(value, expected) -> None:
    assert processing.confidence_band(value) == expected


def test_derive_location_single_item_copies_item_location() -> None:
    location = processing.derive_location({"related_item_ids": ["C1"]}, ITEMS)
    assert location == {
        "page": 1,
        "section": "Header",
        "bbox": [0.1, 0.1, 0.4, 0.2],
        "quote": "Claim",
    }


def test_derive_location_several_items_uses_union_box() -> None:
    location = processing.derive_location({"related_item_ids": ["C1", "C2"]}, ITEMS)
    assert location["bbox"] == [0.1, 0.1, 0.9, 0.95]
    assert location["section"] == "Header, Footer"
    assert location["page"] == 1
    assert location["quote"] is None


def test_derive_location_explicit_bbox_overrides_item_box() -> None:
    location = processing.derive_location(
        {"related_item_ids": ["C1"], "bbox": [0.5, 0.5, 0.6, 0.6]}, ITEMS
    )
    assert location["bbox"] == [0.5, 0.5, 0.6, 0.6]
    assert location["section"] == "Header"


def test_derive_location_without_usable_items() -> None:
    assert processing.derive_location({"related_item_ids": ["C3", "C9"]}, ITEMS) is None
    assert processing.derive_location({}, ITEMS) is None
    only_box = processing.derive_location({"bbox": [0.1, 0.1, 0.2, 0.2]}, ITEMS)
    assert only_box == {
        "page": None,
        "section": None,
        "bbox": [0.1, 0.1, 0.2, 0.2],
        "quote": None,
    }


def test_derive_location_keeps_legacy_model_location_fields() -> None:
    location = processing.derive_location(
        {
            "related_item_ids": ["C1"],
            "location": {"section": "Custom", "bbox": [0.3, 0.3, 0.5, 0.5]},
        },
        ITEMS,
    )
    assert location["section"] == "Custom"
    assert location["bbox"] == [0.3, 0.3, 0.5, 0.5]
    assert location["page"] == 1


def test_expand_assigns_ids_lens_band_and_location() -> None:
    output = {
        "reviewer_summary": "summary",
        "findings": [
            _raw(),
            _raw(
                category="tone",
                severity="medium",
                confidence=0.4,
                evidence_depth="moderate",
                related_item_ids=["C2"],
                suggested_actions=["a"],
            ),
        ],
    }
    expanded = processing.expand_reviewer_output("editorial", output, ITEMS)
    assert expanded["lens"] == "editorial"
    assert [f["finding_id"] for f in expanded["findings"]] == ["F-ED-1", "F-ED-2"]
    assert {f["review_lens"] for f in expanded["findings"]} == {"editorial"}
    assert [f["confidence_band"] for f in expanded["findings"]] == ["high", "low"]
    assert expanded["findings"][0]["location"]["bbox"] == [0.1, 0.1, 0.4, 0.2]
    assert expanded["findings"][1]["suggested_actions"] == ["a"]
    ReviewerOutput.model_validate(expanded)


def test_expand_handles_empty_and_garbage_output() -> None:
    empty = processing.expand_reviewer_output("custom", {"findings": []}, {})
    assert empty["findings"] == []
    garbage = processing.expand_reviewer_output(
        "legal", {"findings": ["x", None, 3]}, {}
    )
    assert garbage["findings"] == []
    assert garbage["reviewer_summary"] == ""


def test_expand_clamps_bad_confidence() -> None:
    finding = processing.expand_reviewer_output(
        "medical", {"findings": [_raw(confidence="nope", related_item_ids=[])]}, {}
    )["findings"][0]
    assert finding["confidence"] == 0.5
    assert finding["confidence_band"] == "medium"
    assert finding["location"] is None


@pytest.mark.parametrize("lens", list(REVIEWER_FILES))
def test_baseline_reviewer_findings_roundtrip(lens: str) -> None:
    items = processing.items_by_id(_fixture("intake"))
    baseline = _fixture(REVIEWER_FILES[lens])
    slim = {
        "reviewer_summary": baseline["reviewer_summary"],
        "findings": [_slim(f) for f in baseline["findings"]],
    }
    expanded = processing.expand_reviewer_output(lens, slim, items)
    assert len(expanded["findings"]) == len(baseline["findings"])
    ReviewerOutput.model_validate(expanded)
    prefix = processing.LENS_PREFIX[lens]
    ids = [f["finding_id"] for f in expanded["findings"]]
    assert ids == [f"F-{prefix}-{n}" for n in range(1, len(ids) + 1)]
    for new, old in zip(expanded["findings"], baseline["findings"], strict=True):
        for key in (
            "category",
            "severity",
            "confidence",
            "evidence_depth",
            "observation",
            "discussion",
        ):
            assert new[key] == old[key]
        assert new["review_lens"] == lens


def _baseline_rows() -> list[tuple[str, dict, dict]]:
    rows = []
    for lens, name in REVIEWER_FILES.items():
        for finding in _fixture(name)["findings"]:
            rows.append((lens, finding, (finding.get("location") or {}).get("bbox")))
    return rows


def _expand_one(lens: str, slim: dict) -> dict:
    items = processing.items_by_id(_fixture("intake"))
    output = {"reviewer_summary": "", "findings": [slim]}
    return processing.expand_reviewer_output(lens, output, items)["findings"][0]


def test_single_item_findings_recover_the_baseline_box_exactly() -> None:
    single = [r for r in _baseline_rows() if len(r[1]["related_item_ids"]) == 1]
    assert len(single) == 25
    for lens, finding, old_box in single:
        new = _expand_one(lens, _slim(finding))
        assert new["location"]["bbox"] == old_box


def test_multi_item_findings_keep_the_models_box_when_it_supplies_one() -> None:
    multi = [r for r in _baseline_rows() if len(r[1]["related_item_ids"]) > 1]
    assert len(multi) == 15
    for lens, finding, old_box in multi:
        slim = _slim(finding)
        slim["bbox"] = old_box
        assert _expand_one(lens, slim)["location"]["bbox"] == old_box


def test_multi_item_fallback_box_covers_the_models_choice() -> None:
    multi = [r for r in _baseline_rows() if len(r[1]["related_item_ids"]) > 1]
    covered = 0
    for lens, finding, old_box in multi:
        union = _expand_one(lens, _slim(finding))["location"]["bbox"]
        tolerance = 0.01
        if (
            union[0] <= old_box[0] + tolerance
            and union[1] <= old_box[1] + tolerance
            and union[2] >= old_box[2] - tolerance
            and union[3] >= old_box[3] - tolerance
        ):
            covered += 1
    assert covered / len(multi) >= 0.8


def test_every_baseline_finding_gets_a_location() -> None:
    for lens, finding, _ in _baseline_rows():
        assert _expand_one(lens, _slim(finding))["location"]["bbox"] is not None


def test_normalize_reviewer_state_expands_every_present_key() -> None:
    state = {
        "intake_findings": _fixture("intake"),
        "medical_findings": {"reviewer_summary": "m", "findings": []},
        "rules_findings": {"reviewer_summary": "none", "findings": []},
    }
    expanded = processing.normalize_reviewer_state(state)
    assert set(expanded) == {"medical_findings", "rules_findings"}
    assert expanded["rules_findings"]["lens"] == "custom"


def test_normalize_findings_node_rewrites_state() -> None:
    from app.agent import normalize_findings

    class Ctx:
        def __init__(self, state: dict) -> None:
            self.state = state

    items = _fixture("intake")["items"]
    ctx = Ctx(
        {
            "intake_findings": {"items": items},
            "legal_findings": {
                "reviewer_summary": "s",
                "findings": [
                    _raw(
                        category="citation",
                        severity="high",
                        confidence=0.7,
                        related_item_ids=[items[0]["item_id"]],
                    )
                ],
            },
        }
    )
    assert normalize_findings(ctx, "passthrough") == "passthrough"
    finding = ctx.state["legal_findings"]["findings"][0]
    assert finding["finding_id"] == "F-LEG-1"
    assert finding["confidence_band"] == "medium"
    assert finding["location"]["bbox"] == items[0]["location"]["bbox"]
