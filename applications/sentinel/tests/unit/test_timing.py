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

from types import SimpleNamespace

import pytest
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from app import timing


def _context(agent: str = "intake", invocation: str = "inv-1") -> SimpleNamespace:
    return SimpleNamespace(agent_name=agent, invocation_id=invocation)


def _request() -> LlmRequest:
    return LlmRequest(
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part.from_text(text="hello"),
                    types.Part.from_bytes(data=b"x" * 100, mime_type="image/png"),
                ],
            )
        ],
        config=types.GenerateContentConfig(system_instruction="sys"),
    )


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    timing.records.clear()
    timing._starts.clear()
    monkeypatch.delenv("SENTINEL_TIMING_LOG", raising=False)


def test_request_bytes_counts_text_inline_data_and_instruction() -> None:
    assert timing.request_bytes(_request()) == len("hello") + 100 + len("sys")


def test_disabled_by_default_records_nothing() -> None:
    context = _context()
    assert timing.timing_before_model(context, _request()) is None
    assert timing.timing_after_model(context, LlmResponse()) is None
    assert timing.records == []
    assert timing._starts == {}


def test_enabled_records_duration_tokens_and_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SENTINEL_TIMING_LOG", "1")
    context = _context("legal_reviewer")
    timing.timing_before_model(context, _request())
    response = LlmResponse(
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=11, candidates_token_count=22, thoughts_token_count=33
        )
    )
    assert timing.timing_after_model(context, response) is None
    assert len(timing.records) == 1
    record = timing.records[0]
    assert record["agent"] == "legal_reviewer"
    assert record["prompt_tokens"] == 11
    assert record["output_tokens"] == 22
    assert record["thought_tokens"] == 33
    assert record["request_bytes"] == len("hello") + 100 + len("sys")
    assert record["seconds"] >= 0
    assert timing._starts == {}


def test_parallel_agents_are_tracked_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SENTINEL_TIMING_LOG", "true")
    first, second = _context("medical_reviewer"), _context("legal_reviewer")
    timing.timing_before_model(first, _request())
    timing.timing_before_model(second, _request())
    timing.timing_after_model(second, LlmResponse())
    timing.timing_after_model(first, LlmResponse())
    assert [r["agent"] for r in timing.records] == [
        "legal_reviewer",
        "medical_reviewer",
    ]


def test_partial_responses_and_unknown_calls_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SENTINEL_TIMING_LOG", "1")
    context = _context()
    timing.timing_after_model(context, LlmResponse())
    timing.timing_before_model(context, _request())
    timing.timing_after_model(context, LlmResponse(partial=True))
    assert timing.records == []
    assert (context.invocation_id, context.agent_name) in timing._starts


def test_enabling_the_timing_log_also_surfaces_sdk_retry_messages(monkeypatch) -> None:
    import logging

    sdk_logger = logging.getLogger("google_genai")
    previous = (sdk_logger.level, list(sdk_logger.handlers), sdk_logger.propagate)
    monkeypatch.setenv("SENTINEL_TIMING_LOG", "1")
    monkeypatch.setattr(timing, "_handler_ready", False)
    try:
        timing.timing_before_model(_context(), _request())
        assert sdk_logger.level == logging.INFO
        assert sdk_logger.handlers
        assert sdk_logger.propagate is False
    finally:
        sdk_logger.setLevel(previous[0])
        sdk_logger.handlers[:] = previous[1]
        sdk_logger.propagate = previous[2]
