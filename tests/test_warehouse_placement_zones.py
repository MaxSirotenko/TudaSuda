import json

import pytest

from warehouse_placement_zones import (
    ASSIGNABLE_PLACEMENT_ZONE_IDS,
    DEFAULT_PLACEMENT_ZONE_ORDER,
    PLACEMENT_ZONE_IDS,
    UNASSIGNED_ZONE,
    get_assignable_placement_zones,
    get_placement_zone_label,
    is_assignable_placement_zone,
    normalize_placement_zone,
    validate_placement_zone,
)


def test_canonical_contract_and_default_order_are_exact_and_json_serializable():
    expected = [
        "heavy", "medium", "medium_light", "light", "fragile", "bulky",
        "small_and_bulky", "show_boxes",
    ]
    assert get_assignable_placement_zones() == expected
    assert list(DEFAULT_PLACEMENT_ZONE_ORDER) == expected
    assert list(ASSIGNABLE_PLACEMENT_ZONE_IDS) == expected
    assert list(PLACEMENT_ZONE_IDS) == expected + [UNASSIGNED_ZONE]
    assert json.loads(json.dumps(get_assignable_placement_zones())) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Тяжёлое", "heavy"),
        ("Тяжелое", "heavy"),
        ("Среднее", "medium"),
        ("Средне-лёгкое", "medium_light"),
        ("Средне-легкое", "medium_light"),
        ("Лёгкое", "light"),
        ("Легкое", "light"),
        ("Хрупкое", "fragile"),
        ("Объёмное", "bulky"),
        ("Объемное", "bulky"),
        ("  Малогабаритное   и объёмное ", "small_and_bulky"),
        ("Малогабаритное и объемное", "small_and_bulky"),
        ("ШОУ-БОКСЫ", "show_boxes"),
        ("Не назначено", "unassigned"),
        ("", "unassigned"),
        (None, "unassigned"),
    ],
)
def test_normalizes_labels_missing_values_and_spelling_variants(value, expected):
    assert normalize_placement_zone(value) == expected


def test_all_canonical_ids_validate_without_fuzzy_matching():
    assert [validate_placement_zone(zone) for zone in PLACEMENT_ZONE_IDS] == list(PLACEMENT_ZONE_IDS)
    assert normalize_placement_zone("super_heavy") is None
    with pytest.raises(ValueError, match="Unknown placement zone"):
        validate_placement_zone("super_heavy")


def test_unassigned_is_not_assignable_and_distinct_zones_stay_distinct():
    assert not is_assignable_placement_zone("unassigned")
    assert is_assignable_placement_zone("medium_light")
    assert normalize_placement_zone("medium_light") != normalize_placement_zone("medium")
    with pytest.raises(ValueError, match="not an assignable"):
        validate_placement_zone(None, allow_unassigned=False)


def test_labels_are_available_for_every_canonical_id():
    assert {zone: get_placement_zone_label(zone) for zone in PLACEMENT_ZONE_IDS} == {
        "heavy": "Тяжёлое",
        "medium": "Среднее",
        "medium_light": "Средне-лёгкое",
        "light": "Лёгкое",
        "fragile": "Хрупкое",
        "bulky": "Объёмное",
        "small_and_bulky": "Малогабаритное и объёмное",
        "show_boxes": "Шоу-боксы",
        "unassigned": "Не назначено",
    }
