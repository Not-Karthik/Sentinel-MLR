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
import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "compare_runs", ROOT / "tests" / "eval" / "compare_runs.py"
)
compare_runs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compare_runs)

BASELINE = json.loads(
    (ROOT / "tests" / "unit" / "fixtures" / "vitalynx" / "final_report.json").read_text(
        encoding="utf-8"
    )
)


def _complete(report: dict) -> dict:
    report = copy.deepcopy(report)
    lens, severity = {}, {}
    for finding in report["findings"]:
        lens[finding["review_lens"]] = lens.get(finding["review_lens"], 0) + 1
        severity[finding["severity"]] = severity.get(finding["severity"], 0) + 1
    report["counts_by_lens"] = lens
    report["counts_by_severity"] = severity
    return report


def test_identical_report_passes_once_counts_are_populated() -> None:
    result = compare_runs.compare(BASELINE, _complete(BASELINE))
    assert result["passed"], result["checks"]


def test_baseline_itself_fails_on_the_known_empty_counts() -> None:
    result = compare_runs.compare(BASELINE, BASELINE)
    failed = {c["name"] for c in result["checks"] if not c["passed"]}
    assert failed == {"counts_consistent"}


def _synthetic(severities: list[str]) -> dict:
    findings = []
    for index, severity in enumerate(severities):
        x = 0.05 + index * 0.1
        findings.append(
            {
                "finding_id": f"F-{index}",
                "review_lens": "medical",
                "category": "clarity",
                "severity": severity,
                "location": {"bbox": [x, 0.1, x + 0.08, 0.5]},
            }
        )
    return _complete(
        {
            "executive_summary": "s",
            "content_summary": "c",
            "themes": ["t"],
            "open_questions_for_reviewers": ["q"],
            "recommended_discussion_topics": ["d"],
            "findings": findings,
        }
    )


def _check_named(result: dict, name: str) -> dict:
    return next(c for c in result["checks"] if c["name"] == name)


def test_dropping_a_high_finding_fails_recall() -> None:
    baseline = _synthetic(["high", "high", "medium", "low"])
    candidate = _synthetic(["high", "high", "medium", "low"])
    candidate["findings"].pop(1)
    result = compare_runs.compare(baseline, _complete(candidate))
    check = _check_named(result, "high_critical_recall")
    assert not check["passed"]
    assert "F-1" in check["detail"]


def test_downgrading_a_high_finding_below_medium_fails_recall() -> None:
    baseline = _synthetic(["high", "high", "medium", "low"])
    candidate = _synthetic(["high", "low", "medium", "low"])
    result = compare_runs.compare(baseline, candidate)
    assert not _check_named(result, "high_critical_recall")["passed"]


def test_downgrading_a_high_finding_to_medium_still_counts_as_covered() -> None:
    baseline = _synthetic(["high", "high", "medium", "low"])
    candidate = _synthetic(["high", "medium", "medium", "low"])
    assert _check_named(
        compare_runs.compare(baseline, candidate), "high_critical_recall"
    )["passed"]


def test_one_candidate_cannot_cover_two_baseline_findings() -> None:
    baseline = _synthetic(["high", "high"])
    for finding in baseline["findings"]:
        finding["location"] = {"bbox": [0.1, 0.1, 0.5, 0.5]}
    candidate = _synthetic(["high"])
    candidate["findings"][0]["location"] = {"bbox": [0.1, 0.1, 0.5, 0.5]}
    result = compare_runs.compare(baseline, _complete(candidate))
    assert not _check_named(result, "high_critical_recall")["passed"]


def test_missing_bbox_fails_coverage_unless_not_an_image_run() -> None:
    candidate = _complete(BASELINE)
    candidate["findings"][0]["location"] = None
    assert not compare_runs.compare(BASELINE, _complete(candidate))["passed"]
    relaxed = compare_runs.compare(BASELINE, _complete(candidate), image=False)
    assert all(c["name"] != "bbox_coverage" for c in relaxed["checks"])


def test_empty_narrative_fails() -> None:
    candidate = _complete(BASELINE)
    candidate["executive_summary"] = ""
    candidate["themes"] = []
    result = compare_runs.compare(BASELINE, candidate)
    narrative = next(c for c in result["checks"] if c["name"] == "narrative_complete")
    assert not narrative["passed"]
    assert "executive_summary" in narrative["detail"]


def test_large_severity_shift_fails_distribution() -> None:
    candidate = _complete(BASELINE)
    for finding in candidate["findings"]:
        if finding["severity"] == "medium":
            finding["severity"] = "low"
    result = compare_runs.compare(BASELINE, _complete(candidate))
    assert not next(
        c for c in result["checks"] if c["name"] == "severity_distribution"
    )["passed"]


def test_bbox_iou_and_finding_covered() -> None:
    assert compare_runs.bbox_iou([0, 0, 1, 1], [0, 0, 1, 1]) == pytest.approx(1.0)
    assert compare_runs.bbox_iou([0, 0, 0.1, 0.1], [0.5, 0.5, 0.6, 0.6]) == 0.0
    base = {"category": "clarity", "location": {"bbox": [0.1, 0.1, 0.5, 0.5]}}
    near = {
        "category": "clarity",
        "severity": "medium",
        "location": {"bbox": [0.1, 0.1, 0.5, 0.5]},
    }
    other_category_exact = {
        "category": "tone",
        "severity": "high",
        "location": {"bbox": [0.1, 0.1, 0.5, 0.5]},
    }
    low = {
        "category": "clarity",
        "severity": "low",
        "location": {"bbox": [0.1, 0.1, 0.5, 0.5]},
    }
    assert compare_runs.finding_covered(base, [near])
    assert compare_runs.finding_covered(base, [other_category_exact])
    assert not compare_runs.finding_covered(base, [low])


def test_report_from_stream_picks_the_last_report_artifact(
    tmp_path: pathlib.Path,
) -> None:
    def event(text: str, key: str = "result") -> str:
        payload = {
            "result": {
                "kind": "artifact-update",
                "artifact": {"parts": [{"kind": "text", "text": text}]},
            }
        }
        return "data: " + json.dumps(payload)

    first = json.dumps({"executive_summary": "a", "findings": []})
    second = json.dumps({"executive_summary": "b", "findings": [{"severity": "low"}]})
    lines = [
        event("plain text"),
        event(first),
        event(json.dumps({"other": 1})),
        event(second),
        "noise",
    ]
    assert compare_runs.report_from_stream(lines)["executive_summary"] == "b"
    path = tmp_path / "run.jsonl"
    path.write_text("\n".join(lines), encoding="utf-8")
    assert compare_runs.load_report(path)["executive_summary"] == "b"
    with pytest.raises(ValueError):
        compare_runs.report_from_stream([event("nothing")])


def test_report_from_stream_supports_protocol_1_shape() -> None:
    body = json.dumps({"executive_summary": "x", "findings": []})
    payload = {"result": {"artifactUpdate": {"artifact": {"parts": [{"text": body}]}}}}
    assert (
        compare_runs.report_from_stream(["data: " + json.dumps(payload)])[
            "executive_summary"
        ]
        == "x"
    )


def test_cli_returns_nonzero_on_failure(tmp_path: pathlib.Path, capsys) -> None:
    baseline = tmp_path / "base.json"
    baseline.write_text(json.dumps(BASELINE), encoding="utf-8")
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_complete(BASELINE)), encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(BASELINE), encoding="utf-8")
    assert compare_runs.main([str(good), "--baseline", str(baseline)]) == 0
    assert compare_runs.main([str(bad), "--baseline", str(baseline)]) == 1
    assert "RESULT: FAIL" in capsys.readouterr().out


def test_same_category_with_enclosing_or_missing_box_counts_as_covered() -> None:
    base = {"category": "fair_balance", "location": {"bbox": [0.16, 0.76, 0.87, 0.83]}}
    enclosing = {
        "category": "fair_balance",
        "severity": "high",
        "location": {"bbox": [0.13, 0.2, 0.88, 0.83]},
    }
    no_box = {"category": "fair_balance", "severity": "high", "location": None}
    different_category = {
        "category": "tone",
        "severity": "high",
        "location": {"bbox": [0.13, 0.2, 0.88, 0.83]},
    }
    elsewhere = {
        "category": "fair_balance",
        "severity": "high",
        "location": {"bbox": [0.0, 0.0, 0.1, 0.1]},
    }
    assert compare_runs.finding_covered(base, [enclosing])
    assert compare_runs.finding_covered(base, [no_box])
    assert not compare_runs.finding_covered(base, [different_category])
    assert not compare_runs.finding_covered(base, [elsewhere])


def test_bbox_containment() -> None:
    assert compare_runs.bbox_containment(
        [0.2, 0.2, 0.4, 0.4], [0, 0, 1, 1]
    ) == pytest.approx(1.0)
    assert compare_runs.bbox_containment([0, 0, 0.1, 0.1], [0.5, 0.5, 1, 1]) == 0.0
    assert compare_runs.bbox_containment(None, [0, 0, 1, 1]) == 0.0
