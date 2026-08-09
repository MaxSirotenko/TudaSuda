"""Deterministic explicit SKU-adjacency profile (stdlib only)."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from warehouse_business_identity import canonical_sku_key

ADJACENCY_PROFILE_VERSION = 1


def _group(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold().replace("ё", "е")


def _sku(row: Mapping[str, Any]) -> str:
    supplied = " ".join(str(row.get("sku_key") or "").split())
    if supplied.startswith("sku:v2:"):
        return supplied
    return canonical_sku_key(row)


def build_sku_adjacency_profile(
    adjacency_rows: list[dict[str, Any]] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Canonicalize only explicit group assignments; never infer relationships."""
    errors: list[dict[str, Any]] = []
    assignments: dict[str, set[str]] = defaultdict(set)
    if adjacency_rows is not None and not isinstance(adjacency_rows, list):
        errors.append({"code": "invalid_adjacency_rows"})
    for raw in adjacency_rows if isinstance(adjacency_rows, list) else []:
        if not isinstance(raw, Mapping):
            errors.append({"code": "invalid_adjacency_row"})
            continue
        sku = _sku(raw)
        if not sku:
            errors.append({"code": "invalid_adjacency_sku_key"})
            continue
        assignments[sku].add(_group(raw.get("adjacency_group")))
    for sku, groups in sorted(assignments.items()):
        nonempty = sorted(group for group in groups if group)
        if len(nonempty) > 1:
            errors.append({"code": "conflicting_adjacency_group_assignment",
                           "sku_key": sku, "adjacency_groups": nonempty})
    rows = [{"sku_key": sku, "adjacency_group": next(iter(sorted(groups - {""})), "")}
            for sku, groups in sorted(assignments.items())]
    groups = {row["adjacency_group"] for row in rows if row["adjacency_group"]}
    identity = {"adjacency_profile_version": ADJACENCY_PROFILE_VERSION, "rows": rows}
    profile_id = "sha256:" + hashlib.sha256(json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    profile = {**identity, "adjacency_profile_id": profile_id,
               "summary": {"sku_rows": len(rows),
                           "grouped_skus": sum(bool(row["adjacency_group"]) for row in rows),
                           "groups_total": len(groups),
                           "ungrouped_skus": sum(not row["adjacency_group"] for row in rows)},
               "limitations": ["explicit_groups_only_no_fuzzy_matching", "one_group_per_sku_v1"],
               "validation_errors": errors}
    diagnostics = {"valid": not errors, "errors": errors, "warnings": []}
    return profile, diagnostics
