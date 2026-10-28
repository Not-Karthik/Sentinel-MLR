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

"""Compare two Sentinel reports by meaning with a model judge.

Usage:
    python tests/eval/judge_runs.py REFERENCE CANDIDATE [--model MODEL] [--output FILE]

REFERENCE and CANDIDATE can be anything compare_runs.load_report accepts. The
judge sees each finding's observation, category, severity and region (not the
image), maps every reference finding to the candidate finding that raises the
same issue, and assesses candidate findings that matched nothing.
"""

import argparse
import json
import os
import pathlib
import sys
from typing import Any, Literal

from pydantic import BaseModel

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import compare_runs

TOP = {"critical", "high"}


class Match(BaseModel):
    reference_id: str
    candidate_id: str | None
    relation: Literal["same_issue", "partial", "missing"]
    note: str


class CandidateOnly(BaseModel):
    candidate_id: str
    assessment: Literal["valid_additional", "restates_reference", "questionable"]
    note: str


class Judgement(BaseModel):
    matches: list[Match]
    candidate_only: list[CandidateOnly]
    overall: str


INSTRUCTION = """You compare two automated MLR (medical, legal, regulatory) reviews of the \
same promotional piece. REFERENCE is the earlier review; CANDIDATE is the new one.

For EVERY reference finding, decide whether the candidate review raises the same issue:
- same_issue: some candidate finding makes essentially the same point (wording, severity and \
lens may differ, and one candidate finding may cover several reference findings).
- partial: the candidate touches the topic but misses an important part of the concern.
- missing: the candidate review does not raise it.
Set candidate_id to the best matching candidate finding (null when missing).

Then, for every candidate finding that is NOT the best match of any reference finding, set \
candidate_only with assessment:
- valid_additional: a substantive, correct issue the reference did not raise.
- restates_reference: it repeats an issue the reference already raised.
- questionable: vague, wrong, or not supported by its own observation.

Judge only from the text given. Return JSON for the provided schema, with one match entry per \
reference finding and one candidate_only entry per unmatched candidate finding."""


def build_digest(report: dict[str, Any]) -> list[dict[str, Any]]:
    digest = []
    for finding in report.get("findings", []):
        location = finding.get("location") or {}
        digest.append(
            {
                "id": finding.get("finding_id"),
                "lens": finding.get("review_lens"),
                "severity": finding.get("severity"),
                "category": finding.get("category"),
                "region": location.get("section"),
                "observation": finding.get("observation"),
            }
        )
    return digest


def build_prompt(reference: dict[str, Any], candidate: dict[str, Any]) -> str:
    return (
        "REFERENCE findings:\n"
        + json.dumps(build_digest(reference), ensure_ascii=False)
        + "\n\nCANDIDATE findings:\n"
        + json.dumps(build_digest(candidate), ensure_ascii=False)
    )


def judge(
    reference: dict[str, Any], candidate: dict[str, Any], model: str | None = None
) -> Judgement:
    from google import genai
    from google.genai import types

    client = genai.Client()
    config = types.GenerateContentConfig(
        system_instruction=INSTRUCTION,
        response_mime_type="application/json",
        response_schema=Judgement,
        max_output_tokens=65535,
        thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.MEDIUM),
    )
    response = client.models.generate_content(
        model=model or os.getenv("SENTINEL_MODEL", "gemini-3.8-flash"),
        contents=build_prompt(reference, candidate),
        config=config,
    )
    return Judgement.model_validate_json(response.text)


def summarize(
    reference: dict[str, Any], candidate: dict[str, Any], judgement: Judgement
) -> dict[str, Any]:
    ref_by_id = {f.get("finding_id"): f for f in reference.get("findings", [])}
    cand_by_id = {f.get("finding_id"): f for f in candidate.get("findings", [])}
    matches = {
        m.reference_id: m for m in judgement.matches if m.reference_id in ref_by_id
    }
    covered = [m for m in matches.values() if m.relation in ("same_issue", "partial")]
    strict = [m for m in matches.values() if m.relation == "same_issue"]
    top_ids = [i for i, f in ref_by_id.items() if f.get("severity") in TOP]
    top_covered = [
        i for i in top_ids if i in matches and matches[i].relation != "missing"
    ]
    rank = {s: n for n, s in enumerate(compare_runs.SEVERITIES)}
    agree = within_one = compared = 0
    for match in covered:
        other = cand_by_id.get(match.candidate_id)
        if not other:
            continue
        a, b = ref_by_id[match.reference_id].get("severity"), other.get("severity")
        if a in rank and b in rank:
            compared += 1
            agree += a == b
            within_one += abs(rank[a] - rank[b]) <= 1
    only = [c for c in judgement.candidate_only if c.candidate_id in cand_by_id]
    count = len(ref_by_id)
    return {
        "reference_findings": count,
        "candidate_findings": len(cand_by_id),
        "recall_same_or_partial": len(covered) / count if count else 1.0,
        "recall_same_issue": len(strict) / count if count else 1.0,
        "high_critical_covered": f"{len(top_covered)}/{len(top_ids)}",
        "missing_reference_ids": sorted(
            i for i in ref_by_id if i not in matches or matches[i].relation == "missing"
        ),
        "severity_exact_agreement": agree / compared if compared else None,
        "severity_within_one_band": within_one / compared if compared else None,
        "candidate_only": {
            kind: sum(1 for c in only if c.assessment == kind)
            for kind in ("valid_additional", "restates_reference", "questionable")
        },
    }


def format_summary(summary: dict[str, Any]) -> str:
    def pct(value: Any) -> str:
        return "n/a" if value is None else f"{value:.0%}"

    return "\n".join(
        [
            f"reference findings: {summary['reference_findings']} | "
            f"candidate findings: {summary['candidate_findings']}",
            f"reference findings covered (same or partial): {pct(summary['recall_same_or_partial'])}"
            f" | fully the same issue: {pct(summary['recall_same_issue'])}",
            f"high/critical reference findings covered: {summary['high_critical_covered']}",
            f"severity agreement on covered findings: exact {pct(summary['severity_exact_agreement'])}"
            f", within one band {pct(summary['severity_within_one_band'])}",
            f"candidate-only findings: {summary['candidate_only']}",
            f"reference findings the candidate missed: {summary['missing_reference_ids']}",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("reference", type=pathlib.Path)
    parser.add_argument("candidate", type=pathlib.Path)
    parser.add_argument("--model")
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args(argv)
    from dotenv import load_dotenv

    load_dotenv()
    reference = compare_runs.load_report(args.reference)
    candidate = compare_runs.load_report(args.candidate)
    judgement = judge(reference, candidate, args.model)
    summary = summarize(reference, candidate, judgement)
    print(format_summary(summary))
    if args.output:
        args.output.write_text(
            json.dumps(
                {"summary": summary, "judgement": judgement.model_dump()},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
