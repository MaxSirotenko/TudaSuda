from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from warehouse_pick_working_stock import build_pick_working_stock


SKU = "name:Капуста|char_name:Белая"


def _record(cell="5", units=10, unit="короб", **extra):
    value = {
        "sku_key": SKU, "cell_key": f"3|{cell}|1", "row_number": "3",
        "cell_number": str(cell), "tier": "1", "row_order": 3, "zone": "heavy",
        "storage_type": "normal", "quantity": 999.0,
        "occupied_capacity_pallets": 8.0, "capacity_pallets": 1.0,
        "x_center": 10.2, "y_center": 15.4, "source": "test",
        "qty_units": 888, "unit_quantities": [{"unit_name": unit, "qty_units": units}],
    }
    value.update(extra)
    return value


def _index(records=None, **buckets):
    by_sku = {SKU: [] if records is None else records}
    by_sku.update(buckets)
    return {"model_id": "m-1", "source_file_hash": "h-1", "by_sku": by_sku,
            "by_cell": {"must": "not be scanned"}}


def test_empty_valid_index_has_complete_serializable_contract():
    result = build_pick_working_stock(_index())
    assert result["stock_by_key"] == result["stock_keys_by_sku"] == result["stock_keys_by_cell"] == {}
    assert result["diagnostics"]["input_validation_reason"] == ""
    json.dumps(result)


def test_one_variant_uses_only_unit_quantity_and_preserves_metadata():
    result = build_pick_working_stock(_index([_record()]))
    record = next(iter(result["stock_by_key"].values()))
    assert (record["initial_units"], record["remaining_units"]) == (10, 10)
    assert (record["quantity"], record["occupied_capacity_pallets"]) == (999.0, 8.0)
    assert (record["zone"], record["storage_type"], record["x_center"], record["y_center"]) == ("heavy", "normal", 10.2, 15.4)


def test_cells_units_and_characteristic_skus_remain_separate():
    red = SKU.replace("Белая", "Красная")
    records = [_record("1", unit="короб"), _record("2", unit="штука")]
    result = build_pick_working_stock(_index(records, **{red: [_record("1", sku_key=red)]}))
    assert len(result["stock_by_key"]) == 3
    assert len(result["stock_keys_by_sku"][SKU]) == 2
    assert set(result["stock_keys_by_sku"]) == {SKU, red}


@pytest.mark.parametrize("first,second,merged", [
    ("Короб", " короб ", True), ("бёрдо", "БЕРДО", True),
    ("короб", "короба", False), ("шт", "штука", False), ("", "", True),
])
def test_units_normalize_only_spelling_case_and_whitespace(first, second, merged):
    record = _record()
    record["unit_quantities"] = [
        {"unit_name": first, "qty_units": 2}, {"unit_name": second, "qty_units": 3},
    ]
    result = build_pick_working_stock(_index([record]))
    assert len(result["stock_by_key"]) == (1 if merged else 2)
    if merged:
        item = next(iter(result["stock_by_key"].values()))
        assert (item["unit_name"], item["initial_units"], item["remaining_units"]) == (" ".join(first.split()), 5, 5)


@pytest.mark.parametrize("units,diagnostic", [
    (True, "skipped_invalid_unit_variant"), (1.0, "skipped_invalid_unit_variant"),
    ("1", "skipped_invalid_unit_variant"), (0, "skipped_non_positive_units"),
    (-1, "skipped_non_positive_units"),
])
def test_only_positive_non_boolean_int_units_are_included(units, diagnostic):
    result = build_pick_working_stock(_index([_record(units=units)]))
    assert result["stock_by_key"] == {}
    assert result["diagnostics"][diagnostic] == 1


def test_invalid_variant_missing_cell_bucket_and_mismatched_sku_are_diagnosed():
    bad_cell = _record(); bad_cell.pop("cell_key")
    mismatch = _record(sku_key="other")
    result = build_pick_working_stock(_index([bad_cell, mismatch, "bad", _record(unit_quantities=[None])], **{" ": []}))
    diagnostics = result["diagnostics"]
    assert diagnostics["skipped_missing_cell_key"] == 1
    assert diagnostics["mismatched_sku_records"] == 1
    assert diagnostics["skipped_invalid_location_record"] == 1
    assert diagnostics["skipped_invalid_unit_variant"] == 1
    assert diagnostics["skipped_invalid_sku_bucket"] == 1


def test_duplicate_key_sums_units_keeps_first_metadata_and_reports_conflict():
    first = _record(units=2, zone="heavy")
    second = _record(units=7, unit=" КОРОБ ", zone="light")
    result = build_pick_working_stock(_index([first, second]))
    item = next(iter(result["stock_by_key"].values()))
    assert (item["initial_units"], item["remaining_units"], item["unit_name"], item["zone"]) == (9, 9, "короб", "heavy")
    assert result["diagnostics"]["merged_duplicate_stock_records"] == 1
    assert result["diagnostics"]["stock_metadata_conflicts"] == 1


def test_stock_key_is_deterministic_quantity_independent_and_collision_safe():
    a = next(iter(build_pick_working_stock(_index([_record(units=1)]))["stock_by_key"]))
    b = next(iter(build_pick_working_stock(_index([_record(units=99)]))["stock_by_key"]))
    collision = next(iter(build_pick_working_stock(_index([_record(cell="5,1", unit="короб")]))["stock_by_key"]))
    assert a == b and a != collision
    assert json.loads(a) == [SKU, "3|5|1", "короб"]


def test_indexes_hold_only_live_key_references_and_records_once():
    result = build_pick_working_stock(_index([_record(unit_quantities=[
        {"unit_name": "короб", "qty_units": 2}, {"unit_name": "штука", "qty_units": 3},
    ])]))
    stock_keys = set(result["stock_by_key"])
    sku_refs = {key for values in result["stock_keys_by_sku"].values() for key in values}
    cell_refs = {key for values in result["stock_keys_by_cell"].values() for key in values}
    assert stock_keys == sku_refs == cell_refs
    assert all(isinstance(key, str) for key in sku_refs | cell_refs)


def test_physical_index_sorting_is_numeric_and_top_level_is_deterministic():
    records = [
        _record("10", row_number="2", row_order=2),
        _record("2", row_number="2", row_order=2),
        _record("1", row_number="10", row_order=10),
    ]
    result = build_pick_working_stock(_index(list(reversed(records))))
    ordered = [result["stock_by_key"][key] for key in result["stock_keys_by_sku"][SKU]]
    assert [(item["row_number"], item["cell_number"]) for item in ordered] == [("2", "2"), ("2", "10"), ("10", "1")]
    assert list(result["stock_by_key"]) == sorted(result["stock_by_key"])


@pytest.mark.parametrize("value,reason", [(None, "inventory_index_not_dict"), ([], "inventory_index_not_dict"), ({}, "by_sku_not_dict"), ({"by_sku": []}, "by_sku_not_dict")])
def test_invalid_inputs_return_safe_empty_result(value, reason):
    result = build_pick_working_stock(value)
    assert result["stock_by_key"] == {}
    assert result["diagnostics"]["input_validation_reason"] == reason


def test_inputs_and_result_are_independent_in_both_directions():
    index = _index([_record(payload={"ignored": []})])
    before = copy.deepcopy(index)
    result = build_pick_working_stock(index)
    key = next(iter(result["stock_by_key"]))
    result["stock_by_key"][key]["remaining_units"] = 0
    assert index == before
    index["by_sku"][SKU][0]["unit_quantities"][0]["qty_units"] = 500
    index["by_sku"][SKU][0]["zone"] = "changed"
    assert result["stock_by_key"][key]["initial_units"] == 10
    assert result["stock_by_key"][key]["zone"] == "heavy"


def test_diagnostics_are_counts_only_and_totals_are_complete():
    result = build_pick_working_stock(_index([_record("1", units=2), _record("2", units=3)]))
    diagnostics = result["diagnostics"]
    assert (diagnostics["working_stock_record_count"], diagnostics["sku_count"], diagnostics["cell_count"], diagnostics["total_initial_units"]) == (2, 1, 2, 5)
    assert diagnostics["cells_with_working_stock"] == 2
    assert diagnostics["skus_with_working_stock"] == 1
    assert not any(isinstance(value, (list, dict)) for value in diagnostics.values())


def test_module_has_no_io_ui_cache_execution_or_persisted_writes():
    source = (Path(__file__).resolve().parents[1] / "warehouse_pick_working_stock.py").read_text(encoding="utf-8")
    imports = {alias.name for node in ast.walk(ast.parse(source)) if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names}
    assert "pandas" not in imports and "streamlit" not in imports
    assert all(token not in source for token in (
        "open(", ".read_text(", ".write_text(", "save_placement_state",
        "execute_outbound_orders", "update_data_revisions", "revision_token",
        "st.cache_data.clear",
    ))
