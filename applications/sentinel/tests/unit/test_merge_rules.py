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

from app import processing
from app.schemas import FinalReport
from tests.unit.test_assembly import _finding, _state


def _group(ids, keep):
    return {"duplicate_groups": [{"finding_ids": ids, "rationale": "r", "keep": keep}]}


def _assemble(findings_by_lens, critic):
    return processing.assemble_findings(_state(findings_by_lens, critic))


def test_the_kept_finding_takes_the_most_severe_severity_of_the_group() -> None:
    assembled = _assemble(
        {
            "medical": [_finding("F-MED-1", severity="low")],
            "legal": [_finding("F-LEG-1", lens="legal", severity="high")],
        },
        _group(["F-MED-1", "F-LEG-1"], keep="F-MED-1"),
    )
    assert [f["severity"] for f in assembled["findings"]] == ["high"]


def test_a_critic_downgrade_of_the_keeper_cannot_go_below_a_merged_member() -> None:
    critic = _group(["F-MED-1", "F-LEG-1"], keep="F-MED-1")
    critic["severity_adjustments"] = [
        {
            "finding_id": "F-MED-1",
            "current_severity": "high",
            "recommended_severity": "low",
            "rationale": "r",
        }
    ]
    assembled = _assemble(
        {
            "medical": [_finding("F-MED-1", severity="high")],
            "legal": [_finding("F-LEG-1", lens="legal", severity="medium")],
        },
        critic,
    )
    assert [f["severity"] for f in assembled["findings"]] == ["medium"]


def test_merged_findings_carry_the_other_lenses_actions_and_questions() -> None:
    kept = _finding("F-MED-1", suggested_actions=["Fix A"], suggested_questions=["Q1?"])
    other = _finding(
        "F-LEG-1",
        lens="legal",
        suggested_actions=["fix a", "Fix B"],
        suggested_questions=["Q2?"],
    )
    assembled = _assemble(
        {"medical": [kept], "legal": [other]},
        _group(["F-MED-1", "F-LEG-1"], keep="F-MED-1"),
    )
    finding = assembled["findings"][0]
    assert finding["suggested_actions"] == ["Fix A", "Fix B"]
    assert finding["suggested_questions"] == ["Q1?", "Q2?"]


def test_carried_actions_are_capped() -> None:
    many = [f"action {n}" for n in range(20)]
    assembled = _assemble(
        {
            "medical": [_finding("F-MED-1")],
            "legal": [_finding("F-LEG-1", lens="legal", suggested_actions=many)],
        },
        _group(["F-MED-1", "F-LEG-1"], keep="F-MED-1"),
    )
    carried = assembled["findings"][0]["suggested_actions"]
    assert len(carried) == processing.MAX_CARRIED_ITEMS


def test_merged_from_records_what_each_merged_finding_said() -> None:
    other = _finding(
        "F-LEG-1", lens="legal", severity="high", mlr_principle="legal principle"
    )
    assembled = _assemble(
        {"medical": [_finding("F-MED-1")], "legal": [other]},
        _group(["F-MED-1", "F-LEG-1"], keep="F-MED-1"),
    )
    finding = assembled["findings"][0]
    assert finding["merged_from"] == [
        {
            "review_lens": "legal",
            "category": "clarity",
            "severity": "high",
            "observation": "obs F-LEG-1",
            "mlr_principle": "legal principle",
        }
    ]
    assert finding["also_raised_by"] == ["legal"]


def test_findings_that_are_not_merged_have_no_merged_from() -> None:
    assembled = _assemble({"medical": [_finding("F-MED-1")]}, {})
    assert assembled["findings"][0]["merged_from"] == []


def test_findings_about_different_items_are_not_merged() -> None:
    assembled = _assemble(
        {
            "medical": [_finding("F-MED-1", related_item_ids=["C1", "C2"])],
            "legal": [_finding("F-LEG-1", lens="legal", related_item_ids=["C2", "C3"])],
        },
        _group(["F-MED-1", "F-LEG-1"], keep="F-MED-1"),
    )
    assert len(assembled["findings"]) == 2
    assert assembled["dropped"] == []


def test_findings_without_items_are_not_merged() -> None:
    assembled = _assemble(
        {
            "medical": [_finding("F-MED-1", related_item_ids=[])],
            "legal": [_finding("F-LEG-1", lens="legal", related_item_ids=[])],
        },
        _group(["F-MED-1", "F-LEG-1"], keep="F-MED-1"),
    )
    assert len(assembled["findings"]) == 2


def test_a_finding_about_a_subset_or_superset_of_the_items_is_merged() -> None:
    assembled = _assemble(
        {
            "medical": [_finding("F-MED-1", related_item_ids=["C1", "C2"])],
            "legal": [_finding("F-LEG-1", lens="legal", related_item_ids=["C2"])],
            "regulatory": [
                _finding(
                    "F-REG-1", lens="regulatory", related_item_ids=["C1", "C2", "C3"]
                )
            ],
        },
        _group(["F-MED-1", "F-LEG-1", "F-REG-1"], keep="F-MED-1"),
    )
    assert len(assembled["findings"]) == 1
    assert sorted(assembled["findings"][0]["also_raised_by"]) == ["legal", "regulatory"]


def test_malformed_members_do_not_break_the_merge() -> None:
    odd = _finding("F-LEG-1", lens="legal")
    odd["suggested_actions"] = "not a list"
    odd["review_lens"] = ["not", "a", "string"]
    assembled = _assemble(
        {"medical": [_finding("F-MED-1")], "legal": [odd]},
        _group(["F-MED-1", "F-LEG-1"], keep="F-MED-1"),
    )
    assert len(assembled["findings"]) >= 1


def test_the_final_report_validates_with_merged_from() -> None:
    state = _state(
        {
            "medical": [_finding("F-MED-1")],
            "legal": [_finding("F-LEG-1", lens="legal", severity="high")],
        },
        _group(["F-MED-1", "F-LEG-1"], keep="F-MED-1"),
    )
    assembled = processing.assemble_findings(state)
    report = processing.compose_final_report(state, assembled)
    validated = FinalReport.model_validate(report)
    assert validated.findings[0].merged_from[0].review_lens == "legal"
