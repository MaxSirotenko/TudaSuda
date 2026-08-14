"""Persisted business rules for weight classes (not special placement flags)."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Mapping

from warehouse_persistence import atomic_write_json, read_json
import warehouse_revisions

SCHEMA_VERSION = 1
WEIGHT_RULES_PATH = Path("data/last_import/weight_zone_rules.json")
WEIGHT_CLASSES = ("light", "medium_light", "medium", "heavy")
WEIGHT_CLASS_LABELS = {
    "light": "Лёгкое", "medium_light": "Средне-лёгкое",
    "medium": "Среднее", "heavy": "Тяжёлое",
}


def empty_weight_rules() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "bands": {}, "revision": 0}


def validate_weight_bands(bands: Mapping[str, Any]) -> dict[str, dict[str, float | None]]:
    """Validate ordered half-open bands. Gaps are deliberately allowed."""
    if set(bands) != set(WEIGHT_CLASSES):
        raise ValueError("Нужно настроить все четыре весовые категории.")
    normalized: dict[str, dict[str, float | None]] = {}
    previous_max: float | None = None
    for position, name in enumerate(WEIGHT_CLASSES):
        value = bands.get(name)
        if not isinstance(value, Mapping):
            raise ValueError(f"Диапазон «{WEIGHT_CLASS_LABELS[name]}» не заполнен.")
        minimum, maximum = value.get("min"), value.get("max")
        if isinstance(minimum, bool) or not isinstance(minimum, (int, float)) or not math.isfinite(minimum):
            raise ValueError(f"Минимум «{WEIGHT_CLASS_LABELS[name]}» должен быть числом.")
        if minimum < 0:
            raise ValueError(f"Минимум «{WEIGHT_CLASS_LABELS[name]}» не может быть отрицательным.")
        if maximum is None and name != "heavy":
            raise ValueError(f"Максимум «{WEIGHT_CLASS_LABELS[name]}» должен быть числом.")
        if maximum is not None:
            if isinstance(maximum, bool) or not isinstance(maximum, (int, float)) or not math.isfinite(maximum):
                raise ValueError(f"Максимум «{WEIGHT_CLASS_LABELS[name]}» должен быть числом.")
            if maximum < minimum:
                raise ValueError(f"В диапазоне «{WEIGHT_CLASS_LABELS[name]}» минимум больше максимума.")
        if previous_max is not None and minimum < previous_max:
            previous = WEIGHT_CLASS_LABELS[WEIGHT_CLASSES[position - 1]]
            raise ValueError(f"Диапазоны «{previous}» и «{WEIGHT_CLASS_LABELS[name]}» пересекаются.")
        normalized[name] = {"min": float(minimum), "max": None if maximum is None else float(maximum)}
        previous_max = maximum
    return normalized


def classify_weight(weight: Any, bands: Mapping[str, Any] | None) -> tuple[str | None, str]:
    if not bands:
        return None, "weight_rules_not_configured"
    checked = validate_weight_bands(bands)
    if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not math.isfinite(weight) or weight <= 0:
        return None, "weight_unresolved"
    for index, name in enumerate(WEIGHT_CLASSES):
        band = checked[name]
        # Upper bounds are exclusive, except the final finite bound. This makes
        # touching boundaries deterministic without epsilon-based guessing.
        upper_matches = band["max"] is None or weight < band["max"] or (
            index == len(WEIGHT_CLASSES) - 1 and weight == band["max"])
        if weight >= band["min"] and upper_matches:
            return name, f"weight_in_{name}_band"
    return None, "weight_outside_configured_bands"


def load_weight_rules(path: Path = WEIGHT_RULES_PATH) -> dict[str, Any]:
    if not path.exists():
        return empty_weight_rules()
    payload = read_json(path)
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Неподдерживаемая версия настроек весовых категорий.")
    result = dict(payload)  # preserve unknown keys for forward compatibility
    result.setdefault("bands", {})
    result.setdefault("revision", 0)
    return result


def save_weight_rules(bands: Mapping[str, Any], *, model_id: str | None = None,
                      path: Path = WEIGHT_RULES_PATH) -> dict[str, Any]:
    checked = validate_weight_bands(bands)
    current = load_weight_rules(path)
    if current.get("bands") == checked:
        return current
    result = {**current, "schema_version": SCHEMA_VERSION, "bands": checked,
              "revision": int(current.get("revision") or 0) + 1}
    atomic_write_json(path, result)
    warehouse_revisions.bump_revisions(model_id, ["weight_rules"], "save_weight_rules")
    return result
