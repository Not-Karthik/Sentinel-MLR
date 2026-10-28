# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Compare a fresh Sentinel run with a saved baseline.

Usage:
    python tests/eval/compare_runs.py CANDIDATE [--baseline PATH]

CANDIDATE and the baseline can be a run directory containing final_report.json,
a final_report.json file, or an A2A stream saved as JSON lines (one "data:" line
per event). Exit code 0 when every check passes, 1 otherwise.
"""

import argparse
import json
import pathlib
import sys
from typing import Any

SEVERITIES = ["critical", "high", "medium", "low", "informational"]
TOP_SEVERITIES = {"critical", "high"}
COVERING_SEVERITIES = {"critical", "high", "medium"}
DEFAULT_BASELINE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "run_outputs"
    / "vitalynx-test-image-2"
    / "final_report.json"
)


def _normalize_bbox(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    return [x0, y0, x1, y1]


def bbox_iou(first: Any, second: Any) -> float:
    a, b = _normalize_bbox(first), _normalize_bbox(second)
    if not a or not b:
        return 0.0
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return 0.0
    inter = width * height
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def bbox_containment(first: Any, second: Any) -> float:
    a, b = _normalize_bbox(first), _normalize_bbox(second)
    if not a or not b:
        return 0.0
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return 0.0
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return (width * height) / smaller if smaller > 0 else 0.0


def report_from_stream(lines: list[str]) -> dict[str, Any]:
    report: dict[str, Any] | None = None
    for line in lines:
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        result = event.get("result", {})
        update = result.get("artifactUpdate") or result
        artifact = update.get("artifact")
        if not artifact:
            continue
        text = "".join(
            part.get("text", "")
            for part in artifact.get("parts", [])
            if part.get("text")
        )
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(data, dict)
            and "executive_summary" in data
            and "findings" in data
        ):
            report = data
    if report is None:
        raise ValueError("No final report found in the stream")
    return report


def load_report(path: pathlib.Path) -> dict[str, Any]:
    path = pathlib.Path(path)
    if path.is_dir():
        path = path / "final_report.json"
    if path.suffix == ".jsonl":
        return report_from_stream(path.read_text(encoding="utf-8").splitlines())
    return json.loads(path.read_text(encoding="utf-8"))


def severity_counts(report: dict[str, Any]) -> dict[str, int]:
    counts = dict.fromkeys(SEVERITIES, 0)
    for finding in report.get("findings", []):
        severity = finding.get("severity")
        if severity in counts:
            counts[severity] += 1
    return counts


def _bbox(finding: dict[str, Any]) -> Any:
    return (finding.get("location") or {}).get("bbox")


def finding_covered(baseline: dict[str, Any], candidates: list[dict[str, Any]]) -> bool:
    """A candidate covers a baseline finding when it is the same issue.

    Same issue: a finding of at least medium severity with the same category and
    a matching place (box overlap of 0.4, one box mostly inside the other, or a
    missing box), or a box overlap of 0.6 whatever the category.
    """
    base_box = _bbox(baseline)
    for candidate in candidates:
        if candidate.get("severity") not in COVERING_SEVERITIES:
            continue
        box = _bbox(candidate)
        overlap = bbox_iou(base_box, box)
        same_category = candidate.get("category") == baseline.get("category")
        same_place = (
            overlap >= 0.4
            or bbox_containment(base_box, box) >= 0.8
            or not _normalize_bbox(base_box)
            or not _normalize_bbox(box)
        )
        if (same_category and same_place) or overlap >= 0.6:
            return True
    return False


def match_findings(
    baseline_findings: list[dict[str, Any]], candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    used: set[int] = set()
    matched = []
    for base in baseline_findings:
        choice, best = None, -1.0
        for index, candidate in enumerate(candidates):
            if index in used or not finding_covered(base, [candidate]):
                continue
            overlap = bbox_iou(_bbox(base), _bbox(candidate))
            if overlap > best:
                choice, best = index, overlap
        if choice is not None:
            used.add(choice)
            matched.append(base)
    return matched


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": passed, "detail": detail}


def compare(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    image: bool = True,
    top_tolerance: int = 1,
    other_tolerance: int = 3,
    min_recall: float = 0.9,
) -> dict[str, Any]:
    base_findings = baseline.get("findings", [])
    new_findings = candidate.get("findings", [])
    checks = []

    top = [f for f in base_findings if f.get("severity") in TOP_SEVERITIES]
    covered = match_findings(top, new_findings)
    recall = len(covered) / len(top) if top else 1.0
    missing = [f.get("finding_id") for f in top if f not in covered]
    lenient = sum(1 for f in top if finding_covered(f, new_findings))
    checks.append(
        _check(
            "high_critical_recall",
            recall >= min_recall,
            f"{len(covered)}/{len(top)} baseline high or critical findings covered "
            f"one-to-one ({lenient}/{len(top)} if one finding may cover several)"
            + (f"; unmatched {missing}" if missing else ""),
        )
    )

    base_counts, new_counts = severity_counts(baseline), severity_counts(candidate)
    deviations = []
    for severity in SEVERITIES:
        tolerance = top_tolerance if severity in TOP_SEVERITIES else other_tolerance
        if abs(base_counts[severity] - new_counts[severity]) > tolerance:
            deviations.append(
                f"{severity} {base_counts[severity]} -> {new_counts[severity]}"
            )
    checks.append(
        _check(
            "severity_distribution",
            not deviations,
            f"baseline {base_counts} candidate {new_counts}"
            + (f"; outside tolerance: {deviations}" if deviations else ""),
        )
    )

    if image:
        without_box = [f.get("finding_id") for f in new_findings if not _bbox(f)]
        checks.append(
            _check(
                "bbox_coverage",
                not without_box and bool(new_findings),
                f"{len(new_findings) - len(without_box)}/{len(new_findings)} findings have a bbox"
                + (f"; missing {without_box}" if without_box else ""),
            )
        )

    empty = [
        field
        for field in (
            "executive_summary",
            "content_summary",
            "themes",
            "open_questions_for_reviewers",
            "recommended_discussion_topics",
        )
        if not candidate.get(field)
    ]
    checks.append(
        _check(
            "narrative_complete",
            not empty,
            "all narrative fields present" if not empty else f"empty: {empty}",
        )
    )

    lens_counts = candidate.get("counts_by_lens") or {}
    severity_report = candidate.get("counts_by_severity") or {}
    consistent = (
        sum(lens_counts.values()) == len(new_findings)
        and sum(severity_report.values()) == len(new_findings)
        and bool(new_findings)
    )
    checks.append(
        _check(
            "counts_consistent",
            consistent,
            f"counts_by_lens {lens_counts} counts_by_severity {severity_report}",
        )
    )

    return {
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
        "baseline_findings": len(base_findings),
        "candidate_findings": len(new_findings),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("candidate", type=pathlib.Path)
    parser.add_argument("--baseline", type=pathlib.Path, default=DEFAULT_BASELINE)
    parser.add_argument("--no-image", action="store_true")
    parser.add_argument("--top-tolerance", type=int, default=1)
    parser.add_argument("--other-tolerance", type=int, default=3)
    args = parser.parse_args(argv)
    result = compare(
        load_report(args.baseline),
        load_report(args.candidate),
        image=not args.no_image,
        top_tolerance=args.top_tolerance,
        other_tolerance=args.other_tolerance,
    )
    print(
        f"baseline findings: {result['baseline_findings']} | "
        f"candidate findings: {result['candidate_findings']}"
    )
    for check in result["checks"]:
        print(
            f"[{'PASS' if check['passed'] else 'FAIL'}] {check['name']}: {check['detail']}"
        )
    print("RESULT:", "PASS" if result["passed"] else "FAIL")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
