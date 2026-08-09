from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from warehouse_business_identity import canonical_sku_key
from warehouse_outbound_orders import detect_outbound_columns, normalize_outbound_table
from warehouse_pick_demands import build_outbound_pick_demands
from warehouse_placement_zones import ASSIGNABLE_PLACEMENT_ZONE_IDS
from warehouse_receipts import calculate_receipt_zones, normalize_receipt_table
from warehouse_scenario_comparison_ui import build_sku_zone_rows


def _outbound(pick: object, line: int, *, source: int, name: str = "A") -> dict:
    return {"outbound_order_number": "RO", "created_at": "2026-08-01", "warehouse": "W",
            "order_key": "order", "nomenclature": name, "characteristic": "x",
            "qty_units": 1, "unit_name": "короб", "quantity_validation_reason": "",
            "pick_order": pick, "line_number": line, "source_index": source,
            "route_sequence_authoritative": pick is not None}


def _receipt(weight: float | str, *, name: str = "A", zone: str = "") -> dict:
    return {"sku_name": name, "characteristic_name": "x", "source_weight": weight,
            "weight_parse_status": "ok" if weight != "" else "empty", "source_zone": zone,
            "qty_pallets": 1, "receipt_number": "R"}


def test_query_carries_factual_pick_order_and_orders_by_it():
    query = Path("queries_1c/mass_outbound_orders.query").read_text(encoding="utf-8")
    assert query.count("ПорядокСборки") >= 5
    tail = query.split("УПОРЯДОЧИТЬ ПО", 1)[1]
    assert tail.index("ДатаРО") < tail.index("НомерРО") < tail.index("ПорядокСборки") < tail.index("НомерСтроки")


def test_parser_preserves_pick_order_and_line_number_and_diagnoses_invalid():
    table = pd.DataFrame([{"НомерРО": "RO", "ДатаРО": "2026-08-01", "Склад": "W",
                           "Номенклатура": "A", "Количество": 1, "Единица": "короб",
                           "ПорядокСборки": 7, "НомерСтроки": 4}])
    rows, diagnostics = normalize_outbound_table(table, detect_outbound_columns(table))
    assert (rows[0]["pick_order"], rows[0]["line_number"]) == (7, 4)
    assert diagnostics == []
    table["ПорядокСборки"] = "bad"
    rows, diagnostics = normalize_outbound_table(table, detect_outbound_columns(table))
    assert rows[0]["pick_order"] is None and not rows[0]["route_sequence_authoritative"]
    assert [item["reason"] for item in diagnostics] == ["pick_order_invalid"]


def test_same_sku_lines_remain_separate_and_permutation_is_stable():
    rows = [_outbound(2, 20, source=1), _outbound(1, 10, source=2)]
    first = build_outbound_pick_demands(rows)
    second = build_outbound_pick_demands(list(reversed(rows)))
    reindexed = build_outbound_pick_demands([_outbound(1, 10, source=1), _outbound(2, 20, source=2)])
    assert len(first["orders"][0]["demands"]) == 2
    assert [d["pick_order"] for d in first["orders"][0]["demands"]] == [1, 2]
    assert first == second
    assert first["outbound_demand_state_id"] == second["outbound_demand_state_id"]
    assert first["outbound_demand_state_id"] == reindexed["outbound_demand_state_id"]
    assert first["readiness"]["route_sequence_authoritative"]


def test_missing_and_conflicting_pick_order_are_not_authoritative():
    missing = build_outbound_pick_demands([_outbound(None, 1, source=1)])
    assert not missing["readiness"]["route_sequence_authoritative"]
    assert missing["readiness"]["reasons"] == {"pick_order_missing": 1}
    conflict = build_outbound_pick_demands([_outbound(1, 1, source=1), _outbound(2, 1, source=2, name="B")])
    assert conflict["readiness"]["reasons"]["pick_order_conflict"] == 1


def test_receipt_normalization_uses_v2_key():
    table = pd.DataFrame([{"Код": "legacy-code", "Наименование": "A", "Паллеты": 1,
                           "Характеристика": "x"}])
    from warehouse_receipts import detect_receipt_columns
    rows, _, _ = normalize_receipt_table(table, detect_receipt_columns(table))
    assert rows.iloc[0]["sku_key"] == canonical_sku_key({"nomenclature": "A", "characteristic": "x"})


@pytest.mark.parametrize(("weight", "zone"), [(0.003, "small_and_bulky"), (0.250, "small_and_bulky"),
    (0.251, "bulky"), (0.999, "bulky"), (1.000, "fragile"), (2.500, "fragile"),
    (2.501, "light"), (3.500, "light"), (3.501, "unassigned"), (5.0, "unassigned"),
    (15.0, "unassigned")])
def test_only_confirmed_weight_boundaries_resolve(weight: float, zone: str):
    rows, _ = calculate_receipt_zones([_receipt(weight)], {})
    assert rows[0]["calculated_zone"] == zone


def test_zone_adapter_recanonicalizes_legacy_metadata_supports_all_zones_and_rejects_conflict():
    evidence = [{"sku_key": "legacy", "sku_name": zone, "characteristic_name": "x",
                 "calculated_zone": zone} for zone in ASSIGNABLE_PLACEMENT_ZONE_IDS]
    rows = build_sku_zone_rows(evidence)
    assert {row["target_zone"] for row in rows} == set(ASSIGNABLE_PLACEMENT_ZONE_IDS)
    conflict = build_sku_zone_rows([
        {"sku_key": "old-1", "sku_name": "A", "characteristic_name": "x", "calculated_zone": "light"},
        {"sku_key": "old-2", "sku_name": "A", "characteristic_name": "x", "calculated_zone": "heavy"},
    ])
    assert conflict == []
