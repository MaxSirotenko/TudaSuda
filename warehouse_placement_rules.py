"""Deterministic, declarative placement rules for the PROPOSED scenario.

Physical warehouse invariants are always enforced and are not rules that can be
disabled.  This module does not place stock.  A future optimizer must evaluate
the complete rule set against the original baseline state, rather than applying
rules sequentially or modifying a previous PROPOSED result.  PlacementRuleSet
is never applied to the factual CURRENT scenario.

The identifier describes the full configuration, so parameters participate in
identity even while their rule is disabled.  An effective-rules identifier is
intentionally outside this contract.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

PLACEMENT_RULESET_VERSION = 1

RULE_DISPLAY_LABELS = {
    "weight_zones": "Весовые зоны",
    "adjacency": "Товарное соседство",
    "velocity": "Оборачиваемость / частота отбора",
    "picking_storage": "Комплектация / хранение",
    "replenishment": "Пополнение комплектации",
    "reserve_capacity": "Резерв свободной ёмкости",
    "deep_lane_optimization": "Оптимизация deep lane",
    "base_sku_capacity": "Базовое место для SKU",
    "demand_forecast": "Прогноз расхода",
    "receipt_forecast": "Прогноз прихода",
    "demand_spikes": "Акции / сезонные всплески",
    "sku_exceptions": "Специальные правила SKU",
}

_DEFAULT_PARAMETERS: dict[str, dict[str, Any]] = {
    rule_id: {} for rule_id in RULE_DISPLAY_LABELS
}
_DEFAULT_PARAMETERS["reserve_capacity"] = {"reserve_percent": 30.0}
_DEFAULT_PARAMETERS["base_sku_capacity"] = {"minimum_positions_per_sku": 1}
_RULE_IDS = tuple(sorted(RULE_DISPLAY_LABELS))


def _error(code: str, **details: Any) -> dict[str, Any]:
    return {"code": code, **details}


def validate_placement_rule_set(rule_set: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic validation diagnostics for a complete rule set."""
    errors: list[dict[str, Any]] = []
    if not isinstance(rule_set, Mapping):
        return {"valid": False, "errors": [_error("invalid_placement_rule_set")]}
    if rule_set.get("version") != PLACEMENT_RULESET_VERSION:
        errors.append(_error("unsupported_placement_rule_set_version"))

    rules = rule_set.get("rules")
    if not isinstance(rules, list):
        return {"valid": False, "errors": errors + [_error("invalid_placement_rules")]}

    by_id: dict[str, Mapping[str, Any]] = {}
    for rule in rules:
        if not isinstance(rule, Mapping) or not isinstance(rule.get("rule_id"), str):
            errors.append(_error("invalid_placement_rule"))
            continue
        rule_id = rule["rule_id"]
        if rule_id not in _DEFAULT_PARAMETERS:
            errors.append(_error("unknown_placement_rule", rule_id=rule_id))
            continue
        if rule_id in by_id:
            errors.append(_error("duplicate_placement_rule", rule_id=rule_id))
            continue
        by_id[rule_id] = rule
        if not isinstance(rule.get("enabled"), bool):
            errors.append(_error("invalid_rule_enabled", rule_id=rule_id))
        parameters = rule.get("parameters")
        if not isinstance(parameters, Mapping):
            errors.append(_error("invalid_rule_parameters", rule_id=rule_id))
            continue
        unknown = sorted(set(parameters) - set(_DEFAULT_PARAMETERS[rule_id]))
        for parameter in unknown:
            errors.append(_error("unknown_rule_parameter", rule_id=rule_id, parameter=parameter))

    for rule_id in _RULE_IDS:
        if rule_id not in by_id:
            errors.append(_error("missing_placement_rule", rule_id=rule_id))

    reserve = by_id.get("reserve_capacity", {}).get("parameters", {})
    if isinstance(reserve, Mapping):
        value = reserve.get("reserve_percent")
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0 <= value < 100):
            errors.append(_error("invalid_reserve_percent"))

    base = by_id.get("base_sku_capacity", {}).get("parameters", {})
    if isinstance(base, Mapping):
        value = base.get("minimum_positions_per_sku")
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            errors.append(_error("invalid_minimum_positions_per_sku"))

    if (by_id.get("replenishment", {}).get("enabled") is True
            and by_id.get("picking_storage", {}).get("enabled") is not True):
        errors.append(_error("replenishment_requires_picking_storage"))
    return {"valid": not errors, "errors": errors}


def _identity_payload(rule_set: Mapping[str, Any]) -> dict[str, Any]:
    rules = sorted(rule_set["rules"], key=lambda rule: rule["rule_id"])
    return {
        "version": rule_set["version"],
        "rules": [
            {"rule_id": rule["rule_id"], "enabled": rule["enabled"],
             "parameters": rule["parameters"]}
            for rule in rules
        ],
    }


def compute_placement_rule_set_id(rule_set: dict[str, Any]) -> str:
    """Compute a canonical SHA-256 identity from business fields only."""
    validation = validate_placement_rule_set(rule_set)
    if not validation["valid"]:
        raise ValueError(validation["errors"])
    canonical = json.dumps(
        _identity_payload(rule_set), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def get_enabled_rule_ids(rule_set: dict[str, Any]) -> list[str]:
    """Return enabled rule IDs in deterministic lexical order."""
    return sorted(rule["rule_id"] for rule in rule_set["rules"] if rule["enabled"])


def build_placement_rule_set(
    config: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a complete all-off-by-default rule set without mutating config."""
    source = {} if config is None else deepcopy(config)
    build_errors: list[dict[str, Any]] = []
    if not isinstance(source, Mapping):
        source = {}
        build_errors.append(_error("invalid_placement_rule_config"))

    for rule_id in sorted(set(source) - set(_RULE_IDS)):
        build_errors.append(_error("unknown_placement_rule", rule_id=rule_id))

    rules: list[dict[str, Any]] = []
    for rule_id in _RULE_IDS:
        supplied = source.get(rule_id, {})
        if isinstance(supplied, bool):
            supplied = {"enabled": supplied}
        if not isinstance(supplied, Mapping):
            build_errors.append(_error("invalid_placement_rule", rule_id=rule_id))
            supplied = {}
        unknown_fields = sorted(set(supplied) - {"enabled", "parameters"})
        for field in unknown_fields:
            build_errors.append(_error("unknown_rule_field", rule_id=rule_id, field=field))
        parameters = deepcopy(_DEFAULT_PARAMETERS[rule_id])
        supplied_parameters = supplied.get("parameters", {})
        if isinstance(supplied_parameters, Mapping):
            parameters.update(deepcopy(dict(supplied_parameters)))
        else:
            build_errors.append(_error("invalid_rule_parameters", rule_id=rule_id))
        if rule_id == "reserve_capacity":
            reserve_percent = parameters.get("reserve_percent")
            if (not isinstance(reserve_percent, bool)
                    and isinstance(reserve_percent, (int, float))
                    and math.isfinite(reserve_percent)):
                parameters["reserve_percent"] = float(reserve_percent)
        rules.append({
            "rule_id": rule_id,
            "enabled": supplied.get("enabled", False),
            "parameters": parameters,
        })

    rule_set: dict[str, Any] = {
        "version": PLACEMENT_RULESET_VERSION,
        "rules": rules,
        "physical_constraints_always_enabled": True,
    }
    enabled = get_enabled_rule_ids(rule_set)
    rule_set.update({
        "enabled_rule_count": len(enabled),
        "disabled_rule_count": len(rules) - len(enabled),
        "enabled_rule_ids": enabled,
    })
    validation = validate_placement_rule_set(rule_set)
    validation["errors"] = build_errors + validation["errors"]
    validation["valid"] = not validation["errors"]
    if validation["valid"]:
        rule_set["placement_rule_set_id"] = compute_placement_rule_set_id(rule_set)
    return rule_set, validation
