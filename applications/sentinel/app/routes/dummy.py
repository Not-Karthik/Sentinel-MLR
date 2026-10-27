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

import threading
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/v1", tags=["placeholders"])

MAX_RECORDS = 1000
STORAGE_HEADER = "X-Storage"
STORAGE_VALUE = "ephemeral"

_lock = threading.Lock()
_feedback: list["FeedbackRecord"] = []
_reviews: list["ReviewRecord"] = []
_saves: list["SaveRecord"] = []


def _new_id() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


def _append(store: list[Any], record: Any) -> None:
    with _lock:
        store.append(record)
        if len(store) > MAX_RECORDS:
            del store[0]


def _snapshot(store: list[Any]) -> list[Any]:
    with _lock:
        return list(store)


class FeedbackIn(BaseModel):
    task_id: str | None = Field(None, description="A2A task id the feedback is about.")
    context_id: str | None = Field(None, description="A2A context id.")
    rating: int = Field(ge=1, le=5, description="1 (poor) to 5 (excellent).")
    comment: str = Field("", max_length=2000)


class FeedbackRecord(FeedbackIn):
    id: str
    created_at: datetime


class ReviewIn(BaseModel):
    task_id: str = Field(description="A2A task id of the analysis being reviewed.")
    context_id: str | None = None
    finding_id: str = Field(description="Identifier of the finding being reviewed.")
    decision: Literal["accept", "reject", "edit"]
    comment: str = Field("", max_length=2000)


class ReviewRecord(ReviewIn):
    id: str
    created_at: datetime


class SaveIn(BaseModel):
    task_id: str = Field(description="A2A task id the report belongs to.")
    context_id: str | None = None
    name: str = Field("", max_length=200)
    report: dict[str, Any] = Field(
        description="The final_report JSON returned by the agent."
    )


class SaveRecord(SaveIn):
    id: str
    created_at: datetime


def _mark_ephemeral(response: Response) -> None:
    response.headers[STORAGE_HEADER] = STORAGE_VALUE


@router.post(
    "/feedback",
    response_model=FeedbackRecord,
    status_code=status.HTTP_201_CREATED,
    summary="Placeholder: submit feedback (in memory)",
)
def create_feedback(payload: FeedbackIn, response: Response) -> FeedbackRecord:
    record = FeedbackRecord(id=_new_id(), created_at=_now(), **payload.model_dump())
    _append(_feedback, record)
    _mark_ephemeral(response)
    return record


@router.get(
    "/feedback",
    response_model=list[FeedbackRecord],
    summary="Placeholder: list feedback (in memory)",
)
def list_feedback(response: Response) -> list[FeedbackRecord]:
    _mark_ephemeral(response)
    return _snapshot(_feedback)


@router.post(
    "/review",
    response_model=ReviewRecord,
    status_code=status.HTTP_201_CREATED,
    summary="Placeholder: submit a reviewer decision (in memory)",
)
def create_review(payload: ReviewIn, response: Response) -> ReviewRecord:
    record = ReviewRecord(id=_new_id(), created_at=_now(), **payload.model_dump())
    _append(_reviews, record)
    _mark_ephemeral(response)
    return record


@router.get(
    "/review",
    response_model=list[ReviewRecord],
    summary="Placeholder: list reviewer decisions (in memory)",
)
def list_reviews(response: Response) -> list[ReviewRecord]:
    _mark_ephemeral(response)
    return _snapshot(_reviews)


@router.post(
    "/save",
    response_model=SaveRecord,
    status_code=status.HTTP_201_CREATED,
    summary="Placeholder: save a report (in memory)",
)
def create_save(payload: SaveIn, response: Response) -> SaveRecord:
    record = SaveRecord(id=_new_id(), created_at=_now(), **payload.model_dump())
    _append(_saves, record)
    _mark_ephemeral(response)
    return record


@router.get(
    "/save",
    response_model=list[SaveRecord],
    summary="Placeholder: list saved reports (in memory)",
)
def list_saves(response: Response) -> list[SaveRecord]:
    _mark_ephemeral(response)
    return _snapshot(_saves)
