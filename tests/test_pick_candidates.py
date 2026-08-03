from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from warehouse_pick_candidates import find_pick_candidates_for_demand


SKU = "name:Капуста|char_name:Белая"


def _demand(**extra):
    value = {
        "demand_key": "d-1", "order_key": "o-1", "outbound_order_number": "РО-1",
        "sku_key": SKU, "requested_units": 5, "unit_name": "короб",
    }
    value.update(extra)
    return value


def _record(cell="1", units=7, unit="короб", **extra):
    value = {
        "sku_key": SKU, "cell_key": f"3|{cell}|1", "row_number": "3",
        "cell_number": str(cell), "tier": "1", "row_order": 3, "zone": "heavy",
        "storage_type": "normal", "quantity": 700.0,
        "occupied_capacity_pallets": 10.0, "capacity_pallets": 12.0,
        "x_center": 10.2, "y_center": 15.4, "source": "test",
        "qty_units": 999, "unit_quantities": [{"unit_name": unit, "qty_units": units}],
    }
    value.update(extra)
    return value


def _index(records=None, **by_sku):
    values = {SKU: records or []}
    values.update(by_sku)
    return {"model_id": "m-1", "source_file_hash": "h-1", "by_sku": values, "by_cell": {"must": "not scan"}}


def test_one_candidate_preserves_compact_contract_and_metadata():
    result = find_pick_candidates_for_demand(_demand(), _index([_record()]))
    assert (result["status"], result["can_fulfill"], result["total_available_units"], result["shortage_units"]) == ("sufficient_stock", True, 7, 0)
    assert (result["model_id"], result["source_file_hash"], result["candidate_count"]) == ("m-1", "h-1", 1)
    candidate = result["candidates"][0]
    assert candidate == {
        "sku_key": SKU, "cell_key": "3|1|1", "row_number": "3", "cell_number": "1",
        "tier": "1", "row_order": 3, "zone": "heavy", "storage_type": "normal",
        "quantity": 700.0, "occupied_capacity_pallets": 10.0, "capacity_pallets": 12.0,
        "x_center": 10.2, "y_center": 15.4, "source": "test",
        "available_units": 7, "unit_name": "короб",
    }
    json.dumps(result)


def test_multiple_cells_remain_separate_sum_and_sort_numerically():
    records = [_record("10", 2), _record("2", 4), _record("1", 1, row_order=10)]
    result = find_pick_candidates_for_demand(_demand(requested_units=8), _index(records))
    assert [item["cell_number"] for item in result["candidates"]] == ["2", "10", "1"]
    assert [item["available_units"] for item in result["candidates"]] == [4, 2, 1]
    assert (result["status"], result["total_available_units"], result["shortage_units"]) == ("insufficient_stock", 7, 1)


def test_missing_exact_sku_and_distinct_characteristic_are_not_matched():
    result = find_pick_candidates_for_demand(_demand(sku_key=SKU.lower()), _index([_record()]))
    assert (result["status"], result["candidates"], result["shortage_units"]) == ("sku_not_found", [], 5)
    other = SKU.replace("Белая", "Красная")
    assert find_pick_candidates_for_demand(_demand(sku_key=other), _index([_record()]))["status"] == "sku_not_found"


@pytest.mark.parametrize("demand_unit,inventory_unit,matched", [
    ("КОРОБ", "  короб ", True), ("бёрдо", "бердо", True),
    ("короб", "короба", False), ("шт", "штука", False),
    ("", "", True), ("", "короб", False), ("короб", "", False),
])
def test_unit_matching_is_normalized_but_not_semantic_or_wildcard(demand_unit, inventory_unit, matched):
    result = find_pick_candidates_for_demand(_demand(unit_name=demand_unit), _index([_record(unit=inventory_unit)]))
    assert bool(result["candidates"]) is matched
    assert result["status"] == ("sufficient_stock" if matched else "unit_not_found")


def test_only_unit_quantities_determine_stock_and_other_units_are_diagnosed():
    record = _record(units=0, quantity=999999, occupied_capacity_pallets=999999)
    record["unit_quantities"].append({"unit_name": "штука", "qty_units": 50})
    result = find_pick_candidates_for_demand(_demand(), _index([record]))
    assert result["status"] == "unit_not_found" and result["total_available_units"] == 0
    assert result["diagnostics"]["non_positive_unit_variant_count"] == 1
    assert result["diagnostics"]["unmatched_unit_variant_count"] == 1


@pytest.mark.parametrize("quantity,diagnostic", [("7", "invalid_unit_variant_count"), (1.5, "invalid_unit_variant_count"), (True, "invalid_unit_variant_count"), (0, "non_positive_unit_variant_count"), (-2, "non_positive_unit_variant_count")])
def test_invalid_and_non_positive_variant_quantities_are_skipped(quantity, diagnostic):
    result = find_pick_candidates_for_demand(_demand(), _index([_record(units=quantity)]))
    assert result["status"] == "unit_not_found"
    assert result["diagnostics"][diagnostic] == 1


def test_duplicate_normalized_units_in_one_cell_sum_to_one_candidate():
    record = _record()
    record["unit_quantities"] = [
        {"unit_name": " Короб ", "qty_units": 2},
        {"unit_name": "КОРОБ", "qty_units": 4},
    ]
    result = find_pick_candidates_for_demand(_demand(requested_units=6), _index([record]))
    assert len(result["candidates"]) == 1 and result["candidates"][0]["available_units"] == 6
    assert result["candidates"][0]["unit_name"] == "Короб"
    assert result["diagnostics"]["duplicate_matching_unit_variants"] == 1


@pytest.mark.parametrize("updates,reason,shortage", [
    ({"sku_key": ""}, "sku_key_missing", 5),
    ({"requested_units": 0}, "requested_units_not_positive", 0),
    ({"requested_units": -1}, "requested_units_not_positive", 0),
    ({"requested_units": 5.0}, "requested_units_not_integer", 0),
    ({"requested_units": "5"}, "requested_units_not_integer", 0),
    ({"requested_units": True}, "requested_units_boolean", 0),
])
def test_invalid_demands_return_safe_result(updates, reason, shortage):
    result = find_pick_candidates_for_demand(_demand(**updates), _index([_record()]))
    assert (result["status"], result["can_fulfill"], result["candidates"]) == ("invalid_demand", False, [])
    assert result["shortage_units"] == shortage
    assert result["diagnostics"]["demand_validation_reason"] == reason


def test_non_dict_and_missing_unit_are_invalid():
    assert find_pick_candidates_for_demand(None, _index())["diagnostics"]["demand_validation_reason"] == "demand_not_dict"
    demand = _demand(); demand.pop("unit_name")
    assert find_pick_candidates_for_demand(demand, _index())["diagnostics"]["demand_validation_reason"] == "unit_name_missing"


def test_inputs_including_nested_records_are_not_modified():
    demand = _demand(source_indexes=[1])
    index = _index([_record()])
    before = copy.deepcopy((demand, index))
    find_pick_candidates_for_demand(demand, index)
    assert (demand, index) == before


def test_diagnostics_count_locations_variants_and_cells():
    records = [_record("1"), _record("2", unit="штука")]
    result = find_pick_candidates_for_demand(_demand(), _index(records))
    assert result["diagnostics"] == {
        "sku_found": True, "source_location_count": 2, "source_unit_variant_count": 2,
        "matched_candidate_count": 1, "unmatched_unit_variant_count": 1,
        "invalid_unit_variant_count": 0, "non_positive_unit_variant_count": 0,
        "duplicate_matching_unit_variants": 0, "cells_without_matching_unit": 1,
        "cells_with_matching_unit": 1, "demand_validation_reason": "",
    }


def test_module_has_no_io_ui_cache_execution_or_persisted_writes():
    source = (Path(__file__).resolve().parents[1] / "warehouse_pick_candidates.py").read_text(encoding="utf-8")
    imports = {
        alias.name for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names
    }
    assert "pandas" not in imports and "streamlit" not in imports
    assert all(token not in source for token in (
        "open(", ".read_text(", ".write_text(", "execute_outbound_orders",
        "save_placement_state", "update_data_revisions", "revision_token", "st.cache_data.clear",
    ))
