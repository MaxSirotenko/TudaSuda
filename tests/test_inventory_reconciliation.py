from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from warehouse_inventory_placement import reconcile_placements_with_inventory


def _placement(sku_key: str, cell_number: int, qty: float, characteristic: str = "A", receipt: str = "R1"):
    return {
        "placement_id": f"p-{sku_key}-{cell_number}",
        "sku_key": sku_key,
        "sku_code": sku_key.split("|")[0],
        "sku_name": "Товар",
        "item_name": "Товар",
        "characteristic_name": characteristic,
        "row_number": "1",
        "cell_number": str(cell_number),
        "tier": "1",
        "cell_key": f"1|{cell_number}|1",
        "qty_pallets": qty,
        "occupied_capacity_pallets": qty,
        "source": "receipt",
        "placement_mode": "calculated",
        "receipt_numbers": [receipt],
        "receipt_line_ids": [f"{receipt}-{cell_number}"],
    }


def _state(placements):
    return {"placements": placements, "unplaced_inventory": [], "journal": []}


def _inventory(sku_key: str, qty: float, characteristic: str = "A", receipt: str = "INV"):
    return {
        "sku_key": sku_key,
        "sku_name": "Товар",
        "item_name": "Товар",
        "characteristic_name": characteristic,
        "qty_pallets": qty,
        "receipt_number": receipt,
    }


def test_inventory_absent_sku_is_removed():
    state, report = reconcile_placements_with_inventory({}, _state([_placement("sku-a|A", 1, 2)]), [], inventory_date="2026-07-16")

    assert report["success"] is True
    assert state["placements"] == []
    assert report["details"][0]["Статус"] == "removed"


def test_matching_quantity_keeps_existing_cell_and_marks_carryover():
    state, report = reconcile_placements_with_inventory({}, _state([_placement("sku-a|A", 1, 2)]), [_inventory("sku-a|A", 2)], inventory_date="2026-07-16")

    assert report["details"][0]["Статус"] == "unchanged"
    assert state["placements"][0]["cell_key"] == "1|1|1"
    assert state["placements"][0]["source"] == "inventory_carryover"
    assert state["placements"][0]["placement_mode"] == "factual"
    assert state["placements"][0]["inventory_reconciled"] is True


def test_smaller_inventory_removes_last_cells_first():
    placements = [_placement("sku-a|A", 1, 1), _placement("sku-a|A", 2, 1), _placement("sku-a|A", 3, 1)]
    state, report = reconcile_placements_with_inventory({}, _state(placements), [_inventory("sku-a|A", 1)], inventory_date="2026-07-16")

    assert [p["cell_key"] for p in state["placements"]] == ["1|1|1"]
    assert report["details"][0]["Статус"] == "reduced"
    assert report["details"][0]["Освобождено ячеек"] == 2


def test_smaller_inventory_reduces_inside_last_cell():
    placements = [_placement("sku-a|A", 1, 1), _placement("sku-a|A", 2, 1)]
    state, _ = reconcile_placements_with_inventory({}, _state(placements), [_inventory("sku-a|A", 1.5)], inventory_date="2026-07-16")

    assert [(p["cell_key"], p["qty_pallets"]) for p in state["placements"]] == [("1|1|1", 1), ("1|2|1", 0.5)]


def test_inventory_greater_than_placements_goes_to_unplaced_inventory():
    state, report = reconcile_placements_with_inventory({}, _state([_placement("sku-a|A", 1, 1)]), [_inventory("sku-a|A", 2.5)], inventory_date="2026-07-16")

    assert report["details"][0]["Статус"] == "inventory_exceeds_placements"
    assert state["placements"][0]["qty_pallets"] == 1
    assert state["unplaced_inventory"][0]["qty_pallets"] == 1.5
    assert state["unplaced_inventory"][0]["reason"] == "inventory_quantity_exceeds_placements"


def test_multiple_inventory_rows_for_same_sku_are_summed():
    inventory = [_inventory("sku-a|A", 1), _inventory("sku-a|A", 2)]
    state, report = reconcile_placements_with_inventory({}, _state([_placement("sku-a|A", 1, 3)]), inventory, inventory_date="2026-07-16")

    assert report["summary"]["SKU в инвенте"] == 1
    assert state["placements"][0]["qty_pallets"] == 3
    assert report["details"][0]["Статус"] == "unchanged"


def test_different_characteristics_are_reconciled_separately():
    placements = [_placement("name:Товар|char_name:A", 1, 1, "A"), _placement("name:Товар|char_name:B", 2, 1, "B")]
    state, report = reconcile_placements_with_inventory({}, _state(placements), [_inventory("name:Товар|char_name:A", 1, "A")], inventory_date="2026-07-16")

    assert [p["sku_key"] for p in state["placements"]] == ["name:Товар|char_name:A"]
    statuses = {row["sku_key"]: row["Статус"] for row in report["details"]}
    assert statuses["name:Товар|char_name:B"] == "removed"


def test_receipt_numbers_do_not_participate_in_sku_key():
    placement = _placement("name:Товар|char_name:A", 1, 1, "A", receipt="B02022885")
    inventory = [_inventory("name:Товар|char_name:A", 1, "A", receipt="OTHER")]
    state, _ = reconcile_placements_with_inventory({}, _state([placement]), inventory, inventory_date="2026-07-16")

    assert len(state["placements"]) == 1
    assert state["placements"][0]["receipt_numbers"] == ["B02022885"]


def test_deep_lane_quantity_is_reduced_without_moving_cell():
    placement = _placement("sku-a|A", 1, 4)
    placement["storage_type"] = "deep_lane"
    placement["capacity_pallets"] = 4
    state, _ = reconcile_placements_with_inventory({}, _state([placement]), [_inventory("sku-a|A", 2.5)], inventory_date="2026-07-16")

    assert len(state["placements"]) == 1
    assert state["placements"][0]["cell_key"] == "1|1|1"
    assert state["placements"][0]["occupied_capacity_pallets"] == 2.5


def test_invalid_inventory_row_blocks_transaction():
    original = _state([_placement("sku-a|A", 1, 2)])
    state, report = reconcile_placements_with_inventory({}, original, [{"sku_key": "sku-a|A", "qty_pallets": "bad"}], inventory_date="2026-07-16")

    assert report["success"] is False
    assert report["details"][0]["Статус"] == "invalid_inventory_row"
    assert state == original


def test_reconciliation_does_not_delete_model_or_receipts_files(tmp_path):
    model_path = tmp_path / "warehouse_model.json"
    receipts_path = tmp_path / "receipts.json"
    placements_path = tmp_path / "placements.json"
    for path in (model_path, receipts_path, placements_path):
        path.write_text("{}", encoding="utf-8")

    reconcile_placements_with_inventory({}, _state([_placement("sku-a|A", 1, 1)]), [_inventory("sku-a|A", 1)], inventory_date="2026-07-16")

    assert model_path.exists()
    assert receipts_path.exists()
    assert placements_path.exists()


def test_all_remaining_placements_become_inventory_carryover():
    placements = [_placement("sku-a|A", 1, 1), _placement("sku-a|A", 2, 1)]
    state, _ = reconcile_placements_with_inventory({}, _state(placements), [_inventory("sku-a|A", 1.5)], inventory_date="2026-07-16")

    assert all(p["source"] == "inventory_carryover" for p in state["placements"])
    assert all(p["placement_mode"] == "factual" for p in state["placements"])
    assert state["last_inventory_reconciliation"]["quantity_after"] == 1.5
