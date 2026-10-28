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
import re

import pytest

from app import processing

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "vitalynx"
PROMPTS = pathlib.Path(__file__).resolve().parents[2] / "app" / "prompts"
REVIEWER_FILES = {
    "medical": "medical_reviewer",
    "legal": "legal_reviewer",
    "regulatory": "regulatory_reviewer",
    "editorial": "editorial_reviewer",
}
CRITIC_PROMPTS = ("dedupe_critic", "severity_critic", "gap_critic")
AVAILABLE_STATE = {
    "intake_findings",
    "findings_digest",
    "dup_candidates",
    "coverage_hints",
    "defense_by_item",
}


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _baseline_state() -> dict:
    intake = _fixture("intake")
    items = processing.items_by_id(intake)
    state = {
        "intake_findings": intake,
        "submitter_defense": _fixture("submitter_advocate"),
    }
    for lens, name in REVIEWER_FILES.items():
        output = _fixture(name)
        state[processing.REVIEWER_STATE_KEYS[lens]] = processing.expand_reviewer_output(
            lens, output, items
        )
    return state


@pytest.mark.parametrize("name", CRITIC_PROMPTS)
def test_critic_prompts_only_use_placeholders_the_graph_provides(name: str) -> None:
    text = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    placeholders = {p.rstrip("?") for p in re.findall(r"\{([a-z_]+\??)\}", text)}
    assert placeholders <= AVAILABLE_STATE, placeholders - AVAILABLE_STATE


def test_prepare_critic_inputs_state_has_all_keys_as_json_strings() -> None:
    prepared = processing.prepare_critic_inputs_state(_baseline_state())
    assert set(prepared) == AVAILABLE_STATE - {"intake_findings"}
    for value in prepared.values():
        assert isinstance(value, str)
        json.loads(value)


def test_digest_is_compact_and_keeps_the_fields_critics_need() -> None:
    state = _baseline_state()
    digest = json.loads(
        processing.prepare_critic_inputs_state(state)["findings_digest"]
    )
    full_chars = sum(
        len(json.dumps(state.get(key, {})))
        for key in processing.REVIEWER_STATE_KEYS.values()
    )
    assert len(digest) == 40
    assert len(json.dumps(digest)) < full_chars * 0.4
    entry = digest[0]
    assert set(entry) == {
        "id",
        "lens",
        "severity",
        "confidence",
        "category",
        "observation",
        "principle",
        "items",
        "region",
        "bbox",
        "quote",
    }
    assert "discussion" not in entry
    assert len({d["id"] for d in digest}) == 40


def test_duplicate_pairs_recover_the_baseline_critic_groups() -> None:
    state = _baseline_state()
    findings = processing.all_reviewer_findings(state)
    pairs = {frozenset(p) for p in processing.candidate_duplicate_pairs(findings)}
    linked = total = 0
    for group in _fixture("dedupe_critic")["duplicate_groups"]:
        keep = processing.normalize_finding_id(group["keep"])
        for raw in group["finding_ids"]:
            member = processing.normalize_finding_id(raw)
            if member == keep:
                continue
            total += 1
            linked += frozenset((keep, member)) in pairs
    assert total == 9
    assert linked == total
    assert len(pairs) < 120


def test_duplicate_pairs_need_shared_items_or_overlapping_boxes() -> None:
    def make(finding_id, items, bbox):
        return {
            "finding_id": finding_id,
            "related_item_ids": items,
            "location": {"bbox": bbox},
        }

    findings = [
        make("A", ["C1"], [0.1, 0.1, 0.3, 0.3]),
        make("B", ["C1"], [0.6, 0.6, 0.9, 0.9]),
        make("C", ["C2"], [0.1, 0.1, 0.3, 0.3]),
        make("D", ["C3"], [0.7, 0.1, 0.9, 0.2]),
        make("E", [], [0.7, 0.1, 0.9, 0.2]),
        make("F", [], [0.0, 0.9, 0.1, 1.0]),
    ]
    pairs = processing.candidate_duplicate_pairs(findings)
    assert pairs == [["A", "B"], ["A", "C"], ["D", "E"]]


def test_normalize_finding_id_handles_zero_padding_and_aliases() -> None:
    assert processing.normalize_finding_id("F-REG-01") == "F-REG-1"
    assert processing.normalize_finding_id("f-edt-02") == "F-ED-2"
    assert processing.normalize_finding_id(" F-ED-10 ") == "F-ED-10"
    assert processing.normalize_finding_id("F-CRIT-3") == "F-GAP-3"
    assert processing.normalize_finding_id("F-GAP-01") == "F-GAP-1"
    assert processing.normalize_finding_id("nonsense") is None
    assert processing.normalize_finding_id(None) is None


def test_baseline_critic_ids_all_resolve_to_expanded_ids() -> None:
    known = {
        f["finding_id"] for f in processing.all_reviewer_findings(_baseline_state())
    }
    critic = _fixture("critic_merger")
    referenced = [i for g in critic["duplicate_groups"] for i in g["finding_ids"]]
    referenced += [a["finding_id"] for a in critic["severity_adjustments"]]
    resolved = [processing.normalize_finding_id(i) for i in referenced]
    assert all(r in known for r in resolved)


def test_coverage_hints_report_uncovered_items_and_absent_kinds() -> None:
    inventory = {
        "items": [
            {
                "item_id": "C1",
                "kind": "headline",
                "text": "Big claim",
                "location": {"bbox": [0, 0, 1, 1]},
            },
            {"item_id": "C2", "kind": "disclaimer", "text": "Fine print " * 20},
            {"item_id": "C3", "kind": "call_to_action", "text": "Buy"},
        ]
    }
    findings = [
        {
            "finding_id": "F-MED-1",
            "review_lens": "medical",
            "category": "clarity",
            "related_item_ids": ["C1"],
        },
        {
            "finding_id": "F-LEG-1",
            "review_lens": "legal",
            "category": "citation",
            "related_item_ids": [],
        },
    ]
    hints = processing.coverage_hints(inventory, findings)
    assert [i["item_id"] for i in hints["items_without_findings"]] == ["C2", "C3"]
    assert len(hints["items_without_findings"][0]["text"]) == 80
    assert hints["findings_per_lens"] == {
        "medical": 1,
        "legal": 1,
        "regulatory": 0,
        "editorial": 0,
        "custom": 0,
    }
    assert hints["categories_present"] == ["citation", "clarity"]
    assert "important_safety_info" in hints["absent_item_kinds"]
    assert "disclaimer" not in hints["absent_item_kinds"]
    assert hints["image_submission"] is True


def test_defense_mapping_links_findings_through_shared_items() -> None:
    brief = {
        "overall_framing": "frame",
        "defenses": [
            {
                "addresses_topic": "the claim",
                "related_item_ids": ["C1"],
                "argument": "x" * 500,
                "proposed_compromise": None,
                "likely_to_dispute": ["legal"],
            },
            "garbage",
        ],
    }
    findings = [
        {"finding_id": "F-A", "related_item_ids": ["C1", "C2"]},
        {"finding_id": "F-B", "related_item_ids": ["C9"]},
    ]
    mapped = processing.defense_by_item(brief, findings)
    assert mapped["overall_framing"] == "frame"
    assert len(mapped["defenses"]) == 1
    assert mapped["defenses"][0]["related_findings"] == ["F-A"]
    assert len(mapped["defenses"][0]["argument"]) == processing.DEFENSE_ARGUMENT_LIMIT


def test_prepare_critic_inputs_node_writes_state() -> None:
    from app.agent import prepare_critic_inputs

    class Ctx:
        def __init__(self, state: dict) -> None:
            self.state = state

    ctx = Ctx(_baseline_state())
    assert prepare_critic_inputs(ctx, "passthrough") == "passthrough"
    for key in AVAILABLE_STATE - {"intake_findings"}:
        assert isinstance(ctx.state[key], str)
    assert len(json.loads(ctx.state["findings_digest"])) == 40
