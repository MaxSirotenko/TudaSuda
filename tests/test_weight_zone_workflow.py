from pathlib import Path

import pytest

import warehouse_revisions as revisions
from warehouse_receipts import build_vgh_weight_index, calculate_receipt_zones, enrich_receipts_with_vgh
from warehouse_weight_rules import classify_weight, load_weight_rules, save_weight_rules, validate_weight_bands
from warehouse_workspace_ui import build_data_source_cards, structured_warning


def bands():
    return {"light": {"min": 0, "max": 2}, "medium_light": {"min": 2, "max": 5},
            "medium": {"min": 5, "max": 10}, "heavy": {"min": 10, "max": None}}


def receipt(name="A", characteristic="red", manual=None):
    return {"sku_name": name, "characteristic_name": characteristic,
            "source_weight": manual, "weight_parse_status": "ok" if manual is not None else "not_supplied"}


def vgh(name="A", characteristic="red", weight=8.0):
    return {"nomenclature": name, "characteristic": characteristic, "weight": weight}


def test_vgh_is_authoritative_and_canonical_characteristic_is_part_of_join():
    rows = enrich_receipts_with_vgh([receipt(manual=7), receipt(characteristic="blue")],
                                    [vgh(weight=8), vgh(characteristic="blue", weight=12)])
    assert [(row["resolved_weight"], row["weight_source"]) for row in rows] == [(8, "vgh"), (12, "vgh")]
    assert rows[0]["weight_diagnostics"] == ["manual_weight_differs_from_vgh"]


def test_vgh_missing_empty_invalid_and_conflict_are_explicit():
    index = build_vgh_weight_index([vgh(weight=None), vgh(name="B", weight=-1),
                                    vgh(name="C", weight=4), vgh(name="C", weight=5)])
    rows = enrich_receipts_with_vgh([receipt(), receipt(name="B"), receipt(name="C"), receipt(name="D")],
                                    [vgh(weight=None), vgh(name="B", weight=-1),
                                     vgh(name="C", weight=4), vgh(name="C", weight=5)])
    assert index[next(iter(index))]["weight"] is None
    assert [row["weight_status"] for row in rows] == ["invalid_weight", "invalid_weight", "conflicting_weight", "missing_vgh"]
    assert all(row["resolved_weight"] is None for row in rows)


def test_duplicate_equal_vgh_weight_is_resolved():
    result = enrich_receipts_with_vgh([receipt()], [vgh(), vgh()])[0]
    assert (result["weight_status"], result["resolved_weight"]) == ("resolved", 8)


def test_configured_weight_classes_never_fabricate_special_attributes():
    rows, diagnostics = calculate_receipt_zones([receipt()], {"weight_bands": bands()}, [vgh(weight=12)])
    assert rows[0]["weight_class"] == "heavy"
    assert rows[0]["special_attributes"] == []
    assert rows[0]["calculated_zone"] not in {"fragile", "bulky", "small_and_bulky", "show_boxes"}
    assert diagnostics["Вес найден"] == 1


def test_fragile_evidence_stays_separate_from_weight_class():
    item = {**receipt(), "fragile_flag": True}
    rows, _ = calculate_receipt_zones([item], {"weight_bands": bands()}, [vgh(weight=12)])
    assert rows[0]["weight_class"] == "heavy"
    assert rows[0]["special_attributes"] == ["fragile"]


def test_weight_band_boundaries_gaps_and_open_heavy():
    assert classify_weight(2, bands())[0] == "medium_light"
    assert classify_weight(5, bands())[0] == "medium"
    assert classify_weight(1000, bands())[0] == "heavy"
    gapped = bands(); gapped["medium_light"]["min"] = 3
    assert classify_weight(2.5, gapped) == (None, "weight_outside_configured_bands")
    assert classify_weight(8, None) == (None, "weight_rules_not_configured")


@pytest.mark.parametrize("value, text", [
    ({**bands(), "medium": {"min": 4, "max": 10}}, "пересекаются"),
    ({**bands(), "light": {"min": -1, "max": 2}}, "отрицательным"),
    ({**bands(), "medium": {"min": 10, "max": 5}}, "минимум больше"),
])
def test_invalid_bands_are_rejected(value, text):
    with pytest.raises(ValueError, match=text):
        validate_weight_bands(value)


def test_rules_atomic_round_trip_and_revision_changes_only_on_change(tmp_path, monkeypatch):
    rules_path = tmp_path / "rules.json"
    monkeypatch.setattr(revisions, "REVISION_PATH", tmp_path / "revisions.json")
    first = save_weight_rules(bands(), model_id="m", path=rules_path)
    revision = revisions.get_revision("m", "weight_rules")
    same = save_weight_rules(bands(), model_id="m", path=rules_path)
    changed = bands(); changed["heavy"] = {"min": 11, "max": None}; changed["medium"]["max"] = 11
    second = save_weight_rules(changed, model_id="m", path=rules_path)
    assert load_weight_rules(rules_path)["bands"] == second["bands"]
    assert first["revision"] == same["revision"] and second["revision"] == first["revision"] + 1
    assert revisions.get_revision("m", "weight_rules") == revision + 1


def test_structured_warning_and_loaded_card_limitations_are_independent():
    warning = structured_warning(title="Не для всех SKU найден ВГХ", cause="1 SKU отсутствует",
        impact="Весовая категория недоступна", can_continue=True, action="Дополнить ВГХ")
    assert all(key in warning for key in ("title", "cause", "impact", "can_continue", "action"))
    card = build_data_source_cards({"datasets": [{"active": True, "source_type": "vgh", "rows": 1,
        "warnings": ["missing"], "errors": [], "index": {}}]})[-1]
    assert card["load_status"] == "loaded" and card["warning_count"] == 1
    error_card = build_data_source_cards({"datasets": [{"active": True, "source_type": "vgh", "rows": 0,
        "warnings": [], "errors": ["broken"], "index": {}}]})[-1]
    assert error_card["load_status"] == "error"
