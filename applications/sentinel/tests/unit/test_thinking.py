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
from google.adk.agents.llm_agent import LlmAgent
from google.adk.models.llm_request import LlmRequest
from google.genai import types

import app.agent as agent_module
from app.thinking import (
    normalize_level,
    resolve_thinking_level,
    thinking_before_model,
)
from app.timing import timing_after_model, timing_before_model


def _run_config(thinking: object = None, key: str = "a2a_metadata") -> SimpleNamespace:
    metadata = {} if thinking is None else {key: {"thinking": thinking}}
    return SimpleNamespace(custom_metadata=metadata)


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SENTINEL_THINKING_LEVEL", raising=False)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("low", "low"),
        ("MEDIUM", "medium"),
        ("  High ", "high"),
        ("minimal", None),
        ("", None),
        (None, None),
        (3, None),
    ],
)
def test_normalize_level(value, expected) -> None:
    assert normalize_level(value) == expected


def test_default_is_medium() -> None:
    assert resolve_thinking_level(None) == "medium"
    assert resolve_thinking_level(SimpleNamespace(custom_metadata=None)) == "medium"
    assert resolve_thinking_level(_run_config()) == "medium"


@pytest.mark.parametrize("level", ["low", "medium", "high"])
def test_request_metadata_selects_level(level: str) -> None:
    assert resolve_thinking_level(_run_config(level)) == level


def test_request_metadata_overrides_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SENTINEL_THINKING_LEVEL", "high")
    assert resolve_thinking_level(_run_config("low")) == "low"


def test_environment_sets_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTINEL_THINKING_LEVEL", "HIGH")
    assert resolve_thinking_level(_run_config()) == "high"


@pytest.mark.parametrize("bad", ["minimal", "extreme", "", 5, ["low"]])
def test_invalid_request_value_falls_back(bad, monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_thinking_level(_run_config(bad)) == "medium"
    monkeypatch.setenv("SENTINEL_THINKING_LEVEL", "low")
    assert resolve_thinking_level(_run_config(bad)) == "low"


def test_invalid_environment_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTINEL_THINKING_LEVEL", "minimal")
    assert resolve_thinking_level(_run_config()) == "medium"


def test_non_dict_metadata_is_ignored() -> None:
    assert (
        resolve_thinking_level(SimpleNamespace(custom_metadata={"a2a_metadata": "x"}))
        == "medium"
    )
    assert resolve_thinking_level(SimpleNamespace(custom_metadata="x")) == "medium"


def test_other_metadata_keys_are_ignored() -> None:
    assert resolve_thinking_level(_run_config("low", key="other")) == "medium"


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (None, types.ThinkingLevel.MEDIUM),
        ("low", types.ThinkingLevel.LOW),
        ("high", types.ThinkingLevel.HIGH),
    ],
)
def test_callback_sets_thinking_config(requested, expected) -> None:
    context = SimpleNamespace(agent_name="intake", run_config=_run_config(requested))
    request = LlmRequest(config=types.GenerateContentConfig(max_output_tokens=10))
    assert thinking_before_model(context, request) is None
    assert request.config.thinking_config.thinking_level == expected
    assert request.config.max_output_tokens == 10


def _all_llm_agents() -> list[LlmAgent]:
    return [
        value for value in vars(agent_module).values() if isinstance(value, LlmAgent)
    ]


def test_every_llm_agent_has_thinking_and_timing_callbacks() -> None:
    agents = _all_llm_agents()
    assert len(agents) >= 12
    for llm_agent in agents:
        before = llm_agent.canonical_before_model_callbacks
        assert thinking_before_model in before, llm_agent.name
        assert timing_before_model in before, llm_agent.name
        assert timing_after_model in llm_agent.canonical_after_model_callbacks, (
            llm_agent.name
        )
