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

import logging
import os
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

logger = logging.getLogger("sentinel.thinking")

THINKING_ENV = "SENTINEL_THINKING_LEVEL"
DEFAULT_LEVEL = "medium"
ALLOWED_LEVELS = ("low", "medium", "high")
A2A_METADATA_KEY = "a2a_metadata"
METADATA_FIELD = "thinking"

TIMEOUT_ENV = "SENTINEL_MODEL_TIMEOUT_SECONDS"
# Per-call limit before a stalled model request is cut off and retried. Normal
# calls finish well inside these limits; stalls of several minutes were seen
# with no error. Raise it through the environment for very large documents.
TIMEOUT_SECONDS = {"low": 120.0, "medium": 150.0, "high": 300.0}


def normalize_level(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    level = value.strip().lower()
    return level if level in ALLOWED_LEVELS else None


def resolve_thinking_level(run_config: Any) -> str:
    metadata = getattr(run_config, "custom_metadata", None)
    if isinstance(metadata, dict):
        a2a_metadata = metadata.get(A2A_METADATA_KEY)
        if isinstance(a2a_metadata, dict):
            requested = normalize_level(a2a_metadata.get(METADATA_FIELD))
            if requested:
                return requested
    return normalize_level(os.getenv(THINKING_ENV)) or DEFAULT_LEVEL


def request_timeout_seconds(level: str) -> float:
    try:
        override = float(os.environ[TIMEOUT_ENV])
    except (KeyError, ValueError):
        return TIMEOUT_SECONDS[level]
    return override if override > 0 else TIMEOUT_SECONDS[level]


def thinking_before_model(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> LlmResponse | None:
    level = resolve_thinking_level(callback_context.run_config)
    if llm_request.config is None:
        llm_request.config = types.GenerateContentConfig()
    llm_request.config.thinking_config = types.ThinkingConfig(
        thinking_level=types.ThinkingLevel[level.upper()]
    )
    if llm_request.config.http_options is None:
        llm_request.config.http_options = types.HttpOptions()
    llm_request.config.http_options.timeout = int(request_timeout_seconds(level) * 1000)
    logger.debug("agent=%s thinking_level=%s", callback_context.agent_name, level)
    return None
