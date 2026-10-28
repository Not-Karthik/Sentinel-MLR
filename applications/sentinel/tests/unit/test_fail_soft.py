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
import logging
import pathlib

import pytest

from app import agent, processing
from app.schemas import FinalReport

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "vitalynx"
REVIEWERS = {
    "medical": "medical_reviewer",
    "legal": "legal_reviewer",
    "regulatory": "regulatory_reviewer",
    "editorial": "editorial_reviewer",
}


class Ctx:
    def __init__(self, state: dict) -> None:
        self.state = state


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _state() -> dict:
    intake = _fixture("intake")
    items = processing.items_by_id(intake)
    state = {
        "intake_findings": intake,
        "submitter_defense": _fixture("submitter_advocate"),
        "dedupe_critic_output": _fixture("dedupe_critic"),
        "severity_critic_output": _fixture("severity_critic"),
        "gap_critic_output": _fixture("gap_critic"),
    }
    for lens, name in REVIEWERS.items():
        state[processing.REVIEWER_STATE_KEYS[lens]] = processing.expand_reviewer_output(
            lens, _fixture(name), items
        )
    state["critic_review"] = processing.merge_critic_outputs(state)
    return state


def _boom(*args, **kwargs):
    raise RuntimeError("simulated bug")


def test_normalize_intake_survives_a_failure(monkeypatch, caplog) -> None:
    monkeypatch.setattr(processing, "normalize_inventory", _boom)
    ctx = Ctx(_state())
    with caplog.at_level(logging.ERROR):
        assert agent.normalize_intake(ctx, "x") == "x"
    assert "normalize_intake failed" in caplog.text
    assert ctx.state["intake_findings"]["items"]


def test_normalize_findings_survives_a_failure(monkeypatch) -> None:
    monkeypatch.setattr(processing, "normalize_reviewer_state", _boom)
    ctx = Ctx(_state())
    assert agent.normalize_findings(ctx, "x") == "x"


def test_prepare_critic_inputs_falls_back_to_empty_hints(monkeypatch) -> None:
    monkeypatch.setattr(processing, "prepare_critic_inputs_state", _boom)
    ctx = Ctx({})
    assert agent.prepare_critic_inputs(ctx, "x") == "x"
    assert ctx.state["findings_digest"] == "[]"
    assert set(processing.CRITIC_INPUT_DEFAULTS) <= set(ctx.state)


def test_merge_critics_falls_back_to_an_empty_assessment(monkeypatch) -> None:
    ctx = Ctx(_state())
    monkeypatch.setattr(processing, "merge_critic_outputs", _boom)
    assert agent.merge_critics(ctx, "x") == "x"
    assert (
        ctx.state["critic_review"]["iteration_recommendation"]
        == "reviewers_have_converged"
    )
    assert ctx.state["critic_review"]["duplicate_groups"] == []


def test_assemble_report_keeps_every_finding_when_assembly_fails(monkeypatch) -> None:
    monkeypatch.setattr(processing, "assemble_findings", _boom)
    ctx = Ctx(_state())
    expected = len(processing.all_reviewer_findings(ctx.state)) + len(
        ctx.state["critic_review"]["additional_findings"]
    )
    assert agent.assemble_report(ctx, "x") == "x"
    assert len(ctx.state["assembled_report"]["findings"]) == expected
    assert set(processing.NARRATIVE_INPUT_DEFAULTS) <= set(ctx.state)


def test_assemble_report_survives_narrative_input_failure(monkeypatch) -> None:
    monkeypatch.setattr(processing, "narrative_inputs_state", _boom)
    ctx = Ctx(_state())
    agent.assemble_report(ctx, "x")
    assert ctx.state["final_findings_digest"] == "[]"
    assert len(ctx.state["assembled_report"]["findings"]) == 34


def test_compose_report_still_emits_a_report_when_composition_fails(
    monkeypatch,
) -> None:
    ctx = Ctx(_state())
    agent.assemble_report(ctx, "x")
    monkeypatch.setattr(processing, "compose_final_report", _boom)
    event = agent.compose_report(ctx, "x")
    report = json.loads(event.content.parts[0].text)
    assert len(report["findings"]) == 34
    assert "could not be fully composed" in report["executive_summary"]
    FinalReport.model_validate(report)
    assert ctx.state["final_report"] == report


def test_every_node_failing_together_still_ends_with_a_valid_report(
    monkeypatch,
) -> None:
    ctx = Ctx(_state())
    for name in (
        "normalize_inventory",
        "normalize_reviewer_state",
        "prepare_critic_inputs_state",
        "merge_critic_outputs",
        "assemble_findings",
        "narrative_inputs_state",
        "compose_final_report",
    ):
        monkeypatch.setattr(processing, name, _boom)
    for node in (
        agent.normalize_intake,
        agent.normalize_findings,
        agent.prepare_critic_inputs,
        agent.merge_critics,
        agent.assemble_report,
    ):
        node(ctx, "x")
    event = agent.compose_report(ctx, "x")
    report = FinalReport.model_validate_json(event.content.parts[0].text)
    assert len(report.findings) == 40
    assert sum(report.counts_by_lens.values()) == 40


def test_fallback_assembly_with_empty_state_is_valid() -> None:
    assembled = processing.fallback_assembly({})
    assert assembled["findings"] == []
    report = processing.minimal_report(assembled)
    FinalReport.model_validate(report)


@pytest.mark.parametrize("garbage", [None, "x", 5, [], {"findings": "no"}])
def test_minimal_report_tolerates_garbage_assembly(garbage) -> None:
    assembled = garbage if isinstance(garbage, dict) else {}
    FinalReport.model_validate(processing.minimal_report(assembled))
