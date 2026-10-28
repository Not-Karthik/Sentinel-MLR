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
import pathlib
import random

import pytest

from app import processing
from app.schemas import FinalReport

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "vitalynx"
REVIEWERS = {
    "medical": "medical_reviewer",
    "legal": "legal_reviewer",
    "regulatory": "regulatory_reviewer",
    "editorial": "editorial_reviewer",
}
JUNK = [None, "", "garbage", 0, -3, 7.5, [], {}, ["x"], {"a": 1}, True, 10**9]


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _state() -> dict:
    intake = _fixture("intake")
    items = processing.items_by_id(intake)
    critic = _fixture("critic_merger")
    state = {
        "intake_findings": intake,
        "submitter_defense": _fixture("submitter_advocate"),
        "dedupe_critic_output": _fixture("dedupe_critic"),
        "severity_critic_output": _fixture("severity_critic"),
        "gap_critic_output": _fixture("gap_critic"),
        "report_narrative": {
            "executive_summary": "s",
            "open_questions_for_reviewers": ["q"],
            "recommended_discussion_topics": ["t"],
        },
    }
    for lens, name in REVIEWERS.items():
        state[processing.REVIEWER_STATE_KEYS[lens]] = _fixture(name)
    state["critic_review"] = critic
    state["items"] = items
    return state


def _paths(value, prefix=()):
    yield prefix
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _paths(child, (*prefix, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _paths(child, (*prefix, index))


def _mutate(state: dict, rng: random.Random, edits: int) -> dict:
    mutated = copy.deepcopy(state)
    for _ in range(edits):
        candidates = [p for p in _paths(mutated) if p]
        path = rng.choice(candidates)
        parent = mutated
        for step in path[:-1]:
            parent = parent[step]
        action = rng.choice(["junk", "delete", "junk"])
        last = path[-1]
        if action == "delete" and isinstance(parent, dict):
            parent.pop(last, None)
        elif action == "delete" and isinstance(parent, list) and parent:
            parent.pop(min(last, len(parent) - 1))
        else:
            parent[last] = rng.choice(JUNK)
    return mutated


def _run_pipeline(state: dict) -> dict:
    state = dict(state)
    state.pop("items", None)
    state["intake_findings"] = processing.normalize_inventory(
        processing.as_dict(state.get("intake_findings"))
    )
    state.update(processing.normalize_reviewer_state(state))
    state.update(processing.prepare_critic_inputs_state(state))
    state["critic_review"] = processing.merge_critic_outputs(state)
    assembled = processing.assemble_findings(state)
    state.update(processing.narrative_inputs_state(state, assembled))
    report = processing.compose_final_report(state, assembled)
    return {"assembled": assembled, "report": report, "state": state}


@pytest.mark.parametrize("seed", range(150))
def test_corrupted_model_output_never_crashes_the_code_nodes(seed: int) -> None:
    rng = random.Random(seed)
    corrupted = _mutate(_state(), rng, edits=rng.randint(1, 25))
    result = _run_pipeline(corrupted)
    report = result["report"]
    assert isinstance(report["findings"], list)
    assert isinstance(report["executive_summary"], str) and report["executive_summary"]
    assert set(report["counts_by_severity"]) == set(processing.SEVERITIES)
    assert sum(report["counts_by_lens"].values()) == len(report["findings"])
    json.dumps(report)


@pytest.mark.parametrize("seed", range(60))
def test_corruption_that_leaves_findings_valid_still_yields_a_valid_report(
    seed: int,
) -> None:
    rng = random.Random(1000 + seed)
    corrupted = _mutate(_state(), rng, edits=rng.randint(1, 6))
    report = _run_pipeline(corrupted)["report"]
    try:
        FinalReport.model_validate(report)
    except Exception as error:
        pytest.fail(f"report no longer matches the FinalReport schema: {error}")


def test_total_garbage_in_every_slot_still_produces_a_report() -> None:
    state = {
        key: rng_value
        for key, rng_value in zip(
            [
                "intake_findings",
                "submitter_defense",
                "dedupe_critic_output",
                "severity_critic_output",
                "gap_critic_output",
                "medical_findings",
                "legal_findings",
                "regulatory_findings",
                "editorial_findings",
                "rules_findings",
                "critic_review",
                "report_narrative",
            ],
            [
                "x",
                5,
                [],
                None,
                "{",
                3.2,
                True,
                ["a"],
                {"findings": "no"},
                {"findings": [None]},
                "z",
                9,
            ],
            strict=True,
        )
    }
    report = _run_pipeline(state)["report"]
    assert report["findings"] == []
    assert "could not be generated" in report["executive_summary"]


def test_findings_are_never_lost_when_every_critic_reference_is_garbage() -> None:
    state = _state()
    baseline_total = len(
        processing.assemble_findings(_run_pipeline(state)["state"])["findings"]
    )
    state["dedupe_critic_output"] = {
        "duplicate_groups": [{"finding_ids": ["???", None, 4], "keep": "nope"}],
        "cross_lens_themes": [{"theme": "t"}],
        "rationale": "r",
    }
    state["severity_critic_output"] = {
        "severity_adjustments": [{"finding_id": "zzz", "recommended_severity": "high"}],
        "advocate_weighed": True,
        "rationale": "r",
    }
    result = _run_pipeline(state)
    reviewer_total = sum(
        len(result["state"][key]["findings"])
        for key in processing.REVIEWER_STATE_KEYS.values()
        if key in result["state"]
    )
    gap_total = len(result["state"]["critic_review"]["additional_findings"])
    assert len(result["report"]["findings"]) == reviewer_total + gap_total
    assert len(result["report"]["findings"]) > baseline_total
