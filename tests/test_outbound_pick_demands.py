from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from warehouse_inventory_placement import make_sku_key
from warehouse_outbound_orders import outbound_order_key
from warehouse_pick_demands import build_outbound_pick_demands


def _row(number="РО-1", quantity=2, *, index=1, characteristic="Белая", unit="короб", **extra):
    row = {
        "outbound_order_number": number,
        "created_at": "2026-08-01T10:00:00",
        "nomenclature": "Капуста",
        "characteristic": characteristic,
        "sku_key": f"name:Капуста|char_name:{characteristic}",
        "qty_units": quantity,
        "quantity_validation_reason": "",
        "unit_name": unit,
        "warehouse": "Вешки",
        "source_index": index,
        "order_key": f"key-{number}",
    }
    row.update(extra)
    return row


def test_empty_input_is_serializable():
    result = build_outbound_pick_demands([])
    assert result["orders"] == []
    assert result["diagnostics"]["rows_total"] == 0
    assert result["diagnostics"]["quantity_reason_counts"] == {}
    json.dumps(result)


def test_one_valid_line_preserves_contract():
    result = build_outbound_pick_demands([_row()])
    order = result["orders"][0]
    demand = order["demands"][0]
    assert (order["order_key"], order["outbound_order_number"], order["warehouse"]) == ("key-РО-1", "РО-1", "Вешки")
    assert (demand["sku_name"], demand["characteristic_name"], demand["requested_units"]) == ("Капуста", "Белая", 2)
    assert (order["source_indexes"], demand["source_indexes"]) == ([1], [1])
    assert result["diagnostics"]["orders_count"] == result["diagnostics"]["demands_count"] == 1


def test_orders_group_by_key_and_sort_deterministically():
    rows = [
        _row("same", index=9, order_key="b", created_at="2026-08-02"),
        _row("same", index=3, order_key="a", created_at="2026-08-01"),
    ]
    assert [order["order_key"] for order in build_outbound_pick_demands(rows)["orders"]] == ["a", "b"]


def test_duplicates_merge_quantities_units_and_sorted_sources():
    rows = [_row(quantity=3, index=8, unit=" Короб "), _row(quantity=2, index=2, unit="КОРОБ")]
    result = build_outbound_pick_demands(rows)
    demand = result["orders"][0]["demands"][0]
    assert (demand["requested_units"], demand["unit_name"], demand["source_indexes"]) == (5, "короб", [2, 8])
    assert result["diagnostics"]["merged_duplicate_lines"] == 1


def test_characteristics_and_unit_variants_are_separate_and_sorted_by_source():
    rows = [
        _row(index=3, characteristic="Белая", unit="штука"),
        _row(index=2, characteristic="Красная", unit="короб"),
        _row(index=1, characteristic="Белая", unit=""),
    ]
    result = build_outbound_pick_demands(rows)
    demands = result["orders"][0]["demands"]
    assert [(item["characteristic_name"], item["unit_name"]) for item in demands] == [("Красная", "короб")]
    assert result["diagnostics"]["unsupported_unit"] == 2


def test_existing_legacy_keys_are_replaced_and_diagnosed():
    existing = _row(order_key="custom-order", sku_key="custom-sku")
    fallback = _row("РО-2", index=2, order_key="", sku_key="", sku_code="SKU", characteristic_code="WHITE")
    result = build_outbound_pick_demands([existing, fallback])
    expected_canonical = make_sku_key({"sku_name": "Капуста", "characteristic_name": "Белая"})
    assert result["orders"][0]["demands"][0]["sku_key"] == expected_canonical
    expected_order = outbound_order_key("Вешки", "РО-2", "2026-08-01T10:00:00")
    expected_sku = make_sku_key({"sku_code": "SKU", "sku_name": "Капуста", "characteristic_code": "WHITE", "characteristic_name": "Белая"})
    assert result["orders"][1]["order_key"] == expected_order
    assert result["orders"][1]["demands"][0]["sku_key"] == expected_sku
    assert result["diagnostics"]["legacy_sku_key_mismatch"] == 1


def test_missing_order_and_sku_are_skipped():
    missing_order = _row(order_key="", outbound_order_number="")
    missing_sku = _row(sku_key="", nomenclature="", characteristic="")
    diagnostics = build_outbound_pick_demands([missing_order, missing_sku])["diagnostics"]
    assert diagnostics["skipped_missing_order"] == 1
    assert diagnostics["skipped_missing_sku"] == 1
    assert diagnostics["orders_without_valid_demands"] == 1


@pytest.mark.parametrize("quantity,reason", [(-1, "quantity_negative"), (5.0, "quantity_not_integer"), (1.5, "quantity_fractional"), ("5", "quantity_not_integer"), (True, "quantity_boolean"), (None, "quantity_missing")])
def test_invalid_quantities_are_strictly_rejected(quantity, reason):
    result = build_outbound_pick_demands([_row(quantity=quantity, qty_units_raw="7")])
    assert result["orders"] == []
    assert result["diagnostics"]["skipped_invalid_quantity"] == 1
    assert result["diagnostics"]["quantity_reason_counts"] == {reason: 1}


def test_zero_and_validation_reason_are_diagnosed_separately():
    result = build_outbound_pick_demands([
        _row(quantity=0),
        _row(index=2, quantity=3, quantity_validation_reason="quantity_fractional"),
        _row(index=3, quantity=4),
    ])
    assert result["orders"][0]["demands"][0]["requested_units"] == 4
    assert result["diagnostics"]["skipped_non_positive_quantity"] == 1
    assert result["diagnostics"]["quantity_reason_counts"] == {"quantity_fractional": 1}


def test_metadata_conflicts_keep_first_nonempty_values_and_other_warehouse():
    rows = [
        _row(warehouse="Другой", sku_name="First"),
        _row(index=2, warehouse="Третий", outbound_order_number="РО-X", sku_name="Second"),
    ]
    result = build_outbound_pick_demands(rows)
    order = result["orders"][0]
    assert (order["warehouse"], order["outbound_order_number"], order["demands"][0]["sku_name"]) == ("Другой", "РО-1", "First")
    assert result["diagnostics"]["order_metadata_conflicts"] == 2
    assert result["diagnostics"]["sku_metadata_conflicts"] == 0


def test_demand_key_is_stable_quantity_independent_and_collision_safe():
    first = build_outbound_pick_demands([_row(quantity=2, order_key="a,b", sku_key="c")])["orders"][0]["demands"][0]["demand_key"]
    second = build_outbound_pick_demands([_row(quantity=99, order_key="a,b", sku_key="c")])["orders"][0]["demands"][0]["demand_key"]
    collision = build_outbound_pick_demands([_row(order_key="a", sku_key="b,c")])["orders"][0]["demands"][0]["demand_key"]
    assert first == second
    assert first != collision
    assert json.loads(first) == ["a,b", make_sku_key({"sku_name": "Капуста", "characteristic_name": "Белая"}), "короб"]


def test_input_list_rows_and_nested_values_are_not_modified():
    rows = [_row(payload={"nested": [1, 2]})]
    before = copy.deepcopy(rows)
    build_outbound_pick_demands(rows)
    assert rows == before


def test_module_has_no_io_ui_cache_or_persisted_write_calls():
    source = (Path(__file__).resolve().parents[1] / "warehouse_pick_demands.py").read_text(encoding="utf-8")
    imports = {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "pandas" not in imports and "streamlit" not in imports
    assert all(token not in source for token in (
        "open(", ".read_text(", ".write_text(", "save_outbound_orders",
        "execute_outbound_orders", "save_placement_state", "update_data_revisions",
        "st.cache_data.clear",
    ))
