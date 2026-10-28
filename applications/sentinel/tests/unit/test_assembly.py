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
from google.adk.agents.context import Context
from google.adk.apps import App
from google.adk.runners import InMemoryRunner
from google.adk.workflow import START, Workflow
from google.genai import types

from app import processing, prompts
from app.agent import assemble_report, compose_report
from app.schemas import FinalReport, ReportNarrative

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "vitalynx"
REVIEWERS = {
    "medical": "medical_reviewer",
    "legal": "legal_reviewer",
    "regulatory": "regulatory_reviewer",
    "editorial": "editorial_reviewer",
}


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _baseline_state() -> dict:
    intake = _fixture("intake")
    items = processing.items_by_id(intake)
    critic = _fixture("critic_merger")
    critic["additional_findings"] = processing.expand_gap_findings(
        critic["additional_findings"], items
    )
    state = {
        "intake_findings": intake,
        "critic_review": critic,
        "submitter_defense": _fixture("submitter_advocate"),
    }
    for lens, name in REVIEWERS.items():
        state[processing.REVIEWER_STATE_KEYS[lens]] = processing.expand_reviewer_output(
            lens, _fixture(name), items
        )
    return state


def _finding(finding_id, lens="medical", severity="medium", **extra) -> dict:
    finding = {
        "finding_id": finding_id,
        "review_lens": lens,
        "category": "clarity",
        "severity": severity,
        "confidence": 0.8,
        "confidence_band": "high",
        "evidence_depth": "surface",
        "related_item_ids": ["C1"],
        "quoted_content": None,
        "observation": f"obs {finding_id}",
        "mlr_principle": "p",
        "discussion": "d",
        "suggested_questions": [],
        "suggested_actions": [],
        "location": None,
    }
    finding.update(extra)
    return finding


def _state(findings_by_lens, critic=None) -> dict:
    state = {"intake_findings": {"items": []}, "critic_review": critic or {}}
    for lens, findings in findings_by_lens.items():
        state[processing.REVIEWER_STATE_KEYS[lens]] = {
            "lens": lens,
            "reviewer_summary": "",
            "findings": findings,
        }
    return state


def test_assembly_reproduces_the_baseline_llm_synthesizer_findings() -> None:
    assembled = processing.assemble_findings(_baseline_state())
    baseline = _fixture("final_report")
    assert len(assembled["findings"]) == len(baseline["findings"]) == 34
    assert assembled["unresolved_ids"] == []
    assert [f["observation"] for f in assembled["findings"]] == [
        f["observation"] for f in baseline["findings"]
    ]
    assert [f["severity"] for f in assembled["findings"]] == [
        f["severity"] for f in baseline["findings"]
    ]
    assert len(assembled["dropped"]) == 9


def test_assembled_ids_are_gap_free_per_lens_and_valid() -> None:
    assembled = processing.assemble_findings(_baseline_state())
    per_lens: dict[str, list[int]] = {}
    for finding in assembled["findings"]:
        match = re.fullmatch(r"F-(MED|LEG|REG|ED|CUSTOM)-(\d+)", finding["finding_id"])
        assert match, finding["finding_id"]
        per_lens.setdefault(finding["review_lens"], []).append(int(match.group(2)))
    for numbers in per_lens.values():
        assert sorted(numbers) == list(range(1, len(numbers) + 1))
    assert len({f["finding_id"] for f in assembled["findings"]}) == 34


def test_assembled_findings_are_sorted_by_severity_then_lens() -> None:
    findings = processing.assemble_findings(_baseline_state())["findings"]
    ranks = [
        (
            processing.SEVERITY_RANK[f["severity"]],
            processing.LENS_ORDER.index(f["review_lens"]),
        )
        for f in findings
    ]
    assert ranks == sorted(ranks)


def test_counts_are_populated_with_zero_entries() -> None:
    assembled = processing.assemble_findings(_baseline_state())
    assert assembled["counts_by_lens"].keys() == set(processing.LENS_ORDER)
    assert assembled["counts_by_severity"].keys() == set(processing.SEVERITIES)
    assert sum(assembled["counts_by_lens"].values()) == 34
    assert sum(assembled["counts_by_severity"].values()) == 34
    assert assembled["counts_by_lens"]["custom"] == 0
    assert assembled["counts_by_severity"]["critical"] == 0


def test_themes_use_the_critics_theme_and_discussion_text() -> None:
    assembled = processing.assemble_findings(_baseline_state())
    critic_themes = _fixture("critic_merger")["cross_lens_themes"]
    assert len(assembled["themes"]) == len(critic_themes)
    for text, theme in zip(assembled["themes"], critic_themes, strict=True):
        assert text == f"{theme['theme']}: {theme['discussion']}"


def test_input_state_is_not_mutated() -> None:
    state = _baseline_state()
    before = json.dumps(state, sort_keys=True)
    processing.assemble_findings(state)
    assert json.dumps(state, sort_keys=True) == before


def test_duplicates_keep_the_named_finding_and_map_ids() -> None:
    critic = {
        "duplicate_groups": [
            {
                "finding_ids": ["F-MED-1", "F-LEG-1", "F-REG-1"],
                "rationale": "r",
                "keep": "F-LEG-1",
            }
        ]
    }
    state = _state(
        {
            "medical": [_finding("F-MED-1")],
            "legal": [_finding("F-LEG-1", lens="legal")],
            "regulatory": [_finding("F-REG-1", lens="regulatory")],
        },
        critic,
    )
    assembled = processing.assemble_findings(state)
    assert [f["observation"] for f in assembled["findings"]] == ["obs F-LEG-1"]
    assert assembled["findings"][0]["finding_id"] == "F-LEG-1"
    assert assembled["dropped"] == ["F-MED-1", "F-REG-1"]
    assert set(assembled["id_map"].values()) == {"F-LEG-1"}


def test_a_finding_named_as_keep_is_never_dropped_by_another_group() -> None:
    critic = {
        "duplicate_groups": [
            {
                "finding_ids": ["F-MED-1", "F-MED-2"],
                "rationale": "r",
                "keep": "F-MED-1",
            },
            {
                "finding_ids": ["F-MED-2", "F-MED-3"],
                "rationale": "r",
                "keep": "F-MED-3",
            },
        ]
    }
    state = _state({"medical": [_finding(f"F-MED-{n}") for n in (1, 2, 3, 4)]}, critic)
    assembled = processing.assemble_findings(state)
    observations = sorted(f["observation"] for f in assembled["findings"])
    assert observations == ["obs F-MED-1", "obs F-MED-3", "obs F-MED-4"]
    assert assembled["dropped"] == ["F-MED-2"]


def test_conflicting_keeps_for_the_same_pair_keep_both() -> None:
    critic = {
        "duplicate_groups": [
            {
                "finding_ids": ["F-MED-1", "F-MED-2"],
                "rationale": "r",
                "keep": "F-MED-1",
            },
            {
                "finding_ids": ["F-MED-1", "F-MED-2"],
                "rationale": "r",
                "keep": "F-MED-2",
            },
        ]
    }
    state = _state({"medical": [_finding("F-MED-1"), _finding("F-MED-2")]}, critic)
    assert len(processing.assemble_findings(state)["findings"]) == 2


def test_groups_without_a_valid_keep_fall_back_to_the_first_member_without_cycles() -> (
    None
):
    critic = {
        "duplicate_groups": [
            {
                "finding_ids": ["F-MED-1", "F-MED-2"],
                "rationale": "r",
                "keep": "F-NOPE-1",
            },
            {
                "finding_ids": ["F-MED-2", "F-MED-1"],
                "rationale": "r",
                "keep": "F-NOPE-2",
            },
        ]
    }
    state = _state({"medical": [_finding("F-MED-1"), _finding("F-MED-2")]}, critic)
    assembled = processing.assemble_findings(state)
    assert len(assembled["findings"]) == 1
    assert assembled["findings"][0]["observation"] == "obs F-MED-1"


def test_unknown_ids_are_reported_not_fatal() -> None:
    critic = {
        "duplicate_groups": [
            {
                "finding_ids": ["F-MED-1", "F-XXX-9"],
                "rationale": "r",
                "keep": "F-MED-1",
            },
            {
                "finding_ids": ["F-QQQ-1", "F-QQQ-2"],
                "rationale": "r",
                "keep": "F-QQQ-1",
            },
        ],
        "severity_adjustments": [
            {
                "finding_id": "F-ZZZ-1",
                "current_severity": "low",
                "recommended_severity": "high",
                "rationale": "r",
            }
        ],
    }
    state = _state({"medical": [_finding("F-MED-1")]}, critic)
    assembled = processing.assemble_findings(state)
    assert len(assembled["findings"]) == 1
    assert set(assembled["unresolved_ids"]) == {
        "F-XXX-9",
        "F-QQQ-1",
        "F-QQQ-2",
        "F-ZZZ-1",
    }


def test_severity_adjustments_apply_and_invalid_values_are_ignored() -> None:
    adjustments = [
        {
            "finding_id": "F-MED-1",
            "current_severity": "low",
            "recommended_severity": "critical",
            "rationale": "r",
        },
        {
            "finding_id": "F-MED-2",
            "current_severity": "low",
            "recommended_severity": "banana",
            "rationale": "r",
        },
    ]
    state = _state(
        {
            "medical": [
                _finding("F-MED-1", severity="low"),
                _finding("F-MED-2", severity="low"),
            ]
        },
        {"severity_adjustments": adjustments},
    )
    findings = processing.assemble_findings(state)["findings"]
    assert [f["severity"] for f in findings] == ["critical", "low"]
    assert findings[0]["observation"] == "obs F-MED-1"


def test_a_dropped_duplicates_adjusted_severity_raises_the_kept_finding() -> None:
    critic = {
        "duplicate_groups": [
            {"finding_ids": ["F-MED-1", "F-MED-2"], "rationale": "r", "keep": "F-MED-1"}
        ],
        "severity_adjustments": [
            {
                "finding_id": "F-MED-2",
                "current_severity": "low",
                "recommended_severity": "high",
                "rationale": "r",
            }
        ],
    }
    state = _state(
        {
            "medical": [
                _finding("F-MED-1", severity="low"),
                _finding("F-MED-2", severity="low"),
            ]
        },
        critic,
    )
    assert processing.assemble_findings(state)["findings"][0]["severity"] == "high"


def test_gap_findings_join_their_lens_sequence() -> None:
    gap = _finding("F-GAP-1", lens="legal", severity="high")
    state = _state(
        {"legal": [_finding("F-LEG-1", lens="legal", severity="low")]},
        {"additional_findings": [gap]},
    )
    findings = processing.assemble_findings(state)["findings"]
    assert [(f["severity"], f["finding_id"]) for f in findings] == [
        ("high", "F-LEG-1"),
        ("low", "F-LEG-2"),
    ]


def test_empty_state_yields_an_empty_but_valid_assembly() -> None:
    assembled = processing.assemble_findings({"intake_findings": {"items": []}})
    assert assembled["findings"] == []
    assert sum(assembled["counts_by_severity"].values()) == 0


def test_compose_final_report_validates_and_carries_intake_fields() -> None:
    state = _baseline_state()
    state["report_narrative"] = {
        "executive_summary": "Summary text.",
        "open_questions_for_reviewers": ["q1"],
        "recommended_discussion_topics": ["t1", "t2"],
    }
    assembled = processing.assemble_findings(state)
    report = processing.compose_final_report(state, assembled)
    FinalReport.model_validate(report)
    intake = _fixture("intake")
    assert report["content_summary"] == intake["content_summary"]
    assert report["intended_audience"] == intake["intended_audience"]
    assert report["promotional_intent"] == intake["promotional_intent"]
    assert report["executive_summary"] == "Summary text."
    assert report["open_questions_for_reviewers"] == ["q1"]
    assert report["counts_by_severity"] == assembled["counts_by_severity"]
    assert len(report["findings"]) == 34


def test_compose_falls_back_to_a_generated_summary_without_narrative() -> None:
    state = _baseline_state()
    assembled = processing.assemble_findings(state)
    report = processing.compose_final_report(state, assembled)
    assert "34 findings" in report["executive_summary"]
    assert "could not be generated" in report["executive_summary"]
    assert report["open_questions_for_reviewers"] == []


def test_narrative_inputs_are_json_strings_with_final_ids() -> None:
    state = _baseline_state()
    assembled = processing.assemble_findings(state)
    inputs = processing.narrative_inputs_state(state, assembled)
    assert set(inputs) == {
        "intake_summary",
        "final_findings_digest",
        "report_themes",
        "final_counts",
        "final_defense_by_item",
    }
    digest = json.loads(inputs["final_findings_digest"])
    assert [d["id"] for d in digest] == [f["finding_id"] for f in assembled["findings"]]
    json.loads(inputs["final_defense_by_item"])


def test_narrative_prompt_placeholders_are_all_provided() -> None:
    placeholders = set(re.findall(r"\{([a-z_]+\??)\}", prompts.NARRATIVE_SYNTHESIZER))
    provided = set(
        processing.narrative_inputs_state(
            _baseline_state(), processing.assemble_findings(_baseline_state())
        )
    )
    assert placeholders == provided


def test_narrative_schema_is_prose_only() -> None:
    assert set(ReportNarrative.model_fields) == {
        "executive_summary",
        "open_questions_for_reviewers",
        "recommended_discussion_topics",
    }


@pytest.mark.asyncio
async def test_final_nodes_emit_the_report_as_content_and_state() -> None:
    def seed(ctx: Context, node_input):
        for key, value in _baseline_state().items():
            ctx.state[key] = value
        return node_input

    def fake_narrative(ctx: Context, node_input):
        ctx.state["report_narrative"] = {
            "executive_summary": "From the narrative agent.",
            "open_questions_for_reviewers": ["q"],
            "recommended_discussion_topics": ["t"],
        }
        return node_input

    workflow = Workflow(
        name="final_nodes",
        edges=[
            (START, seed),
            (seed, assemble_report),
            (assemble_report, fake_narrative),
            (fake_narrative, compose_report),
        ],
    )
    runner = InMemoryRunner(app=App(name="final_nodes_app", root_agent=workflow))
    session = await runner.session_service.create_session(
        app_name="final_nodes_app", user_id="u"
    )
    message = types.Content(role="user", parts=[types.Part.from_text(text="go")])
    texts = []
    async for event in runner.run_async(
        user_id="u", session_id=session.id, new_message=message
    ):
        if event.content and event.content.parts:
            texts.extend(p.text for p in event.content.parts if p.text)
    report = FinalReport.model_validate_json(texts[-1])
    assert len(report.findings) == 34
    assert report.executive_summary == "From the narrative agent."
    assert sum(report.counts_by_severity.values()) == 34
    stored = await runner.session_service.get_session(
        app_name="final_nodes_app", user_id="u", session_id=session.id
    )
    assert (
        stored.state["final_report"]["executive_summary"] == "From the narrative agent."
    )


def test_merged_duplicates_record_which_other_lenses_raised_the_issue() -> None:
    critic = {
        "duplicate_groups": [
            {
                "finding_ids": ["F-MED-1", "F-LEG-1", "F-REG-1"],
                "rationale": "r",
                "keep": "F-LEG-1",
            }
        ]
    }
    state = _state(
        {
            "medical": [_finding("F-MED-1")],
            "legal": [_finding("F-LEG-1", lens="legal")],
            "regulatory": [_finding("F-REG-1", lens="regulatory")],
        },
        critic,
    )
    assembled = processing.assemble_findings(state)
    assert [f["review_lens"] for f in assembled["findings"]] == ["legal"]
    assert assembled["findings"][0]["also_raised_by"] == ["medical", "regulatory"]
    assert assembled["counts_by_lens"]["legal"] == 1
    assert assembled["counts_by_lens"]["medical"] == 0
    assert assembled["counts_raised_by_lens"]["medical"] == 1
    assert assembled["counts_raised_by_lens"]["regulatory"] == 1


def test_findings_without_duplicates_or_with_same_lens_duplicates_have_no_attribution() -> (
    None
):
    critic = {
        "duplicate_groups": [
            {"finding_ids": ["F-MED-1", "F-MED-2"], "rationale": "r", "keep": "F-MED-1"}
        ]
    }
    state = _state(
        {"medical": [_finding("F-MED-1"), _finding("F-MED-2"), _finding("F-MED-3")]},
        critic,
    )
    findings = processing.assemble_findings(state)["findings"]
    assert len(findings) == 2
    assert all(f["also_raised_by"] == [] for f in findings)


def test_raised_counts_cover_every_finding_before_merging() -> None:
    assembled = processing.assemble_findings(_baseline_state())
    raised = assembled["counts_raised_by_lens"]
    assert sum(raised.values()) == 43
    assert sum(raised.values()) - sum(assembled["counts_by_lens"].values()) == len(
        assembled["dropped"]
    )
    for lens in processing.LENS_ORDER:
        assert raised[lens] >= assembled["counts_by_lens"][lens]
    attributed = [f for f in assembled["findings"] if f["also_raised_by"]]
    assert attributed
    for finding in attributed:
        assert finding["review_lens"] not in finding["also_raised_by"]


def test_final_report_and_digest_carry_the_attribution() -> None:
    state = _baseline_state()
    assembled = processing.assemble_findings(state)
    report = processing.compose_final_report(state, assembled)
    FinalReport.model_validate(report)
    assert report["counts_raised_by_lens"] == assembled["counts_raised_by_lens"]
    assert any(f["also_raised_by"] for f in report["findings"])
    digest = processing.finding_digest(assembled["findings"][0])
    assert "also_raised_by" in digest
