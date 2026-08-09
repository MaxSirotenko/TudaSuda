"""Deterministic characteristic anti-adjacency evidence from factual stock."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from warehouse_business_identity import canonical_sku_key

ADJACENCY_PROFILE_VERSION = 2


def normalize_adjacency_text(value: Any) -> str:
    """Apply the exact (non-fuzzy) warehouse comparison normalization."""
    return " ".join(str(value or "").split()).casefold().replace("ё", "е")


def adjacency_conflict(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    """Return whether two pieces of factual SKU evidence may not be neighbours."""
    characteristic = first.get("normalized_characteristic", "")
    return bool(characteristic and characteristic == second.get("normalized_characteristic")
                and first.get("normalized_nomenclature")
                and second.get("normalized_nomenclature")
                and first.get("normalized_nomenclature") != second.get("normalized_nomenclature"))


def build_sku_adjacency_profile(
    simulation_state: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build canonical per-SKU evidence from baseline stock-lot metadata."""
    errors: list[dict[str, Any]] = []
    evidence: dict[str, set[tuple[str, str]]] = {}
    if simulation_state is not None and not isinstance(simulation_state, Mapping):
        errors.append({"code": "invalid_simulation_state"})
    lots = simulation_state.get("stock_lots", []) if isinstance(simulation_state, Mapping) else []
    for lot in lots or []:
        if not isinstance(lot, Mapping):
            continue
        sku = " ".join(str(lot.get("sku_key") or canonical_sku_key(lot) or "").split())
        if not sku:
            continue
        pair = (normalize_adjacency_text(lot.get("nomenclature")),
                normalize_adjacency_text(lot.get("characteristic")))
        evidence.setdefault(sku, set()).add(pair)
    rows = []
    for sku, pairs in sorted(evidence.items()):
        if len(pairs) > 1:
            errors.append({"code": "conflicting_sku_business_metadata", "sku_key": sku,
                           "evidence": [list(pair) for pair in sorted(pairs)]})
        nomenclature, characteristic = sorted(pairs)[0]
        rows.append({"sku_key": sku, "normalized_nomenclature": nomenclature,
                     "normalized_characteristic": characteristic})
    identity = {"adjacency_profile_version": ADJACENCY_PROFILE_VERSION, "rows": rows}
    profile_id = "sha256:" + hashlib.sha256(json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    characteristics = {row["normalized_characteristic"] for row in rows
                       if row["normalized_characteristic"]}
    profile = {**identity, "adjacency_profile_id": profile_id,
               "summary": {"sku_rows": len(rows),
                           "characteristic_groups": len(characteristics)},
               "limitations": ["exact_normalized_business_metadata_only_no_fuzzy_matching"],
               "validation_errors": errors}
    return profile, {"valid": not errors, "errors": errors, "warnings": []}
