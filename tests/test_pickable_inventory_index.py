from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from warehouse_inventory_placement import make_sku_key
from warehouse_pick_inventory import build_pickable_inventory_index


def _cell(number="1", **extra):
    return {
        "row_number": "1", "cell_number": str(number), "tier": "1",
        "row_order": 1, "weight_zone": "heavy", "storage_type": "normal",
        "capacity_pallets": 2, "x_center": 1.5, "y_center": float(number), **extra,
    }


def _model(cells=None, **extra):
    return {"model_id": "model-1", "source_file_hash": "hash-1", "cells": cells or [], **extra}


def _placement(cell="1", characteristic="Red", **extra):
    item = {
        "sku_code": "SKU-1", "sku_name": "Product", "characteristic_name": characteristic,
        "cell_key": f"1|{cell}|1", "quantity": 3, "occupied_capacity_pallets": 1,
        "calculated_zone": "light", "source": "inventory",
    }
    item["sku_key"] = make_sku_key(item)
    item.update(extra)
    return item


def test_empty_inputs_return_empty_serializable_indexes():
    result = build_pickable_inventory_index({}, {})
    assert result["by_sku"] == result["by_cell"] == {}
    assert result["diagnostics"]["placements_total"] == 0
    json.dumps(result)


def test_one_sku_uses_geometry_metadata_and_model_zone():
    result = build_pickable_inventory_index(_model([_cell()]), {"placements": [_placement()]})
    record = next(iter(result["by_sku"].values()))[0]
    assert record == next(iter(result["by_cell"].values()))[0]
    assert (record["zone"], record["storage_type"], record["capacity_pallets"]) == ("heavy", "normal", 2.0)
    assert (record["x_center"], record["y_center"]) == (1.5, 1.0)


def test_one_sku_in_multiple_cells_is_sorted_numerically():
    model = _model([_cell("10"), _cell("2")])
    state = {"placements": [_placement("10"), _placement("2")]}
    records = next(iter(build_pickable_inventory_index(model, state)["by_sku"].values()))
    assert [record["cell_number"] for record in records] == ["2", "10"]


def test_characteristics_form_distinct_canonical_skus_without_stored_key():
    red = _placement(characteristic="Red")
    blue = _placement(characteristic="Blue")
    red.pop("sku_key")
    blue.pop("sku_key")
    result = build_pickable_inventory_index(_model([_cell()]), {"placements": [red, blue]})
    assert set(result["by_sku"]) == {"code:SKU-1|char_name:Red", "code:SKU-1|char_name:Blue"}


def test_duplicates_sum_stock_fields_separately_and_report_metadata_conflict():
    first = _placement(quantity="2.5", occupied_capacity_pallets="1,25", sku_name="First")
    second = _placement(quantity=4, occupied_capacity_pallets=2, sku_name="Second")
    result = build_pickable_inventory_index(_model([_cell()]), {"placements": [first, second]})
    record = next(iter(result["by_sku"].values()))[0]
    assert (record["quantity"], record["occupied_capacity_pallets"], record["sku_name"]) == (6.5, 3.25, "First")
    assert result["diagnostics"]["merged_duplicate_records"] == 1
    assert result["diagnostics"]["sku_metadata_conflicts"] == 1


@pytest.mark.parametrize("raw", [5, 5.0, "5", "5,0"])
def test_positive_unit_quantity_is_normalized_and_can_be_the_only_stock(raw):
    placement = _placement(quantity=0, occupied_capacity_pallets=0, qty_units=raw, unit_name="короб")
    result = build_pickable_inventory_index(_model([_cell()]), {"placements": [placement]})
    record = next(iter(result["by_sku"].values()))[0]
    assert record["qty_units"] == 5
    assert isinstance(record["qty_units"], int)
    assert record["unit_name"] == "короб"
    assert record["unit_quantities"] == [{"unit_name": "короб", "qty_units": 5}]
    assert result["diagnostics"]["placements_with_positive_qty_units"] == 1


@pytest.mark.parametrize("raw", [1.5, -1, "bad", float("nan"), float("inf")])
def test_invalid_unit_quantity_does_not_create_unit_stock(raw):
    result = build_pickable_inventory_index(
        _model([_cell()]), {"placements": [_placement(qty_units=raw, unit_name="короб")]}
    )
    record = next(iter(result["by_sku"].values()))[0]
    assert record["qty_units"] is None
    assert record["unit_name"] == ""
    assert record["unit_quantities"] == []
    assert result["diagnostics"]["invalid_qty_units"] == 1


def test_zero_unit_quantity_is_not_stock_and_is_diagnosed():
    result = build_pickable_inventory_index(
        _model([_cell()]), {"placements": [_placement(qty_units=0, unit_name="короб")]}
    )
    record = next(iter(result["by_sku"].values()))[0]
    assert record["unit_quantities"] == []
    assert result["diagnostics"]["non_positive_qty_units"] == 1


def test_missing_unit_name_is_preserved_as_an_unknown_variant():
    result = build_pickable_inventory_index(
        _model([_cell()]), {"placements": [_placement(qty_units=5, unit_name="  ")]}
    )
    record = next(iter(result["by_sku"].values()))[0]
    assert record["unit_quantities"] == [{"unit_name": "", "qty_units": 5}]
    assert (record["qty_units"], record["unit_name"]) == (5, "")
    assert result["diagnostics"]["placements_missing_unit_name"] == 1


def test_duplicate_units_are_grouped_without_mixing_other_stock_fields():
    placements = [
        _placement(quantity=2, occupied_capacity_pallets=1, qty_units=3, unit_name=" Короб "),
        _placement(quantity=4, occupied_capacity_pallets=2, qty_units=2, unit_name="КОРОБ"),
    ]
    result = build_pickable_inventory_index(_model([_cell()]), {"placements": placements})
    record = next(iter(result["by_sku"].values()))[0]
    assert (record["quantity"], record["occupied_capacity_pallets"]) == (6.0, 3.0)
    assert (record["qty_units"], record["unit_name"]) == (5, "Короб")
    assert record["unit_quantities"] == [{"unit_name": "Короб", "qty_units": 5}]


def test_distinct_units_are_kept_sorted_and_make_scalar_ambiguous():
    placements = [
        _placement(qty_units=5, unit_name=""),
        _placement(qty_units=4, unit_name="штука"),
        _placement(qty_units=3, unit_name="короб"),
    ]
    result = build_pickable_inventory_index(_model([_cell()]), {"placements": placements})
    sku_record = next(iter(result["by_sku"].values()))[0]
    cell_record = next(iter(result["by_cell"].values()))[0]
    assert sku_record == cell_record
    assert (sku_record["qty_units"], sku_record["unit_name"]) == (None, "")
    assert sku_record["unit_quantities"] == [
        {"unit_name": "короб", "qty_units": 3},
        {"unit_name": "штука", "qty_units": 4},
        {"unit_name": "", "qty_units": 5},
    ]
    diagnostics = result["diagnostics"]
    assert (
        diagnostics["unit_variants_in_same_cell"],
        diagnostics["indexed_unit_variants"],
        diagnostics["cells_with_unit_stock"],
        diagnostics["skus_with_unit_stock"],
    ) == (1, 3, 1, 1)


@pytest.mark.parametrize("quantity,occupied", [(0, 0), (-1, -2), ("", "bad")])
def test_non_positive_and_invalid_stock_is_skipped(quantity, occupied):
    result = build_pickable_inventory_index(
        _model([_cell()]),
        {"placements": [_placement(quantity=quantity, occupied_capacity_pallets=occupied)]},
    )
    assert result["by_sku"] == {}
    assert result["diagnostics"]["skipped_non_positive_stock"] == 1


def test_unknown_and_missing_cells_and_missing_sku_are_diagnosed():
    missing_cell = _placement()
    missing_cell.pop("cell_key")
    missing_sku = _placement(sku_key="", sku_code="", sku_name="", characteristic_name="")
    result = build_pickable_inventory_index(
        _model([_cell()]), {"placements": [_placement("99"), missing_cell, missing_sku]}
    )
    diagnostics = result["diagnostics"]
    assert diagnostics["skipped_unknown_cell"] == 1
    assert diagnostics["skipped_missing_cell_key"] == 1
    assert diagnostics["skipped_missing_sku"] == 1


def test_calculated_zone_is_only_a_fallback():
    cell = _cell()
    cell["weight_zone"] = ""
    record = next(iter(build_pickable_inventory_index(_model([cell]), {"placements": [_placement()]})["by_sku"].values()))[0]
    assert record["zone"] == "light"


@pytest.mark.parametrize("field", ["model_id", "source_file_hash"])
def test_explicitly_incompatible_state_raises(field):
    state = {field: "different", "placements": []}
    with pytest.raises(ValueError, match=field):
        build_pickable_inventory_index(_model(), state)


def test_missing_compatibility_identifier_is_allowed():
    assert build_pickable_inventory_index(_model(), {"placements": []})["model_id"] == "model-1"


def test_inputs_are_not_modified():
    model = _model([_cell()])
    state = {"placements": [_placement()]}
    before = copy.deepcopy((model, state))
    build_pickable_inventory_index(model, state)
    assert (model, state) == before


def test_module_has_no_io_streamlit_cache_or_persisted_write_calls():
    path = Path(__file__).resolve().parents[1] / "warehouse_pick_inventory.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "streamlit" not in imports
    assert all(token not in source for token in (
        "open(", ".read_text(", ".write_text(", "save_placement_state",
        "update_data_revisions", "st.cache_data.clear",
    ))
