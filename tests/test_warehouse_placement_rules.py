import copy
import json
import math

import pytest

from warehouse_placement_rules import (
    PLACEMENT_RULESET_VERSION,
    build_placement_rule_set,
    compute_placement_rule_set_id,
    get_enabled_rule_ids,
    validate_placement_rule_set,
)


RULE_IDS = {
    "weight_zones", "adjacency", "velocity", "picking_storage",
    "replenishment", "reserve_capacity", "deep_lane_optimization",
    "base_sku_capacity", "demand_forecast", "receipt_forecast",
    "demand_spikes", "sku_exceptions",
}


def build(config=None):
    rule_set, validation = build_placement_rule_set(config)
    return rule_set, validation


def error_codes(validation):
    return [error["code"] for error in validation["errors"]]


def test_empty_config_is_valid_all_off_physical_invariants_remain_on():
    rule_set, validation = build()
    assert validation == {"valid": True, "errors": []}
    assert rule_set["version"] == PLACEMENT_RULESET_VERSION == 1
    assert {rule["rule_id"] for rule in rule_set["rules"]} == RULE_IDS
    assert not any(rule["enabled"] for rule in rule_set["rules"])
    assert rule_set["enabled_rule_ids"] == []
    assert rule_set["enabled_rule_count"] == 0
    assert rule_set["disabled_rule_count"] == 12
    assert rule_set["physical_constraints_always_enabled"] is True
    assert rule_set["placement_rule_set_id"].startswith("sha256:")


def test_partial_config_fills_defaults_and_reports_enabled_rules():
    rule_set, validation = build({"weight_zones": {"enabled": True}})
    assert validation["valid"]
    assert get_enabled_rule_ids(rule_set) == ["weight_zones"]
    assert rule_set["enabled_rule_ids"] == ["weight_zones"]


def test_input_order_is_not_identity_or_business_content():
    first, _ = build({"adjacency": {"enabled": True}, "weight_zones": {"enabled": True}})
    second, _ = build({"weight_zones": {"enabled": True}, "adjacency": {"enabled": True}})
    assert first == second
    assert first["placement_rule_set_id"] == second["placement_rule_set_id"]


def test_toggle_changes_identity_and_toggle_back_restores_it():
    default, _ = build()
    enabled, _ = build({"weight_zones": True})
    disabled, _ = build({"weight_zones": False})
    assert enabled["placement_rule_set_id"] != default["placement_rule_set_id"]
    assert disabled["placement_rule_set_id"] == default["placement_rule_set_id"]


def test_parameters_are_full_identity_even_when_rule_is_disabled():
    enabled_30, _ = build({"reserve_capacity": {"enabled": True, "parameters": {"reserve_percent": 30}}})
    enabled_20, _ = build({"reserve_capacity": {"enabled": True, "parameters": {"reserve_percent": 20}}})
    disabled_30, _ = build({"reserve_capacity": {"parameters": {"reserve_percent": 30}}})
    disabled_20, _ = build({"reserve_capacity": {"parameters": {"reserve_percent": 20}}})
    assert enabled_30["placement_rule_set_id"] != enabled_20["placement_rule_set_id"]
    assert disabled_30["placement_rule_set_id"] != disabled_20["placement_rule_set_id"]


def test_replenishment_dependency():
    _, invalid = build({"replenishment": True})
    valid_rule_set, valid = build({"replenishment": True, "picking_storage": True})
    assert "replenishment_requires_picking_storage" in error_codes(invalid)
    assert valid["valid"]
    assert valid_rule_set["enabled_rule_ids"] == ["picking_storage", "replenishment"]


def test_unknown_rule_is_not_ignored():
    rule_set, validation = build({"magic_optimizer": True})
    assert not validation["valid"]
    assert "unknown_placement_rule" in error_codes(validation)
    assert "placement_rule_set_id" not in rule_set


@pytest.mark.parametrize("value", [-1, 100, math.nan, math.inf, True])
def test_invalid_reserve_percent(value):
    _, validation = build({"reserve_capacity": {"parameters": {"reserve_percent": value}}})
    assert "invalid_reserve_percent" in error_codes(validation)


@pytest.mark.parametrize("value", [0, 1.5, True])
def test_invalid_minimum_positions(value):
    _, validation = build({"base_sku_capacity": {"parameters": {"minimum_positions_per_sku": value}}})
    assert "invalid_minimum_positions_per_sku" in error_codes(validation)


@pytest.mark.parametrize("value", [1, 2])
def test_valid_minimum_positions(value):
    rule_set, validation = build({"base_sku_capacity": {"parameters": {"minimum_positions_per_sku": value}}})
    assert validation["valid"]
    assert compute_placement_rule_set_id(rule_set) == rule_set["placement_rule_set_id"]


def test_json_serializable_no_input_mutation_and_repeatable():
    config = {"reserve_capacity": {"enabled": True, "parameters": {"reserve_percent": 25}}}
    before = copy.deepcopy(config)
    first, first_validation = build(config)
    second, second_validation = build(config)
    assert config == before
    assert first == second
    assert first_validation == second_validation
    json.dumps(first, ensure_ascii=False, allow_nan=False)


def test_validation_rejects_unknown_rule_in_complete_contract():
    rule_set, _ = build()
    rule_set["rules"].append({"rule_id": "magic_optimizer", "enabled": True, "parameters": {}})
    validation = validate_placement_rule_set(rule_set)
    assert "unknown_placement_rule" in error_codes(validation)
