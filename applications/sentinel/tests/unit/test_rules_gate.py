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
from types import SimpleNamespace

import pytest
from google.adk.apps import App
from google.adk.runners import InMemoryRunner
from google.genai import types

from app import processing
from app.agent import rules_reviewer
from app.schemas import ReviewerOutput
from app.thinking import thinking_before_model


def _context(state: dict) -> SimpleNamespace:
    return SimpleNamespace(state=state)


def test_gate_returns_the_exact_empty_output_without_rules() -> None:
    for state in ({}, {"custom_rules": ""}, {"custom_rules": None}):
        response = processing.rules_reviewer_gate(_context(state), None)
        assert response is not None
        text = response.content.parts[0].text
        assert json.loads(text) == processing.EMPTY_RULES_OUTPUT
        output = ReviewerOutput.model_validate_json(text)
        assert output.lens == "custom"
        assert output.findings == []


def test_gate_lets_the_model_run_when_rules_exist() -> None:
    assert (
        processing.rules_reviewer_gate(_context({"custom_rules": "Rule 1"}), None)
        is None
    )


def test_gate_runs_before_thinking_callback() -> None:
    callbacks = rules_reviewer.canonical_before_model_callbacks
    assert callbacks[0] is processing.rules_reviewer_gate
    assert thinking_before_model in callbacks


@pytest.mark.asyncio
async def test_rules_reviewer_makes_no_model_call_without_rules() -> None:
    runner = InMemoryRunner(app=App(name="rules_gate_test", root_agent=rules_reviewer))
    session = await runner.session_service.create_session(
        app_name="rules_gate_test",
        user_id="u",
        state={"intake_findings": {"items": []}},
    )
    message = types.Content(role="user", parts=[types.Part.from_text(text="review")])
    texts = []
    async for event in runner.run_async(
        user_id="u", session_id=session.id, new_message=message
    ):
        if event.content and event.content.parts:
            texts.extend(p.text for p in event.content.parts if p.text)
    stored = await runner.session_service.get_session(
        app_name="rules_gate_test", user_id="u", session_id=session.id
    )
    assert any(json.loads(t) == processing.EMPTY_RULES_OUTPUT for t in texts)
    saved = stored.state["rules_findings"]
    saved = json.loads(saved) if isinstance(saved, str) else saved
    assert saved["findings"] == []
    assert saved["lens"] == "custom"
