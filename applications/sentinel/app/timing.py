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
import time
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse

logger = logging.getLogger("sentinel.timing")

_ENV_FLAG = "SENTINEL_TIMING_LOG"
_SDK_RETRY_LOGGER = "google_genai"
_MAX_RECORDS = 500

_starts: dict[tuple[str, str], tuple[float, int]] = {}
records: list[dict[str, Any]] = []
_handler_ready = False


def timing_enabled() -> bool:
    return os.getenv(_ENV_FLAG, "").lower() in ("1", "true", "yes")


def _ensure_handler() -> None:
    global _handler_ready
    if _handler_ready:
        return
    for target in (logger, logging.getLogger(_SDK_RETRY_LOGGER)):
        target.setLevel(logging.INFO)
        if not target.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(name)s %(message)s"))
            target.addHandler(handler)
        target.propagate = False
    _handler_ready = True


def request_bytes(llm_request: LlmRequest) -> int:
    total = 0
    for content in llm_request.contents or []:
        for part in content.parts or []:
            if part.text:
                total += len(part.text.encode("utf-8"))
            inline = getattr(part, "inline_data", None)
            if inline is not None and inline.data:
                total += len(inline.data)
    instruction = llm_request.config.system_instruction if llm_request.config else None
    if isinstance(instruction, str):
        total += len(instruction.encode("utf-8"))
    return total


def _key(callback_context: CallbackContext) -> tuple[str, str]:
    return (callback_context.invocation_id, callback_context.agent_name)


def timing_before_model(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> LlmResponse | None:
    if timing_enabled():
        _ensure_handler()
        _starts[_key(callback_context)] = (
            time.perf_counter(),
            request_bytes(llm_request),
        )
    return None


def timing_after_model(
    callback_context: CallbackContext, llm_response: LlmResponse
) -> LlmResponse | None:
    if not timing_enabled() or llm_response.partial:
        return None
    started = _starts.pop(_key(callback_context), None)
    if started is None:
        return None
    start, size = started
    usage = llm_response.usage_metadata
    record = {
        "agent": callback_context.agent_name,
        "seconds": round(time.perf_counter() - start, 2),
        "request_bytes": size,
        "prompt_tokens": getattr(usage, "prompt_token_count", None),
        "output_tokens": getattr(usage, "candidates_token_count", None),
        "thought_tokens": getattr(usage, "thoughts_token_count", None),
        "finish": str(llm_response.finish_reason)
        if llm_response.finish_reason
        else None,
        "error": llm_response.error_code,
    }
    records.append(record)
    if len(records) > _MAX_RECORDS:
        del records[0]
    logger.info(
        "sentinel.timing agent=%s seconds=%s request_bytes=%s prompt_tokens=%s "
        "output_tokens=%s thought_tokens=%s finish=%s error=%s",
        record["agent"],
        record["seconds"],
        record["request_bytes"],
        record["prompt_tokens"],
        record["output_tokens"],
        record["thought_tokens"],
        record["finish"],
        record["error"],
    )
    return None
