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

import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "judge_runs", ROOT / "tests" / "eval" / "judge_runs.py"
)
judge_runs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(judge_runs)

BASELINE = json.loads(
    (ROOT / "tests" / "unit" / "fixtures" / "vitalynx" / "final_report.json").read_text(
        encoding="utf-8"
    )
)


def _report(*specs) -> dict:
    return {
        "findings": [
            {
                "finding_id": fid,
                "review_lens": "medical",
                "severity": severity,
                "category": "clarity",
                "observation": f"observation {fid}",
                "location": {"section": "Header"},
            }
            for fid, severity in specs
        ]
    }


def test_digest_keeps_only_what_the_judge_needs() -> None:
    digest = judge_runs.build_digest(BASELINE)
    assert len(digest) == 34
    assert set(digest[0]) == {
        "id",
        "lens",
        "severity",
        "category",
        "region",
        "observation",
    }


def test_prompt_contains_both_reports() -> None:
    prompt = judge_runs.build_prompt(_report(("R1", "high")), _report(("C1", "low")))
    assert "REFERENCE findings" in prompt and "CANDIDATE findings" in prompt
    assert "observation R1" in prompt and "observation C1" in prompt


def test_summary_computes_recall_severity_and_candidate_only_counts() -> None:
    reference = _report(("R1", "high"), ("R2", "medium"), ("R3", "high"), ("R4", "low"))
    candidate = _report(("C1", "high"), ("C2", "low"), ("C3", "low"), ("C4", "medium"))
    judgement = judge_runs.Judgement(
        matches=[
            judge_runs.Match(
                reference_id="R1", candidate_id="C1", relation="same_issue", note=""
            ),
            judge_runs.Match(
                reference_id="R2", candidate_id="C2", relation="partial", note=""
            ),
            judge_runs.Match(
                reference_id="R3", candidate_id=None, relation="missing", note=""
            ),
            judge_runs.Match(
                reference_id="R4", candidate_id="C3", relation="same_issue", note=""
            ),
        ],
        candidate_only=[
            judge_runs.CandidateOnly(
                candidate_id="C4", assessment="valid_additional", note=""
            )
        ],
        overall="",
    )
    summary = judge_runs.summarize(reference, candidate, judgement)
    assert summary["recall_same_or_partial"] == 0.75
    assert summary["recall_same_issue"] == 0.5
    assert summary["high_critical_covered"] == "1/2"
    assert summary["missing_reference_ids"] == ["R3"]
    assert summary["severity_exact_agreement"] == 2 / 3
    assert summary["severity_within_one_band"] == 1.0
    assert summary["candidate_only"] == {
        "valid_additional": 1,
        "restates_reference": 0,
        "questionable": 0,
    }
    assert "75%" in judge_runs.format_summary(summary)


def test_summary_ignores_ids_the_judge_invented_and_unlisted_references() -> None:
    reference = _report(("R1", "high"), ("R2", "low"))
    candidate = _report(("C1", "high"))
    judgement = judge_runs.Judgement(
        matches=[
            judge_runs.Match(
                reference_id="R1", candidate_id="C1", relation="same_issue", note=""
            ),
            judge_runs.Match(
                reference_id="GHOST", candidate_id="C1", relation="same_issue", note=""
            ),
        ],
        candidate_only=[
            judge_runs.CandidateOnly(
                candidate_id="NOPE", assessment="questionable", note=""
            )
        ],
        overall="",
    )
    summary = judge_runs.summarize(reference, candidate, judgement)
    assert summary["recall_same_or_partial"] == 0.5
    assert summary["missing_reference_ids"] == ["R2"]
    assert summary["candidate_only"]["questionable"] == 0


def test_summary_of_an_empty_reference_is_full_recall() -> None:
    summary = judge_runs.summarize(
        {"findings": []},
        _report(("C1", "low")),
        judge_runs.Judgement(matches=[], candidate_only=[], overall=""),
    )
    assert summary["recall_same_or_partial"] == 1.0
    assert summary["severity_exact_agreement"] is None
