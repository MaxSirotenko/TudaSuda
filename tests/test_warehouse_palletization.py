from __future__ import annotations

import copy

import pytest

from warehouse_business_identity import canonical_sku_key
from warehouse_palletization import (
    build_palletization_rule_state, palletize_boxes, palletize_receipt_event,
)


SKU = canonical_sku_key({"nomenclature": "A", "characteristic": "red"})


def rule(value=50, source="nomenclature_master"):
    return {"sku_key": SKU, "boxes_per_pallet": value, "source": source}


def event(event_id="r1", qty=135):
    return {"event_id": event_id, "event_type": "receipt", "receipt_batches": [{
        "sku_key": SKU, "nomenclature": "A", "characteristic": "red",
        "qty_units": qty, "unit_name": "короб", "receipt_line_keys": [event_id + "-line"],
    }]}


def test_rule_state_deduplicates_and_is_permutation_stable():
    rows = [rule(), rule(), rule(50, "second_authority")]
    first, diagnostics = build_palletization_rule_state(rows)
    second, _ = build_palletization_rule_state(list(reversed(rows)))
    assert first == second
    assert len(first["rules"]) == 1
    assert diagnostics["duplicate_identical_rules"] == 1
    assert first["palletization_rule_state_id"].startswith("sha256:")


@pytest.mark.parametrize("value", [0, -1, 1.5, True, float("nan"), float("inf")])
def test_rule_rejects_non_positive_non_integer_values(value):
    state, diagnostics = build_palletization_rule_state([rule(value)])
    assert state["rules"] == []
    assert diagnostics["invalid_palletization_rule"] == 1


def test_conflicting_rule_is_unresolved_not_first_wins():
    state, diagnostics = build_palletization_rule_state([rule(50), rule(60)])
    assert state["rules"] == [] and state["conflicting_skus"] == [SKU]
    assert diagnostics["conflicts"][0]["reason"] == "conflicting_palletization_rule"


def test_palletize_135_preserves_real_partial_quantity():
    chunks = palletize_boxes(SKU, 135, 50)
    assert [chunk["qty_boxes"] for chunk in chunks] == [50, 50, 35]
    assert chunks[-1] == {"pallet_sequence": 3, "qty_boxes": 35, "capacity_boxes": 50,
                          "fill_ratio": 0.7, "is_partial": True}


def test_events_do_not_cross_consolidate_and_ids_are_stable():
    state, _ = build_palletization_rule_state([rule()])
    morning = palletize_receipt_event(event("morning", 30), state)
    afternoon = palletize_receipt_event(event("afternoon", 20), state)
    assert [p["initial_boxes"] for p in morning["pallet_units"]] == [30]
    assert [p["initial_boxes"] for p in afternoon["pallet_units"]] == [20]
    assert morning["pallet_units"][0]["pallet_unit_id"] != afternoon["pallet_units"][0]["pallet_unit_id"]
    unchanged = copy.deepcopy(morning["pallet_units"][0])
    unchanged["remaining_boxes"] = 1
    assert unchanged["pallet_unit_id"] == morning["pallet_units"][0]["pallet_unit_id"]


def test_missing_and_conflicting_rules_preserve_unresolved_boxes():
    missing, _ = build_palletization_rule_state([])
    conflict, _ = build_palletization_rule_state([rule(50), rule(60)])
    for state, reason in ((missing, "palletization_rule_missing"),
                          (conflict, "palletization_rule_conflict")):
        result = palletize_receipt_event(event(qty=100), state)
        assert result["pallet_units"] == []
        assert result["unresolved_batches"] == [{"sku_key": SKU, "qty_boxes": 100, "reason": reason}]
