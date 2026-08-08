"""Canonical placement-zone identifiers, labels, and boundary validation."""

from __future__ import annotations

from typing import Any

UNASSIGNED_ZONE = "unassigned"

ASSIGNABLE_PLACEMENT_ZONE_IDS = (
    "heavy",
    "medium",
    "medium_light",
    "light",
    "fragile",
    "bulky",
    "small_and_bulky",
    "show_boxes",
)
PLACEMENT_ZONE_IDS = ASSIGNABLE_PLACEMENT_ZONE_IDS + (UNASSIGNED_ZONE,)
DEFAULT_PLACEMENT_ZONE_ORDER = ASSIGNABLE_PLACEMENT_ZONE_IDS

_ZONE_LABELS = {
    "heavy": "Тяжёлое",
    "medium": "Среднее",
    "medium_light": "Средне-лёгкое",
    "light": "Лёгкое",
    "fragile": "Хрупкое",
    "bulky": "Объёмное",
    "small_and_bulky": "Малогабаритное и объёмное",
    "show_boxes": "Шоу-боксы",
    UNASSIGNED_ZONE: "Не назначено",
}


def _normalization_key(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().casefold().replace("ё", "е").split())


_NORMALIZED_ZONES = {
    _normalization_key(value): zone_id
    for zone_id, label in _ZONE_LABELS.items()
    for value in (zone_id, label)
}


def normalize_placement_zone(value: Any) -> str | None:
    """Return a canonical ID; missing values mean ``unassigned``.

    Unknown values return ``None`` so callers cannot confuse invalid input with
    a missing zone.  Use :func:`validate_placement_zone` at input boundaries.
    """
    key = _normalization_key(value)
    if not key:
        return UNASSIGNED_ZONE
    return _NORMALIZED_ZONES.get(key)


def validate_placement_zone(value: Any, *, allow_unassigned: bool = True) -> str:
    """Normalize and validate a placement zone, raising on invalid input."""
    zone_id = normalize_placement_zone(value)
    if zone_id is None:
        raise ValueError(f"Unknown placement zone: {value!r}")
    if not allow_unassigned and zone_id == UNASSIGNED_ZONE:
        raise ValueError("Unassigned is not an assignable placement zone")
    return zone_id


def is_assignable_placement_zone(value: Any) -> bool:
    return normalize_placement_zone(value) in ASSIGNABLE_PLACEMENT_ZONE_IDS


def get_assignable_placement_zones() -> list[str]:
    return list(ASSIGNABLE_PLACEMENT_ZONE_IDS)


def get_placement_zone_label(zone_id: Any) -> str:
    canonical = validate_placement_zone(zone_id)
    return _ZONE_LABELS[canonical]
