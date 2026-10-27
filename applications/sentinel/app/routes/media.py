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

import os
import uuid
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from google.cloud import storage
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

router = APIRouter(prefix="/api/v1", tags=["media"])

MEDIA_PREFIX = "uploads"
DEFAULT_MAX_BYTES = 32 * 1024 * 1024
READ_CHUNK = 1024 * 1024


class MediaUploadResponse(BaseModel):
    id: str = Field(description="Object name without the folder prefix.")
    uri: str = Field(
        description=(
            "gs:// URI to send in an A2A message as "
            '{"kind": "file", "file": {"uri": "<uri>", "mimeType": "<mime_type>"}}.'
        )
    )
    mime_type: str
    size: int = Field(description="Size in bytes.")


def _bucket_name() -> str:
    name = (
        os.getenv("MEDIA_BUCKET_NAME")
        or os.getenv("LOGS_BUCKET_NAME")
        or os.getenv("GCS_BUCKET_NAME")
    )
    if not name:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Media storage is not configured (set MEDIA_BUCKET_NAME).",
        )
    return name


def _max_bytes() -> int:
    return int(os.getenv("MEDIA_MAX_BYTES", str(DEFAULT_MAX_BYTES)))


def sniff_media_type(data: bytes) -> tuple[str, str] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    if data.startswith(b"%PDF-"):
        return "application/pdf", "pdf"
    return None


def _store(bucket_name: str, object_name: str, data: bytes, mime_type: str) -> None:
    client = storage.Client()
    client.bucket(bucket_name).blob(object_name).upload_from_string(
        data, content_type=mime_type
    )


async def _read_capped(file: UploadFile, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"File exceeds the {limit} byte limit.",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post(
    "/media",
    response_model=MediaUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload an image or PDF to feed to the A2A agent",
)
async def upload_media(file: Annotated[UploadFile, File()]) -> MediaUploadResponse:
    bucket_name = _bucket_name()
    data = await _read_capped(file, _max_bytes())
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Empty file.")
    detected = sniff_media_type(data)
    if detected is None:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "Unsupported file type. Allowed: png, jpeg, gif, webp, pdf.",
        )
    mime_type, extension = detected
    media_id = f"{uuid.uuid4().hex}.{extension}"
    object_name = f"{MEDIA_PREFIX}/{media_id}"
    await run_in_threadpool(_store, bucket_name, object_name, data, mime_type)
    return MediaUploadResponse(
        id=media_id,
        uri=f"gs://{bucket_name}/{object_name}",
        mime_type=mime_type,
        size=len(data),
    )
