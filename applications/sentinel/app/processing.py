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

import copy
import json
import math
from typing import Any

from pydantic import BaseModel


def as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def normalize_bbox(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        numbers = [float(v) for v in value]
    except (TypeError, ValueError):
        return None
    if any(math.isnan(n) or math.isinf(n) for n in numbers):
        return None
    x_min, y_min, x_max, y_max = (min(1.0, max(0.0, n)) for n in numbers)
    if x_min > x_max:
        x_min, x_max = x_max, x_min
    if y_min > y_max:
        y_min, y_max = y_max, y_min
    if x_max - x_min <= 0 or y_max - y_min <= 0:
        return None
    return [round(n, 4) for n in (x_min, y_min, x_max, y_max)]


def bbox_union(boxes: list[list[float]]) -> list[float] | None:
    valid = [b for b in (normalize_bbox(box) for box in boxes) if b]
    if not valid:
        return None
    return [
        min(b[0] for b in valid),
        min(b[1] for b in valid),
        max(b[2] for b in valid),
        max(b[3] for b in valid),
    ]


def bbox_iou(first: Any, second: Any) -> float:
    a, b = normalize_bbox(first), normalize_bbox(second)
    if not a or not b:
        return 0.0
    inter_w = min(a[2], b[2]) - max(a[0], b[0])
    inter_h = min(a[3], b[3]) - max(a[1], b[1])
    if inter_w <= 0 or inter_h <= 0:
        return 0.0
    inter = inter_w * inter_h
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def normalize_inventory(inventory: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(inventory)
    items = result.get("items")
    if not isinstance(items, list):
        result["items"] = []
        return result
    used = {
        item.get("item_id")
        for item in items
        if isinstance(item, dict) and item.get("item_id")
    }
    seen: set[str] = set()
    next_number = 1
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("item_id")
        if not item_id or item_id in seen:
            while f"C{next_number}" in used or f"C{next_number}" in seen:
                next_number += 1
            item_id = f"C{next_number}"
            used.add(item_id)
            item["item_id"] = item_id
        seen.add(item_id)
        location = item.get("location")
        if isinstance(location, dict) and "bbox" in location:
            location["bbox"] = normalize_bbox(location.get("bbox"))
    return result
