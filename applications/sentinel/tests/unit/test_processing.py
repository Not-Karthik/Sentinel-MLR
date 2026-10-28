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

from app import processing
from app.schemas import ContentLocation


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ([0.1, 0.2, 0.5, 0.6], [0.1, 0.2, 0.5, 0.6]),
        ([0.5, 0.6, 0.1, 0.2], [0.1, 0.2, 0.5, 0.6]),
        ([-0.2, 0.0, 1.4, 1.0], [0.0, 0.0, 1.0, 1.0]),
        ([0.123456, 0.2, 0.5, 0.6], [0.1235, 0.2, 0.5, 0.6]),
        (["0.1", "0.2", "0.5", "0.6"], [0.1, 0.2, 0.5, 0.6]),
        ([0.1, 0.2, 0.5], None),
        ([0.1, 0.2, 0.5, 0.6, 0.7], None),
        ([0.2, 0.2, 0.2, 0.6], None),
        ([0.1, 0.2, "x", 0.6], None),
        ([0.1, 0.2, float("nan"), 0.6], None),
        ([0.1, 0.2, float("inf"), 0.6], None),
        (None, None),
        ("0.1,0.2,0.5,0.6", None),
        ({"x": 1}, None),
    ],
)
def test_normalize_bbox(value, expected) -> None:
    assert processing.normalize_bbox(value) == expected


def test_normalized_bbox_is_valid_for_content_location() -> None:
    bbox = processing.normalize_bbox([0.9, 0.9, 0.1, 0.1])
    assert ContentLocation(bbox=bbox).bbox == [0.1, 0.1, 0.9, 0.9]


def test_bbox_union() -> None:
    union = processing.bbox_union([[0.1, 0.2, 0.3, 0.4], [0.2, 0.1, 0.6, 0.3]])
    assert union == [0.1, 0.1, 0.6, 0.4]
    assert processing.bbox_union([]) is None
    assert processing.bbox_union([[1, 1, 1, 1]]) is None


def test_bbox_iou() -> None:
    assert processing.bbox_iou([0, 0, 1, 1], [0, 0, 1, 1]) == pytest.approx(1.0)
    assert processing.bbox_iou([0, 0, 0.5, 0.5], [0.5, 0.5, 1, 1]) == 0.0
    assert processing.bbox_iou([0, 0, 0.5, 1], [0.25, 0, 0.75, 1]) == pytest.approx(
        1 / 3
    )
    assert processing.bbox_iou(None, [0, 0, 1, 1]) == 0.0


def test_as_dict_handles_models_dicts_json_and_garbage() -> None:
    assert processing.as_dict({"a": 1}) == {"a": 1}
    assert processing.as_dict('{"a": 1}') == {"a": 1}
    assert processing.as_dict("[1]") == {}
    assert processing.as_dict("not json") == {}
    assert processing.as_dict(None) == {}
    assert processing.as_dict(ContentLocation(page=2)) == {
        "page": 2,
        "section": None,
        "bbox": None,
        "quote": None,
    }


def test_normalize_inventory_fixes_bboxes_and_ids_without_mutating_input() -> None:
    inventory = {
        "content_summary": "s",
        "items": [
            {"item_id": "C1", "text": "a", "location": {"bbox": [0.9, 0.9, 0.1, 0.1]}},
            {"item_id": "C1", "text": "b", "location": {"bbox": [0, 0, 5, 5]}},
            {"item_id": "", "text": "c", "location": {"bbox": [0.5, 0.5, 0.5, 0.5]}},
            {"text": "d", "location": None},
            {"item_id": "C2", "text": "e"},
        ],
    }
    original = {"items": [dict(i) for i in inventory["items"]]}
    result = processing.normalize_inventory(inventory)
    ids = [item["item_id"] for item in result["items"]]
    assert ids[0] == "C1"
    assert len(set(ids)) == len(ids) == 5
    assert "C2" in ids
    assert result["items"][0]["location"]["bbox"] == [0.1, 0.1, 0.9, 0.9]
    assert result["items"][1]["location"]["bbox"] == [0.0, 0.0, 1.0, 1.0]
    assert result["items"][2]["location"]["bbox"] is None
    assert result["content_summary"] == "s"
    assert inventory["items"][0]["location"]["bbox"] == [0.9, 0.9, 0.1, 0.1]
    assert [i.get("text") for i in inventory["items"]] == [
        i["text"] for i in original["items"]
    ]


def test_normalize_inventory_tolerates_missing_or_invalid_items() -> None:
    assert processing.normalize_inventory({})["items"] == []
    assert processing.normalize_inventory({"items": "oops"})["items"] == []
    assert processing.normalize_inventory({"items": ["x", None]})["items"] == [
        "x",
        None,
    ]


def test_normalize_intake_node_rewrites_state() -> None:
    from app.agent import normalize_intake

    class Ctx:
        def __init__(self, state: dict) -> None:
            self.state = state

    ctx = Ctx(
        {
            "intake_findings": {
                "items": [{"item_id": "C1", "location": {"bbox": [0.8, 0.8, 0.2, 0.2]}}]
            }
        }
    )
    assert normalize_intake(ctx, "passthrough") == "passthrough"
    assert ctx.state["intake_findings"]["items"][0]["location"]["bbox"] == [
        0.2,
        0.2,
        0.8,
        0.8,
    ]
    empty = Ctx({})
    assert normalize_intake(empty, "x") == "x"
    assert "intake_findings" not in empty.state
