from warehouse_workspace_ui import (
    WORKSPACE_TABS, SUPPORTED_RULES, UNSUPPORTED_RULES, RULE_CARDS,
    build_warehouse_zone_summary, build_workspace_rule_config, normalize_rule_selection,
)


def test_five_business_workspace_tabs_are_fixed():
    assert WORKSPACE_TABS == ("Склад", "Данные", "Условия модели", "CURRENT / PROPOSED", "Аналитика")


def test_only_supported_rules_are_active_and_dependency_is_deterministic():
    assert set(SUPPORTED_RULES).isdisjoint(UNSUPPORTED_RULES)
    a = normalize_rule_selection({"replenishment": True, "picking_storage": False})
    b = normalize_rule_selection({"picking_storage": False, "replenishment": True})
    assert a == b and not a["replenishment"]
    assert build_workspace_rule_config({"picking_storage": True, "replenishment": True})["replenishment"]["enabled"]


def test_rule_contract_has_only_real_parameter_and_exact_adjacency_copy():
    config = build_workspace_rule_config({"base_sku_capacity": True}, 2)
    assert config["base_sku_capacity"]["parameters"] == {"minimum_positions_per_sku": 2}
    assert config["deep_lane_optimization"] == {"enabled": False}
    assert RULE_CARDS["adjacency"][1] == "Разная номенклатура с одинаковой непустой характеристикой не размещается в соседних ячейках."


def test_zone_summary_uses_canonical_zone_ids_and_physical_capacity():
    model = {"cells": [
        {"row_number": 1, "weight_zone": "heavy", "storage_type": "normal", "capacity_pallets": 1},
        {"row_number": 2, "weight_zone": "medium_light", "storage_type": "deep_lane", "capacity_pallets": 4},
        {"row_number": 3, "weight_zone": "unknown", "storage_type": "normal", "capacity_pallets": 1},
    ]}
    rows = build_warehouse_zone_summary(model)
    assert [row["ID зоны"] for row in rows] == ["heavy", "medium_light", "unassigned"]
    assert rows[1]["Количество ячеек"] == 1 and rows[1]["Deep lane"] == 4
