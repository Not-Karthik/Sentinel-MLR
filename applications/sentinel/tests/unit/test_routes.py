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

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes import dummy, dummy_router, media, media_router

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PDF = b"%PDF-1.7\n" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 16
GIF = b"GIF89a" + b"\x00" * 16


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    stored: dict[str, tuple[bytes, str]] = {}

    def fake_store(bucket: str, name: str, data: bytes, mime: str) -> None:
        stored[f"{bucket}/{name}"] = (data, mime)

    monkeypatch.setenv("MEDIA_BUCKET_NAME", "test-bucket")
    monkeypatch.delenv("MEDIA_MAX_BYTES", raising=False)
    monkeypatch.setattr(media, "_store", fake_store)
    for store in (dummy._feedback, dummy._reviews, dummy._saves):
        store.clear()
    api = FastAPI()
    api.include_router(media_router)
    api.include_router(dummy_router)
    test_client = TestClient(api)
    test_client.stored = stored
    return test_client


@pytest.mark.parametrize(
    ("payload", "mime", "extension"),
    [
        (PNG, "image/png", "png"),
        (JPEG, "image/jpeg", "jpg"),
        (PDF, "application/pdf", "pdf"),
        (WEBP, "image/webp", "webp"),
        (GIF, "image/gif", "gif"),
    ],
)
def test_upload_accepts_supported_types(client, payload, mime, extension):
    response = client.post(
        "/api/v1/media", files={"file": ("anything.bin", payload, "text/plain")}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["mime_type"] == mime
    assert body["size"] == len(payload)
    assert body["uri"].startswith("gs://test-bucket/uploads/")
    assert body["uri"].endswith(f".{extension}")
    assert body["id"].endswith(f".{extension}")
    assert next(iter(client.stored.values())) == (payload, mime)


def test_upload_rejects_unsupported_type(client):
    response = client.post(
        "/api/v1/media", files={"file": ("x.png", b"not an image", "image/png")}
    )
    assert response.status_code == 415
    assert not client.stored


def test_upload_rejects_empty_file(client):
    response = client.post("/api/v1/media", files={"file": ("x.png", b"", "image/png")})
    assert response.status_code == 400


def test_upload_rejects_oversized_file(client, monkeypatch):
    monkeypatch.setenv("MEDIA_MAX_BYTES", "16")
    response = client.post("/api/v1/media", files={"file": ("x.png", PNG, "image/png")})
    assert response.status_code == 413
    assert not client.stored


def test_upload_requires_bucket(client, monkeypatch):
    monkeypatch.delenv("MEDIA_BUCKET_NAME", raising=False)
    monkeypatch.delenv("LOGS_BUCKET_NAME", raising=False)
    monkeypatch.delenv("GCS_BUCKET_NAME", raising=False)
    response = client.post("/api/v1/media", files={"file": ("x.png", PNG, "image/png")})
    assert response.status_code == 503


def test_upload_falls_back_to_logs_bucket(client, monkeypatch):
    monkeypatch.delenv("MEDIA_BUCKET_NAME", raising=False)
    monkeypatch.setenv("LOGS_BUCKET_NAME", "logs-bucket")
    response = client.post("/api/v1/media", files={"file": ("x.png", PNG, "image/png")})
    assert response.status_code == 201
    assert response.json()["uri"].startswith("gs://logs-bucket/uploads/")


def test_upload_object_names_are_random(client):
    first = client.post("/api/v1/media", files={"file": ("a.png", PNG, "image/png")})
    second = client.post("/api/v1/media", files={"file": ("a.png", PNG, "image/png")})
    assert first.json()["uri"] != second.json()["uri"]


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("feedback", {"task_id": "t1", "rating": 4, "comment": "good"}),
        (
            "review",
            {"task_id": "t1", "finding_id": "f1", "decision": "accept"},
        ),
        ("save", {"task_id": "t1", "name": "r", "report": {"findings": []}}),
    ],
)
def test_placeholder_round_trip(client, path, payload):
    created = client.post(f"/api/v1/{path}", json=payload)
    assert created.status_code == 201
    assert created.headers["X-Storage"] == "ephemeral"
    record = created.json()
    assert record["id"]
    assert record["created_at"]
    listed = client.get(f"/api/v1/{path}")
    assert listed.status_code == 200
    assert listed.headers["X-Storage"] == "ephemeral"
    assert [item["id"] for item in listed.json()] == [record["id"]]


def test_feedback_rating_is_validated(client):
    response = client.post("/api/v1/feedback", json={"rating": 9})
    assert response.status_code == 422


def test_review_decision_is_validated(client):
    response = client.post(
        "/api/v1/review",
        json={"task_id": "t", "finding_id": "f", "decision": "maybe"},
    )
    assert response.status_code == 422


def test_placeholder_store_is_bounded(client, monkeypatch):
    monkeypatch.setattr(dummy, "MAX_RECORDS", 3)
    for index in range(5):
        client.post("/api/v1/feedback", json={"rating": 3, "comment": str(index)})
    comments = [item["comment"] for item in client.get("/api/v1/feedback").json()]
    assert comments == ["2", "3", "4"]


def test_upload_falls_back_to_gcs_bucket_name(client, monkeypatch):
    monkeypatch.delenv("MEDIA_BUCKET_NAME", raising=False)
    monkeypatch.delenv("LOGS_BUCKET_NAME", raising=False)
    monkeypatch.setenv("GCS_BUCKET_NAME", "legacy-bucket")
    response = client.post("/api/v1/media", files={"file": ("x.png", PNG, "image/png")})
    assert response.status_code == 201
    assert response.json()["uri"].startswith("gs://legacy-bucket/uploads/")


def test_default_limit_is_32_mib(client):
    assert media._max_bytes() == 32 * 1024 * 1024
