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
from pydantic import ValidationError

from app import processing
from app.schemas import CriticAssessment, GapCriticOutputLLM, GapFindingLLM

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "vitalynx"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _state(**overrides) -> dict:
    state = {
        "intake_findings": _fixture("intake"),
        "dedupe_critic_output": _fixture("dedupe_critic"),
        "severity_critic_output": _fixture("severity_critic"),
        "gap_critic_output": _fixture("gap_critic"),
    }
    state.update(overrides)
    return state


def test_merge_matches_the_baseline_merger_on_every_copied_field() -> None:
    baseline = _fixture("critic_merger")
    merged = processing.merge_critic_outputs(_state())
    assert merged["duplicate_groups"] == baseline["duplicate_groups"]
    assert merged["cross_lens_themes"] == baseline["cross_lens_themes"]
    assert merged["severity_adjustments"] == baseline["severity_adjustments"]
    assert merged["gaps_identified"] == baseline["gaps_identified"]
    assert merged["iteration_recommendation"] == baseline["iteration_recommendation"]
    CriticAssessment.model_validate(merged)


def test_gap_findings_get_gap_ids_and_keep_lens_severity_and_text() -> None:
    baseline = _fixture("critic_merger")["additional_findings"]
    merged = processing.merge_critic_outputs(_state())["additional_findings"]
    assert [f["finding_id"] for f in merged] == ["F-GAP-1", "F-GAP-2", "F-GAP-3"]
    assert len(merged) == len(baseline)
    for new, old in zip(merged, baseline, strict=True):
        assert new["review_lens"] == old["review_lens"]
        assert new["severity"] == old["severity"]
        assert new["observation"] == old["observation"]
        assert new["location"] is not None


def test_overall_assessment_is_built_from_the_critics_own_text() -> None:
    state = _state()
    merged = processing.merge_critic_outputs(state)
    for source in (
        state["dedupe_critic_output"]["rationale"],
        state["severity_critic_output"]["rationale"],
        state["gap_critic_output"]["completeness_assessment"],
    ):
        assert source.strip() in merged["overall_assessment"]


@pytest.mark.parametrize(
    ("gaps", "adjustments", "iteration", "expected"),
    [
        (2, 0, 0, "another_pass_would_help"),
        (0, 3, 0, "another_pass_would_help"),
        (1, 2, 0, "reviewers_have_converged"),
        (5, 5, 1, "reviewers_have_converged"),
        (0, 0, 0, "reviewers_have_converged"),
    ],
)
def test_iteration_recommendation_rule(gaps, adjustments, iteration, expected) -> None:
    state = _state(
        gap_critic_output={
            "gaps_identified": [f"g{n}" for n in range(gaps)],
            "additional_findings": [],
            "completeness_assessment": "c",
        },
        severity_critic_output={
            "severity_adjustments": [
                {
                    "finding_id": f"F-MED-{n}",
                    "current_severity": "high",
                    "recommended_severity": "medium",
                    "rationale": "r",
                }
                for n in range(adjustments)
            ],
            "advocate_weighed": True,
            "rationale": "r",
        },
        review_iteration_count=iteration,
    )
    merged = processing.merge_critic_outputs(state)
    assert merged["iteration_recommendation"] == expected


def test_missing_critic_outputs_still_produce_a_valid_assessment() -> None:
    merged = processing.merge_critic_outputs({"intake_findings": {"items": []}})
    CriticAssessment.model_validate(merged)
    assert merged["duplicate_groups"] == []
    assert merged["additional_findings"] == []
    assert merged["overall_assessment"]
    assert merged["iteration_recommendation"] == "reviewers_have_converged"


def test_gap_finding_with_unknown_lens_falls_back_to_regulatory() -> None:
    state = _state(
        gap_critic_output={
            "gaps_identified": [],
            "completeness_assessment": "c",
            "additional_findings": [
                {
                    "review_lens": "nonsense",
                    "category": "other",
                    "severity": "low",
                    "confidence": 0.7,
                    "evidence_depth": "surface",
                    "related_item_ids": ["C1"],
                    "observation": "o",
                    "mlr_principle": "p",
                    "discussion": "d",
                }
            ],
        }
    )
    finding = processing.merge_critic_outputs(state)["additional_findings"][0]
    assert finding["review_lens"] == "regulatory"
    assert finding["finding_id"] == "F-GAP-1"
    assert finding["confidence_band"] == "medium"


def test_gap_schema_requires_a_lens_and_has_no_id_fields() -> None:
    assert "finding_id" not in GapFindingLLM.model_fields
    assert "confidence_band" not in GapFindingLLM.model_fields
    assert "location" not in GapFindingLLM.model_fields
    with pytest.raises(ValidationError):
        GapFindingLLM(
            category="other",
            severity="low",
            confidence=0.5,
            evidence_depth="surface",
            observation="o",
            mlr_principle="p",
            discussion="d",
        )
    output = GapCriticOutputLLM(completeness_assessment="c")
    assert output.additional_findings == []


def test_merge_critics_node_writes_critic_review_to_state() -> None:
    from app.agent import merge_critics

    class Ctx:
        def __init__(self, state: dict) -> None:
            self.state = state

    ctx = Ctx(_state())
    assert merge_critics(ctx, "passthrough") == "passthrough"
    assert (
        ctx.state["critic_review"]["iteration_recommendation"]
        == "another_pass_would_help"
    )
    assert len(ctx.state["critic_review"]["additional_findings"]) == 3
